#!/usr/bin/env node

import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5173'
const fixture = path.resolve('tests/fixtures/synthetic/geometry-nozzle-040.png')
const downloadDirectory = path.resolve('workspace/qa/project-bundle-history-downloads')
const screenshot = path.resolve('workspace/qa/project-bundle-history.png')
let activeSocket
const childProcesses = []

function stopChildren() {
  for (const child of childProcesses) child.kill('SIGTERM')
  childProcesses.splice(0)
}

async function startIsolatedApp() {
  const workspace = await fs.mkdtemp(path.join(os.tmpdir(), 'image23mf-bundle-browser-'))
  const apiUrl = 'http://127.0.0.1:8397'
  const webUrl = 'http://127.0.0.1:5197'
  const api = spawn(
    path.resolve('.venv/bin/python'),
    ['-m', 'uvicorn', 'image23mf.api.app:app', '--host', '127.0.0.1', '--port', '8397'],
    {
      cwd: path.resolve('.'),
      env: { ...process.env, IMAGE23MF_WORKSPACE: workspace },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  )
  const vite = spawn(
    'npm',
    ['run', 'dev', '--', '--host', '127.0.0.1', '--port', '5197'],
    {
      cwd: path.resolve('frontend'),
      env: { ...process.env, IMAGE23MF_API_TARGET: apiUrl },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  )
  childProcesses.push(api, vite)
  await waitFor(async () => {
    try {
      const response = await fetch(`${apiUrl}/api/health`)
      return response.ok
    } catch {
      return false
    }
  }, 'isolated API startup')
  await waitFor(async () => {
    try {
      const response = await fetch(webUrl)
      return response.ok
    } catch {
      return false
    }
  }, 'isolated Vite startup')
  return { apiUrl, webUrl, workspace }
}

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
      expression, awaitPromise: true, returnByValue: true, userGesture: true,
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

async function setFileInput(cdp, filename, selector = 'input[type=file]') {
  const inputObject = await cdp.send('Runtime.evaluate', {
    expression: `document.querySelector(${JSON.stringify(selector)})`,
    returnByValue: false,
  })
  assert.ok(inputObject.result.objectId, 'one file input must be available')
  await cdp.send('DOM.getDocument', { depth: 0, pierce: true })
  const input = await cdp.send('DOM.requestNode', { objectId: inputObject.result.objectId })
  await cdp.send('DOM.setFileInputFiles', { nodeId: input.nodeId, files: [filename] })
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
    if (matches.length !== 1) return { count: matches.length }
    matches[0].scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = matches[0].getBoundingClientRect()
    return { count: 1, x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.equal(point.count, 1, `${label} must resolve to one enabled button`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

async function clickHistory(cdp, direction) {
  const point = await cdp.evaluate(`(() => {
    const matches = [...document.querySelectorAll('[aria-label="Edit history"] button')].filter(
      (button) => [...button.querySelectorAll('span')].some(
        (span) => span.textContent.trim() === ${JSON.stringify(direction)},
      ) && !button.disabled,
    )
    if (matches.length !== 1) return { count: matches.length }
    const bounds = matches[0].getBoundingClientRect()
    return { count: 1, x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.equal(point.count, 1, `${direction} must resolve to one enabled history button`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

async function main() {
  await fs.access(fixture)
  await fs.rm(downloadDirectory, { recursive: true, force: true })
  await fs.mkdir(downloadDirectory, { recursive: true })
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
    cdp.send('Page.enable'), cdp.send('Runtime.enable'), cdp.send('DOM.enable'),
  ])
  await cdp.send('Page.setDownloadBehavior', {
    behavior: 'allow', downloadPath: downloadDirectory,
  })
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(() => cdp.evaluate("document.readyState === 'complete'"), 'application load')
  await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Choose an image')"),
    'image import screen',
  )
  await setFileInput(cdp, fixture)
  const sourceProjectId = await waitFor(
    () => cdp.evaluate("document.querySelector('[aria-label=\"Exact artwork preview and comparison\"]') && localStorage.getItem('image23mf.recentProjectId')"),
    'source project editor',
    90_000,
  )

  const history = await cdp.evaluate(`(async () => {
    const projectId = ${JSON.stringify(sourceProjectId)}
    const read = async (response) => {
      const body = await response.json()
      if (!response.ok) throw new Error(body.error?.message ?? 'QA request failed')
      return body
    }
    let current = (await read(await fetch('/api/projects/' + projectId))).draft
    const save = async (width, label) => {
      const config = structuredClone(current.config)
      config.canvas.width_mm = width
      current = await read(await fetch('/api/projects/' + projectId + '/draft', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          config,
          operations: current.operations,
          expected_draft_generation: current.generation,
          history_command: {
            schema_version: 1,
            id: crypto.randomUUID(),
            command_type: 'config_change',
            label,
            before_state_sha256: current.history.state_sha256,
            expected_cursor_node_id: current.history.cursor_node_id,
          },
        }),
      }))
    }
    const undo = async () => {
      current = await read(await fetch('/api/projects/' + projectId + '/draft/history/undo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request_id: crypto.randomUUID(),
          expected_draft_generation: current.generation,
          expected_cursor_node_id: current.history.cursor_node_id,
        }),
      }))
    }
    await save(230, 'Width 230')
    await save(240, 'Abandoned width 240')
    await undo()
    await save(250, 'Active branch width 250')
    await undo()
    return { total: current.history.total, canRedo: current.history.can_redo }
  })()`)
  assert.ok(history.total >= 2)
  assert.equal(history.canRedo, true)

  await clickButton(cdp, 'Projects')
  await waitFor(() => cdp.evaluate("document.body.innerText.includes('Your recent work')"), 'project library')
  const cardSelector = `.project-card[data-project-id=${JSON.stringify(sourceProjectId)}]`
  await waitFor(() => cdp.evaluate(`Boolean(document.querySelector(${JSON.stringify(cardSelector)}))`), 'source card')
  await clickButton(cdp, `Export ${path.parse(fixture).name} with full history`, `document.querySelector(${JSON.stringify(cardSelector)})`)
  const bundlePath = await waitFor(async () => {
    const entries = await fs.readdir(downloadDirectory)
    const bundle = entries.find((entry) => entry.endsWith('.image23mf'))
    return bundle ? path.join(downloadDirectory, bundle) : null
  }, 'downloaded full-history bundle')
  await setFileInput(cdp, bundlePath, 'input[aria-label="Choose project bundle"]')
  const importOutcome = await waitFor(
    () => cdp.evaluate(`(() => {
      const dialog = document.querySelector('.project-browser-import-result')
      const error = document.querySelector('.project-browser-error')
      return dialog ? {
        report: dialog.innerText,
        entries: Object.fromEntries([...dialog.querySelectorAll('dl > div')].map((item) => [
          item.querySelector('dt')?.textContent.trim(), item.querySelector('dd')?.textContent.trim(),
        ])),
        backgroundInert: document.querySelector('.project-browser')?.hasAttribute('inert'),
        activeButton: document.activeElement?.textContent.trim(),
      } : error ? { error: error.innerText } : null
    })()`),
    'reviewable import completion report',
  )
  assert.equal(importOutcome.error, undefined, `bundle import failed: ${importOutcome.error}`)
  const report = importOutcome.report
  assert.ok(report.includes('Full history'))
  assert.deepEqual(importOutcome.entries, {
    History: 'Full history',
    'Imported nodes': '4',
    'Abandoned audit nodes': '1',
    Restore: 'New project copy',
  })
  assert.equal(importOutcome.backgroundInert, true)
  assert.equal(importOutcome.activeButton, 'Keep browsing')
  await clickButton(cdp, 'Open imported project')
  const importedProjectId = await waitFor(
    () => cdp.evaluate(`(() => {
      const id = localStorage.getItem('image23mf.recentProjectId')
      return id && id !== ${JSON.stringify(sourceProjectId)} ? id : null
    })()`),
    'imported project selection',
  )
  let opened
  try {
    opened = await waitFor(
      () => cdp.evaluate(`(() => {
        if (document.body.innerText.includes('geometry-nozzle-040 (Imported)')
          && document.querySelector('[aria-label="Edit history"]')) return { ready: true }
        const error = document.querySelector('.global-notice')
        return error ? { ready: false, error: error.innerText } : null
      })()`),
      'explicitly opened imported project editor',
      20_000,
    )
  } catch (error) {
    const diagnostic = await cdp.evaluate(`({
      url: location.href,
      recentProjectId: localStorage.getItem('image23mf.recentProjectId'),
      body: document.body.innerText.slice(0, 2000),
      viteOverlay: Boolean(document.querySelector('vite-error-overlay')),
    })`)
    throw new Error(`${error.message}; diagnostic=${JSON.stringify(diagnostic)}`)
  }
  assert.equal(opened.ready, true, `imported project did not open: ${opened.error}`)
  await clickHistory(cdp, 'Redo')
  const redoneWidth = await waitFor(
    () => cdp.evaluate(`fetch('/api/projects/${importedProjectId}').then((response) => response.json()).then((body) => body.draft?.config?.canvas?.width_mm === 250 ? 250 : null)`),
    'redo on restored history',
  )
  assert.equal(redoneWidth, 250)
  await clickHistory(cdp, 'Undo')
  const undoneWidth = await waitFor(
    () => cdp.evaluate(`fetch('/api/projects/${importedProjectId}').then((response) => response.json()).then((body) => body.draft?.config?.canvas?.width_mm === 230 ? 230 : null)`),
    'undo on restored history',
  )
  assert.equal(undoneWidth, 230)

  const isolated = await startIsolatedApp()
  const emptyProjects = await fetch(`${isolated.apiUrl}/api/projects?include_archived=true`)
    .then((response) => response.json())
  assert.equal(emptyProjects.total, 0, 'isolated destination workspace must start empty')
  await cdp.send('Page.navigate', { url: isolated.webUrl })
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Choose an image')"),
    'isolated clean import screen',
  )
  await clickButton(cdp, 'Projects')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Your recent work')"),
    'isolated empty project library',
  )
  await setFileInput(cdp, bundlePath, 'input[aria-label="Choose project bundle"]')
  const cleanImport = await waitFor(
    () => cdp.evaluate(`(() => {
      const dialog = document.querySelector('.project-browser-import-result')
      if (!dialog) return null
      return {
        entries: Object.fromEntries([...dialog.querySelectorAll('dl > div')].map((item) => [
          item.querySelector('dt')?.textContent.trim(), item.querySelector('dd')?.textContent.trim(),
        ])),
        backgroundInert: document.querySelector('.project-browser')?.hasAttribute('inert'),
        activeButton: document.activeElement?.textContent.trim(),
      }
    })()`),
    'clean-workspace import report',
  )
  assert.deepEqual(cleanImport.entries, {
    History: 'Full history',
    'Imported nodes': '4',
    'Abandoned audit nodes': '1',
    Restore: 'New project copy',
  })
  assert.equal(cleanImport.backgroundInert, true)
  assert.equal(cleanImport.activeButton, 'Keep browsing')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 720, height: 320, deviceScaleFactor: 1, mobile: false,
  })
  const shortViewportModal = await cdp.evaluate(`(() => {
    const dialog = document.querySelector('.project-browser-import-result')
    if (!dialog) return null
    const style = getComputedStyle(dialog)
    return {
      clientHeight: dialog.clientHeight,
      scrollHeight: dialog.scrollHeight,
      overflowY: style.overflowY,
      viewportHeight: window.innerHeight,
      backgroundInert: document.querySelector('.project-browser')?.hasAttribute('inert'),
    }
  })()`)
  assert.equal(shortViewportModal.overflowY, 'auto')
  assert.equal(shortViewportModal.backgroundInert, true)
  assert.ok(
    shortViewportModal.clientHeight <= shortViewportModal.viewportHeight - 32,
    `short modal must fit viewport; got ${shortViewportModal.clientHeight}px in ${shortViewportModal.viewportHeight}px`,
  )
  assert.ok(shortViewportModal.scrollHeight > shortViewportModal.clientHeight)
  await clickButton(cdp, 'Open imported project')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  const cleanProjectId = await waitFor(
    () => cdp.evaluate("localStorage.getItem('image23mf.recentProjectId')"),
    'clean imported project selection',
  )
  await waitFor(
    () => cdp.evaluate(`document.body.innerText.includes('geometry-nozzle-040')
      && Boolean(document.querySelector('[aria-label="Edit history"]'))`),
    'clean imported project editor',
    30_000,
  )
  await clickHistory(cdp, 'Redo')
  const cleanRedoneWidth = await waitFor(
    () => fetch(`${isolated.apiUrl}/api/projects/${cleanProjectId}`)
      .then((response) => response.json())
      .then((body) => body.draft?.config?.canvas?.width_mm === 250 ? 250 : null),
    'clean-workspace restored redo',
  )
  await clickHistory(cdp, 'Undo')
  const cleanUndoneWidth = await waitFor(
    () => fetch(`${isolated.apiUrl}/api/projects/${cleanProjectId}`)
      .then((response) => response.json())
      .then((body) => body.draft?.config?.canvas?.width_mm === 230 ? 230 : null),
    'clean-workspace restored undo',
  )
  assert.equal(cleanRedoneWidth, 250)
  assert.equal(cleanUndoneWidth, 230)

  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  const capture = await cdp.send('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: true,
  })
  await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))
  console.log(JSON.stringify({
    sourceProjectId,
    importedProjectId,
    cleanProjectId,
    importedHistoryNodes: 4,
    abandonedHistoryNodes: 1,
    redoneWidth,
    undoneWidth,
    cleanRedoneWidth,
    cleanUndoneWidth,
    cleanWorkspace: isolated.workspace,
    screenshot,
  }))
  socket.close()
  activeSocket = undefined
  stopChildren()
}

main().catch((error) => {
  activeSocket?.close()
  stopChildren()
  console.error(error)
  process.exitCode = 1
})
