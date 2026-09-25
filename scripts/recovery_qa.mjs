#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5173'
const screenshot = path.resolve(
  process.env.IMAGE23MF_QA_SCREENSHOT ?? 'workspace/qa/startup-recovery-mvp.png',
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
      clearTimeout(pending.timer)
      if (message.error) pending.reject(new Error(`${pending.method}: ${message.error.message}`))
      else pending.resolve(message.result)
    })
  }

  send(method, params = {}) {
    const id = ++this.nextId
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`${method}: timed out waiting for the browser`))
      }, 30_000)
      this.pending.set(id, { method, resolve, reject, timer })
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

async function waitFor(check, description, timeoutMs = 30_000) {
  const started = Date.now()
  let last
  while (Date.now() - started < timeoutMs) {
    last = await check()
    if (last) return last
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`Timed out waiting for ${description}; last value: ${JSON.stringify(last)}`)
}

async function clickButton(cdp, label) {
  const point = await cdp.evaluate(`(() => {
    const matches = [...document.querySelectorAll('button')].filter(
      (button) => button.textContent.trim() === ${JSON.stringify(label)} && !button.disabled,
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

async function main() {
  console.error('[recovery-qa] opening isolated app')
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
    width: 1280, height: 900, deviceScaleFactor: 1, mobile: false,
  })
  await cdp.send('Page.navigate', { url: appUrl })
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Some work was interrupted')"),
    'the startup recovery notice',
  )
  console.error('[recovery-qa] recovery notice visible')
  const latest = await cdp.evaluate(
    "fetch('/api/workspace/recovery/latest').then((response) => response.json())",
  )
  assert.equal(latest.interrupted_jobs.length, 1)
  assert.equal(latest.interrupted_jobs[0].stage_before_restart, 'packaging')
  assert.equal(latest.interrupted_jobs[0].resumable, false)
  assert.equal(latest.automatic_deletions, 0)

  await clickButton(cdp, 'View details')
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('[role=dialog]'))"),
    'the recovery dialog',
  )
  const modalState = await cdp.evaluate(`({
    inert: document.querySelector('.app-shell').hasAttribute('inert'),
    overflow: document.body.style.overflow,
    focused: document.activeElement?.getAttribute('aria-label'),
    text: document.querySelector('[role=dialog]').innerText,
  })`)
  assert.equal(modalState.inert, true)
  assert.equal(modalState.overflow, 'hidden')
  assert.equal(modalState.focused, 'Close recovery details')
  assert.match(modalState.text, /Stopped during packaging\. It was not silently resumed\./)
  assert.match(modalState.text, /Automatic deletions\s*0/)
  console.error('[recovery-qa] recovery dialog verified')

  await clickButton(cdp, 'Review cleanup dry run')
  await waitFor(
    () => cdp.evaluate("document.body.innerText.includes('Nothing has been deleted')"),
    'the cleanup dry run',
  )
  const cleanupText = await cdp.evaluate(
    "document.querySelector('.workspace-cleanup-plan').innerText",
  )
  assert.match(cleanupText, /temp\/interrupted\.tmp/)
  assert.match(cleanupText, /Nothing has been deleted/)
  console.error('[recovery-qa] cleanup dry run verified')

  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  const capture = await cdp.send('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: true,
  })
  await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))
  console.error('[recovery-qa] screenshot captured')

  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Escape', code: 'Escape' })
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape' })
  await waitFor(
    () => cdp.evaluate("!document.querySelector('[role=dialog]')"),
    'the dialog to close',
  )
  const restored = await cdp.evaluate(`({
    inert: document.querySelector('.app-shell').hasAttribute('inert'),
    overflow: document.body.style.overflow,
    focused: document.activeElement?.textContent?.trim(),
  })`)
  assert.equal(restored.inert, false)
  assert.equal(restored.overflow, '')
  assert.equal(restored.focused, 'View details')
  console.error('[recovery-qa] keyboard close and focus restoration verified')

  console.log(JSON.stringify({
    status: 'passed',
    job_id: latest.interrupted_jobs[0].job_id,
    cleanup_candidate_count: latest.stale_temp_file_count + latest.orphan_file_count,
    automatic_deletions: latest.automatic_deletions,
    screenshot,
  }))
  socket.close()
  await fetch(`${devtools}/json/close/${target.id}`)
}

main().catch((error) => {
  console.error(error)
  // A live CDP WebSocket otherwise keeps Node alive after a failed assertion,
  // obscuring the real failure behind the outer QA timeout.
  process.exit(1)
})
