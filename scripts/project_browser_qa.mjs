#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5173'
const fixture = path.resolve(
  process.env.IMAGE23MF_QA_FIXTURE ?? 'tests/fixtures/synthetic/geometry-nozzle-040.png',
)
const screenshot = path.resolve(
  process.env.IMAGE23MF_QA_SCREENSHOT ?? 'workspace/qa/project-browser-mvp.png',
)
let activeSocket

class CdpSession {
  constructor(socket) {
    this.socket = socket
    this.nextId = 0
    this.pending = new Map()
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (!message.id) return
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
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`Timed out waiting for ${description}; last value: ${JSON.stringify(last)}`)
}

async function setFileInput(cdp, filename) {
  const inputObject = await cdp.send('Runtime.evaluate', {
    expression: "document.querySelector('input[type=file]')",
    returnByValue: false,
  })
  assert.ok(inputObject.result.objectId, 'the source file input must exist')
  await cdp.send('DOM.getDocument', { depth: 0, pierce: true })
  const input = await cdp.send('DOM.requestNode', { objectId: inputObject.result.objectId })
  await cdp.send('DOM.setFileInputFiles', { nodeId: input.nodeId, files: [filename] })
  await cdp.evaluate(
    "document.querySelector('input[type=file]').dispatchEvent(new Event('change', { bubbles: true }))",
  )
}

async function clickButton(cdp, label, scope = 'document') {
  const point = await cdp.evaluate(`(() => {
    const root = ${scope}
    const matches = [...root.querySelectorAll('button')].filter(
      (button) => (
        button.textContent.trim() === ${JSON.stringify(label)}
        || button.getAttribute('aria-label') === ${JSON.stringify(label)}
      ) && !button.disabled,
    )
    if (matches.length !== 1) {
      return {
        count: matches.length,
        available: [...root.querySelectorAll('button')].map((button) => button.textContent.trim()),
      }
    }
    matches[0].scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = matches[0].getBoundingClientRect()
    return { count: 1, x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.equal(
    point.count,
    1,
    `${label} must resolve to one enabled button; available: ${JSON.stringify(point.available ?? [])}`,
  )
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

async function clickRevision(cdp, label) {
  const point = await cdp.evaluate(`(() => {
    const matches = [...document.querySelectorAll('.revision-list button')].filter(
      (button) => button.querySelector('strong')?.textContent.trim() === ${JSON.stringify(label)},
    )
    if (matches.length !== 1) return { count: matches.length }
    matches[0].scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = matches[0].getBoundingClientRect()
    return { count: 1, x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.equal(point.count, 1, `${label} must resolve to one revision`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

async function fill(cdp, selector, value) {
  const documentNode = await cdp.send('DOM.getDocument', { depth: -1, pierce: true })
  const input = await cdp.send('DOM.querySelector', {
    nodeId: documentNode.root.nodeId,
    selector,
  })
  assert.notEqual(input.nodeId, 0, `input must exist: ${selector}`)
  await cdp.send('DOM.focus', { nodeId: input.nodeId })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'rawKeyDown', key: 'a', code: 'KeyA', modifiers: 4, commands: ['SelectAll'],
  })
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 4 })
  await cdp.send('Input.insertText', { text: value })
}

async function clickCheckbox(cdp) {
  const point = await cdp.evaluate(`(() => {
    const input = document.querySelector('.project-browser-archive-toggle input')
    if (!input) return null
    const bounds = input.getBoundingClientRect()
    return { x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.ok(point, 'archive checkbox must exist')
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

async function main() {
  await fs.access(fixture)
  const target = await fetch(`${devtools}/json/new?${encodeURIComponent(appUrl)}`, {
    method: 'PUT',
  }).then((response) => response.json())
  const socket = new WebSocket(target.webSocketDebuggerUrl)
  activeSocket = socket
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  const cdp = new CdpSession(socket)
  await Promise.all([
    cdp.send('Page.enable'),
    cdp.send('Runtime.enable'),
    cdp.send('DOM.enable'),
    cdp.send('Network.enable'),
  ])
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("document.readyState === 'complete' && Boolean(document.querySelector('input[type=file]'))"),
    'the import screen',
  )
  await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Choose an image')"),
    'the cleared import screen',
  )

  await setFileInput(cdp, fixture)
  const createdProjectId = await waitFor(
    () => cdp.evaluate("document.querySelector('[aria-label=\"Exact artwork preview and comparison\"]') && localStorage.getItem('image23mf.recentProjectId')"),
    'the created project editor',
    90_000,
  )
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the persisted preview',
    90_000,
  )

  // Exercise replacement in a genuinely short viewport. The app intentionally locks body
  // scrolling while a modal is open, so the dialog itself must remain scrollable and its
  // actions must still be reachable.
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 720, height: 360, deviceScaleFactor: 1, mobile: false,
  })
  await clickButton(cdp, 'Replace image')
  await setFileInput(cdp, fixture)
  const replacementDialog = await waitFor(
    () => cdp.evaluate(`(() => {
      const dialog = document.querySelector('.confirm-dialog')
      const confirm = [...document.querySelectorAll('button')].find(
        (button) => button.textContent.trim() === 'Import replacement',
      )
      if (!dialog || !confirm) return null
      const style = getComputedStyle(dialog)
      return {
        bodyLocked: document.body.style.overflow === 'hidden',
        overflowY: style.overflowY,
        clientHeight: dialog.clientHeight,
        scrollHeight: dialog.scrollHeight,
      }
    })()`),
    'the replacement confirmation in a short viewport',
  )
  assert.equal(replacementDialog.bodyLocked, true)
  assert.equal(replacementDialog.overflowY, 'auto')
  assert.ok(
    replacementDialog.clientHeight <= 320,
    `replacement dialog must fit the 360px viewport; got ${replacementDialog.clientHeight}px`,
  )
  await clickButton(cdp, 'Keep current image')
  await waitFor(
    () => cdp.evaluate(`(
      !document.querySelector('.confirm-dialog')
      && document.body.style.overflow === ''
      && !document.querySelector('.app-shell')?.hasAttribute('inert')
    )`),
    'replacement cancellation and page-scroll restoration',
  )
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })

  const seededRevisions = await cdp.evaluate(`(async () => {
    const projectId = ${JSON.stringify(createdProjectId)}
    const read = async (response) => {
      const body = await response.json()
      if (!response.ok) throw new Error(body.error?.message ?? 'QA request failed')
      return body
    }
    const workspace = await read(await fetch('/api/projects/' + projectId))
    const baseline = await read(await fetch('/api/projects/' + projectId + '/revisions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        label: 'Baseline proof',
        expected_draft_generation: workspace.draft.generation,
        preview_job_id: workspace.latest_preview_job.id,
      }),
    }))
    const config = structuredClone(baseline.draft.config)
    config.canvas.width_mm += 10
    const saved = await read(await fetch('/api/projects/' + projectId + '/draft', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        config,
        operations: baseline.draft.operations,
        expected_draft_generation: baseline.draft.generation,
      }),
    }))
    const preview = await read(await fetch('/api/projects/' + projectId + '/previews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: saved.config, expected_draft_generation: saved.generation }),
    }))
    let job
    do {
      job = await read(await fetch('/api/jobs/' + preview.job.id))
      if (!['succeeded', 'failed', 'canceled'].includes(job.state)) {
        await new Promise((resolve) => setTimeout(resolve, 50))
      }
    } while (!['succeeded', 'failed', 'canceled'].includes(job.state))
    if (job.state !== 'succeeded') throw new Error('second QA preview did not succeed')
    const adjusted = await read(await fetch('/api/projects/' + projectId + '/revisions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        label: 'Adjusted proof',
        expected_draft_generation: preview.draft.generation,
        preview_job_id: preview.job.id,
      }),
    }))
    return { baselineId: baseline.revision.id, adjustedId: adjusted.revision.id }
  })()`)
  const revisionQaToken = String(Date.now())
  await cdp.send('Page.navigate', { url: `${appUrl}?revisionQa=${revisionQaToken}` })
  await waitFor(
    () => cdp.evaluate(`(
      new URLSearchParams(location.search).get('revisionQa') === ${JSON.stringify(revisionQaToken)}
      && Boolean(document.querySelector('[aria-label="Exact artwork preview and comparison"]'))
    )`),
    'the reloaded editor with seeded revision history',
    90_000,
  )
  await clickButton(cdp, 'Revisions')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Baseline proof') && document.body.innerText.includes('Adjusted proof')"),
    'both immutable revisions',
  )
  await clickRevision(cdp, 'Adjusted proof')
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Revision details for Adjusted proof\"]'))"),
    'the adjusted revision detail',
  )
  const comparison = await waitFor(
    () => cdp.evaluate(`(() => {
      const section = document.querySelector('[aria-label="Compare Adjusted proof"]')
      if (!section || section.innerText.includes('Comparing immutable snapshots')) return null
      return section.innerText
    })()`),
    'the completed parent configuration comparison',
  )
  assert.ok(
    comparison.toLowerCase().includes('canvas · width mm'),
    `comparison must include the canvas width delta; got: ${JSON.stringify(comparison)}`,
  )
  assert.ok(comparison.includes('200'))
  assert.ok(comparison.includes('210'))

  const previewArtifactScope = `[...(document.querySelector('[aria-label="Revision details for Adjusted proof"]')
    ?.querySelector('[aria-label="Revision artifacts"]')
    ?.querySelectorAll('.revision-artifact') ?? [])].find(
    (item) => item.querySelector('.revision-artifact-title strong')?.textContent.trim() === 'preview image'
  )`
  const immutableArtifactHref = await waitFor(
    () => cdp.evaluate(`(() => {
      const item = ${previewArtifactScope}
      const text = item?.innerText.toLowerCase() ?? ''
      return text.includes('verified') && text.includes('locked with this published revision.')
        && !text.includes('delete cached artifact')
        ? item.querySelector('a[download]')?.getAttribute('href')
        : null
    })()`),
    'verified immutable revision evidence',
  )

  const workingArtifactScope = `[...(document.querySelector('[aria-label="Current preview artifacts"]')
    ?.querySelectorAll('.revision-artifact') ?? [])].find(
      (item) => item.querySelector('.revision-artifact-title strong')?.textContent.trim() === 'palette preview image'
    )`
  const deletedWorkingArtifactHref = await waitFor(
    () => cdp.evaluate(`(() => {
      const item = ${workingArtifactScope}
      return item?.innerText.toLowerCase().includes('verified')
        && item?.innerText.includes('Delete cached artifact')
        ? item.querySelector('a[download]')?.getAttribute('href')
        : null
    })()`),
    'verified regenerable working artifact',
  )
  await clickButton(cdp, 'Delete cached artifact', workingArtifactScope)
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Generate a new preview to recreate this artifact.')"),
    'the explicit preview recovery action',
  )
  await clickButton(cdp, 'Delete artifact', workingArtifactScope)
  await waitFor(() => cdp.evaluate(`!(${workingArtifactScope})`), 'the regenerable artifact deletion')

  await clickButton(cdp, 'Close revisions')
  await waitFor(
    () => cdp.evaluate(`(
      !document.querySelector('#revision-drawer')
      && document.body.style.overflow === ''
    )`),
    'revision drawer close and page-scroll restoration',
  )
  const recoveryReloadToken = String(Date.now())
  await cdp.send('Page.navigate', { url: `${appUrl}?artifactRecovery=${recoveryReloadToken}` })
  const recoveryAction = await waitFor(
    () => cdp.evaluate(`(() => {
      if (new URLSearchParams(location.search).get('artifactRecovery') !== ${JSON.stringify(recoveryReloadToken)}) return null
      if (!document.querySelector('[aria-label="Exact artwork preview and comparison"]')) return null
      const button = [...document.querySelectorAll('button')].find(
        (item) => ['Render preview', 'Update preview'].includes(item.textContent.trim()) && !item.disabled,
      )
      return button?.textContent.trim() ?? null
    })()`),
    'the reloadable stale preview with a regenerate action',
    90_000,
  )
  await clickButton(cdp, recoveryAction, "document.querySelector('.i23-editor-header__actions')")
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the regenerated preview after artifact recovery',
    90_000,
  )
  await clickButton(cdp, 'Revisions')
  const regeneratedWorkingArtifactHref = await waitFor(
    () => cdp.evaluate(`(() => {
      const item = ${workingArtifactScope}
      return item?.innerText.toLowerCase().includes('verified')
        ? item.querySelector('a[download]')?.getAttribute('href')
        : null
    })()`),
    'the regenerated working artifact',
  )
  assert.notEqual(regeneratedWorkingArtifactHref, deletedWorkingArtifactHref)
  await clickRevision(cdp, 'Adjusted proof')
  const retainedImmutableHref = await waitFor(
    () => cdp.evaluate(`(() => {
      const item = ${previewArtifactScope}
      return item?.innerText.toLowerCase().includes('verified')
        ? item.querySelector('a[download]')?.getAttribute('href')
        : null
    })()`),
    'the retained immutable artifact after shared-cache deletion',
  )
  assert.equal(retainedImmutableHref, immutableArtifactHref)
  await clickButton(cdp, 'Close revisions')
  await waitFor(
    () => cdp.evaluate("!document.querySelector('#revision-drawer') && document.body.style.overflow === ''"),
    'recovery drawer close and page-scroll restoration',
  )
  await clickButton(cdp, 'Build 3MF', "document.querySelector('.i23-editor-header__actions')")
  await waitFor(
    () => cdp.evaluate(`(
      [...document.querySelectorAll('a')].some(
        (link) => link.textContent.trim() === 'Download 3MF package',
      )
      && [...document.querySelectorAll('button')].some(
        (button) => button.textContent.trim() === 'Reveal verified 3MF in Finder',
      )
    )`),
    'verified output download and capability-gated Finder reveal actions',
    180_000,
  )

  await clickButton(cdp, 'Projects')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Your recent work')"),
    'the project browser',
  )
  await fill(cdp, 'input[type=search]', path.parse(fixture).name)
  const cardSelector = `.project-card[data-project-id=${JSON.stringify(createdProjectId)}]`
  const beforeOpen = await waitFor(
    () => cdp.evaluate(`(() => {
      const card = document.querySelector(${JSON.stringify(cardSelector)})
      const image = card?.querySelector('img[data-thumbnail-kind]')
      return card && image?.complete && image.naturalWidth > 0
        ? { src: image.getAttribute('src'), text: card.innerText }
        : null
    })()`),
    'the searchable project card and persisted thumbnail',
  )
  assert.ok(beforeOpen.text.includes('Adjusted proof'))

  const keyboardOpen = await cdp.evaluate(`(() => {
    const button = document.querySelector(${JSON.stringify(cardSelector)})
      ?.querySelector('button.button-primary')
    if (!button) return false
    button.focus()
    return document.activeElement === button
  })()`)
  assert.equal(keyboardOpen, true, 'open-project button must accept keyboard focus')
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyDown', key: 'Enter', code: 'Enter', text: '\r', unmodifiedText: '\r',
    windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
  })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13,
    nativeVirtualKeyCode: 13,
  })
  await waitFor(
    () => cdp.evaluate(`localStorage.getItem('image23mf.recentProjectId') === ${JSON.stringify(createdProjectId)} && Boolean(document.querySelector('[aria-label="Exact artwork preview and comparison"]'))`),
    'the keyboard-opened project after reload',
    90_000,
  )

  await clickButton(cdp, 'Projects')
  await waitFor(() => cdp.evaluate("document.body.innerText.includes('Your recent work')"), 'the reopened library')
  await fill(cdp, 'input[type=search]', path.parse(fixture).name)
  await waitFor(() => cdp.evaluate(`Boolean(document.querySelector(${JSON.stringify(cardSelector)}))`), 'the searched project')
  await clickButton(cdp, 'Archive', `document.querySelector(${JSON.stringify(cardSelector)})`)
  await waitFor(() => cdp.evaluate(`!document.querySelector(${JSON.stringify(cardSelector)})`), 'the archived project leaving active results')
  await clickCheckbox(cdp)
  await waitFor(() => cdp.evaluate(`document.querySelector(${JSON.stringify(cardSelector)})?.innerText.includes('Archived')`), 'the archived project result')
  await clickButton(cdp, 'Restore', `document.querySelector(${JSON.stringify(cardSelector)})`)
  await waitFor(() => cdp.evaluate(`!document.querySelector(${JSON.stringify(cardSelector)})`), 'the restored project leaving archived results')
  await clickCheckbox(cdp)
  const restored = await waitFor(
    () => cdp.evaluate(`(() => {
      const card = document.querySelector(${JSON.stringify(cardSelector)})
      const image = card?.querySelector('img[data-thumbnail-kind]')
      return image?.complete && image.naturalWidth > 0
        ? { src: image.getAttribute('src'), archived: card.innerText.includes('Archived') }
        : null
    })()`),
    'the restored project with its persisted thumbnail',
  )
  assert.equal(restored.src, beforeOpen.src)
  assert.equal(restored.archived, false)

  await clickButton(cdp, 'Open project', `document.querySelector(${JSON.stringify(cardSelector)})`)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Exact artwork preview and comparison\"]'))"),
    'the restored project editor',
    90_000,
  )
  await clickButton(cdp, 'Revisions')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Baseline proof') && document.body.innerText.includes('Adjusted proof')"),
    'the restored immutable revision history',
  )
  await clickRevision(cdp, 'Baseline proof')
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Revision details for Baseline proof\"]'))"),
    'the older immutable revision detail',
  )
  await clickButton(cdp, 'Branch from this revision')
  await clickButton(cdp, 'Create branch draft')
  await waitFor(
    () => cdp.evaluate(`(() => {
      const button = [...document.querySelectorAll('.revision-list button')].find(
        (item) => item.querySelector('strong')?.textContent.trim() === 'Baseline proof',
      )
      return button?.innerText.includes('Draft branched here')
        && [...document.querySelectorAll('.revision-list button')].some(
          (item) => item.querySelector('strong')?.textContent.trim() === 'Adjusted proof'
            && item.innerText.includes('Current published'),
        )
    })()`),
    'a branch draft with immutable current publication preserved',
  )
  await clickButton(cdp, 'Close revisions')
  await waitFor(
    () => cdp.evaluate("!document.querySelector('#revision-drawer') && document.body.style.overflow === ''"),
    'branched revision drawer close and page-scroll restoration',
  )

  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  const capture = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
  await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))
  console.log(JSON.stringify({
    projectId: createdProjectId,
    thumbnailUrl: restored.src,
    keyboardOpen: true,
    archivedAndRestored: true,
    replacementShortViewport: true,
    revisionComparison: seededRevisions,
    immutableArtifactVerified: true,
    regenerableArtifactRecoveredAcrossReload: true,
    verifiedOutputFinderAction: true,
    olderRevisionBranched: true,
    screenshot,
  }, null, 2))
  const closed = new Promise((resolve) => socket.addEventListener('close', resolve, { once: true }))
  socket.close()
  await Promise.race([closed, new Promise((resolve) => setTimeout(resolve, 1_000))])
  activeSocket = undefined
}

main().catch((error) => {
  console.error(error)
  activeSocket?.close()
  process.exitCode = 1
})
