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
  process.env.IMAGE23MF_QA_SELECTION_DIR ?? 'workspace/qa/canvas-selection',
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
    if (!response.ok) return null
    return response.json()
  })()`)
}

async function selectionState(cdp, expectedSteps) {
  return waitFor(async () => {
    const project = await projectState(cdp)
    const operations = project?.draft?.operations ?? []
    const selections = operations.filter((item) => item.operation_type === 'canvas_selection_v1')
    const command = selections.at(-1)?.parameters?.command
    const steps = command?.selector?.selection?.primitives?.length
    if (steps !== expectedSteps) return false
    return { project, selections, command, steps }
  }, `${expectedSteps} saved selection steps`)
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

  const initial = await projectState(cdp)
  assert.ok(initial?.draft?.editor_sequence_sha256)
  const initialSequence = initial.draft.editor_sequence_sha256
  await click(cdp, buttonExpression('Rectangle'), 'Rectangle tool')
  const drag = await cdp.evaluate(`(() => {
    const plane = document.querySelector('.i23-canvas-plane[data-plane="primary"]')
    plane.scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = plane.getBoundingClientRect()
    return {
      start: { x: bounds.left + bounds.width * 0.2, y: bounds.top + bounds.height * 0.2 },
      end: { x: bounds.left + bounds.width * 0.58, y: bounds.top + bounds.height * 0.62 },
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
  const rectangle = await selectionState(cdp, 1)
  assert.equal(rectangle.project.draft.editor_sequence_sha256, initialSequence)
  assert.equal(rectangle.command.selector.selection.primitives[0].kind, 'rectangle')
  await new Promise((resolve) => setTimeout(resolve, 500))
  await waitFor(
    () => cdp.evaluate(`(() => {
      const previewCurrent = document.body.innerText.includes('Preview complete and current.')
        || (document.body.innerText.includes('Preview matches these cleanup settings.')
          && [...document.querySelectorAll('button')].some((button) => button.textContent.trim() === 'Build 3MF' && !button.disabled))
      const overlays = document.querySelectorAll('[data-canvas-selection="true"]').length
      return previewCurrent && overlays >= 1 ? { previewCurrent, overlays } : false
    })()`),
    'current preview and rectangle overlay after save',
  )

  await click(
    cdp,
    "document.querySelector('.region-inspector-region-list button')",
    'accessible region-list selection',
  )
  await click(cdp, buttonExpression('Add selected region'), 'Add selected region')
  const regionAdded = await selectionState(cdp, 2)
  assert.equal(regionAdded.project.draft.editor_sequence_sha256, initialSequence)
  assert.equal(regionAdded.command.selector.selection.primitives[1].kind, 'regions')
  assert.ok(regionAdded.project.draft.history.can_undo)

  await cdp.send('Page.reload', { ignoreCache: true })
  await new Promise((resolve) => setTimeout(resolve, 1500))
  await waitFor(
    () => cdp.evaluate(`(
      document.body.innerText.includes('Preview matches these cleanup settings.')
      && [...document.querySelectorAll('button')].some((button) => button.textContent.trim() === 'Build 3MF' && !button.disabled)
      && document.querySelectorAll('[data-canvas-selection="true"]').length >= 2
    )`),
    'restored selection overlays after reload',
  )
  await click(
    cdp,
    "[...document.querySelectorAll('.editor-history-controls button')].find((button) => button.getAttribute('aria-label')?.startsWith('Undo:') && !button.disabled)",
    'selection undo',
  )
  const undone = await selectionState(cdp, 1)
  assert.equal(undone.command.selector.selection.primitives[0].kind, 'rectangle')
  await click(
    cdp,
    "[...document.querySelectorAll('.editor-history-controls button')].find((button) => button.getAttribute('aria-label')?.startsWith('Redo:') && !button.disabled)",
    'selection redo',
  )
  const redone = await selectionState(cdp, 2)

  const ui = await cdp.evaluate(`(() => ({
    previewCurrent: document.body.innerText.includes('Preview complete and current.')
      || (document.body.innerText.includes('Preview matches these cleanup settings.')
        && [...document.querySelectorAll('button')].some((button) => button.textContent.trim() === 'Build 3MF' && !button.disabled)),
    controls: [...document.querySelectorAll('.i23-canvas-selection-bar input, .i23-canvas-selection-bar select')].map((item) => ({
      label: item.closest('label')?.innerText.replace(/\\s+/g, ' ').trim(),
      height: item.getBoundingClientRect().height,
    })),
    selectionStatus: document.querySelector('.i23-canvas-selection-bar output')?.textContent,
    viteOverlay: Boolean(document.querySelector('vite-error-overlay')),
  }))()`)
  assert.equal(ui.previewCurrent, true)
  assert.equal(ui.viteOverlay, false)
  assert.ok(ui.controls.every((item) => item.label && item.height >= 44), JSON.stringify(ui.controls))

  await cdp.evaluate(`document.querySelector('.i23-canvas-selection-bar')?.scrollIntoView({ block: 'center' })`)
  await new Promise((resolve) => setTimeout(resolve, 250))
  const screenshot = await cdp.send('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: false, fromSurface: true,
  })
  const screenshotPath = path.join(outputDirectory, 'canvas-selection.png')
  await fs.writeFile(screenshotPath, Buffer.from(screenshot.data, 'base64'))
  const browserErrors = cdp.events.filter((event) => {
    if (event.method === 'Runtime.exceptionThrown') return true
    if (event.method !== 'Log.entryAdded' || event.params?.entry?.level !== 'error') return false
    const entry = event.params.entry
    const expectedMissingMuralPlan =
      entry.source === 'network'
      && entry.text.includes('404')
      && entry.url?.endsWith('/mural-plan')
    return !expectedMissingMuralPlan
  })
  assert.deepEqual(browserErrors, [])

  const report = {
    ok: true,
    fixture,
    projectId: redone.project.project.id,
    initialEditorSequenceSha256: initialSequence,
    finalEditorSequenceSha256: redone.project.draft.editor_sequence_sha256,
    selectionOperationCount: redone.selections.length,
    finalSelectionSteps: redone.steps,
    renderEvidenceStayedCurrent: ui.previewCurrent,
    controls: ui.controls,
    screenshot: screenshotPath,
  }
  const reportPath = path.join(outputDirectory, 'canvas-selection-report.json')
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`)
  console.log(JSON.stringify(report, null, 2))
  socket.close()
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
