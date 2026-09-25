#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5174'
const fixture = path.resolve(
  process.env.IMAGE23MF_QA_FIXTURE ?? 'tests/fixtures/synthetic/geometry-nozzle-040.png',
)
const screenshot = path.resolve(
  process.env.IMAGE23MF_QA_SCREENSHOT ?? 'workspace/qa/replacement-revision-scroll.png',
)

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

async function clickButton(cdp, name) {
  const point = await cdp.evaluate(`(() => {
    const expected = ${JSON.stringify(name)}
    const matches = [...document.querySelectorAll('button')].filter((button) => {
      const accessibleName = button.getAttribute('aria-label') || button.textContent.trim()
      return accessibleName === expected && !button.disabled
    })
    if (matches.length !== 1) return { count: matches.length }
    matches[0].scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = matches[0].getBoundingClientRect()
    return { count: 1, x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.equal(point.count, 1, `${name} must resolve to one enabled button`)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1,
  })
}

function assertAligned(rects, description) {
  const tolerance = 0.75
  for (const key of ['left', 'top', 'width', 'height']) {
    assert.ok(
      Math.abs(rects.raster[key] - rects.overlay[key]) <= tolerance,
      `${description}: raster and risk overlay ${key} differ (${rects.raster[key]} vs ${rects.overlay[key]})`,
    )
    assert.ok(
      Math.abs(rects.plane[key] - rects.overlay[key]) <= tolerance,
      `${description}: plane and risk overlay ${key} differ (${rects.plane[key]} vs ${rects.overlay[key]})`,
    )
  }
}

async function riskRects(cdp) {
  return cdp.evaluate(`(() => {
    const plane = document.querySelector('[data-plane="primary"][data-visible="true"]')
    const raster = plane?.querySelector('img')
    const overlay = plane?.querySelector('canvas.i23-canvas-risks')
    const zoom = document.querySelector('output[aria-label="Zoom level"]')?.textContent?.trim()
    if (!plane || !raster || !overlay || !zoom) return null
    const serialize = (element) => {
      const bounds = element.getBoundingClientRect()
      return { left: bounds.left, top: bounds.top, width: bounds.width, height: bounds.height }
    }
    return {
      plane: serialize(plane),
      raster: serialize(raster),
      overlay: serialize(overlay),
      zoom,
      overlayWidth: overlay.width,
      overlayHeight: overlay.height,
      rasterWidth: raster.naturalWidth,
      rasterHeight: raster.naturalHeight,
    }
  })()`)
}

async function verifyRiskAlignment(cdp) {
  await clickButton(cdp, 'Risk')
  await clickButton(cdp, 'Fit artwork to canvas')
  const fit = await waitFor(
    async () => {
      const current = await riskRects(cdp)
      return current?.zoom === '100%' && current.rasterWidth > 0 ? current : null
    },
    'the exact risk overlay at Fit',
  )
  assert.equal(fit.overlayWidth, fit.rasterWidth)
  assert.equal(fit.overlayHeight, fit.rasterHeight)
  assertAligned(fit, 'Fit')

  await clickButton(cdp, 'Zoom in')
  const zoomed = await waitFor(
    async () => {
      const current = await riskRects(cdp)
      return current?.zoom === '125%' ? current : null
    },
    'the exact risk overlay at 125% zoom',
  )
  assert.ok(zoomed.plane.width > fit.plane.width, 'zoom must enlarge the artwork plane')
  assertAligned(zoomed, '125% zoom')
  return { fit, zoomed }
}

async function main() {
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
  ])
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1200, height: 800, deviceScaleFactor: 1, mobile: false,
  })
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("document.readyState === 'complete' && Boolean(document.querySelector('input[type=file]'))"),
    'the import screen',
  )
  await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(() => cdp.evaluate("document.body.innerText.includes('Choose an image')"), 'the cleared import screen')

  await setFileInput(cdp, fixture)
  const originalProjectId = await waitFor(
    () => cdp.evaluate("document.querySelector('[aria-label=\"Exact artwork preview and comparison\"]') && localStorage.getItem('image23mf.recentProjectId')"),
    'the original project editor',
    90_000,
  )
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the original persisted preview',
    90_000,
  )

  await clickButton(cdp, 'Replace image')
  await setFileInput(cdp, fixture)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.confirm-dialog')) && document.body.style.overflow === 'hidden'"),
    'the cancellable replacement confirmation and body lock',
  )
  await clickButton(cdp, 'Keep current image')
  const cancelledState = await waitFor(
    () => cdp.evaluate(`(() => {
      if (document.querySelector('.confirm-dialog')) return null
      return {
        bodyOverflow: document.body.style.overflow,
        computedOverflowY: getComputedStyle(document.body).overflowY,
        appInert: document.querySelector('.app-shell')?.hasAttribute('inert') ?? false,
        projectId: localStorage.getItem('image23mf.recentProjectId'),
        overlay: Boolean(document.querySelector('vite-error-overlay')),
      }
    })()`),
    'replacement cancellation and scroll restoration',
  )
  assert.equal(cancelledState.bodyOverflow, '')
  assert.notEqual(cancelledState.computedOverflowY, 'hidden')
  assert.equal(cancelledState.appInert, false)
  assert.equal(cancelledState.projectId, originalProjectId)
  assert.equal(cancelledState.overlay, false)

  await clickButton(cdp, 'Replace image')
  await setFileInput(cdp, fixture)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.confirm-dialog')) && document.body.style.overflow === 'hidden'"),
    'the replacement confirmation and body lock',
  )
  await clickButton(cdp, 'Import replacement')

  const replacementProjectId = await waitFor(
    () => cdp.evaluate(`(() => {
      const id = localStorage.getItem('image23mf.recentProjectId')
      return id && id !== ${JSON.stringify(originalProjectId)}
        && document.querySelector('[aria-label="Exact artwork preview and comparison"]') ? id : null
    })()`),
    'the replacement project editor',
    90_000,
  )
  assert.notEqual(replacementProjectId, originalProjectId)
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the replacement persisted preview',
    90_000,
  )
  const replacementState = await cdp.evaluate(`({
    overlay: Boolean(document.querySelector('vite-error-overlay')),
    bodyOverflow: document.body.style.overflow,
    appInert: document.querySelector('.app-shell')?.hasAttribute('inert') ?? false,
  })`)
  assert.equal(replacementState.overlay, false, 'the replacement must not trigger a Vite error overlay')
  assert.equal(replacementState.bodyOverflow, '', 'replacement completion must restore body scrolling')
  assert.equal(replacementState.appInert, false, 'replacement completion must restore app interaction')

  const alignment = await verifyRiskAlignment(cdp)

  await clickButton(cdp, 'Revisions')
  const revisionOpen = await waitFor(
    () => cdp.evaluate(`(() => {
      const close = [...document.querySelectorAll('button')].find(
        (button) => (button.getAttribute('aria-label') || button.textContent.trim()) === 'Close revisions',
      )
      return close ? {
        bodyOverflow: document.body.style.overflow,
        drawerVisible: Boolean(close.closest('.revision-drawer')),
      } : null
    })()`),
    'the replacement revision drawer',
  )
  assert.equal(revisionOpen.bodyOverflow, 'hidden')
  assert.equal(revisionOpen.drawerVisible, true)
  await clickButton(cdp, 'Close revisions')
  const revisionClosed = await waitFor(
    () => cdp.evaluate(`(() => {
      const closed = ![...document.querySelectorAll('button')].some(
        (button) => (button.getAttribute('aria-label') || button.textContent.trim()) === 'Close revisions',
      )
      return closed ? {
        bodyOverflow: document.body.style.overflow,
        computedOverflowY: getComputedStyle(document.body).overflowY,
        appInert: document.querySelector('.app-shell')?.hasAttribute('inert') ?? false,
        overlay: Boolean(document.querySelector('vite-error-overlay')),
      } : null
    })()`),
    'the revision drawer closing and scroll restoration',
  )
  assert.equal(revisionClosed.bodyOverflow, '')
  assert.notEqual(revisionClosed.computedOverflowY, 'hidden')
  assert.equal(revisionClosed.appInert, false)
  assert.equal(revisionClosed.overlay, false)

  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  const capture = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true })
  await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))

  console.log(JSON.stringify({
    originalProjectId,
    replacementProjectId,
    replacementCancelledSafely: true,
    replacementConfirmed: true,
    revisionsScrollRestored: true,
    riskOverlayFit: alignment.fit.zoom,
    riskOverlayZoomed: alignment.zoomed.zoom,
    screenshot,
  }, null, 2))
  const closed = new Promise((resolve) => socket.addEventListener('close', resolve, { once: true }))
  socket.close()
  await Promise.race([closed, new Promise((resolve) => setTimeout(resolve, 1_000))])
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
