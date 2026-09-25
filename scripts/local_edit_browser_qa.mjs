#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5173'
const fixture = path.resolve(
  process.env.IMAGE23MF_QA_FIXTURE ?? 'tests/fixtures/synthetic/geometry-nozzle-040.png',
)
const outputDirectory = path.resolve(
  process.env.IMAGE23MF_QA_LOCAL_EDIT_DIR ?? 'workspace/qa/local-edits',
)

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

async function waitFor(check, description, timeoutMs = 90_000) {
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
  assert.ok(inputObject.result.objectId, 'source file input must exist')
  await cdp.send('DOM.getDocument', { depth: 0, pierce: true })
  const input = await cdp.send('DOM.requestNode', { objectId: inputObject.result.objectId })
  await cdp.send('DOM.setFileInputFiles', { nodeId: input.nodeId, files: [filename] })
  await cdp.evaluate(
    "document.querySelector('input[type=file]').dispatchEvent(new Event('change', { bubbles: true }))",
  )
}

async function click(cdp, expression, description) {
  const point = await cdp.evaluate(`(() => {
    const element = ${expression}
    if (!element || element.disabled) return null
    element.scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = element.getBoundingClientRect()
    return { x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.ok(point, `${description} must be enabled and visible`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

function buttonExpression(label) {
  return `[...document.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === ${JSON.stringify(label)}
  )`
}

async function projectState(cdp) {
  return cdp.evaluate(`(async () => {
    const projectId = localStorage.getItem('image23mf.recentProjectId')
    if (!projectId) return null
    const response = await fetch('/api/projects/' + projectId)
    return response.ok ? response.json() : null
  })()`)
}

async function localEditState(cdp) {
  return waitFor(async () => {
    const project = await projectState(cdp)
    const operations = project?.draft?.operations ?? []
    const selections = operations.filter((item) => item.operation_type === 'canvas_selection_v1')
    const edits = operations.filter((item) =>
      item.operation_type === 'editor_command_v1'
      && item.parameters?.command?.command_type === 'local_raster_edit'
    )
    if (selections.length < 1 || edits.length !== 1) return false
    return { project, selection: selections.at(-1), edit: edits[0] }
  }, 'saved local raster edit')
}

async function main() {
  await fs.mkdir(outputDirectory, { recursive: true })
  await fs.access(fixture)
  const target = await fetch(`${devtools}/json/new?${encodeURIComponent(appUrl)}`, {
    method: 'PUT',
  }).then((response) => response.json())
  const socket = new WebSocket(target.webSocketDebuggerUrl)
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
    cdp.send('Log.enable'),
  ])
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(() => cdp.evaluate("document.readyState === 'complete'"), 'application load')
  await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('input[type=file]'))"),
    'source input',
  )
  await setFileInput(cdp, fixture)
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'current initial preview',
  )

  await click(cdp, buttonExpression('Rectangle'), 'Rectangle tool')
  const drag = await cdp.evaluate(`(() => {
    const plane = document.querySelector('.i23-canvas-plane[data-plane="primary"]')
    plane.scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = plane.getBoundingClientRect()
    return {
      start: { x: bounds.left + bounds.width * 0.12, y: bounds.top + bounds.height * 0.12 },
      end: { x: bounds.left + bounds.width * 0.48, y: bounds.top + bounds.height * 0.48 },
    }
  })()`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: drag.start.x, y: drag.start.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseMoved', x: drag.end.x, y: drag.end.y, button: 'left', buttons: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: drag.end.x, y: drag.end.y, button: 'left', clickCount: 1,
  })
  await waitFor(async () => {
    const project = await projectState(cdp)
    return project?.draft?.operations?.some((item) => item.operation_type === 'canvas_selection_v1')
  }, 'saved rectangle selection')
  await waitFor(
    () => cdp.evaluate("document.querySelector('.local-edits select') && document.body.innerText.includes('Draft saved locally')"),
    'local edit panel and saved selection',
  )

  const targetLabel = await cdp.evaluate(`(() => {
    const label = [...document.querySelectorAll('.local-edits-field')]
      .find((item) => item.innerText.toLowerCase().includes('fill color'))
    const select = label?.querySelector('select')
    if (!select || select.options.length < 2) return null
    select.value = select.options[select.options.length - 1].value
    select.dispatchEvent(new Event('change', { bubbles: true }))
    return Number(select.value)
  })()`)
  assert.ok(Number.isInteger(targetLabel), 'fill target label must be selectable')
  await waitFor(
    () => cdp.evaluate(`(() => {
      const button = ${buttonExpression('Review local edit')}
      return Boolean(button && !button.disabled)
    })()`),
    'actionable local fill',
  )
  await click(cdp, buttonExpression('Review local edit'), 'Review local edit')
  assert.equal(
    await cdp.evaluate(`document.activeElement?.textContent.trim() === 'Cancel'`),
    true,
  )
  await click(cdp, buttonExpression('Save edit'), 'Save local edit')
  const saved = await localEditState(cdp)
  const command = saved.edit.parameters.command
  assert.equal(command.edit.kind, 'fill')
  assert.equal(command.edit.target_label, targetLabel)
  assert.deepEqual(command.selector.selection, saved.selection.parameters.command.selector.selection)
  assert.equal(command.provenance.selection_snapshot, true)
  const provenance = saved.edit.provenance?.regional_edit
  assert.equal(provenance?.execution_kind, 'local')
  assert.equal(provenance?.reproducibility, 'deterministic')
  assert.equal(provenance?.engine_id, 'image23mf-local-raster')
  assert.equal(provenance?.engine_version, '1')
  assert.match(provenance?.selection_sha256 ?? '', /^[0-9a-f]{64}$/)
  assert.equal(Object.hasOwn(provenance, 'parent_revision_id'), true)
  await waitFor(
    () => cdp.evaluate(`(() => {
      const render = ${buttonExpression('Update preview')}
      return Boolean(
        render
        && !render.disabled
        && document.body.innerText.includes('Preview is out of date.')
      )
    })()`),
    'stale preview after local edit',
  )

  await click(cdp, buttonExpression('Update preview'), 'Render deterministic local edit preview')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'current local-edit preview',
  )
  const evidence = await cdp.evaluate(`(async () => {
    const projectId = localStorage.getItem('image23mf.recentProjectId')
    const project = await fetch('/api/projects/' + projectId).then((response) => response.json())
    const jobId = project.latest_preview_job.id
    const result = await fetch('/api/jobs/' + jobId + '/result').then((response) => response.json())
    const artifact = result.artifacts.find((item) => item.kind === 'editor-replay')
    const record = await fetch(artifact.download_url).then((response) => response.json())
    return {
      jobId,
      editorSequenceSha256: project.draft.editor_sequence_sha256,
      commandCount: record.command_count,
      step: record.steps.at(-1),
      changedPixelCount: record.changed_pixel_count,
      reprocessingPlan: result.reprocessing_plan,
    }
  })()`)
  assert.equal(evidence.commandCount, 1)
  assert.equal(evidence.step.command_type, 'local_raster_edit')
  assert.ok(evidence.step.changed_pixel_count > 0)
  assert.ok(evidence.changedPixelCount > 0)
  assert.equal(evidence.reprocessingPlan?.mode, 'full')
  assert.equal(evidence.reprocessingPlan?.reason, 'boundary_edit')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Full reprocessing')"),
    'visible full-reprocessing fallback evidence',
  )

  await click(
    cdp,
    `[...document.querySelectorAll('button')].find((button) => button.getAttribute('aria-label') === 'Revisions')`,
    'Open revision history',
  )
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.revision-publish input'))"),
    'revision publication form',
  )
  await cdp.evaluate(`(() => {
    const input = document.querySelector('.revision-publish input')
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(input, 'Local provenance browser gate')
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
    return input.value
  })()`)
  await waitFor(
    () => cdp.evaluate(`(() => {
      const button = ${buttonExpression('Publish revision')}
      return Boolean(button && !button.disabled)
    })()`),
    'publishable named revision',
  )
  await click(cdp, buttonExpression('Publish revision'), 'Publish provenance revision')
  await waitFor(
    () => cdp.evaluate(`Boolean(
      document.querySelector('[aria-label="Regional edit reproducibility"]')
      && document.body.innerText.includes('Deterministic replay')
      && document.body.innerText.includes('Local image23mf engine')
    )`),
    'visible deterministic provenance evidence',
  )
  await cdp.evaluate(`document.querySelector('[aria-label="Regional edit reproducibility"]')
    ?.scrollIntoView({ block: 'center', inline: 'nearest' })`)
  await new Promise((resolve) => setTimeout(resolve, 250))
  const provenanceScreenshot = await cdp.send('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: false, fromSurface: true,
  })
  const provenanceScreenshotPath = path.join(outputDirectory, 'local-edit-provenance.png')
  await fs.writeFile(provenanceScreenshotPath, Buffer.from(provenanceScreenshot.data, 'base64'))
  await click(
    cdp,
    `[...document.querySelectorAll('button')].find((button) => button.getAttribute('aria-label') === 'Close revisions')`,
    'Close revision history',
  )

  await cdp.evaluate(`document.querySelector('.local-edits')?.scrollIntoView({ block: 'center' })`)
  await new Promise((resolve) => setTimeout(resolve, 250))
  const controls = await cdp.evaluate(`[
    ...document.querySelectorAll('.local-edits select, .local-edits input, .local-edits button')
  ].filter((item) => item.offsetParent !== null).map((item) => ({
    label: item.getAttribute('aria-label') || item.textContent.trim() || item.closest('label')?.innerText,
    height: item.getBoundingClientRect().height,
  }))`)
  assert.ok(controls.every((item) => item.label && item.height >= 20), JSON.stringify(controls))
  const primaryControls = controls.filter((item) => item.height > 30)
  assert.ok(primaryControls.every((item) => item.height >= 44), JSON.stringify(primaryControls))
  const screenshot = await cdp.send('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: false, fromSurface: true,
  })
  const screenshotPath = path.join(outputDirectory, 'local-edit.png')
  await fs.writeFile(screenshotPath, Buffer.from(screenshot.data, 'base64'))

  const browserErrors = cdp.events.filter((event) => {
    if (event.method === 'Runtime.exceptionThrown') return true
    if (event.method !== 'Log.entryAdded' || event.params?.entry?.level !== 'error') return false
    const entry = event.params.entry
    return !(
      entry.source === 'network'
      && entry.text.includes('404')
      && entry.url?.endsWith('/mural-plan')
    )
  })
  assert.deepEqual(browserErrors, [])
  assert.equal(await cdp.evaluate("Boolean(document.querySelector('vite-error-overlay'))"), false)

  const report = {
    ok: true,
    fixture,
    projectId: saved.project.project.id,
    commandId: command.command_id,
    targetLabel,
    editorSequenceSha256: evidence.editorSequenceSha256,
    changedPixelCount: evidence.changedPixelCount,
    stepChangedPixelCount: evidence.step.changed_pixel_count,
    provenance: {
      classification: provenance.reproducibility,
      selectionSha256: provenance.selection_sha256,
      parentRevisionId: provenance.parent_revision_id,
    },
    controls,
    screenshot: screenshotPath,
    provenanceScreenshot: provenanceScreenshotPath,
  }
  await fs.writeFile(
    path.join(outputDirectory, 'local-edit-report.json'),
    `${JSON.stringify(report, null, 2)}\n`,
  )
  console.log(JSON.stringify(report, null, 2))
  socket.close()
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
