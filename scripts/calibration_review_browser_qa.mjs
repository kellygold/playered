#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const devtoolsUrl = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5173'
const profileId = process.env.IMAGE23MF_CALIBRATION_PROFILE_ID ?? ''
const controlDirectory = process.env.IMAGE23MF_QA_CONTROL_DIR
  ? path.resolve(process.env.IMAGE23MF_QA_CONTROL_DIR)
  : null
const outputDirectory = path.resolve(
  process.env.IMAGE23MF_CALIBRATION_QA_OUTPUT ?? 'workspace/qa/calibration-review',
)
const reportPath = path.resolve(
  process.env.IMAGE23MF_CALIBRATION_QA_REPORT
    ?? path.join(outputDirectory, 'calibration-review.json'),
)
const downloadDirectory = path.resolve(
  process.env.IMAGE23MF_CALIBRATION_QA_DOWNLOADS
    ?? path.join(outputDirectory, 'downloads'),
)
const axeSourcePath = path.resolve('frontend/node_modules/axe-core/axe.min.js')
const evidenceFiles = {
  project_3mf: process.env.IMAGE23MF_CALIBRATION_PROJECT_3MF,
  sliced_gcode: process.env.IMAGE23MF_CALIBRATION_GCODE,
  slicer_settings: process.env.IMAGE23MF_CALIBRATION_SETTINGS,
  photo: (process.env.IMAGE23MF_CALIBRATION_PHOTOS ?? '')
    .split(path.delimiter)
    .filter(Boolean),
}
const attestationAllowed = process.env.IMAGE23MF_CALIBRATION_ATTESTATION === 'confirmed'
const proposalDisposition = process.env.IMAGE23MF_CALIBRATION_PROPOSAL_DISPOSITION ?? 'accept'
const reviewer = process.env.IMAGE23MF_CALIBRATION_REVIEWER ?? 'Image23MF browser QA'
const reviewReason = process.env.IMAGE23MF_CALIBRATION_REVIEW_REASON
  ?? 'End-to-end browser proof of the explicitly supplied physical calibration evidence.'
const hash = (character) => character.repeat(64)

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))

class CdpSession {
  constructor(socket) {
    this.socket = socket
    this.nextId = 0
    this.pending = new Map()
    this.events = []
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (!message.id) {
        this.events.push(message)
        return
      }
      const pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id)
      if (message.error) pending.reject(new Error(`${pending.method}: ${message.error.message}`))
      else pending.resolve(message.result)
    })
  }

  send(method, params = {}) {
    const id = ++this.nextId
    return new Promise((resolve, reject) => {
      this.pending.set(id, { method, resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    })
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description ?? 'Browser evaluation failed')
    }
    return result.result.value
  }
}

async function waitFor(check, description, timeoutMs = 60_000) {
  const started = Date.now()
  let last
  while (Date.now() - started < timeoutMs) {
    last = await check()
    if (last) return last
    await sleep(100)
  }
  throw new Error(`Timed out waiting for ${description}; last value: ${JSON.stringify(last)}`)
}

async function atomicJson(filename, value) {
  await fs.mkdir(path.dirname(filename), { recursive: true })
  const temporary = `${filename}.${process.pid}.tmp`
  await fs.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`)
  await fs.rename(temporary, filename)
}

async function setViewport(cdp, width, height) {
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await waitFor(
    () => cdp.evaluate(`innerWidth === ${width} && innerHeight === ${height}`),
    `${width}x${height} viewport`,
  )
}

async function pressKey(cdp, key, code = key) {
  const enter = key === 'Enter'
  const tab = key === 'Tab'
  const virtualKey = enter ? 13 : tab ? 9 : key === 'Escape' ? 27 : 0
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyDown',
    key,
    code,
    text: enter ? '\r' : '',
    unmodifiedText: enter ? '\r' : '',
    windowsVirtualKeyCode: virtualKey,
    nativeVirtualKeyCode: virtualKey,
  })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyUp', key, code, windowsVirtualKeyCode: virtualKey, nativeVirtualKeyCode: virtualKey,
  })
}

async function clickElement(cdp, expression, description) {
  const point = await cdp.evaluate(`(() => {
    const element = ${expression}
    if (!(element instanceof HTMLElement) || element.matches(':disabled')) return null
    element.scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = element.getBoundingClientRect()
    return { x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.ok(point, `${description} must resolve to one enabled element`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

function exactButton(label) {
  return `[...document.querySelectorAll('button')].find((button) =>
    (button.textContent.trim() === ${JSON.stringify(label)}
      || button.getAttribute('aria-label') === ${JSON.stringify(label)}) && !button.disabled)`
}

async function clickButton(cdp, label) {
  await clickElement(cdp, exactButton(label), label)
}

async function activateButtonByKeyboard(cdp, label) {
  const focused = await cdp.evaluate(`(() => {
    const button = ${exactButton(label)}
    if (!button) return false
    button.scrollIntoView({ block: 'center' })
    button.focus()
    return document.activeElement === button
  })()`)
  assert.equal(focused, true, `${label} must accept keyboard focus`)
  await pressKey(cdp, 'Enter', 'Enter')
}

async function setLabeledValue(cdp, label, value, tag = 'input', occurrence = 0) {
  const changed = await cdp.evaluate(`(() => {
    const expected = ${JSON.stringify(label)}
    const control = [...document.querySelectorAll('label')]
      .filter((candidate) => candidate.textContent.trim().startsWith(expected))
      [${occurrence}]?.querySelector(${JSON.stringify(tag)})
    if (!(control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement)) return false
    const prototype = control instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set
    if (!setter) return false
    const previous = control.value
    setter.call(control, ${JSON.stringify(String(value))})
    control._valueTracker?.setValue(previous)
    control.dispatchEvent(new Event('input', { bubbles: true }))
    control.dispatchEvent(new Event('change', { bubbles: true }))
    return control.value === ${JSON.stringify(String(value))}
  })()`)
  assert.equal(changed, true, `${label} must accept ${JSON.stringify(value)}`)
}

async function setCheckbox(cdp, expression, checked, description) {
  const result = await cdp.evaluate(`(() => {
    const input = ${expression}
    if (!(input instanceof HTMLInputElement) || input.type !== 'checkbox') return false
    if (input.checked !== ${checked}) input.click()
    return input.checked === ${checked}
  })()`)
  assert.equal(result, true, `${description} must become ${checked ? 'checked' : 'unchecked'}`)
}

async function setFileInput(cdp, selectorExpression, filename, description) {
  const inputObject = await cdp.send('Runtime.evaluate', {
    expression: selectorExpression,
    returnByValue: false,
  })
  assert.ok(inputObject.result.objectId, `${description} file input must exist`)
  await cdp.send('DOM.getDocument', { depth: 0, pierce: true })
  const input = await cdp.send('DOM.requestNode', { objectId: inputObject.result.objectId })
  assert.notEqual(input.nodeId, 0, `${description} file input must have a DOM node`)
  await cdp.send('DOM.setFileInputFiles', { nodeId: input.nodeId, files: [filename] })
  await cdp.evaluate(`(() => {
    const input = ${selectorExpression}
    input.dispatchEvent(new Event('change', { bubbles: true }))
    return true
  })()`)
}

async function browserJson(cdp, url) {
  return cdp.evaluate(`fetch(${JSON.stringify(url)}).then(async (response) => {
    const body = await response.json()
    if (!response.ok) throw new Error(body.error?.message ?? String(response.status))
    return body
  })`)
}

async function currentDraft(cdp, draftId) {
  return browserJson(cdp, `/api/calibration/drafts/${encodeURIComponent(draftId)}`)
}

async function waitForDraftGeneration(cdp, draftId, generation) {
  return waitFor(async () => {
    const draft = await currentDraft(cdp, draftId)
    return draft.generation > generation ? draft : null
  }, `draft ${draftId} generation after ${generation}`)
}

async function capture(cdp, filename) {
  const result = await cdp.send('Page.captureScreenshot', {
    format: 'png',
    fromSurface: true,
    captureBeyondViewport: false,
  })
  await fs.writeFile(path.join(outputDirectory, filename), Buffer.from(result.data, 'base64'))
}

async function axeScan(cdp, axeSource, label) {
  if (!await cdp.evaluate("typeof globalThis.axe === 'object'")) {
    const injected = await cdp.send('Runtime.evaluate', {
      expression: axeSource,
      awaitPromise: true,
      returnByValue: true,
    })
    assert.equal(Boolean(injected.exceptionDetails), false, 'axe-core injection must succeed')
  }
  return cdp.evaluate(`axe.run(document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] },
    resultTypes: ['violations'],
  }).then((result) => ({
    label: ${JSON.stringify(label)},
    violations: result.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      help: violation.help,
      nodes: violation.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
    })),
  }))`)
}

async function downloadSpecimen(cdp, profileIndex) {
  const before = new Set(await fs.readdir(downloadDirectory))
  const expression = `(() => {
    const cards = [...document.querySelectorAll('.calibration-profile-card')]
    const card = cards[${profileIndex}]
    return [...(card?.querySelectorAll('a') ?? [])].find((anchor) => anchor.textContent.trim() === 'Download specimen')
  })()`
  await clickElement(cdp, expression, 'Download specimen')
  return waitFor(async () => {
    const entries = await fs.readdir(downloadDirectory)
    const downloaded = entries.find((entry) => !before.has(entry) && !entry.endsWith('.crdownload'))
    return downloaded ? path.join(downloadDirectory, downloaded) : null
  }, 'specimen bundle browser download')
}

async function chooseProfileAndStart(cdp) {
  const catalogs = await browserJson(cdp, '/api/printability-profile-catalogs')
  const active = catalogs.items.find((catalog) => catalog.active)
  const configuredIndex = profileId
    ? active?.catalog.profiles.findIndex((profile) => profile.id === profileId)
    : 0
  assert.ok(configuredIndex >= 0, `calibration profile ${profileId} must exist in the active catalog`)
  const profile = await cdp.evaluate(`(() => {
    const cards = [...document.querySelectorAll('.calibration-profile-card')]
    const card = cards[${configuredIndex}]
    return card ? { heading: card.querySelector('h3')?.textContent.trim() ?? '', index: cards.indexOf(card) } : null
  })()`)
  assert.ok(profile, `calibration profile ${profileId || '(first available)'} must exist`)
  const downloaded = await downloadSpecimen(cdp, profile.index)
  await clickElement(
    cdp,
    `[...document.querySelectorAll('.calibration-profile-card')][${profile.index}].querySelector('button')`,
    `Start physical run for ${profile.heading}`,
  )
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.calibration-run-header'))"),
    'new calibration draft',
  )
  const drafts = await browserJson(cdp, '/api/calibration/drafts')
  assert.ok(drafts.items[0]?.id)
  return { profile: profile.heading, specimenDownload: downloaded, draft: drafts.items[0] }
}

async function fillSetup(cdp) {
  const date = process.env.IMAGE23MF_CALIBRATION_PRINTED_ON ?? new Date().toISOString().slice(0, 10)
  const filament = process.env.IMAGE23MF_CALIBRATION_FILAMENT ?? 'Bambu PLA Matte QA'
  const processProfile = process.env.IMAGE23MF_CALIBRATION_PROCESS_PROFILE ?? '0.20mm Standard @BBL P2S'
  const values = [
    ['Stable run ID', process.env.IMAGE23MF_CALIBRATION_RUN_ID ?? `calibration-run-browser-${Date.now()}`],
    ['Layer height (mm)', '0.2'],
    ['Build plate', 'textured-pei'],
    ['Filament name', filament, 'input', 0],
    ['Operator', process.env.IMAGE23MF_CALIBRATION_OPERATOR ?? 'Image23MF QA operator'],
    ['Printed on', date],
    ['Process profile', processProfile],
    ['Run notes', 'Real-Chrome retained-draft, evidence, and proposal review journey.'],
    ['Slicer application', 'Bambu Studio'],
    ['Slicer version', '2.3.1'],
    ['Slicer executable SHA-256', hash('1')],
    ['Machine profile name', 'Bambu Lab P2S 0.4 nozzle'],
    ['Machine profile SHA-256', hash('2')],
    ['Process profile name', processProfile],
    ['Process profile SHA-256', hash('3')],
    ['Filament profile name', filament],
    ['Filament profile SHA-256', hash('4')],
    ['Local filament ID', 'qa-filament-01'],
    ['Filament manufacturer', 'BambuLab'],
    ['Filament family', 'Matte'],
    ['Filament name', filament, 'input', 1],
    ['Filament material', 'pla'],
    ['Filament finish', 'matte'],
    ['Filament color', '#123456'],
  ]
  for (const [label, value, explicitTag, occurrence] of values) {
    await setLabeledValue(
      cdp,
      label,
      value,
      explicitTag ?? (label === 'Run notes' ? 'textarea' : 'input'),
      occurrence ?? 0,
    )
  }
}

async function restartHandshake(cdp, draft) {
  if (!controlDirectory) return { performed: false }
  const ready = path.join(controlDirectory, 'browser-ready-for-restart.json')
  const restarted = path.join(controlDirectory, 'api-restarted.json')
  await atomicJson(ready, {
    draft_id: draft.id,
    generation: draft.generation,
    candidate_sha256: draft.candidate_sha256,
  })
  await waitFor(async () => {
    try {
      return JSON.parse(await fs.readFile(restarted, 'utf8'))
    } catch {
      return null
    }
  }, 'API restart handshake', 45_000)
  await cdp.send('Page.reload', { ignoreCache: true })
  await waitFor(() => cdp.evaluate("document.readyState === 'complete'"), 'application reload')
  await waitFor(
    () => cdp.evaluate(`Boolean(${exactButton('Calibration')})`),
    'Calibration navigation after API restart',
  )
  await activateButtonByKeyboard(cdp, 'Calibration')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Calibration review center')"),
    'calibration dashboard after restart',
  )
  await clickElement(
    cdp,
    `[...document.querySelectorAll('.calibration-list-row')].find((row) => row.textContent.includes(${JSON.stringify(draft.id)}))`,
    'retained draft after restart',
  )
  const resumed = await currentDraft(cdp, draft.id)
  assert.equal(resumed.generation, draft.generation)
  assert.equal(resumed.candidate_sha256, draft.candidate_sha256)
  assert.equal(
    await cdp.evaluate(`document.body.innerText.includes(${JSON.stringify(`generation ${draft.generation}`)})`),
    true,
  )
  return { performed: true, generation: resumed.generation, candidate_sha256: resumed.candidate_sha256 }
}

async function scoreObservations(cdp, draftId) {
  await clickButton(cdp, '2. Observations')
  const before = await currentDraft(cdp, draftId)
  const result = await cdp.evaluate(`(() => {
    const groups = [...document.querySelectorAll('.observation-table')]
    let total = 0
    let failuresWithZero = 0
    let uncertain = 0
    groups.forEach((group, groupIndex) => {
      const rows = [...group.querySelectorAll('.observation-row:not(.observation-head)')]
      rows.forEach((row, index) => {
        const select = row.querySelector('select')
        const measured = row.querySelector('input[type=number]')
        const notes = [...row.querySelectorAll('input')].find((input) => input.type !== 'number')
        const outcome = index === 0
          ? 'fail'
          : index === 1
            ? (groupIndex === 0 ? 'uncertain' : 'fail')
            : 'pass'
        const set = (control, value) => {
          const prototype = control instanceof HTMLSelectElement
            ? HTMLSelectElement.prototype : HTMLInputElement.prototype
          const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set
          const previous = control.value
          setter.call(control, value)
          control._valueTracker?.setValue(previous)
          control.dispatchEvent(new Event('change', { bubbles: true }))
        }
        set(select, outcome)
        if (outcome === 'fail') {
          const measurement = index === 0 ? '0' : '0.1'
          set(measured, measurement)
          if (measurement === '0') failuresWithZero += 1
        }
        if (outcome === 'uncertain') {
          set(notes, 'Boundary was physically ambiguous under inspection.')
          uncertain += 1
        }
        total += 1
      })
    })
    return { groups: groups.length, total, failuresWithZero, uncertain }
  })()`)
  assert.equal(result.groups, 5)
  assert.ok(result.total > 0)
  assert.equal(result.failuresWithZero, 5)
  assert.equal(result.uncertain, 1)
  await clickButton(cdp, 'Save observations')
  const saved = await waitForDraftGeneration(cdp, draftId, before.generation)
  assert.equal(saved.record.observations.filter((item) => item.outcome === 'untested').length, 0)
  assert.ok(saved.record.observations.some((item) => item.measured_dimension_mm === 0))
  assert.ok(saved.record.observations.some((item) => item.outcome === 'uncertain'))
  return saved
}

async function uploadEvidence(cdp, draftId) {
  assert.ok(evidenceFiles.project_3mf, 'set IMAGE23MF_CALIBRATION_PROJECT_3MF')
  assert.ok(evidenceFiles.sliced_gcode, 'set IMAGE23MF_CALIBRATION_GCODE')
  assert.ok(evidenceFiles.slicer_settings, 'set IMAGE23MF_CALIBRATION_SETTINGS')
  assert.ok(evidenceFiles.photo.length, 'set IMAGE23MF_CALIBRATION_PHOTOS')
  const uploads = [
    ['Slicer project (.3mf)', 'project_3mf', evidenceFiles.project_3mf],
    ['Sliced toolpath (.gcode)', 'sliced_gcode', evidenceFiles.sliced_gcode],
    ['Exported slicer settings', 'slicer_settings', evidenceFiles.slicer_settings],
    ...evidenceFiles.photo.map((filename) => ['Physical print photo', 'photo', filename]),
  ]
  for (const [heading, role, rawFilename] of uploads) {
    const filename = path.resolve(rawFilename)
    await fs.access(filename)
    const before = await currentDraft(cdp, draftId)
    const selector = `(() => {
      const card = [...document.querySelectorAll('.attachment-card')]
        .find((candidate) => candidate.querySelector('h4')?.textContent.trim() === ${JSON.stringify(heading)})
      return card?.querySelector('input[type=file]') ?? null
    })()`
    await setFileInput(cdp, selector, filename, heading)
    const saved = await waitForDraftGeneration(cdp, draftId, before.generation)
    assert.ok(saved.members.some((member) => member.role === role))
  }
  return currentDraft(cdp, draftId)
}

async function sealRun(cdp, draftId) {
  await clickButton(cdp, '4. Review & seal')
  const ready = await currentDraft(cdp, draftId)
  assert.deepEqual(ready.readiness.blockers.map((item) => item.code), ['attestation_missing'])
  assert.equal(ready.readiness.ready_to_attest, true)
  assert.equal(
    await cdp.evaluate(`document.body.innerText.includes(${JSON.stringify(ready.candidate_sha256)})`),
    true,
    'candidate hash must be observable in the review UI',
  )
  assert.equal(attestationAllowed, true, 'set IMAGE23MF_CALIBRATION_ATTESTATION=confirmed after supplying real physical evidence')
  await setCheckbox(
    cdp,
    `document.querySelector('[aria-label="Confirm physical observation attestation"]')`,
    true,
    'physical observation attestation',
  )
  await clickButton(cdp, 'Attest physical observation')
  const attested = await waitFor(async () => {
    const draft = await currentDraft(cdp, draftId)
    return draft.attested_at ? draft : null
  }, 'candidate-bound attestation')
  assert.equal(attested.attested_candidate_sha256, attested.candidate_sha256)
  assert.equal(attested.readiness.ready_to_finalize, true)
  await clickButton(cdp, 'Seal immutable run')
  return waitFor(async () => {
    const draft = await currentDraft(cdp, draftId)
    return draft.finalized_run_id ? draft : null
  }, 'sealed immutable calibration run')
}

async function importPinnedProject(cdp, selectedProfileId, projectName, filename) {
  return cdp.evaluate(`(async () => {
    const source = await fetch('/api/calibration/specimens/'
      + encodeURIComponent(${JSON.stringify(selectedProfileId)}) + '/coupon.png')
    if (!source.ok) throw new Error('Could not load calibration image for pin proof: ' + source.status)
    const imported = await fetch('/api/projects/import?'
      + new URLSearchParams({ project_name: ${JSON.stringify(projectName)} }), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Filename': ${JSON.stringify(filename)},
      },
      body: await source.blob(),
    })
    const body = await imported.json()
    if (!imported.ok) throw new Error(body.error?.message ?? 'Project import failed')
    return {
      id: body.project.id,
      catalogFingerprint: body.draft.config.cleanup.printability_profile_catalog_fingerprint,
    }
  })()`)
}

async function deriveAndReviewProposal(cdp, runId, selectedProfileId, oldProject) {
  await clickButton(cdp, '← Back to calibration dashboard')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Verified physical runs')"),
    'sealed runs dashboard',
  )
  await setCheckbox(
    cdp,
    `[...document.querySelectorAll('.calibration-run-selection label')]
      .find((label) => label.textContent.includes(${JSON.stringify(runId)}))?.querySelector('input[type=checkbox]')`,
    true,
    `sealed run ${runId}`,
  )
  await clickButton(cdp, 'Derive proposal from 1 run')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Proposal review')"),
    'proposal review UI',
  )
  const collections = await Promise.all([
    browserJson(cdp, '/api/calibration/proposals'),
    browserJson(cdp, '/api/printability-profile-catalogs'),
  ])
  const proposal = collections[0].items[0]
  assert.ok(proposal?.proposal?.id)
  assert.equal(proposal.proposal.run_ids.includes(runId), true)
  assert.equal(proposal.proposal.contributions.some((item) => item.measured_dimension_mm === 0), true)
  assert.equal(proposal.proposal.contributions.some((item) => item.outcome === 'uncertain'), true)
  for (const identity of [
    proposal.proposal.base_catalog_fingerprint,
    proposal.proposal.proposed_catalog_fingerprint,
    proposal.proposal.proposal_sha256,
    proposal.proposal.process_fingerprint,
  ]) assert.match(identity, /^[0-9a-f]{64}$/)

  assert.equal(
    oldProject.catalogFingerprint,
    proposal.proposal.base_catalog_fingerprint,
    'project created before promotion must pin the proposal base catalog',
  )
  if (proposalDisposition === 'none') {
    return { proposal, activeCatalog: collections[1].active_fingerprint, oldProject, newProject: null }
  }
  assert.ok(['accept', 'reject'].includes(proposalDisposition))
  const trigger = proposalDisposition === 'accept' ? 'Review acceptance' : 'Review rejection'
  await activateButtonByKeyboard(cdp, trigger)
  await waitFor(() => cdp.evaluate("Boolean(document.querySelector('.calibration-proposal-review__decision form'))"), 'review confirmation form')
  await pressKey(cdp, 'Escape', 'Escape')
  await waitFor(() => cdp.evaluate("!document.querySelector('.calibration-proposal-review__decision form')"), 'Escape review cancellation')
  assert.equal(
    await cdp.evaluate(`document.activeElement?.textContent.trim() === ${JSON.stringify(trigger)}`),
    true,
    'Escape must return focus to the review trigger',
  )
  await activateButtonByKeyboard(cdp, trigger)
  await setLabeledValue(cdp, 'Reviewer', reviewer)
  await setLabeledValue(cdp, 'Review reason', reviewReason, 'textarea')
  await setCheckbox(
    cdp,
    `document.querySelector('.calibration-proposal-review__confirmation input')`,
    true,
    'proposal review confirmation',
  )
  await clickButton(cdp, proposalDisposition === 'accept' ? 'Confirm acceptance' : 'Confirm rejection')
  const dispositionLabel = proposalDisposition === 'accept' ? 'Accepted by' : 'Rejected by'
  await waitFor(
    () => cdp.evaluate(`document.body.innerText.includes(${JSON.stringify(dispositionLabel)})`),
    `${proposalDisposition} proposal disposition`,
  )
  const after = await Promise.all([
    browserJson(cdp, `/api/calibration/proposals/${encodeURIComponent(proposal.proposal.id)}`),
    browserJson(cdp, '/api/printability-profile-catalogs'),
  ])
  assert.equal(after[0].state, proposalDisposition === 'accept' ? 'accepted' : 'rejected')
  if (proposalDisposition === 'accept') {
    assert.equal(after[1].active_fingerprint, proposal.proposal.proposed_catalog_fingerprint)
  }
  const newProject = await importPinnedProject(
    cdp,
    selectedProfileId,
    'Created after calibration promotion',
    'calibration-after-promotion.png',
  )
  if (proposalDisposition === 'accept') {
    assert.equal(newProject.catalogFingerprint, proposal.proposal.proposed_catalog_fingerprint)
    assert.notEqual(newProject.catalogFingerprint, oldProject.catalogFingerprint)
  }
  return { proposal: after[0], activeCatalog: after[1].active_fingerprint, oldProject, newProject }
}

function diagnostics(cdp) {
  const consoleErrors = cdp.events.filter(
    (event) => event.method === 'Runtime.exceptionThrown'
      || (event.method === 'Runtime.consoleAPICalled' && event.params?.type === 'error')
      || (event.method === 'Log.entryAdded' && ['error', 'warning'].includes(event.params?.entry?.level)),
  )
  const networkFailures = cdp.events.filter(
    (event) => event.method === 'Network.loadingFailed'
      && !event.params?.canceled
      && event.params?.errorText !== 'net::ERR_ABORTED',
  )
  const failedResponses = cdp.events.filter(
    (event) => event.method === 'Network.responseReceived'
      && event.params?.response?.status >= 400,
  )
  return { consoleErrors, networkFailures, failedResponses }
}

async function main() {
  await Promise.all([
    fs.access(axeSourcePath),
    fs.mkdir(outputDirectory, { recursive: true }),
    fs.mkdir(downloadDirectory, { recursive: true }),
  ])
  const axeSource = await fs.readFile(axeSourcePath, 'utf8')
  const targetResponse = await fetch(`${devtoolsUrl}/json/new?${encodeURIComponent(appUrl)}`, {
    method: 'PUT',
  })
  assert.equal(targetResponse.ok, true, `DevTools target creation failed: ${targetResponse.status}`)
  const target = await targetResponse.json()
  const socket = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  const cdp = new CdpSession(socket)
  const evidence = { ok: false, appUrl, profileId, startedAt: new Date().toISOString(), axe: [] }
  try {
    await Promise.all([
      cdp.send('Page.enable'),
      cdp.send('Runtime.enable'),
      cdp.send('DOM.enable'),
      cdp.send('Network.enable'),
      cdp.send('Log.enable'),
    ])
    await cdp.send('Page.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: downloadDirectory,
    })
    await setViewport(cdp, 1440, 1000)
    await cdp.send('Page.navigate', { url: appUrl })
    await waitFor(() => cdp.evaluate("document.readyState === 'complete'"), 'application load')
    await activateButtonByKeyboard(cdp, 'Calibration')
    await waitFor(
      () => cdp.evaluate("document.body.innerText.includes('Calibration review center')"),
      'calibration dashboard',
    )
    evidence.axe.push(await axeScan(cdp, axeSource, 'calibration-dashboard'))
    const started = await chooseProfileAndStart(cdp)
    evidence.profile = started.profile
    evidence.specimenDownload = started.specimenDownload
    evidence.draftId = started.draft.id

    await fillSetup(cdp)
    const setupGeneration = (await currentDraft(cdp, evidence.draftId)).generation
    await clickButton(cdp, 'Save setup')
    const incomplete = await waitForDraftGeneration(cdp, evidence.draftId, setupGeneration)
    assert.ok(incomplete.readiness.blockers.some((item) => item.code === 'observation_untested'))
    evidence.restart = await restartHandshake(cdp, incomplete)

    await scoreObservations(cdp, evidence.draftId)
    await clickButton(cdp, '3. Evidence files')
    const attached = await uploadEvidence(cdp, evidence.draftId)
    evidence.memberHashes = Object.fromEntries(
      attached.members.map((member) => [`${member.role}:${member.ordinal}`, member.sha256]),
    )
    const sealed = await sealRun(cdp, evidence.draftId)
    evidence.finalizedRunId = sealed.finalized_run_id
    evidence.candidateSha256 = sealed.candidate_sha256
    evidence.attestedCandidateSha256 = sealed.attested_candidate_sha256
    evidence.axe.push(await axeScan(cdp, axeSource, 'sealed-calibration-run'))

    const oldProject = await importPinnedProject(
      cdp,
      sealed.profile.id,
      'Pinned before calibration promotion',
      'calibration-before-promotion.png',
    )
    const reviewed = await deriveAndReviewProposal(
      cdp,
      sealed.finalized_run_id,
      sealed.profile.id,
      oldProject,
    )
    evidence.proposalId = reviewed.proposal.proposal.id
    evidence.proposalSha256 = reviewed.proposal.proposal.proposal_sha256
    evidence.proposedCatalogFingerprint = reviewed.proposal.proposal.proposed_catalog_fingerprint
    evidence.activeCatalogFingerprint = reviewed.activeCatalog
    evidence.proposalState = reviewed.proposal.state
    evidence.baseCatalogFingerprint = reviewed.proposal.proposal.base_catalog_fingerprint
    evidence.oldProjectId = reviewed.oldProject.id
    evidence.oldProjectCatalogFingerprint = reviewed.oldProject.catalogFingerprint
    evidence.newProjectId = reviewed.newProject?.id ?? null
    evidence.newProjectCatalogFingerprint = reviewed.newProject?.catalogFingerprint ?? null
    evidence.axe.push(await axeScan(cdp, axeSource, 'calibration-proposal-review'))
    await capture(cdp, 'proposal-review-desktop.png')

    await setViewport(cdp, 390, 844)
    const responsive = await cdp.evaluate(`({
      innerWidth,
      innerHeight,
      documentWidth: document.documentElement.scrollWidth,
      verticallyScrollable: document.documentElement.scrollHeight > innerHeight,
    })`)
    assert.deepEqual({ width: responsive.innerWidth, height: responsive.innerHeight }, { width: 390, height: 844 })
    assert.ok(responsive.documentWidth <= 390.5, `compact calibration UI overflows by ${responsive.documentWidth - 390}px`)
    assert.equal(responsive.verticallyScrollable, true)
    evidence.responsive = responsive
    evidence.axe.push(await axeScan(cdp, axeSource, 'calibration-proposal-compact'))
    await capture(cdp, 'proposal-review-compact.png')

    evidence.diagnostics = diagnostics(cdp)
    assert.deepEqual(evidence.diagnostics.consoleErrors, [], 'browser console must remain clean')
    assert.deepEqual(evidence.diagnostics.networkFailures, [], 'browser network must remain clean')
    assert.deepEqual(evidence.diagnostics.failedResponses, [], 'browser responses must remain successful')
    const axeViolations = evidence.axe.flatMap((scan) => scan.violations)
    assert.deepEqual(axeViolations, [], `axe violations: ${JSON.stringify(axeViolations)}`)
    evidence.ok = true
    evidence.status = 'passed'
    evidence.run_id = evidence.finalizedRunId
    evidence.proposal_id = evidence.proposalId
    evidence.active_catalog_fingerprint = evidence.activeCatalogFingerprint
    evidence.base_catalog_fingerprint = evidence.baseCatalogFingerprint
    evidence.old_project_id = evidence.oldProjectId
    evidence.new_project_id = evidence.newProjectId
    evidence.old_project_catalog_fingerprint = evidence.oldProjectCatalogFingerprint
    evidence.new_project_catalog_fingerprint = evidence.newProjectCatalogFingerprint
    evidence.completedAt = new Date().toISOString()
  } catch (error) {
    evidence.error = error instanceof Error ? { message: error.message, stack: error.stack } : String(error)
    evidence.diagnostics = diagnostics(cdp)
    try {
      await capture(cdp, 'failure.png')
    } catch {
      // Preserve the original failure when Chrome has already closed the target.
    }
    throw error
  } finally {
    await atomicJson(reportPath, evidence)
    socket.close()
  }
  process.stdout.write(`${JSON.stringify(evidence)}\n`)
}

main().catch((error) => {
  process.stderr.write(`${error.stack ?? error}\n`)
  process.exitCode = 1
})
