#!/usr/bin/env node

import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs/promises'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'

const root = path.resolve('.')
const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const fixture = path.resolve('tests/fixtures/synthetic/geometry-nozzle-040.png')
const screenshot = path.resolve(
  process.env.IMAGE23MF_QA_SCREENSHOT ?? 'workspace/qa/mural-planner/mural-planner.png',
)
const children = []
let activeSocket
let activeLogs = null

function availablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : null
      server.close((error) => error ? reject(error) : resolve(port))
    })
  })
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

async function startIsolatedApp() {
  const workspace = await fs.mkdtemp(path.join(os.tmpdir(), 'image23mf-mural-browser-'))
  const [apiPort, webPort] = await Promise.all([availablePort(), availablePort()])
  const apiUrl = `http://127.0.0.1:${apiPort}`
  const webUrl = `http://127.0.0.1:${webPort}`
  const logs = { api: '', web: '' }
  activeLogs = logs
  const api = spawn(
    path.resolve('.venv/bin/python'),
    ['-m', 'uvicorn', 'image23mf.api.app:app', '--host', '127.0.0.1', '--port', String(apiPort)],
    {
      cwd: root,
      env: { ...process.env, IMAGE23MF_WORKSPACE: workspace },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  )
  const web = spawn(
    'npm',
    ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(webPort), '--strictPort'],
    {
      cwd: path.resolve('frontend'),
      env: { ...process.env, IMAGE23MF_API_TARGET: apiUrl },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  )
  children.push(api, web)
  api.stdout.on('data', (chunk) => { logs.api += String(chunk) })
  api.stderr.on('data', (chunk) => { logs.api += String(chunk) })
  web.stdout.on('data', (chunk) => { logs.web += String(chunk) })
  web.stderr.on('data', (chunk) => { logs.web += String(chunk) })
  await waitFor(async () => {
    try { return (await fetch(`${apiUrl}/api/health`)).ok } catch { return false }
  }, 'isolated mural API startup')
  await waitFor(async () => {
    try { return (await fetch(webUrl)).ok } catch { return false }
  }, 'isolated mural web startup')
  return { apiUrl, webUrl, workspace, logs }
}

class CdpSession {
  constructor(socket) {
    this.socket = socket
    this.nextId = 0
    this.pending = new Map()
    this.exceptions = []
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (message.method === 'Runtime.exceptionThrown') this.exceptions.push(message.params)
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
        reject(new Error(`${method}: timed out waiting for Chrome`))
      }, 30_000)
      this.pending.set(id, { method, resolve, reject, timer })
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

async function setFileInput(cdp, filename) {
  const object = await cdp.send('Runtime.evaluate', {
    expression: "document.querySelector('input[type=file]')",
    returnByValue: false,
  })
  assert.ok(object.result.objectId, 'the source image input must exist')
  await cdp.send('DOM.getDocument', { depth: 0, pierce: true })
  const input = await cdp.send('DOM.requestNode', { objectId: object.result.objectId })
  await cdp.send('DOM.setFileInputFiles', { nodeId: input.nodeId, files: [filename] })
}

async function clickButton(cdp, label) {
  const outcome = await cdp.evaluate(`(() => {
    const matches = [...document.querySelectorAll('button')].filter((button) => (
      button.textContent.trim() === ${JSON.stringify(label)}
      || button.getAttribute('aria-label') === ${JSON.stringify(label)}
    ) && !button.disabled)
    if (matches.length !== 1) return { count: matches.length }
    matches[0].scrollIntoView({ block: 'center', inline: 'center' })
    matches[0].click()
    return { count: 1 }
  })()`)
  assert.equal(outcome.count, 1, `${label} must resolve to one enabled button`)
}

async function fillLabel(cdp, label, value) {
  const outcome = await cdp.evaluate(`(() => {
    const matches = [...document.querySelectorAll('input')].filter(
      (input) => input.getAttribute('aria-label') === ${JSON.stringify(label)},
    )
    if (matches.length !== 1) return { count: matches.length }
    const input = matches[0]
    input.scrollIntoView({ block: 'center', inline: 'center' })
    input.focus()
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    if (!setter) return { count: 1, updated: false }
    setter.call(input, ${JSON.stringify(String(value))})
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
    return { count: 1, updated: input.value === ${JSON.stringify(String(value))} }
  })()`)
  assert.equal(outcome.count, 1, `${label} must resolve to one input`)
  assert.equal(outcome.updated, true, `${label} must accept the requested value`)
}

async function main() {
  await fs.access(fixture)
  const isolated = await startIsolatedApp()
  const target = await fetch(`${devtools}/json/new?${encodeURIComponent(isolated.webUrl)}`, {
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
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  await cdp.send('Page.navigate', { url: isolated.webUrl })
  await waitFor(() => cdp.evaluate("document.body.innerText.includes('Choose an image')"), 'import screen')
  await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
  await setFileInput(cdp, fixture)
  const projectId = await waitFor(
    () => cdp.evaluate(`(() => {
      const planner = document.querySelector('.mural-planner')
      const projectId = localStorage.getItem('image23mf.recentProjectId')
      const saveState = document.querySelector('.mural-save-state')?.textContent?.trim()
      return planner && projectId && saveState === 'Not saved'
        && planner.innerText.includes('3 × 2 · 6 plates') ? projectId : null
    })()`),
    'mounted 3x2 mural planner',
    90_000,
  )
  assert.equal(await cdp.evaluate("Boolean(document.querySelector('vite-error-overlay'))"), false)
  console.error('[mural-qa] live editor mounted one continuous 3x2 master')

  await waitFor(
    () => cdp.evaluate(`(() => {
      const planner = document.querySelector('.mural-planner')
      const columns = document.querySelector('input[aria-label="Mural columns"]')
      return planner?.getAttribute('aria-busy') === 'false' && columns && !columns.disabled
    })()`),
    'settled mural controls',
  )
  await fillLabel(cdp, 'Mural columns', 4)
  await waitFor(
    () => cdp.evaluate("document.querySelector('.mural-planner')?.innerText.includes('4 × 2 · 8 plates')"),
    'arbitrary 4x2 preview',
  )
  await cdp.evaluate(`(() => {
    const details = [...document.querySelectorAll('details')].find(
      (candidate) => candidate.querySelector('summary')?.textContent.includes('Reserved plate areas'),
    )
    if (!details) return false
    details.open = true
    details.dispatchEvent(new Event('toggle'))
    return true
  })()`)
  await clickButton(cdp, 'Add reserved area')
  await fillLabel(cdp, 'Reservation 1 X millimetres', 200)
  await waitFor(
    () => cdp.evaluate("document.querySelector('.mural-bed-map')?.getAttribute('aria-label')?.startsWith('1 reserved areas')"),
    'reserved area preview',
  )
  await waitFor(
    () => cdp.evaluate("!document.querySelector('.mural-save')?.disabled"),
    'saveable first mural generation',
  )
  await clickButton(cdp, 'Save mural plan')
  await waitFor(
    () => cdp.evaluate("document.querySelector('.mural-save-state')?.textContent.trim() === 'Saved · v1'"),
    'saved mural generation one',
  )
  console.error('[mural-qa] reservation and generation-one save verified')

  await cdp.evaluate('window.__muralQaBeforeReload = true')
  await cdp.send('Page.reload')
  await waitFor(
    () => cdp.evaluate("window.__muralQaBeforeReload === undefined && document.readyState === 'complete'"),
    'browser reload completion',
  )
  await waitFor(
    () => cdp.evaluate("document.querySelector('.mural-save-state')?.textContent.trim() === 'Saved · v1'"),
    'saved mural hydration after reload',
    90_000,
  )
  const hydrated = await cdp.evaluate(`({
    columns: document.querySelector('input[aria-label="Mural columns"]')?.value,
    reservation: document.querySelector('input[aria-label="Reservation 1 X millimetres"]')?.value,
  })`)
  assert.deepEqual(hydrated, { columns: '4', reservation: '200' })
  console.error('[mural-qa] reload restored grid and reserved plate area')

  await fillLabel(cdp, 'Mural columns', 2)
  await fillLabel(cdp, 'Mural rows', 3)
  await fillLabel(cdp, 'Panel width millimetres', 180)
  await fillLabel(cdp, 'Panel height millimetres', 140)
  await waitFor(
    () => cdp.evaluate("document.querySelector('.mural-planner')?.innerText.includes('2 × 3 · 6 plates')"),
    'non-square 2x3 master preview',
  )
  await waitFor(() => cdp.evaluate("!document.querySelector('.mural-save')?.disabled"), 'updated plan save')
  await clickButton(cdp, 'Update mural plan')
  await waitFor(
    () => cdp.evaluate("document.querySelector('.mural-save-state')?.textContent.trim() === 'Saved · v2'"),
    'saved mural generation two',
  )
  const saved = await cdp.evaluate(
    `fetch('/api/projects/${projectId}/mural-plan').then((response) => response.json())`,
  )
  assert.equal(saved.plan.master_size_mm.width, 360)
  assert.equal(saved.plan.master_size_mm.height, 420)
  assert.equal(saved.plan.tiles.length, 6)
  assert.equal(new Set(saved.plan.tiles.map((tile) => JSON.stringify(tile.master_pixel_bounds))).size, 6)
  assert.equal(saved.request.reserved_rectangles.length, 1)
  console.error('[mural-qa] non-square master and six unique crops verified')

  const assemblyEnabled = await cdp.evaluate(`(() => {
    const input = document.querySelector('#mural-assembly-enabled')
    const label = document.querySelector('label[for="mural-assembly-enabled"]')
    if (!(input instanceof HTMLInputElement) || !label) return false
    label.scrollIntoView({ block: 'center', inline: 'center' })
    label.click()
    return input.checked
  })()`)
  assert.equal(assemblyEnabled, true)
  await waitFor(
    () => cdp.evaluate("Boolean(document.querySelector('#mural-assembly-rearIdentifiers'))"),
    'expanded assembly-aid controls',
  )
  await clickButton(cdp, 'Build 6-plate 3MF')
  const buildOutcome = await waitFor(
    () => cdp.evaluate(`(() => {
      const links = Object.fromEntries([...document.querySelectorAll('a')].map((candidate) => [
        candidate.textContent.trim(), candidate,
      ]))
      const packageLink = links['Download verified multi-plate 3MF']
      const sheetLink = links['Download assembly sheet']
      const metadataLink = links['Download assembly metadata']
      if (packageLink && sheetLink && metadataLink) return {
        kind: 'download',
        href: packageLink.getAttribute('href'),
        sheetHref: sheetLink.getAttribute('href'),
        metadataHref: metadataLink.getAttribute('href'),
        download: packageLink.hasAttribute('download')
          && sheetLink.hasAttribute('download')
          && metadataLink.hasAttribute('download'),
      }
      const failure = document.querySelector('.mural-build__failure')?.textContent?.trim()
      return failure ? { kind: 'failure', message: failure } : null
    })()`),
    'terminal multi-plate mural build result',
    300_000,
  )
  assert.equal(buildOutcome.kind, 'download', buildOutcome.message)
  const verifiedPackage = buildOutcome
  assert.equal(verifiedPackage.download, true)
  const downloaded = await cdp.evaluate(`fetch(${JSON.stringify(verifiedPackage.href)})
    .then(async (response) => {
      const bytes = new Uint8Array(await response.arrayBuffer())
      return {
        ok: response.ok,
        contentType: response.headers.get('content-type'),
        byteLength: bytes.byteLength,
        signature: [...bytes.slice(0, 2)],
      }
    })`)
  assert.equal(downloaded.ok, true)
  assert.match(downloaded.contentType ?? '', /3mf|zip|octet-stream/)
  assert.ok(downloaded.byteLength > 1000)
  assert.deepEqual(downloaded.signature, [0x50, 0x4b])
  const assemblyDownloads = await cdp.evaluate(`Promise.all([
    fetch(${JSON.stringify(verifiedPackage.sheetHref)}).then(async (response) => ({
      ok: response.ok,
      contentType: response.headers.get('content-type'),
      body: await response.text(),
    })),
    fetch(${JSON.stringify(verifiedPackage.metadataHref)}).then(async (response) => ({
      ok: response.ok,
      contentType: response.headers.get('content-type'),
      body: await response.json(),
    })),
  ])`)
  assert.equal(assemblyDownloads[0].ok, true)
  assert.match(assemblyDownloads[0].contentType ?? '', /svg/)
  assert.match(assemblyDownloads[0].body, /<svg/)
  assert.match(assemblyDownloads[0].body, /R03C02 · plate 06/)
  assert.equal(assemblyDownloads[1].ok, true)
  assert.match(assemblyDownloads[1].contentType ?? '', /json/)
  assert.equal(assemblyDownloads[1].body.settings.enabled, true)
  assert.equal(assemblyDownloads[1].body.tiles.length, 6)
  assert.equal(assemblyDownloads[1].body.artwork_purity.visible_art_unchanged, true)
  assert.equal(
    assemblyDownloads[1].body.artwork_purity.authoritative_master_sha256,
    assemblyDownloads[1].body.artwork_purity.recomposed_visible_art_sha256,
  )
  assert.equal(
    await cdp.evaluate("Boolean(document.querySelector('.mural-seam-qa'))"),
    true,
  )
  console.error('[mural-qa] automatic geometry, exact seam QA, external assembly aids, Bambu validation, and verified downloads passed')

  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1280, height: 520, deviceScaleFactor: 1, mobile: false,
  })
  const shortRail = await cdp.evaluate(`(() => {
    const rail = document.querySelector('.i23-editor-rail[data-rail="right"] .i23-editor-rail__body')
    if (!rail) return null
    rail.scrollTop = 0
    const rect = rail.getBoundingClientRect()
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + Math.min(rect.height / 2, 160),
      scrollHeight: rail.scrollHeight,
      clientHeight: rail.clientHeight,
    }
  })()`)
  assert.ok(shortRail)
  assert.ok(shortRail.scrollHeight > shortRail.clientHeight)
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseMoved', x: shortRail.x, y: shortRail.y,
  })
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseWheel', x: shortRail.x, y: shortRail.y, deltaX: 0, deltaY: 500,
  })
  await waitFor(
    () => cdp.evaluate("document.querySelector('.i23-editor-rail[data-rail=\"right\"] .i23-editor-rail__body')?.scrollTop ?? 0"),
    'short viewport wheel scroll',
  )
  const shortViewport = await cdp.evaluate(`(() => {
    const rail = document.querySelector('.i23-editor-rail[data-rail="right"] .i23-editor-rail__body')
    if (!rail) return null
    const wheelScrollTop = rail.scrollTop
    const actions = document.querySelector('.mural-actions')
    actions?.scrollIntoView({ block: 'center', inline: 'nearest' })
    const plannerActions = actions?.getBoundingClientRect()
    return {
      wheelScrollTop,
      plannerActionsVisible: Boolean(
        plannerActions && plannerActions.bottom > 0 && plannerActions.top < innerHeight,
      ),
      bodyOverflow: document.body.style.overflow,
      viteOverlay: Boolean(document.querySelector('vite-error-overlay')),
    }
  })()`)
  assert.ok(shortViewport.wheelScrollTop > 0)
  assert.equal(shortViewport.plannerActionsVisible, true)
  assert.equal(shortViewport.bodyOverflow, '')
  assert.equal(shortViewport.viteOverlay, false)
  assert.equal(cdp.exceptions.length, 0, `browser exceptions: ${JSON.stringify(cdp.exceptions)}`)
  console.error('[mural-qa] short viewport rail scrolling and overlay absence verified')

  await fs.mkdir(path.dirname(screenshot), { recursive: true })
  const capture = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
  await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))
  console.log(JSON.stringify({
    status: 'passed',
    project_id: projectId,
    generations: 2,
    unique_tiles: 6,
    verified_package_bytes: downloaded.byteLength,
    assembly_tiles: assemblyDownloads[1].body.tiles.length,
    assembly_sheet_bytes: new TextEncoder().encode(assemblyDownloads[0].body).byteLength,
    short_viewport_scroll: true,
    screenshot,
  }))
  socket.close()
  activeSocket = null
  await fetch(`${devtools}/json/close/${target.id}`)
}

function cleanup() {
  if (activeSocket) activeSocket.close()
  for (const child of children) child.kill('SIGTERM')
}

process.on('SIGINT', () => { cleanup(); process.exit(130) })
process.on('SIGTERM', () => { cleanup(); process.exit(143) })

main().catch((error) => {
  console.error(error)
  if (activeLogs?.api) console.error('[mural-qa] API log tail:\n' + activeLogs.api.slice(-12_000))
  if (activeLogs?.web) console.error('[mural-qa] web log tail:\n' + activeLogs.web.slice(-4_000))
  process.exitCode = 1
}).finally(cleanup)
