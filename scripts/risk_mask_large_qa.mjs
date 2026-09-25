#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const projectId = process.env.IMAGE23MF_QA_PROJECT ?? 'project_2b39e6c9960b4f4294ef5e1eac27fd74'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5173'
const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const screenshot = path.resolve(process.env.IMAGE23MF_QA_SCREENSHOT ?? 'workspace/qa/risk-list-1578.png')

class Cdp {
  constructor(socket) {
    this.socket = socket
    this.id = 0
    this.pending = new Map()
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (!message.id) return
      const pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id)
      if (message.error) pending.reject(new Error(message.error.message))
      else pending.resolve(message.result)
    })
  }

  send(method, params = {}) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text)
    return result.result.value
  }
}

async function waitFor(check, label, timeout = 30_000) {
  const started = Date.now()
  while (Date.now() - started < timeout) {
    let value = null
    try {
      value = await check()
    } catch (error) {
      if (!String(error).includes('navigated')) throw error
    }
    if (value) return value
    await new Promise((resolve) => setTimeout(resolve, 75))
  }
  throw new Error(`Timed out waiting for ${label}`)
}

const target = await fetch(`${devtools}/json/new?${encodeURIComponent(appUrl)}`, { method: 'PUT' }).then((response) => response.json())
const socket = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true })
  socket.addEventListener('error', reject, { once: true })
})
const cdp = new Cdp(socket)
try {
  await Promise.all([cdp.send('Runtime.enable'), cdp.send('Page.enable')])
  await waitFor(() => cdp.evaluate("document.readyState === 'complete'"), 'initial page')
  await cdp.evaluate(`localStorage.setItem('image23mf.recentProjectId', ${JSON.stringify(projectId)}); true`)
  const loadStarted = Date.now()
  await cdp.send('Page.reload', { ignoreCache: true })
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('.region-inspector-warning-section'))"),
    'the persisted project inspector',
  )
  const initial = await cdp.evaluate(`(() => {
    const heading = document.querySelector('.region-inspector-warning-section .region-inspector-section-heading span')
    const total = Number(heading?.textContent ?? 0)
    return {
      total,
      rows: document.querySelectorAll('.region-inspector-warning-list > li').length,
      buttons: document.querySelectorAll('.region-inspector-warning-list > li > button').length,
      overlayNodes: document.querySelectorAll('.i23-canvas-risks').length,
      visibleStatus: document.querySelector('.region-inspector-warning-controls output')?.textContent,
      body: document.body.innerText.slice(0, 500),
    }
  })()`)
  initial.loadMs = Date.now() - loadStarted
  assert.equal(initial.total, 1578, JSON.stringify(initial))
  assert.equal(initial.rows, 60)
  assert.equal(initial.buttons, 60)
  assert.ok(initial.visibleStatus.includes('Showing 60 of 1578'))

  const interaction = await cdp.evaluate(`(async () => {
    const select = document.querySelector('.region-inspector-warning-controls select')
    const warning = document.querySelector('.region-inspector-warning-list > li > button')
    if (!select || !warning) return null
    const started = performance.now()
    select.value = 'error'
    select.dispatchEvent(new Event('change', { bubbles: true }))
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    warning.click()
    await new Promise((resolve) => requestAnimationFrame(resolve))
    return {
      elapsedMs: performance.now() - started,
      rows: document.querySelectorAll('.region-inspector-warning-list > li').length,
      selected: document.querySelector('.region-inspector-warning-list > li > button[aria-pressed="true"]')?.textContent,
      status: document.querySelector('.region-inspector-warning-controls output')?.textContent,
    }
  })()`)
  assert.ok(interaction)
  assert.equal(interaction.rows, 60)
  assert.ok(interaction.selected)
  assert.ok(interaction.status.includes('812 matching warnings'))
  assert.ok(interaction.elapsedMs < 250, JSON.stringify(interaction))

  const capture = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))
  console.log(JSON.stringify({ ok: true, projectId, initial, interaction, screenshot }, null, 2))
} finally {
  socket.close()
  await fetch(`${devtools}/json/close/${target.id}`).catch(() => undefined)
}
