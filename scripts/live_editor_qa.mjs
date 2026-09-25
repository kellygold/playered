#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5173'
const fixture = path.resolve(
  process.env.IMAGE23MF_QA_FIXTURE ?? 'tests/fixtures/synthetic/geometry-nozzle-040.png',
)
const replacementFixture = path.resolve(
  process.env.IMAGE23MF_QA_REPLACEMENT_FIXTURE
    ?? 'tests/fixtures/synthetic/boundary-transparency.png',
)
const screenshot = path.resolve(
  process.env.IMAGE23MF_QA_SCREENSHOT ?? 'workspace/qa/live-editor-mvp.png',
)
const outputOnly = process.env.IMAGE23MF_QA_OUTPUT_ONLY === '1'
let activeSocket = null

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
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`Timed out waiting for ${description}; last value: ${JSON.stringify(last)}`)
}

async function fillInput(cdp, selector, value, { tab = true } = {}) {
  const documentNode = await cdp.send('DOM.getDocument', { depth: -1, pierce: true })
  const input = await cdp.send('DOM.querySelector', {
    nodeId: documentNode.root.nodeId,
    selector,
  })
  assert.notEqual(input.nodeId, 0, `input must exist: ${selector}`)
  await cdp.send('DOM.focus', { nodeId: input.nodeId })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'rawKeyDown',
    key: 'a',
    code: 'KeyA',
    modifiers: 4,
    commands: ['SelectAll'],
  })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyUp',
    key: 'a',
    code: 'KeyA',
    modifiers: 4,
  })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'rawKeyDown',
    key: 'Backspace',
    code: 'Backspace',
  })
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyUp',
    key: 'Backspace',
    code: 'Backspace',
  })
  await cdp.send('Input.insertText', { text: String(value) })
  if (tab) {
    await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Tab', code: 'Tab' })
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab' })
  }
  return cdp.evaluate(`document.querySelector(${JSON.stringify(selector)}).value`)
}

async function setFileInput(cdp, filename) {
  const inputObject = await cdp.send('Runtime.evaluate', {
    expression: "document.querySelector('input[type=file]')",
    returnByValue: false,
  })
  assert.ok(inputObject.result.objectId, 'the source file input must have a runtime object')
  await cdp.send('DOM.getDocument', { depth: 0, pierce: true })
  const input = await cdp.send('DOM.requestNode', { objectId: inputObject.result.objectId })
  assert.notEqual(input.nodeId, 0, 'the source file input must exist')
  await cdp.send('DOM.setFileInputFiles', { nodeId: input.nodeId, files: [filename] })
  await cdp.evaluate(
    "document.querySelector('input[type=file]').dispatchEvent(new Event('change', { bubbles: true }))",
  )
}

async function clickButton(cdp, label, scope = 'document') {
  const point = await cdp.evaluate(`(() => {
    const root = ${scope}
    const matches = [...root.querySelectorAll('button')].filter(
      (button) => button.textContent.trim() === ${JSON.stringify(label)} && !button.disabled,
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
    `${label} must resolve to exactly one enabled button; available: ${JSON.stringify(point.available ?? [])}`,
  )
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x: point.x,
    y: point.y,
    button: 'left',
    clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x: point.x,
    y: point.y,
    button: 'left',
    clickCount: 1,
  })
}

async function clickRevisionButton(cdp, label) {
  const point = await cdp.evaluate(`(() => {
    const matches = [...document.querySelectorAll('.revision-list button')].filter(
      (button) => button.querySelector('strong')?.textContent.trim() === ${JSON.stringify(label)} && !button.disabled,
    )
    if (matches.length !== 1) return { count: matches.length }
    matches[0].scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = matches[0].getBoundingClientRect()
    return { count: 1, x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.equal(point.count, 1, `${label} must resolve to exactly one revision button`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

async function clickHistoryButton(cdp, direction) {
  const prefix = `${direction === 'undo' ? 'Undo' : 'Redo'}:`
  const point = await cdp.evaluate(`(() => {
    const matches = [...document.querySelectorAll('.editor-history-controls button')].filter(
      (button) => button.getAttribute('aria-label')?.startsWith(${JSON.stringify(prefix)}) && !button.disabled,
    )
    if (matches.length !== 1) return {
      count: matches.length,
      controls: [...document.querySelectorAll('.editor-history-controls button')].map((button) => ({
        label: button.getAttribute('aria-label'),
        disabled: button.disabled,
      })),
      status: document.querySelector('.editor-history-controls')?.textContent,
      projectId: localStorage.getItem('image23mf.recentProjectId'),
      body: document.body.innerText.slice(0, 1200),
    }
    matches[0].scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = matches[0].getBoundingClientRect()
    return { count: 1, x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  if (point.count !== 1) {
    console.log('history-button-debug', JSON.stringify(cdp.events.filter(
      (event) => event.method === 'Runtime.exceptionThrown' || event.method === 'Runtime.consoleAPICalled' || event.method === 'Log.entryAdded',
    ).slice(-10).map((event) => ({
      method: event.method,
      type: event.params?.type ?? event.params?.entry?.level,
      exception: event.params?.exceptionDetails?.exception?.description ?? event.params?.exceptionDetails?.text,
      args: event.params?.args?.map((arg) => arg.value ?? arg.description),
      text: event.params?.entry?.text,
    })), null, 2))
  }
  assert.equal(point.count, 1, `${direction} must resolve to exactly one enabled history button: ${JSON.stringify(point)}`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

async function pressKey(cdp, key, code = key, modifiers = 0) {
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key, code, modifiers })
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, modifiers })
}

async function reloadPage(cdp) {
  const marker = `qa-${Date.now()}-${Math.random()}`
  await cdp.evaluate(`window.__image23mfQaReloadMarker = ${JSON.stringify(marker)}`)
  await cdp.send('Page.reload', { ignoreCache: true })
  await waitFor(
    () => cdp.evaluate(`document.readyState === 'complete' && window.__image23mfQaReloadMarker !== ${JSON.stringify(marker)}`),
    'a completed browser reload',
  )
}

async function proveRailScroll(cdp, rail) {
  const selector = `.i23-editor-rail[data-rail=${JSON.stringify(rail)}] .i23-editor-rail__body`
  const before = await cdp.evaluate(`(() => {
    const element = document.querySelector(${JSON.stringify(selector)})
    if (!element) return null
    element.scrollTop = 0
    const bounds = element.getBoundingClientRect()
    return {
      x: bounds.left + bounds.width / 2,
      y: bounds.top + Math.min(bounds.height / 2, 300),
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      scrollTop: element.scrollTop,
    }
  })()`)
  assert.ok(before, `${rail} rail body must exist`)
  assert.ok(
    before.clientHeight < before.scrollHeight,
    `${rail} rail must constrain overflowing content`,
  )
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseWheel',
    x: before.x,
    y: before.y,
    deltaX: 0,
    deltaY: 520,
  })
  const afterScrollTop = await waitFor(
    () => cdp.evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollTop`),
    `${rail} rail wheel scrolling`,
  )
  return { ...before, afterScrollTop }
}

async function runReplacementScrollQa(cdp, initialProjectId) {
  const openReplacement = async () => {
    await clickButton(cdp, 'Replace image')
    await setFileInput(cdp, replacementFixture)
    return waitFor(
      () => cdp.evaluate(`(() => {
        const dialog = document.querySelector('[role="dialog"]')
        if (!dialog?.innerText.includes('Replace the current source?')) return null
        return {
          bodyOverflow: document.body.style.overflow,
          appInert: document.querySelector('.app-shell')?.hasAttribute('inert') ?? false,
          focusedAction: document.activeElement?.textContent?.trim(),
          text: dialog.innerText,
        }
      })()`),
      'the source-replacement confirmation',
    )
  }

  const canceledDialog = await openReplacement()
  assert.ok(canceledDialog.text.includes(path.basename(replacementFixture)))
  assert.equal(canceledDialog.bodyOverflow, 'hidden')
  assert.equal(canceledDialog.appInert, true)
  assert.equal(canceledDialog.focusedAction, 'Keep current image')
  await clickButton(cdp, 'Keep current image')
  const canceledState = await waitFor(
    () => cdp.evaluate(`(() => {
      const state = {
        closed: !document.querySelector('[role="dialog"]'),
        bodyOverflow: document.body.style.overflow,
        appInert: document.querySelector('.app-shell')?.hasAttribute('inert') ?? false,
      }
      return state.closed && state.bodyOverflow === '' && !state.appInert ? state : null
    })()`),
    'the canceled replacement dialog with page scrolling restored',
  )

  const confirmedDialog = await openReplacement()
  assert.equal(confirmedDialog.bodyOverflow, 'hidden')
  assert.equal(confirmedDialog.appInert, true)
  await clickButton(cdp, 'Import replacement')
  const replacementProject = await waitFor(
    () => cdp.evaluate(`(() => {
      const projectId = localStorage.getItem('image23mf.recentProjectId')
      const filename = document.querySelector('.i23-editor-project p strong')?.textContent
      const dialogClosed = !document.querySelector('[role="dialog"]')
      const bodyOverflow = document.body.style.overflow
      const appInert = document.querySelector('.app-shell')?.hasAttribute('inert') ?? false
      return projectId && projectId !== ${JSON.stringify(initialProjectId)}
        && filename === ${JSON.stringify(path.basename(replacementFixture))}
        && dialogClosed && bodyOverflow === '' && !appInert
        ? { projectId, filename, dialogClosed, bodyOverflow, appInert }
        : null
    })()`),
    'the replacement workspace with page scrolling restored',
    90_000,
  )
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the replacement preview',
    90_000,
  )

  const desktopRails = {
    left: await proveRailScroll(cdp, 'left'),
    right: await proveRailScroll(cdp, 'right'),
  }
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 800,
    height: 700,
    deviceScaleFactor: 1,
    mobile: false,
  })
  const compactBefore = await cdp.evaluate(`(() => ({
    innerHeight,
    scrollHeight: document.documentElement.scrollHeight,
    scrollY,
    bodyOverflow: document.body.style.overflow,
  }))()`)
  assert.equal(compactBefore.bodyOverflow, '')
  assert.ok(compactBefore.scrollHeight > compactBefore.innerHeight, JSON.stringify(compactBefore))
  await cdp.evaluate('window.scrollTo(0, 0); true')
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseWheel',
    x: 790,
    y: 350,
    deltaX: 0,
    deltaY: 650,
  })
  const compactScrollY = await waitFor(
    () => cdp.evaluate('window.scrollY'),
    'compact page scrolling after replacement',
  )
  assert.ok(compactScrollY > 0)

  return {
    canceledDialog,
    canceledState,
    confirmedDialog,
    replacementProject,
    desktopRails,
    compact: { ...compactBefore, afterScrollY: compactScrollY },
  }
}

async function firstExactAssignmentPick(result) {
  const artifact = result.artifacts.find((candidate) => candidate.kind === 'region-assignment')
  assert.ok(artifact, 'the preview must publish exact region assignment evidence')
  const graph = result.region_graph
  assert.ok(graph?.regions?.length, 'the preview must publish an inspectable region graph')
  assert.equal(artifact.metadata.graph_fingerprint, result.risk_report.graph_fingerprint)
  assert.equal(artifact.metadata.region_count, graph.regions.length)
  const response = await fetch(new URL(artifact.download_url, 'http://127.0.0.1:8323'))
  assert.equal(response.ok, true)
  const bytes = Buffer.from(await response.arrayBuffer())
  assert.equal(bytes.length, graph.width_px * graph.height_px * 4)
  let best = null
  for (let pixel = 0; pixel < graph.width_px * graph.height_px; pixel += 1) {
    const regionIndex = bytes.readInt32LE(pixel * 4)
    if (regionIndex < 0) continue
    const pixelX = pixel % graph.width_px
    const pixelY = Math.floor(pixel / graph.width_px)
    const score = Math.abs(pixelX + 0.5 - graph.width_px / 2) + Math.abs(pixelY + 0.5 - graph.height_px / 2)
    if (best && best.score <= score) continue
    best = {
      pixelX,
      pixelY,
      regionId: graph.regions[regionIndex].id,
      width: graph.width_px,
      height: graph.height_px,
      score,
    }
  }
  if (!best) throw new Error('the exact assignment did not contain an active region pixel')
  return best
}

async function clickExactCanvasPixel(cdp, pick) {
  await waitFor(
    () =>
      cdp.evaluate(
        `Boolean(document.querySelector('[data-canvas-selection-status="ready"]'))`,
      ),
    'verified exact canvas assignment',
  )
  const point = await cdp.evaluate(`(() => {
    const plane = document.querySelector('.i23-canvas-plane[data-plane="primary"]')
    if (!plane) return null
    plane.scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = plane.getBoundingClientRect()
    return {
      x: bounds.left + ((${pick.pixelX} + 0.5) / ${pick.width}) * bounds.width,
      y: bounds.top + ((${pick.pixelY} + 0.5) / ${pick.height}) * bounds.height,
    }
  })()`)
  assert.ok(point)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x: point.x,
    y: point.y,
    button: 'left',
    clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x: point.x,
    y: point.y,
    button: 'left',
    clickCount: 1,
  })
  if (process.env.IMAGE23MF_QA_DEBUG === '1') {
    console.log('exact-pick-debug', JSON.stringify({ expected: pick, point, actual: await cdp.evaluate(`(() => ({
      coordinate: document.querySelector('[aria-label="Selected canvas coordinate"]')?.textContent,
      selected: document.querySelector('[data-selected-region="true"]')?.getAttribute('data-region-id'),
      active: [...document.querySelectorAll('.i23-canvas-view-switcher button')].find((button) => button.getAttribute('aria-pressed') === 'true')?.getAttribute('aria-label'),
      projectId: localStorage.getItem('image23mf.recentProjectId'),
      body: document.body.innerText.slice(0, 1200),
      plane: (() => { const b = document.querySelector('.i23-canvas-plane[data-plane="primary"]')?.getBoundingClientRect(); return b ? { left: b.left, top: b.top, width: b.width, height: b.height } : null })(),
      viewport: (() => { const b = document.querySelector('.i23-canvas-viewport')?.getBoundingClientRect(); return b ? { left: b.left, top: b.top, width: b.width, height: b.height } : null })(),
    }))()`), browserEvents: cdp.events.filter((event) => event.method === 'Runtime.exceptionThrown' || event.method === 'Runtime.consoleAPICalled' || event.method === 'Log.entryAdded').slice(-10).map((event) => ({
      method: event.method,
      type: event.params?.type ?? event.params?.entry?.level,
      exception: event.params?.exceptionDetails?.exception?.description ?? event.params?.exceptionDetails?.text,
      args: event.params?.args?.map((arg) => arg.value ?? arg.description),
      text: event.params?.entry?.text,
    })) }, null, 2))
  }
  await waitFor(
    () =>
      cdp.evaluate(
        `document.querySelector('.i23-canvas-region-selection [data-selected-region="true"]')?.dataset.regionId === ${JSON.stringify(pick.regionId)} && document.body.innerText.includes(${JSON.stringify(pick.regionId)})`,
      ),
    'exact canvas-to-inspector region synchronization',
  )
}

async function main() {
  await Promise.all([fs.access(fixture), fs.access(replacementFixture)])
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
  await cdp.send('Page.bringToFront')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("document.readyState === 'complete'"),
    'the application document',
  )
  await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("document.readyState === 'complete' && document.body.innerText.includes('Choose an image') && Boolean(document.querySelector('input[type=file]'))"),
    'the import screen',
  )

  await setFileInput(cdp, fixture)

  const importOutcome = await waitFor(
    () => cdp.evaluate(`(() => {
      const alerts = [...document.querySelectorAll('[role="alert"]')].map((item) => item.innerText)
      const canvas = Boolean(document.querySelector('[aria-label="Exact artwork preview and comparison"]'))
      return canvas || alerts.length ? { canvas, alerts, text: document.body.innerText.slice(0, 1500) } : null
    })()`),
    'the exact editor canvas or an actionable import error',
    90_000,
  )
  assert.equal(importOutcome.canvas, true, JSON.stringify(importOutcome, null, 2))
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the initial preview result',
    90_000,
  )

  const initial = await cdp.evaluate(`(() => ({
    projectId: localStorage.getItem('image23mf.recentProjectId'),
    processedCanvas: Boolean(document.querySelector('[aria-label="Processed image canvas"]')),
    quantizedEnabled: [...document.querySelectorAll('button')].some((button) => button.textContent.trim() === 'Quantized' && !button.disabled),
    riskEnabled: [...document.querySelectorAll('button')].some((button) => button.textContent.trim() === 'Risk' && !button.disabled),
    geometryDisabled: [...document.querySelectorAll('button')].some((button) => button.getAttribute('aria-label') === 'Geometry' && button.getAttribute('aria-disabled') === 'true'),
    slicerDisabled: [...document.querySelectorAll('button')].some((button) => button.getAttribute('aria-label') === 'Sliced' && button.getAttribute('aria-disabled') === 'true'),
  }))()`)
  assert.ok(initial.projectId)
  assert.equal(initial.processedCanvas, true)
  assert.equal(initial.quantizedEnabled, true)
  assert.equal(initial.riskEnabled, true)
  assert.equal(initial.geometryDisabled, true, JSON.stringify(initial))
  assert.equal(initial.slicerDisabled, true, JSON.stringify(initial))

  if (process.env.IMAGE23MF_QA_REPLACEMENT_ONLY === '1') {
    const replacement = await runReplacementScrollQa(cdp, initial.projectId)
    console.log(JSON.stringify({
      ok: true,
      fixture,
      replacementFixture,
      initialProjectId: initial.projectId,
      replacement,
    }, null, 2))
    socket.close()
    activeSocket = null
    return
  }

  const canvasReadiness = await cdp.evaluate(`(() => {
    const viewer = document.querySelector('.i23-editor-canvas-viewer')
    const viewport = document.querySelector('.i23-canvas-viewport')
    const riskButton = [...document.querySelectorAll('button')].find(
      (button) => button.getAttribute('aria-label') === 'Risk' && !button.disabled,
    )
    viewer?.scrollIntoView({ block: 'center', inline: 'center' })
    return {
      readiness: document.querySelector('.i23-canvas-output-readiness')?.textContent,
      viewportHeight: viewport?.getBoundingClientRect().height ?? 0,
      riskButton: Boolean(riskButton),
    }
  })()`)
  assert.ok(
    canvasReadiness.readiness?.includes('Build, validate, and download the 3MF'),
    JSON.stringify(canvasReadiness),
  )
  assert.ok(canvasReadiness.viewportHeight >= 300, JSON.stringify(canvasReadiness))
  assert.equal(canvasReadiness.riskButton, true)

  if (outputOnly) {
    const qaDirectory = path.resolve('workspace/qa')
    await fs.mkdir(qaDirectory, { recursive: true })
    const downloadDirectory = await fs.mkdtemp(path.join(qaDirectory, 'output-download-'))
    await cdp.send('Browser.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: downloadDirectory,
      eventsEnabled: true,
    })
    await clickButton(cdp, 'Build 3MF', "document.querySelector('.output-readiness')")
    const output = await waitFor(
      () => cdp.evaluate(`(() => {
        const panel = document.querySelector('.output-readiness')
        const download = [...document.querySelectorAll('button')].find(
          (button) => button.textContent.trim() === 'Download 3MF' && !button.disabled,
        )
        const retry = [...document.querySelectorAll('button')].find(
          (button) => /^(Retry 3MF build|Build again)$/.test(button.textContent.trim()) && !button.disabled,
        )
        return download || retry ? {
          ready: Boolean(download),
          retry: Boolean(retry),
          text: panel?.innerText ?? '',
        } : null
      })()`),
      'a validated 3MF download or an actionable output failure',
      300_000,
    )
    assert.equal(output.ready, true, JSON.stringify(output, null, 2))

    const geometryEnabled = await cdp.evaluate(`(() => {
      const button = [...document.querySelectorAll('button')].find(
        (candidate) => candidate.getAttribute('aria-label') === 'Geometry',
      )
      return Boolean(button && !button.disabled && button.getAttribute('aria-disabled') !== 'true')
    })()`)
    assert.equal(geometryEnabled, true, 'the generated geometry preview must enable the Geometry view')
    await clickButton(cdp, 'Geometry')
    await waitFor(
      () => cdp.evaluate("document.querySelector('img[alt=\"Geometry canvas view\"]') !== null"),
      'the generated geometry canvas view',
    )

    await clickButton(cdp, 'Download 3MF')
    const downloaded = await waitFor(async () => {
      const entries = await fs.readdir(downloadDirectory)
      const name = entries.find((entry) => entry.toLowerCase().endsWith('.3mf'))
      if (!name) return null
      const filename = path.join(downloadDirectory, name)
      const stat = await fs.stat(filename)
      return stat.size > 0 ? { filename, byteSize: stat.size } : null
    }, 'a non-empty downloaded 3MF file', 60_000)

    console.log(JSON.stringify({
      ok: true,
      fixture,
      projectId: initial.projectId,
      output,
      downloaded,
    }, null, 2))
    socket.close()
    activeSocket = null
    return
  }

  await clickButton(cdp, 'Risk')
  await waitFor(
    () => cdp.evaluate("document.querySelector('img[alt=\"Print risks canvas view\"]') !== null"),
    'the risk canvas view',
  )
  await waitFor(
    () => cdp.evaluate("document.querySelector('.i23-canvas-risks[data-presentation=\"exact\"]') instanceof HTMLCanvasElement"),
    'the verified exact risk-mask canvas',
  )
  assert.equal(await cdp.evaluate("document.querySelectorAll('.i23-canvas-risks').length"), 1)
  assert.equal(await cdp.evaluate("document.querySelectorAll('.i23-canvas-risks rect').length"), 0)
  const warningSelected = await cdp.evaluate(`(() => {
    const buttons = [...document.querySelectorAll('.region-inspector-warning-list > li > button')].filter(
      (button) => !button.disabled,
    )
    if (!buttons.length) return false
    buttons[0].click()
    return true
  })()`)
  assert.equal(warningSelected, true)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.i23-canvas-risks[data-mode=\"selected\"][data-risk-id]'))"),
    'one selected exact warning mask',
  )
  const overlayFit = await cdp.evaluate(`(() => {
    const plane = document.querySelector('.i23-canvas-plane[data-plane="primary"]')?.getBoundingClientRect()
    const overlay = document.querySelector('.i23-canvas-risks')?.getBoundingClientRect()
    return plane && overlay ? {
      plane: { left: plane.left, top: plane.top, width: plane.width, height: plane.height },
      overlay: { left: overlay.left, top: overlay.top, width: overlay.width, height: overlay.height },
    } : null
  })()`)
  assert.ok(overlayFit)
  for (const edge of ['left', 'top', 'width', 'height']) {
    assert.ok(Math.abs(overlayFit.plane[edge] - overlayFit.overlay[edge]) <= 0.25, JSON.stringify(overlayFit))
  }
  for (const expectedZoom of ['125%', '156%', '195%']) {
    const zoomPoint = await cdp.evaluate(`(() => {
      const button = document.querySelector('button[aria-label="Zoom in"]')
      if (!button) return null
      const bounds = button.getBoundingClientRect()
      return { x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
    })()`)
    assert.ok(zoomPoint)
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: zoomPoint.x, y: zoomPoint.y, button: 'left', clickCount: 1 })
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: zoomPoint.x, y: zoomPoint.y, button: 'left', clickCount: 1 })
    await waitFor(
      () => cdp.evaluate(`document.querySelector('[aria-label="Zoom level"]')?.textContent === ${JSON.stringify(expectedZoom)}`),
      `${expectedZoom} canvas zoom`,
    )
  }
  await waitFor(
    () => cdp.evaluate("document.querySelector('[aria-label=\"Zoom level\"]')?.textContent === '195%'"),
    'the high-zoom canvas state',
  )
  const overlayZoomed = await cdp.evaluate(`(() => {
    const plane = document.querySelector('.i23-canvas-plane[data-plane="primary"]')?.getBoundingClientRect()
    const overlay = document.querySelector('.i23-canvas-risks')?.getBoundingClientRect()
    return plane && overlay ? {
      plane: { left: plane.left, top: plane.top, width: plane.width, height: plane.height },
      overlay: { left: overlay.left, top: overlay.top, width: overlay.width, height: overlay.height },
    } : null
  })()`)
  assert.ok(overlayZoomed)
  for (const edge of ['left', 'top', 'width', 'height']) {
    assert.ok(Math.abs(overlayZoomed.plane[edge] - overlayZoomed.overlay[edge]) <= 0.25, JSON.stringify(overlayZoomed))
  }
  const zoomAnchor = await cdp.evaluate(`(() => {
    const viewport = document.querySelector('.i23-canvas-viewport')
    if (!viewport) return null
    const bounds = viewport.getBoundingClientRect()
    return { x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.ok(zoomAnchor)
  await cdp.evaluate(`(() => {
    const viewport = document.querySelector('.i23-canvas-viewport')
    viewport?.dispatchEvent(new WheelEvent('wheel', {
      bubbles: true,
      cancelable: true,
      clientX: ${zoomAnchor.x},
      clientY: ${zoomAnchor.y},
      deltaY: ${-Math.log(2 / (1.25 ** 3)) / 0.0015},
    }))
    return true
  })()`)
  await waitFor(
    () => cdp.evaluate("document.querySelector('[aria-label=\"Zoom level\"]')?.textContent === '200%'"),
    '200% canvas zoom',
  )
  const overlay2x = await cdp.evaluate(`(() => {
    const plane = document.querySelector('.i23-canvas-plane[data-plane="primary"]')?.getBoundingClientRect()
    const overlay = document.querySelector('.i23-canvas-risks')?.getBoundingClientRect()
    return plane && overlay ? {
      plane: { left: plane.left, top: plane.top, width: plane.width, height: plane.height },
      overlay: { left: overlay.left, top: overlay.top, width: overlay.width, height: overlay.height },
    } : null
  })()`)
  assert.ok(overlay2x)
  for (const edge of ['left', 'top', 'width', 'height']) {
    assert.ok(Math.abs(overlay2x.plane[edge] - overlay2x.overlay[edge]) <= 0.25, JSON.stringify(overlay2x))
  }
  await cdp.evaluate("document.querySelector('button[aria-label=\"Fit artwork to canvas\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("document.querySelector('[aria-label=\"Zoom level\"]')?.textContent === '100%'"),
    'fit canvas before 8x registration',
  )
  await cdp.evaluate(`(() => {
    const viewport = document.querySelector('.i23-canvas-viewport')
    viewport?.dispatchEvent(new WheelEvent('wheel', {
      bubbles: true,
      cancelable: true,
      clientX: ${zoomAnchor.x},
      clientY: ${zoomAnchor.y},
      deltaY: ${-Math.log(8) / 0.0015},
    }))
    return true
  })()`)
  await waitFor(
    () => cdp.evaluate("document.querySelector('[aria-label=\"Zoom level\"]')?.textContent === '800%'"),
    '800% canvas zoom',
  )
  const overlay8x = await cdp.evaluate(`(() => {
    const plane = document.querySelector('.i23-canvas-plane[data-plane="primary"]')?.getBoundingClientRect()
    const overlay = document.querySelector('.i23-canvas-risks')?.getBoundingClientRect()
    return plane && overlay ? {
      plane: { left: plane.left, top: plane.top, width: plane.width, height: plane.height },
      overlay: { left: overlay.left, top: overlay.top, width: overlay.width, height: overlay.height },
    } : null
  })()`)
  assert.ok(overlay8x)
  for (const edge of ['left', 'top', 'width', 'height']) {
    assert.ok(Math.abs(overlay8x.plane[edge] - overlay8x.overlay[edge]) <= 0.25, JSON.stringify(overlay8x))
  }
  await cdp.evaluate("document.querySelector('button[aria-label=\"Fit artwork to canvas\"]')?.click()")
  const maskPerformance = await cdp.evaluate(`(async () => {
    const button = [...document.querySelectorAll('.i23-canvas-risk-modes button')].find((item) => item.textContent.trim() === 'Overview')
    if (!button) return null
    const started = performance.now()
    button.click()
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    return {
      elapsedMs: performance.now() - started,
      overlayNodes: document.querySelectorAll('.i23-canvas-risks').length,
      warningRows: document.querySelectorAll('.region-inspector-warning-list > li').length,
      warningTotal: Number(document.querySelector('.region-inspector-warning-section .region-inspector-section-heading span')?.textContent ?? 0),
    }
  })()`)
  assert.ok(maskPerformance)
  assert.equal(maskPerformance.overlayNodes, 1)
  assert.ok(maskPerformance.warningRows <= 60, JSON.stringify(maskPerformance))
  assert.ok(maskPerformance.elapsedMs < 250, JSON.stringify(maskPerformance))
  const canvasCapture = await cdp.send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: false,
  })
  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  await fs.writeFile(screenshot, Buffer.from(canvasCapture.data, 'base64'))
  if (process.env.IMAGE23MF_QA_CANVAS_ONLY === '1') {
    console.log(JSON.stringify({ ok: true, canvasReadiness, overlayFit, overlay2x, overlay8x, maskPerformance, screenshot }, null, 2))
    socket.close()
    activeSocket = null
    return
  }
  await clickButton(cdp, 'Processed')
  await waitFor(
    () => cdp.evaluate("document.querySelector('img[alt=\"Processed canvas view\"]') !== null"),
    'the processed canvas view after overlay acceptance',
  )

  const initialProject = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}`,
  ).then((response) => response.json())
  const initialResult = await fetch(
    `http://127.0.0.1:8323/api/jobs/${initialProject.latest_preview_job.id}/result`,
  ).then((response) => response.json())
  await clickExactCanvasPixel(cdp, await firstExactAssignmentPick(initialResult))

  const comparison = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find((item) => item.textContent.trim() === 'Split' && !item.disabled)
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(comparison, true)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.i23-canvas-split-line'))"),
    'split comparison mode',
  )

  const widthInputValue = await fillInput(cdp, '[aria-label="Width crop percent"]', '80')
  assert.equal(widthInputValue, '80')
  await waitFor(
    () => cdp.evaluate("Number(document.querySelector('[aria-label=\"Width crop percent\"]').value) === 80"),
    'the committed crop width render',
  )
  const cropInputValue = await fillInput(cdp, '[aria-label="Left crop percent"]', '7.5')
  assert.equal(cropInputValue, '7.5')
  await waitFor(
    () => cdp.evaluate("Number(document.querySelector('[aria-label=\"Left crop percent\"]').value) === 7.5"),
    'the committed crop render',
  )
  await fillInput(cdp, '[aria-label="Canvas width millimetres"]', '224')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Saving draft…')"),
    'crop and canvas autosave to enter its pending state',
  )
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Draft saved locally')"),
    'debounced crop and canvas autosave',
  )

  const savedDraft = await fetch(`http://127.0.0.1:8323/api/projects/${initial.projectId}`).then(
    (response) => response.json(),
  )
  assert.equal(savedDraft.draft.config.crop.x, 0.075)
  assert.equal(savedDraft.draft.config.crop.width, 0.8)
  assert.equal(savedDraft.draft.config.canvas.width_mm, 224)

  await cdp.evaluate(`(() => {
    const select = document.querySelector('[aria-label="Automatic island policy"]')
    select.value = 'dominant_neighbor'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })()`)
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Saving draft…')"),
    'automatic island policy save to enter its pending state',
  )
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Draft saved locally')"),
    'automatic island policy save',
  )
  const policyDraft = await fetch(`http://127.0.0.1:8323/api/projects/${initial.projectId}`).then(
    (response) => response.json(),
  )
  assert.equal(policyDraft.draft.config.cleanup.merge_policy, 'dominant_neighbor')

  const jobBeforeReplacement = policyDraft.latest_preview_job.id
  const renderClicked = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find((item) => ['Update preview', 'Render preview'].includes(item.textContent.trim()) && !item.disabled)
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(renderClicked, true)
  await waitFor(
    async () => {
      const project = await fetch(`http://127.0.0.1:8323/api/projects/${initial.projectId}`).then(
        (response) => response.json(),
      )
      return project.latest_preview_job?.id !== jobBeforeReplacement && project.latest_preview_job?.state === 'succeeded'
    },
    'the cleanup preview replacement',
    90_000,
  )

  const projectAfterPreview = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}`,
  ).then((response) => response.json())
  const result = await fetch(
    `http://127.0.0.1:8323/api/jobs/${projectAfterPreview.latest_preview_job.id}/result`,
  ).then((response) => response.json())
  const artifactKinds = new Set(result.artifacts.map((artifact) => artifact.kind))
  assert.ok(artifactKinds.has('automatic-cleanup'))
  assert.ok(artifactKinds.has('automatic-cleanup-changed-mask'))
  assert.ok(artifactKinds.has('automatic-cleanup-mask-preview-image'))
  const cleanupArtifact = result.artifacts.find((artifact) => artifact.kind === 'automatic-cleanup')
  assert.ok(cleanupArtifact.metadata.changed_pixel_count > 0)

  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the replacement preview to become current in the editor',
  )
  await clickExactCanvasPixel(cdp, await firstExactAssignmentPick(result))
  const protectSelected = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find((item) => item.textContent.includes('Keep / protect') && !item.disabled)
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(protectSelected, true)
  const reviewSelected = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find((item) => item.textContent.trim() === 'Review exact change' && !item.disabled)
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(reviewSelected, true)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[role=alertdialog][aria-label=\"Confirm protect operation\"]'))"),
    'manual-operation confirmation',
  )
  const protectConfirmed = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find((item) => item.textContent.trim() === 'Confirm protect' && !item.disabled)
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(protectConfirmed, true)
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Update preview before another operation.')"),
    'stale-graph manual-authoring lock',
  )
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Draft saved locally')"),
    'manual command autosave',
  )
  const manualDraft = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}`,
  ).then((response) => response.json())
  assert.equal(manualDraft.draft.operations.at(-1).operation_type, 'editor_command_v1')
  assert.equal(manualDraft.draft.operations.at(-1).parameters.command.operation, 'protect')

  const jobBeforeManual = manualDraft.latest_preview_job.id
  const manualRenderClicked = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find((item) => ['Update preview', 'Render preview'].includes(item.textContent.trim()) && !item.disabled)
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(manualRenderClicked, true)
  const manualProject = await waitFor(
    async () => {
      const project = await fetch(`http://127.0.0.1:8323/api/projects/${initial.projectId}`).then(
        (response) => response.json(),
      )
      return project.latest_preview_job?.id !== jobBeforeManual && project.latest_preview_job?.state === 'succeeded'
        ? project
        : null
    },
    'the exact manual-operation replay preview',
    90_000,
  )
  const manualResult = await fetch(
    `http://127.0.0.1:8323/api/jobs/${manualProject.latest_preview_job.id}/result`,
  ).then((response) => response.json())
  const replayArtifact = manualResult.artifacts.find((artifact) => artifact.kind === 'editor-replay')
  const protectedArtifact = manualResult.artifacts.find(
    (artifact) => artifact.kind === 'editor-protected-mask',
  )
  assert.equal(replayArtifact.metadata.command_count, 1)
  assert.ok(protectedArtifact.metadata.protected_pixel_count > 0)

  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the exact manual preview to become publishable',
  )
  const revisionsOpened = await cdp.evaluate(`(() => {
    const button = document.querySelector('button[aria-label="Revisions"]')
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(revisionsOpened, true)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Published revisions\"]')) && document.body.style.overflow === 'hidden'"),
    'the revision drawer and page scroll lock',
  )
  await fillInput(cdp, '[placeholder="e.g. Cleanup approved"]', 'Live exact editor proof')
  const revisionPublished = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find((item) => item.textContent.trim() === 'Publish revision' && !item.disabled)
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(revisionPublished, true)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Revision details for Live exact editor proof\"]'))"),
    'the immutable revision detail',
  )
  const revisionCollection = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}/revisions`,
  ).then((response) => response.json())
  assert.equal(revisionCollection.total, 1)
  assert.equal(revisionCollection.items[0].label, 'Live exact editor proof')
  assert.equal(revisionCollection.items[0].preview_evidence.status, 'fresh')
  assert.ok(revisionCollection.items[0].artifact_count > 0)
  await cdp.evaluate("document.querySelector('button[aria-label=\"Close revisions\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("!document.querySelector('#revision-drawer') && document.body.style.overflow === ''"),
    'the revision drawer to restore page scrolling',
  )

  await clickExactCanvasPixel(cdp, await firstExactAssignmentPick(manualResult))
  const protectAgain = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find((item) => item.textContent.includes('Keep / protect') && !item.disabled)
    button?.click()
    return Boolean(button)
  })()`)
  assert.equal(protectAgain, true)
  await clickButton(cdp, 'Review exact change')
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[role=alertdialog][aria-label=\"Confirm protect operation\"]'))"),
    'the second exact manual-operation confirmation',
  )
  await clickButton(cdp, 'Confirm protect')
  await waitFor(
    async () => {
      const response = await fetch(`http://127.0.0.1:8323/api/projects/${initial.projectId}`)
      if (!response.ok) return false
      const project = await response.json()
      return project.draft?.operations?.length === 2
    },
    'the second exact manual-operation draft save',
  )
  await cdp.evaluate("document.querySelector('button[aria-label=\"Revisions\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('#revision-drawer'))"),
    'the revision drawer for the second publication',
  )
  await fillInput(cdp, '[placeholder="e.g. Cleanup approved"]', 'Wide canvas proof')
  await clickButton(cdp, 'Publish revision')
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[role=alertdialog][aria-label=\"Publish without current preview artifacts\"]'))"),
    'the stale-preview publication confirmation',
  )
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 460,
    height: 360,
    deviceScaleFactor: 1,
    mobile: false,
  })
  const compactConfirmation = await cdp.evaluate(`(() => {
    const dialog = document.querySelector('[role=alertdialog][aria-label="Publish without current preview artifacts"]')
    const drawer = document.querySelector('#revision-drawer')
    const bounds = dialog.getBoundingClientRect()
    const actions = [...dialog.querySelectorAll('button')].map((button) => button.getBoundingClientRect())
    return {
      focusedAction: document.activeElement?.textContent?.trim(),
      drawerInert: drawer.hasAttribute('inert'),
      drawerHidden: drawer.getAttribute('aria-hidden'),
      bounds: { top: bounds.top, left: bounds.left, right: bounds.right, bottom: bounds.bottom },
      viewport: { width: innerWidth, height: innerHeight },
      overflowY: getComputedStyle(dialog).overflowY,
      actionsStacked: actions.length === 2 && actions[1].top > actions[0].top,
    }
  })()`)
  assert.equal(compactConfirmation.focusedAction, 'Go back')
  assert.equal(compactConfirmation.drawerInert, true)
  assert.equal(compactConfirmation.drawerHidden, 'true')
  assert.ok(compactConfirmation.bounds.top >= 0 && compactConfirmation.bounds.left >= 0)
  assert.ok(compactConfirmation.bounds.right <= compactConfirmation.viewport.width)
  assert.ok(compactConfirmation.bounds.bottom <= compactConfirmation.viewport.height)
  assert.equal(compactConfirmation.overflowY, 'auto')
  assert.equal(compactConfirmation.actionsStacked, true)
  await pressKey(cdp, 'Tab', 'Tab', 8)
  assert.equal(
    await cdp.evaluate("document.activeElement?.textContent?.trim()"),
    'Publish without artifacts',
  )
  await pressKey(cdp, 'Tab', 'Tab')
  assert.equal(await cdp.evaluate("document.activeElement?.textContent?.trim()"), 'Go back')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await clickButton(cdp, 'Publish without artifacts')
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Revision details for Wide canvas proof\"]'))"),
    'the second immutable revision detail',
  )
  await clickRevisionButton(cdp, 'Live exact editor proof')
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Revision details for Live exact editor proof\"]'))"),
    'the older immutable revision detail',
  )
  await clickButton(cdp, 'Branch from this revision')
  const branchConfirmation = await waitFor(
    () => cdp.evaluate(`(() => {
      const dialog = document.querySelector('[role=alertdialog][aria-label="Confirm revision branch"]')
      if (!dialog) return null
      return {
        focusedAction: document.activeElement?.textContent?.trim(),
        drawerInert: document.querySelector('#revision-drawer')?.hasAttribute('inert'),
      }
    })()`),
    'the older-revision branch confirmation',
  )
  assert.deepEqual(branchConfirmation, { focusedAction: 'Keep current draft', drawerInert: true })
  await pressKey(cdp, 'Tab', 'Tab', 8)
  assert.equal(await cdp.evaluate("document.activeElement?.textContent?.trim()"), 'Create branch draft')
  await pressKey(cdp, 'Tab', 'Tab')
  assert.equal(await cdp.evaluate("document.activeElement?.textContent?.trim()"), 'Keep current draft')
  await clickButton(cdp, 'Create branch draft')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Draft branched here') && document.body.innerText.includes('Current published')"),
    'the older revision as draft base while the newer publication stays active',
  )
  const branchedProject = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}`,
  ).then((response) => response.json())
  const branchedRevisions = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}/revisions`,
  ).then((response) => response.json())
  assert.equal(branchedProject.draft.config.canvas.width_mm, 224)
  assert.equal(branchedProject.draft.base_revision_id, revisionCollection.items[0].id)
  assert.equal(branchedProject.project.active_revision_id, branchedRevisions.items[0].id)
  assert.equal(branchedRevisions.total, 2)
  assert.equal(branchedRevisions.items[0].label, 'Wide canvas proof')
  await cdp.evaluate("document.querySelector('button[aria-label=\"Close revisions\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("!document.querySelector('#revision-drawer') && document.body.style.overflow === ''"),
    'the branched revision drawer to restore page scrolling',
  )

  const paletteHistoryMarker = {
    operation_type: 'palette-edit',
    selection: {},
    parameters: {
      action: 'live-qa-history-marker',
      before_palette: branchedProject.draft.config.palette.colors,
      after_palette: branchedProject.draft.config.palette.colors,
    },
    source: 'manual',
    provenance: { ui: 'live-editor-qa' },
  }
  const markerResponse = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}/draft`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        config: branchedProject.draft.config,
        operations: [...branchedProject.draft.operations, paletteHistoryMarker],
        expected_draft_generation: branchedProject.draft.generation,
      }),
    },
  )
  assert.equal(markerResponse.status, 200)
  const markedDraft = await markerResponse.json()
  assert.equal(markedDraft.operations.filter((item) => item.operation_type !== 'palette-edit').length, 1)
  assert.equal(markedDraft.operations.filter((item) => item.operation_type === 'palette-edit').length, 1)

  await reloadPage(cdp)
  await waitFor(
    () => cdp.evaluate(`Boolean(
      document.querySelector('[aria-label="Canvas width millimetres"]')
      && document.body.innerText.includes('Saved preview restored.')
    )`),
    'the palette-marked branched draft to reload before selector safety proof',
  )
  const safetyBaselineResponse = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}`,
  )
  assert.equal(safetyBaselineResponse.status, 200)
  const safetyBaseline = await safetyBaselineResponse.json()
  assert.equal(safetyBaseline.draft.config.canvas.width_mm, 224)
  assert.equal(safetyBaseline.draft.operations.filter((item) => item.operation_type !== 'palette-edit').length, 1)
  assert.equal(safetyBaseline.draft.operations.filter((item) => item.operation_type === 'palette-edit').length, 1)
  const draftRequestCount = () => cdp.events.filter(
    (event) =>
      event.method === 'Network.requestWillBeSent'
      && event.params?.request?.method === 'PUT'
      && event.params.request.url.endsWith(`/api/projects/${initial.projectId}/draft`),
  ).length
  const draftRequestsBeforeGuard = draftRequestCount()

  await fillInput(cdp, '[aria-label="Canvas width millimetres"]', '232', { tab: false })
  const firstSafetyDialog = await waitFor(
    () => cdp.evaluate(`(() => {
      const dialog = document.querySelector('.config-change-guard[role="alertdialog"]')
      if (!dialog) return null
      return {
        text: dialog.innerText,
        ariaModal: dialog.getAttribute('aria-modal'),
        focusedAction: document.activeElement?.textContent?.trim(),
        appInert: document.querySelector('.app-shell')?.hasAttribute('inert'),
        canvasWidth: Number(document.querySelector('[aria-label="Canvas width millimetres"]')?.value),
        persistence: document.querySelector('.autosave-state')?.textContent?.trim(),
      }
    })()`),
    'the exact-selector config safety confirmation',
  )
  assert.equal(firstSafetyDialog.ariaModal, 'true')
  assert.equal(firstSafetyDialog.focusedAction, 'Keep current settings')
  assert.equal(firstSafetyDialog.appInert, true)
  assert.equal(firstSafetyDialog.canvasWidth, 224)
  assert.equal(firstSafetyDialog.persistence, 'Draft saved locally')
  assert.ok(firstSafetyDialog.text.includes('Change settings and clear manual edits?'))
  assert.ok(firstSafetyDialog.text.includes('1 manual edit will be cleared'))
  assert.ok(firstSafetyDialog.text.includes('1 compatible palette step stays in history'))
  assert.ok(firstSafetyDialog.text.includes('same draft update'))
  assert.ok(firstSafetyDialog.text.includes('never rebound'))
  assert.equal(draftRequestCount(), draftRequestsBeforeGuard)
  const cancelServerResponse = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}`,
  )
  assert.equal(cancelServerResponse.status, 200)
  const cancelServerState = await cancelServerResponse.json()
  assert.equal(cancelServerState.draft.generation, safetyBaseline.draft.generation)
  assert.deepEqual(cancelServerState.draft.config, safetyBaseline.draft.config)
  assert.deepEqual(cancelServerState.draft.operations, safetyBaseline.draft.operations)
  await pressKey(cdp, 'Tab', 'Tab', 8)
  assert.equal(
    await cdp.evaluate("document.activeElement?.textContent?.trim()"),
    'Clear 1 manual edit and apply',
  )
  await pressKey(cdp, 'Tab', 'Tab')
  assert.equal(await cdp.evaluate("document.activeElement?.textContent?.trim()"), 'Keep current settings')
  await clickButton(cdp, 'Keep current settings')
  await waitFor(
    () => cdp.evaluate(`Boolean(
      !document.querySelector('.config-change-guard')
      && !document.querySelector('.app-shell')?.hasAttribute('inert')
      && Number(document.querySelector('[aria-label="Canvas width millimetres"]')?.value) === 224
    )`),
    'selector safety cancellation without mutation',
  )
  assert.equal(draftRequestCount(), draftRequestsBeforeGuard)
  assert.equal(
    await cdp.evaluate("document.activeElement?.getAttribute('aria-label')"),
    'Canvas width millimetres',
  )

  await fillInput(cdp, '[aria-label="Canvas width millimetres"]', '236', { tab: false })
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.config-change-guard[role=alertdialog]'))"),
    'the final selector safety confirmation',
  )
  await clickButton(cdp, 'Clear 1 manual edit and apply')
  const safetySaved = await waitFor(
    async () => {
      const response = await fetch(`http://127.0.0.1:8323/api/projects/${initial.projectId}`)
      if (!response.ok) return false
      const project = await response.json()
      return project.draft?.generation === safetyBaseline.draft.generation + 1
        && project.draft?.config?.canvas?.width_mm === 236
        ? project
        : false
    },
    'the atomic selector-safe draft save',
  )
  const guardDraftRequests = cdp.events.filter(
    (event) =>
      event.method === 'Network.requestWillBeSent'
      && event.params?.request?.method === 'PUT'
      && event.params.request.url.endsWith(`/api/projects/${initial.projectId}/draft`),
  ).slice(draftRequestsBeforeGuard)
  assert.equal(guardDraftRequests.length, 1)
  const guardSavePayload = JSON.parse(guardDraftRequests[0].params.request.postData)
  assert.equal(guardSavePayload.expected_draft_generation, safetyBaseline.draft.generation)
  assert.equal(guardSavePayload.config.canvas.width_mm, 236)
  assert.equal(guardSavePayload.operations.filter((item) => item.operation_type !== 'palette-edit').length, 0)
  assert.equal(guardSavePayload.operations.filter((item) => item.operation_type === 'palette-edit').length, 1)
  assert.equal(safetySaved.draft.operations.filter((item) => item.operation_type !== 'palette-edit').length, 0)
  assert.equal(safetySaved.draft.operations.filter((item) => item.operation_type === 'palette-edit').length, 1)

  await reloadPage(cdp)
  await waitFor(
    () => cdp.evaluate(`Boolean(
      Number(document.querySelector('[aria-label="Canvas width millimetres"]')?.value) === 236
      && document.body.innerText.includes('A stale saved preview was restored.')
      && !document.querySelector('.config-change-guard')
    )`),
    'the selector-safe draft after reload',
  )
  const safetyReload = await cdp.evaluate(`(() => ({
    canvasWidth: Number(document.querySelector('[aria-label="Canvas width millimetres"]')?.value),
    appInert: document.querySelector('.app-shell')?.hasAttribute('inert'),
    alerts: [...document.querySelectorAll('[role="alert"]')].map((item) => item.innerText),
  }))()`)
  assert.deepEqual(safetyReload, { canvasWidth: 236, appInert: false, alerts: [] })
  const safetyReloadResponse = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}`,
  )
  assert.equal(safetyReloadResponse.status, 200)
  const safetyReloadProject = await safetyReloadResponse.json()
  assert.equal(safetyReloadProject.draft.config.canvas.width_mm, 236)
  assert.deepEqual(safetyReloadProject.draft.operations, safetySaved.draft.operations)

  const compoundUndoLabel = await cdp.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find(
      (item) => item.getAttribute('aria-label')?.startsWith('Undo:') && !item.disabled,
    )
    return button?.getAttribute('aria-label') ?? null
  })()`)
  assert.ok(compoundUndoLabel?.includes('clear 1 manual edit'), compoundUndoLabel)
  await clickHistoryButton(cdp, 'undo')
  const compoundUndone = await waitFor(
    async () => {
      const response = await fetch(`http://127.0.0.1:8323/api/projects/${initial.projectId}`)
      if (!response.ok) return false
      const project = await response.json()
      return project.draft?.config?.canvas?.width_mm === 224
        && project.draft?.operations?.filter((item) => item.operation_type !== 'palette-edit').length === 1
        && project.draft?.history?.can_redo
        ? project
        : false
    },
    'compound history undo to restore config and exact manual operation',
  )
  assert.equal(compoundUndone.draft.operations.filter((item) => item.operation_type === 'palette-edit').length, 1)
  assert.ok(
    await cdp.evaluate("document.body.innerText.includes('Undid Resize canvas + clear 1 manual edit.')"),
    'the completed compound undo must be announced',
  )

  await reloadPage(cdp)
  await waitFor(
    () => cdp.evaluate(`Boolean(
      Number(document.querySelector('[aria-label="Canvas width millimetres"]')?.value) === 224
      && document.body.innerText.includes('Draft saved locally')
      && [...document.querySelectorAll('button')].some(
        (item) => item.getAttribute('aria-label')?.startsWith('Redo:') && !item.disabled,
      )
    )`),
    'the undone history cursor and redo action after browser reload',
  )
  const undoReloadProject = await fetch(
    `http://127.0.0.1:8323/api/projects/${initial.projectId}`,
  ).then((response) => response.json())
  assert.equal(undoReloadProject.draft.history.can_redo, true)
  assert.equal(undoReloadProject.draft.config.canvas.width_mm, 224)
  assert.equal(
    undoReloadProject.draft.operations.filter((item) => item.operation_type !== 'palette-edit').length,
    1,
  )
  await clickHistoryButton(cdp, 'redo')
  const compoundRedone = await waitFor(
    async () => {
      const response = await fetch(`http://127.0.0.1:8323/api/projects/${initial.projectId}`)
      if (!response.ok) return false
      const project = await response.json()
      return project.draft?.config?.canvas?.width_mm === 236
        && project.draft?.operations?.filter((item) => item.operation_type !== 'palette-edit').length === 0
        && !project.draft?.history?.can_redo
        ? project
        : false
    },
    'compound history redo to reapply config and clear exact manual operation',
  )
  assert.equal(compoundRedone.draft.operations.filter((item) => item.operation_type === 'palette-edit').length, 1)
  assert.ok(
    await cdp.evaluate("document.body.innerText.includes('Redid Resize canvas + clear 1 manual edit.')"),
    'the completed compound redo must be announced',
  )

  await waitFor(
    () => cdp.evaluate(`Boolean(
      document.querySelector('[aria-label="Canvas width millimetres"]') &&
      document.querySelector('[aria-label="Automatic island policy"]') &&
      document.querySelector('.preview-freshness.is-dirty')
    )`),
    'the restored editor',
  )
  const restored = await cdp.evaluate(`(() => ({
    crop: Number(document.querySelector('[aria-label="Left crop percent"]').value),
    canvas: Number(document.querySelector('[aria-label="Canvas width millimetres"]').value),
    policy: document.querySelector('[aria-label="Automatic island policy"]').value,
    current: document.body.innerText.includes('Preview complete and current.'),
  }))()`)
  assert.deepEqual(restored, { crop: 7.5, canvas: 236, policy: 'dominant_neighbor', current: false })
  assert.ok(
    await cdp.evaluate("Boolean(document.querySelector('.preview-freshness.is-dirty'))"),
    'the history-restored draft must keep the prior preview stale without submitting a replacement',
  )
  await cdp.evaluate("document.querySelector('button[aria-label=\"Revisions\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Live exact editor proof') && document.body.innerText.includes('Wide canvas proof') && document.body.innerText.includes('Draft branched here') && document.body.innerText.includes('Current published')"),
    'published revision history after restart',
  )
  await cdp.evaluate("document.querySelector('button[aria-label=\"Close revisions\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("!document.querySelector('#revision-drawer') && document.body.style.overflow === ''"),
    'the restored revision drawer to close',
  )

  const scrollBeforeReplacement = {
    left: await proveRailScroll(cdp, 'left'),
    right: await proveRailScroll(cdp, 'right'),
  }
  await clickButton(cdp, 'Replace image')
  await setFileInput(cdp, replacementFixture)
  const replacementDialog = await waitFor(
    () => cdp.evaluate(`(() => {
      const dialog = document.querySelector('[role="dialog"]')
      if (!dialog?.innerText.includes('Replace the current source?')) return null
      return {
        text: dialog.innerText,
        focusedAction: document.activeElement?.textContent?.trim(),
      }
    })()`),
    'the source-replacement confirmation',
  )
  assert.ok(replacementDialog.text.includes(path.basename(replacementFixture)))
  assert.equal(replacementDialog.focusedAction, 'Keep current image')
  await clickButton(cdp, 'Import replacement')
  const replacementProject = await waitFor(
    () => cdp.evaluate(`(() => {
      const projectId = localStorage.getItem('image23mf.recentProjectId')
      const filename = document.querySelector('.i23-editor-project p strong')?.textContent
      return projectId && projectId !== ${JSON.stringify(initial.projectId)}
        && filename === ${JSON.stringify(path.basename(replacementFixture))}
        ? { projectId, filename }
        : null
    })()`),
    'the replacement project workspace',
  )
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the replacement project preview',
    90_000,
  )
  await fillInput(cdp, '[aria-label="Canvas width millimetres"]', '210')
  await waitFor(
    async () => {
      const project = await fetch(
        `http://127.0.0.1:8323/api/projects/${replacementProject.projectId}`,
      ).then((response) => response.json())
      return project.draft?.config?.canvas?.width_mm === 210 && project.draft?.history?.can_undo
    },
    'replacement project history seed edit',
  )
  await clickHistoryButton(cdp, 'undo')
  const replacementUndone = await waitFor(
    async () => {
      const project = await fetch(
        `http://127.0.0.1:8323/api/projects/${replacementProject.projectId}`,
      ).then((response) => response.json())
      return project.draft?.history?.can_redo ? project : false
    },
    'replacement project undo before divergent edit',
  )
  const replacementOriginalWidth = replacementUndone.draft.config.canvas.width_mm
  await fillInput(cdp, '[aria-label="Name for color 1"]', 'Diverged after undo')
  const replacementDiverged = await waitFor(
    async () => {
      const project = await fetch(
        `http://127.0.0.1:8323/api/projects/${replacementProject.projectId}`,
      ).then((response) => response.json())
      return project.draft?.config?.palette?.colors?.[0]?.name === 'Diverged after undo'
        && !project.draft?.history?.can_redo
        ? project
        : false
    },
    'divergent palette edit to invalidate the active redo path',
  )
  assert.equal(replacementDiverged.draft.config.canvas.width_mm, replacementOriginalWidth)
  await reloadPage(cdp)
  await waitFor(
    () => cdp.evaluate(`Boolean(
      document.querySelector('[aria-label="Name for color 1"]')?.value === 'Diverged after undo'
      && ![...document.querySelectorAll('button')].some(
        (item) => item.getAttribute('aria-label')?.startsWith('Redo:') && !item.disabled,
      )
    )`),
    'divergent history tip and redo invalidation after reload',
  )
  await cdp.evaluate("document.querySelector('button[aria-label=\"Revisions\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('No published revisions yet. Your autosaved draft is still safe.')"),
    'replacement project revision isolation',
  )
  await cdp.evaluate("document.querySelector('button[aria-label=\"Close revisions\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("!document.querySelector('#revision-drawer') && document.body.style.overflow === ''"),
    'the replacement revision drawer to close',
  )
  const scrollAfterReplacement = {
    left: await proveRailScroll(cdp, 'left'),
    right: await proveRailScroll(cdp, 'right'),
  }

  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 800,
    height: 700,
    deviceScaleFactor: 1,
    mobile: false,
  })
  const compactBefore = await cdp.evaluate(`(() => ({
    innerHeight,
    scrollHeight: document.documentElement.scrollHeight,
    scrollY,
  }))()`)
  assert.ok(compactBefore.scrollHeight > compactBefore.innerHeight)
  await cdp.evaluate('window.scrollTo(0, 0); true')
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseWheel',
    x: 790,
    y: 350,
    deltaX: 0,
    deltaY: 650,
  })
  const compactScrollY = await waitFor(
    () => cdp.evaluate('window.scrollY'),
    'compact stacked workspace page scrolling',
  )
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  })
  await cdp.evaluate('window.scrollTo(0, 0); true')
  await cdp.evaluate(
    `localStorage.setItem('image23mf.recentProjectId', ${JSON.stringify(initial.projectId)}); true`,
  )
  await reloadPage(cdp)
  await waitFor(
    () => cdp.evaluate(`Boolean(
      document.querySelector('button[aria-label="Revisions"]')
      && document.querySelector('.i23-editor-project p strong')?.textContent
        === ${JSON.stringify(path.basename(fixture))}
    )`),
    'the original project after replacement proof',
  )
  await cdp.evaluate("document.querySelector('button[aria-label=\"Revisions\"]')?.click()")
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Live exact editor proof') && document.body.innerText.includes('Wide canvas proof') && document.body.innerText.includes('Draft branched here') && document.body.innerText.includes('Current published')"),
    'original project revision history after replacement',
  )

  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  const capture = await cdp.send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: true,
  })
  await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))

  const consoleErrors = await cdp.evaluate(`(() => ({
    alerts: [...document.querySelectorAll('[role="alert"]')].map((item) => item.innerText),
    body: document.body.innerText.slice(0, 500),
  }))()`)
  assert.deepEqual(consoleErrors.alerts, [])
  const browserErrors = cdp.events.filter(
    (event) => event.method === 'Runtime.exceptionThrown'
      || (event.method === 'Runtime.consoleAPICalled' && event.params?.type === 'error')
      || (event.method === 'Log.entryAdded' && event.params?.entry?.level === 'error'),
  )
  assert.deepEqual(browserErrors, [])
  console.log(JSON.stringify({
    ok: true,
    fixture,
    replacementFixture,
    screenshot,
    projectId: initial.projectId,
    replacementProject,
    replacementDialog,
    scrollBeforeReplacement,
    scrollAfterReplacement,
    compact: { ...compactBefore, afterScrollY: compactScrollY },
    compactConfirmation,
    branchConfirmation,
    selectorSafety: {
      canceledValue: 232,
      savedValue: safetySaved.draft.config.canvas.width_mm,
      atomicDraftRequests: guardDraftRequests.length,
      retainedPaletteOperations: safetySaved.draft.operations.filter(
        (item) => item.operation_type === 'palette-edit',
      ).length,
      clearedManualOperations: safetyBaseline.draft.operations.filter(
        (item) => item.operation_type !== 'palette-edit',
      ).length,
      reload: safetyReload,
    },
    historyFlow: {
      compoundUndoLabel,
      undoReloadCursor: undoReloadProject.draft.history.cursor_node_id,
      redoneCursor: compoundRedone.draft.history.cursor_node_id,
      divergentProjectId: replacementProject.projectId,
      divergentRedoAvailable: replacementDiverged.draft.history.can_redo,
    },
    revisionFlow: {
      total: branchedRevisions.total,
      activeRevisionId: branchedProject.project.active_revision_id,
      draftBaseRevisionId: branchedProject.draft.base_revision_id,
    },
  }, null, 2))
  socket.close()
  activeSocket = null
}

main().catch((error) => {
  console.error(error)
  activeSocket?.close()
  process.exitCode = 1
})
