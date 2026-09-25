#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5174'
const screenshot = path.resolve(
  process.env.IMAGE23MF_QA_SCREENSHOT ?? 'workspace/qa/first-run-validated.png',
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

async function clickButton(cdp, name) {
  const point = await cdp.evaluate(`(() => {
    const expected = ${JSON.stringify(name)}
    const matches = [...document.querySelectorAll('button')].filter((button) => {
      const accessibleName = button.getAttribute('aria-label') || button.textContent.trim()
      return accessibleName === expected && !button.disabled && button.getAttribute('aria-disabled') !== 'true'
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

async function main() {
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
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true })
  await waitFor(
    () => cdp.evaluate("document.readyState === 'complete'"),
    'the initial origin document',
  )
  await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
  await cdp.send('Network.setBlockedURLs', { urls: ['*/api/health'] })
  await cdp.send('Page.navigate', { url: appUrl })
  const offline = await waitFor(
    () => cdp.evaluate(`(() => {
      const sample = [...document.querySelectorAll('button')].find(
        (button) => button.textContent.trim() === 'Try guided sample',
      )
      return document.body.innerText.includes('Local engine unavailable') && sample ? {
        sampleDisabled: sample.disabled,
        status: document.querySelector('.connection')?.textContent?.trim(),
      } : null
    })()`),
    'the honest offline first-run state',
  )
  assert.equal(offline.sampleDisabled, true)
  await cdp.send('Network.setBlockedURLs', { urls: [] })
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("document.readyState === 'complete' && Boolean(document.querySelector('button'))"),
    'the app shell',
  )
  await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
  await cdp.send('Page.navigate', { url: appUrl })
  const importState = await waitFor(
    () => cdp.evaluate(`(() => {
      const sample = [...document.querySelectorAll('button')].find(
        (button) => button.textContent.trim() === 'Try guided sample' && !button.disabled,
      )
      const workflow = [...document.querySelectorAll('button')].find(
        (button) => button.textContent.trim() === 'Read the workflow',
      )
      return sample && workflow ? {
        sample: sample.textContent.trim(),
        workflow: workflow.textContent.trim(),
        overlay: Boolean(document.querySelector('vite-error-overlay')),
      } : null
    })()`),
    'the ready first-run entry points',
  )
  assert.equal(importState.overlay, false)

  await clickButton(cdp, 'Read the workflow')
  const sourceGuide = await waitFor(
    () => cdp.evaluate(`(() => {
      const guide = document.querySelector('.first-run-guide[data-stage="source"]')
      return guide ? {
        current: guide.querySelector('[aria-current="step"]')?.textContent,
        text: guide.textContent,
        links: guide.querySelectorAll('a').length,
      } : null
    })()`),
    'the source-stage in-app guide',
  )
  assert.ok(sourceGuide.current.includes('Source'))
  assert.equal(sourceGuide.links, 0, 'the first-run workflow must not require external documentation')
  for (const required of [
    'Palette and physical cleanup',
    'Before the physical print',
    'Saving, revisions, and backups',
    'Current limits',
  ]) assert.ok(sourceGuide.text.includes(required), `guide must include ${required}`)
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 720, height: 360, deviceScaleFactor: 1, mobile: false,
  })
  const compactGuide = await cdp.evaluate(`(() => {
    const guide = document.querySelector('.first-run-guide')
    const close = [...(guide?.querySelectorAll('button') ?? [])].find(
      (button) => button.getAttribute('aria-label') === 'Close workflow guide',
    )
    if (!guide || !close) return null
    const bounds = guide.getBoundingClientRect()
    const closeBounds = close.getBoundingClientRect()
    return {
      innerHeight,
      top: bounds.top,
      bottom: bounds.bottom,
      clientHeight: guide.clientHeight,
      scrollHeight: guide.scrollHeight,
      overflowY: getComputedStyle(guide).overflowY,
      closeVisible: closeBounds.top >= bounds.top && closeBounds.bottom <= bounds.bottom,
    }
  })()`)
  assert.equal(compactGuide.overflowY, 'auto')
  assert.ok(
    compactGuide.top >= 0 && compactGuide.bottom <= compactGuide.innerHeight,
    JSON.stringify(compactGuide),
  )
  assert.ok(compactGuide.scrollHeight > compactGuide.clientHeight, JSON.stringify(compactGuide))
  assert.equal(compactGuide.closeVisible, true)
  await clickButton(cdp, 'Close workflow guide')
  const closedGuide = await waitFor(
    () => cdp.evaluate(`(() => {
      const trigger = [...document.querySelectorAll('button')].find(
        (button) => button.textContent.trim() === 'Workflow guide',
      )
      return trigger && !document.querySelector('.first-run-guide') ? {
        trigger: trigger.textContent.trim(),
        tag: trigger.tagName,
      } : null
    })()`),
    'the accessible collapsed guide trigger',
  )
  assert.equal(closedGuide.tag, 'BUTTON')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  await clickButton(cdp, 'Workflow guide')
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.first-run-guide[data-stage=\"source\"]'))"),
    'the reopened source guide',
  )
  await clickButton(cdp, 'Close workflow guide')

  await clickButton(cdp, 'Try guided sample')
  const projectId = await waitFor(
    () => cdp.evaluate(`(() => {
      const id = localStorage.getItem('image23mf.recentProjectId')
      const sample = document.querySelector('.i23-editor-project')?.textContent
      return id && sample?.includes('image23mf-guided-sample.png') ? id : null
    })()`),
    'the bundled sample project',
    90_000,
  )
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
    'the sample processed proof',
    90_000,
  )
  const processedGuide = await waitFor(
    () => cdp.evaluate(`(() => {
      const guide = document.querySelector('.first-run-guide[data-stage="processed"]')
      const action = [...(guide?.querySelectorAll('button') ?? [])].find(
        (button) => button.textContent.trim() === 'Build and validate 3MF' && !button.disabled,
      )
      return guide && action ? {
        current: guide.querySelector('[aria-current="step"]')?.textContent,
        action: action.textContent.trim(),
        sample: guide.textContent.includes('Guided sample'),
      } : null
    })()`),
    'the processed-stage guide and contextual build action',
  )
  assert.ok(processedGuide.current.includes('Processed'))
  assert.equal(processedGuide.sample, true)

  await clickButton(cdp, 'Build and validate 3MF')
  await waitFor(
    () => cdp.evaluate("document.querySelector('.first-run-guide')?.dataset.stage === 'geometry'"),
    'the geometry-stage guide',
    90_000,
  )
  const validated = await waitFor(
    () => cdp.evaluate(`(() => {
      const guide = document.querySelector('.first-run-guide[data-stage="validated"]')
      const evidence = document.querySelector('.output-evidence')
      const download = [...(guide?.querySelectorAll('button') ?? [])].find(
        (button) => button.textContent.trim() === 'Download verified 3MF' && !button.disabled,
      )
      const failure = document.querySelector('.output-readiness')?.innerText ?? ''
      return guide && evidence && download ? {
        current: guide.querySelector('[aria-current="step"]')?.textContent,
        guideText: guide.textContent,
        evidenceText: evidence.innerText,
        status: evidence.querySelector('[data-status]')?.getAttribute('data-status'),
        failure,
        overlay: Boolean(document.querySelector('vite-error-overlay')),
      } : failure.includes('failed') || failure.includes('Retry') ? { failed: true, failure } : null
    })()`),
    'retained slicer validation evidence or an actionable failure',
    300_000,
  )
  assert.notEqual(validated.failed, true, JSON.stringify(validated, null, 2))
  assert.ok(validated.current.includes('Slicer'))
  assert.ok(validated.guideText.includes('does not guarantee bed adhesion'))
  assert.ok(validated.evidenceText.includes('Bambu Studio'))
  assert.ok(validated.evidenceText.includes('Download 3MF package'))
  assert.equal(validated.overlay, false)

  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  const downloadDirectory = await fs.mkdtemp(path.join(path.dirname(screenshot), 'first-run-download-'))
  await cdp.send('Browser.setDownloadBehavior', {
    behavior: 'allow',
    downloadPath: downloadDirectory,
    eventsEnabled: true,
  })
  await clickButton(cdp, 'Download verified 3MF')
  const downloaded = await waitFor(async () => {
    const entries = await fs.readdir(downloadDirectory)
    const name = entries.find((entry) => entry.toLowerCase().endsWith('.3mf'))
    if (!name) return null
    const filename = path.join(downloadDirectory, name)
    const stat = await fs.stat(filename)
    return stat.size > 0 ? { filename, byteSize: stat.size } : null
  }, 'a non-empty guided-sample 3MF download', 60_000)

  const capture = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true })
  await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))

  console.log(JSON.stringify({
    projectId,
    bundledSample: true,
    processedProof: true,
    geometryGenerated: true,
    retainedSlicerEvidence: true,
    offlineSampleBlocked: offline.sampleDisabled,
    compactGuideScrollable: true,
    guideCloseAndReopen: true,
    validationStatus: validated.status,
    downloaded,
    externalDocumentationRequired: false,
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
