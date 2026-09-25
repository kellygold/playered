#!/usr/bin/env node

import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'

const devtools = process.env.IMAGE23MF_DEVTOOLS_URL ?? 'http://127.0.0.1:9223'
const appUrl = process.env.IMAGE23MF_WEB_URL ?? 'http://127.0.0.1:5173'
const apiUrl = process.env.IMAGE23MF_API_URL ?? 'http://127.0.0.1:8323'
const fixture = path.resolve(
  process.env.IMAGE23MF_QA_FIXTURE
    ?? 'tests/fixtures/synthetic/geometry-nozzle-040.png',
)
const axeSourcePath = path.resolve('frontend/node_modules/axe-core/axe.min.js')
const screenshot = path.resolve(
  process.env.IMAGE23MF_A11Y_SCREENSHOT ?? 'workspace/qa/accessibility.png',
)
const report = path.resolve(
  process.env.IMAGE23MF_A11Y_REPORT ?? 'workspace/qa/accessibility.json',
)

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']
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

async function setViewport(cdp, width, height) {
  let requestedWidth = width
  let requestedHeight = height
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: requestedWidth,
      height: requestedHeight,
      deviceScaleFactor: 1,
      mobile: false,
    })
    await new Promise((resolve) => setTimeout(resolve, 50))
    const actual = await cdp.evaluate('({ width: innerWidth, height: innerHeight })')
    if (actual.width === width && actual.height === height) return
    requestedWidth = Math.max(1, Math.round(requestedWidth * width / actual.width))
    requestedHeight = Math.max(1, Math.round(requestedHeight * height / actual.height))
  }
  const actual = await cdp.evaluate('({ width: innerWidth, height: innerHeight })')
  throw new Error(
    `Could not calibrate ${width}x${height} CSS viewport; received ${actual.width}x${actual.height}`,
  )
}

async function pressKey(cdp, key, code = key, modifiers = 0) {
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key, code, modifiers })
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, modifiers })
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

async function fillInput(cdp, selector, value) {
  const documentNode = await cdp.send('DOM.getDocument', { depth: -1, pierce: true })
  const input = await cdp.send('DOM.querySelector', {
    nodeId: documentNode.root.nodeId,
    selector,
  })
  assert.notEqual(input.nodeId, 0, `input must exist: ${selector}`)
  await cdp.send('DOM.focus', { nodeId: input.nodeId })
  const handled = await cdp.evaluate(`(() => {
    const input = document.querySelector(${JSON.stringify(selector)})
    if (!(input instanceof HTMLInputElement)) return false
    const reactPropsKey = Object.keys(input).find((key) => key.startsWith('__reactProps$'))
    const reactHandler = reactPropsKey ? input[reactPropsKey]?.onChange : null
    if (typeof reactHandler === 'function') {
      reactHandler({ target: { value: ${JSON.stringify(String(value))} } })
      return true
    }
    const previous = input.value
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    if (!setter) return false
    setter.call(input, ${JSON.stringify(String(value))})
    input._valueTracker?.setValue(previous)
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
    return input.value === ${JSON.stringify(String(value))}
  })()`)
  assert.equal(handled, true, `input must accept the requested value: ${selector}`)
  await pressKey(cdp, 'Tab', 'Tab')
}

async function clickButton(cdp, label) {
  const point = await cdp.evaluate(`(() => {
    const matches = [...document.querySelectorAll('button')].filter(
      (button) => (button.textContent.trim() === ${JSON.stringify(label)}
        || button.getAttribute('aria-label') === ${JSON.stringify(label)})
        && !button.disabled,
    )
    if (matches.length !== 1) return { count: matches.length }
    matches[0].scrollIntoView({ block: 'center', inline: 'center' })
    const bounds = matches[0].getBoundingClientRect()
    return { count: 1, x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 }
  })()`)
  assert.equal(point.count, 1, `${label} must resolve to exactly one enabled button`)
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

async function injectAxe(cdp, axeSource) {
  const loaded = await cdp.evaluate("typeof globalThis.axe === 'object'")
  if (loaded) return
  const result = await cdp.send('Runtime.evaluate', {
    expression: axeSource,
    awaitPromise: true,
    returnByValue: true,
  })
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description ?? 'axe-core injection failed')
  }
  assert.equal(await cdp.evaluate("typeof globalThis.axe === 'object'"), true)
}

async function axeScan(cdp, axeSource, label) {
  await injectAxe(cdp, axeSource)
  const result = await cdp.evaluate(`axe.run(document, {
    runOnly: { type: 'tag', values: ${JSON.stringify(WCAG_TAGS)} },
    resultTypes: ['violations'],
  }).then((scan) => ({
    label: ${JSON.stringify(label)},
    passes: scan.passes.length,
    incomplete: scan.incomplete.length,
    violations: scan.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      help: violation.help,
      helpUrl: violation.helpUrl,
      nodes: violation.nodes.map((node) => ({
        target: node.target,
        failureSummary: node.failureSummary,
      })),
    })),
  }))`)
  return result
}

async function focusEvidence(cdp) {
  return cdp.evaluate(`(() => {
    const element = document.activeElement
    if (!(element instanceof HTMLElement)) return null
    const selector = 'a[href],button,input,select,textarea,summary,[tabindex]'
    const baseToken = (candidate) => [
      candidate.tagName.toLowerCase(),
      candidate.id,
      candidate.getAttribute('aria-label'),
      candidate.getAttribute('name'),
      candidate.getAttribute('placeholder'),
      candidate.textContent?.trim().replace(/\\s+/g, ' ').slice(0, 100),
    ].map((part) => part ?? '').join('|')
    const base = baseToken(element)
    const peers = [...document.querySelectorAll(selector)].filter(
      (candidate) => candidate instanceof HTMLElement && baseToken(candidate) === base,
    )
    const style = getComputedStyle(element)
    const bounds = element.getBoundingClientRect()
    const outlineWidth = Number.parseFloat(style.outlineWidth) || 0
    return {
      token: base + '::' + peers.indexOf(element),
      tag: element.tagName.toLowerCase(),
      name: element.getAttribute('aria-label')
        ?? element.labels?.[0]?.textContent?.trim().slice(0, 80)
        ?? element.getAttribute('placeholder')
        ?? element.getAttribute('name')
        ?? element.textContent?.trim().slice(0, 80)
        ?? '',
      focusVisible: element.matches(':focus-visible'),
      outlineStyle: style.outlineStyle,
      outlineWidth: style.outlineWidth,
      outlineColor: style.outlineColor,
      boxShadow: style.boxShadow,
      indicator: (style.outlineStyle !== 'none' && outlineWidth >= 1)
        || (style.boxShadow !== 'none' && style.boxShadow !== ''),
      inViewport: bounds.bottom > 0 && bounds.right > 0
        && bounds.top < innerHeight && bounds.left < innerWidth,
    }
  })()`)
}

async function proveKeyboardAccess(cdp) {
  const positiveTabIndexes = await cdp.evaluate(`[
    ...document.querySelectorAll('[tabindex]'),
  ].filter((element) => element.tabIndex > 0).map((element) => ({
    tag: element.tagName.toLowerCase(),
    tabindex: element.tabIndex,
    label: element.getAttribute('aria-label'),
  }))`)
  assert.deepEqual(positiveTabIndexes, [], 'positive tabindex values are forbidden')

  await cdp.evaluate(`(() => {
    document.body.tabIndex = -1
    document.body.focus()
    return document.activeElement === document.body
  })()`)
  let focused
  let skipLinkPosition = 0
  const focusVisibilityViolations = []
  const initialFocusSequence = []
  for (; skipLinkPosition < 1; skipLinkPosition += 1) {
    await pressKey(cdp, 'Tab', 'Tab')
    focused = await focusEvidence(cdp)
    initialFocusSequence.push(focused)
    if (!focused?.indicator) focusVisibilityViolations.push(focused)
    assert.equal(focused?.inViewport, true, `${focused?.name || focused?.tag} must be visible`)
    if (focused?.name === 'Skip to canvas') break
  }
  assert.equal(
    focused?.name,
    'Skip to canvas',
    `a fresh editor page must expose the skip link as its first Tab stop: ${JSON.stringify(initialFocusSequence)}`,
  )
  assert.equal(focused.indicator, true, 'the skip link must have a visible keyboard focus indicator')
  assert.equal(focused.inViewport, true, 'the focused skip link must be visible in the viewport')
  await pressKey(cdp, 'Enter', 'Enter')
  assert.equal(
    await cdp.evaluate("document.activeElement?.matches('.i23-editor-canvas')"),
    true,
    'activating the skip link must focus the canvas workspace',
  )

  const expected = await cdp.evaluate(`(() => {
    const selector = 'a[href],button,input,select,textarea,summary,[tabindex]'
    const baseToken = (candidate) => [
      candidate.tagName.toLowerCase(),
      candidate.id,
      candidate.getAttribute('aria-label'),
      candidate.getAttribute('name'),
      candidate.getAttribute('placeholder'),
      candidate.textContent?.trim().replace(/\\s+/g, ' ').slice(0, 100),
    ].map((part) => part ?? '').join('|')
    const elements = [...document.querySelectorAll(selector)].filter((element) => {
      if (!(element instanceof HTMLElement) || element.tabIndex < 0) return false
      if (element.matches(':disabled')) return false
      if (element.closest('[inert]') || element.closest('[aria-hidden="true"]')) return false
      const closedDetails = element.closest('details:not([open])')
      if (closedDetails && element !== closedDetails.querySelector(':scope > summary')) return false
      const style = getComputedStyle(element)
      const bounds = element.getBoundingClientRect()
      return style.display !== 'none' && style.visibility !== 'hidden'
        && bounds.width > 0 && bounds.height > 0
    })
    return elements.map((element) => {
      const base = baseToken(element)
      const peers = [...document.querySelectorAll(selector)].filter(
        (candidate) => candidate instanceof HTMLElement && baseToken(candidate) === base,
      )
      return base + '::' + peers.indexOf(element)
    })
  })()`)
  const muralFocusDiagnostics = await cdp.evaluate(`[
    ...document.querySelectorAll('[aria-label$="millimetres"], button'),
  ].filter((element) => element.getAttribute('aria-label')?.includes('gap millimetres')
    || element.getAttribute('aria-label')?.includes('clearance millimetres')
    || element.textContent?.trim() === 'Add reserved area')
    .map((element) => ({
      label: element.getAttribute('aria-label') ?? element.textContent?.trim(),
      tabIndex: element.tabIndex,
      disabled: element.matches(':disabled'),
      inert: Boolean(element.closest('[inert]')),
      hidden: Boolean(element.closest('[hidden]')),
      closedDetails: Boolean(element.closest('details:not([open])')),
      ariaHidden: Boolean(element.closest('[aria-hidden="true"]')),
      parent: element.parentElement?.outerHTML.slice(0, 300),
    }))`)
  assert.ok(expected.length >= 20, `the editor should expose a substantive focus order, got ${expected.length}`)

  await cdp.evaluate('document.body.focus(); true')
  await pressKey(cdp, 'Tab', 'Tab')
  const visited = []
  let becameStaleAt = null
  for (let index = 0; index < expected.length; index += 1) {
    focused = await focusEvidence(cdp)
    assert.ok(focused?.token, `tab stop ${index + 1} must be a known visible control`)
    if (!focused.indicator) focusVisibilityViolations.push(focused)
    assert.equal(focused.inViewport, true, `${focused.name || focused.tag} must scroll into view`)
    visited.push(focused.token)
    if (becameStaleAt === null && await cdp.evaluate("document.body.innerText.includes('Update preview')")) {
      becameStaleAt = { index, current: focused.token, previous: visited.at(-2) ?? null }
    }
    if (index < expected.length - 1) await pressKey(cdp, 'Tab', 'Tab')
  }
  const visitedSet = new Set(visited)
  const expectedSet = new Set(expected)
  const missingAfterDynamicRender = expected.filter((token) => !visitedSet.has(token))
  const addedAfterDynamicRender = visited.filter((token) => !expectedSet.has(token))
  const duplicateStops = visited.filter((token, index) => visited.indexOf(token) !== index)
  assert.equal(
    visitedSet.size,
    expected.length,
    `Tab must visit each visible focus target once: ${JSON.stringify({ becameStaleAt, muralFocusDiagnostics, missingAfterDynamicRender, addedAfterDynamicRender, duplicateStops })}`,
  )
  const coverageRatio = (expected.length - missingAfterDynamicRender.length) / expected.length
  assert.ok(
    coverageRatio >= 0.98,
    `Tab must retain at least 98% coverage across controlled-input re-renders: ${JSON.stringify({ missingAfterDynamicRender, addedAfterDynamicRender })}`,
  )
  for (const criticalName of [
    'Revisions',
    'Crop selection',
    'Canvas width millimetres',
    'Processed image canvas',
    'Resize cleanup rail',
    'Automatic island policy',
  ]) {
    assert.ok(visited.some((token) => token.includes(criticalName)), `Tab must reach ${criticalName}`)
  }
  let cycled = null
  const dynamicCycleStops = []
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await pressKey(cdp, 'Tab', 'Tab')
    cycled = await focusEvidence(cdp)
    if (cycled?.token === visited[0]) break
    if (cycled?.token) dynamicCycleStops.push(cycled.token)
  }
  assert.equal(cycled?.token, visited[0], 'Tab order must cycle deterministically')
  return {
    count: expected.length,
    first: visited[0],
    last: visited.at(-1),
    skipLinkPosition: skipLinkPosition + 1,
    focusVisibilityViolations,
    coverageRatio,
    missingAfterDynamicRender,
    addedAfterDynamicRender,
    dynamicCycleStops,
  }
}

async function proveReducedMotion(cdp) {
  assert.equal(
    await cdp.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"),
    true,
  )
  await clickButton(cdp, 'Flicker')
  await waitFor(
    () => cdp.evaluate("Boolean([...document.querySelectorAll('button')].find((button) => ['Resume flicker', 'Flicker paused (reduced motion)'].includes(button.textContent.trim())))"),
    'reduced-motion flicker pause',
  )
  const before = await cdp.evaluate(`[
    ...document.querySelectorAll('.i23-canvas-plane'),
  ].map((plane) => plane.getAttribute('data-visible'))`)
  await new Promise((resolve) => setTimeout(resolve, 1_550))
  const after = await cdp.evaluate(`[
    ...document.querySelectorAll('.i23-canvas-plane'),
  ].map((plane) => plane.getAttribute('data-visible'))`)
  assert.deepEqual(after, before, 'reduced motion must prevent automatic canvas flicker')

  const moving = await cdp.evaluate(`(() => {
    const seconds = (value) => value.split(',').map((item) => {
      const part = item.trim()
      return part.endsWith('ms') ? Number.parseFloat(part) / 1000 : Number.parseFloat(part)
    }).filter(Number.isFinite)
    return [...document.querySelectorAll('*')].flatMap((element) => {
      if (!(element instanceof HTMLElement)) return []
      const bounds = element.getBoundingClientRect()
      if (bounds.width === 0 || bounds.height === 0) return []
      const style = getComputedStyle(element)
      const animation = style.animationName === 'none' ? 0 : Math.max(0, ...seconds(style.animationDuration))
      const transition = Math.max(0, ...seconds(style.transitionDuration))
      if (animation <= 0.011 && transition <= 0.011) return []
      return [{
        tag: element.tagName.toLowerCase(),
        className: element.className,
        animationName: style.animationName,
        animationDuration: style.animationDuration,
        transitionDuration: style.transitionDuration,
      }]
    })
  })()`)
  assert.deepEqual(moving, [], `reduced motion left nonessential movement: ${JSON.stringify(moving)}`)
  await clickButton(cdp, 'Single')
  return { mediaMatches: true, planesStable: true, computedMotionViolations: 0 }
}

async function proveReflow(cdp) {
  await setViewport(cdp, 320, 800)
  const before = await cdp.evaluate(`(() => ({
    viewport: { width: innerWidth, height: innerHeight },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight,
    },
    overflowOffenders: [...document.querySelectorAll('body *')].flatMap((element) => {
      if (!(element instanceof HTMLElement) || element.hidden) return []
      const bounds = element.getBoundingClientRect()
      const overflow = Math.max(0, bounds.right - document.documentElement.clientWidth)
      if (overflow < 0.5 && element.scrollWidth <= element.clientWidth) return []
      return [{
        tag: element.tagName.toLowerCase(),
        className: typeof element.className === 'string' ? element.className : '',
        ariaLabel: element.getAttribute('aria-label'),
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        left: bounds.left,
        right: bounds.right,
        overflow,
      }]
    }).sort((first, second) => second.overflow - first.overflow).slice(0, 20),
    scrollY,
  }))()`)
  assert.deepEqual(before.viewport, { width: 320, height: 800 })
  const horizontalOverflowPx = Math.max(
    0,
    before.document.scrollWidth - before.document.clientWidth,
  )
  assert.ok(before.document.scrollHeight > before.viewport.height, 'compact editor must remain vertically scrollable')
  await cdp.evaluate('window.scrollTo(0, 0); true')
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseWheel',
    x: 310,
    y: 400,
    deltaX: 0,
    deltaY: 620,
  })
  const afterScrollY = await waitFor(
    () => cdp.evaluate('window.scrollY'),
    '320px page scrolling',
  )
  await cdp.evaluate('window.scrollTo(0, 0); true')
  return { ...before, afterScrollY, horizontalOverflowPx }
}

async function dialogBounds(cdp, selector) {
  return cdp.evaluate(`(() => {
    const dialog = document.querySelector(${JSON.stringify(selector)})
    if (!dialog) return null
    const bounds = dialog.getBoundingClientRect()
    return {
      top: bounds.top,
      left: bounds.left,
      right: bounds.right,
      bottom: bounds.bottom,
      viewportWidth: innerWidth,
      viewportHeight: innerHeight,
      overflowY: getComputedStyle(dialog).overflowY,
    }
  })()`)
}

async function main() {
  await Promise.all([fs.access(fixture), fs.access(axeSourcePath)])
  const axeSource = await fs.readFile(axeSourcePath, 'utf8')
  const evidence = {
    ok: false,
    appUrl,
    fixture,
    axe: [],
  }

  const target = await fetch(`${devtools}/json/new?${encodeURIComponent(appUrl)}`, {
    method: 'PUT',
  }).then((response) => {
    assert.equal(response.ok, true, `DevTools target creation failed: ${response.status}`)
    return response.json()
  })
  const socket = new WebSocket(target.webSocketDebuggerUrl)
  activeSocket = socket
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  const cdp = new CdpSession(socket)
  try {
    await Promise.all([
      cdp.send('Page.enable'),
      cdp.send('Runtime.enable'),
      cdp.send('DOM.enable'),
      cdp.send('Log.enable'),
    ])
    await cdp.send('Emulation.setEmulatedMedia', {
      media: 'screen',
      features: [{ name: 'prefers-reduced-motion', value: 'reduce' }],
    })
    await setViewport(cdp, 1440, 1000)
    await cdp.send('Page.navigate', { url: appUrl })
    await waitFor(() => cdp.evaluate("document.readyState === 'complete'"), 'initial document')
    await cdp.evaluate('localStorage.clear(); sessionStorage.clear(); true')
    await cdp.send('Page.navigate', { url: appUrl })
    await waitFor(
      () => cdp.evaluate("document.readyState === 'complete' && document.body.innerText.includes('Choose an image') && Boolean(document.querySelector('input[type=file]'))"),
      'the import screen',
    )
    evidence.axe.push(await axeScan(cdp, axeSource, 'import'))

    await setFileInput(cdp, fixture)
    await waitFor(
      () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Exact artwork preview and comparison\"]'))"),
      'the loaded editor',
      90_000,
    )
    await waitFor(
      () => cdp.evaluate("document.body.innerText.includes('Preview complete and current.')"),
      'the current preview',
      90_000,
    )
    evidence.projectId = await cdp.evaluate("localStorage.getItem('image23mf.recentProjectId')")
    assert.ok(evidence.projectId)
    evidence.axe.push(await axeScan(cdp, axeSource, 'editor-desktop'))
    evidence.keyboard = await proveKeyboardAccess(cdp)
    evidence.reducedMotion = await proveReducedMotion(cdp)
    evidence.reflow = await proveReflow(cdp)
    evidence.axe.push(await axeScan(cdp, axeSource, 'editor-compact-320'))

    await setViewport(cdp, 1440, 1000)
    await clickButton(cdp, 'Revisions')
    await waitFor(
      () => cdp.evaluate("Boolean(document.querySelector('#revision-drawer'))"),
      'the revision drawer',
    )
    evidence.axe.push(await axeScan(cdp, axeSource, 'revision-drawer'))
    await clickButton(cdp, 'Close revisions')
    await waitFor(
      () => cdp.evaluate("!document.querySelector('#revision-drawer') && document.body.style.overflow === ''"),
      'the revision drawer to close',
    )

    const projectBefore = await fetch(`${apiUrl}/api/projects/${evidence.projectId}`).then((response) => response.json())
    const nextWidth = projectBefore.draft.config.canvas.width_mm === 224 ? 225 : 224
    const staleDraftResponse = await fetch(`${apiUrl}/api/projects/${evidence.projectId}/draft`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        config: {
          ...projectBefore.draft.config,
          canvas: { ...projectBefore.draft.config.canvas, width_mm: nextWidth },
        },
        operations: [],
        expected_draft_generation: projectBefore.draft.generation,
      }),
    })
    assert.equal(staleDraftResponse.ok, true, `stale draft fixture failed: ${await staleDraftResponse.text()}`)
    await cdp.send('Page.reload', { ignoreCache: true })
    await waitFor(
      () => cdp.evaluate("Boolean(document.querySelector('[aria-label=\"Exact artwork preview and comparison\"]'))"),
      'the reloaded stale editor',
      90_000,
    )
    await waitFor(
      () => cdp.evaluate(`document.querySelector('[aria-label="Canvas width millimetres"]')?.value === ${JSON.stringify(String(nextWidth))}`),
      'the reloaded stale draft value',
    )

    await clickButton(cdp, 'Revisions')
    await waitFor(() => cdp.evaluate("Boolean(document.querySelector('#revision-drawer'))"), 'the stale revision drawer')
    await fillInput(cdp, '[placeholder="e.g. Cleanup approved"]', 'Accessibility proof')
    await clickButton(cdp, 'Publish revision')
    const confirmSelector = '[role="alertdialog"][aria-label="Publish without current preview artifacts"]'
    await waitFor(
      () => cdp.evaluate(`Boolean(document.querySelector(${JSON.stringify(confirmSelector)}))`),
      'the stale-publication confirmation',
    )
    await setViewport(cdp, 320, 800)
    evidence.confirmation = await dialogBounds(cdp, confirmSelector)
    assert.ok(evidence.confirmation)
    assert.ok(evidence.confirmation.top >= 0 && evidence.confirmation.left >= 0)
    assert.ok(evidence.confirmation.right <= evidence.confirmation.viewportWidth)
    assert.ok(evidence.confirmation.bottom <= evidence.confirmation.viewportHeight)
    evidence.axe.push(await axeScan(cdp, axeSource, 'publication-confirm-compact-320'))
    evidence.confirmationFocusVisibilityViolations = []
    assert.equal(await cdp.evaluate('document.activeElement?.textContent?.trim()'), 'Go back')
    let confirmationFocus = await focusEvidence(cdp)
    if (!confirmationFocus?.indicator) {
      evidence.confirmationFocusVisibilityViolations.push(confirmationFocus)
    }
    await pressKey(cdp, 'Tab', 'Tab', 8)
    assert.equal(await cdp.evaluate('document.activeElement?.textContent?.trim()'), 'Publish without artifacts')
    confirmationFocus = await focusEvidence(cdp)
    if (!confirmationFocus?.indicator) {
      evidence.confirmationFocusVisibilityViolations.push(confirmationFocus)
    }
    await pressKey(cdp, 'Tab', 'Tab')
    assert.equal(await cdp.evaluate('document.activeElement?.textContent?.trim()'), 'Go back')

    const capture = await cdp.send('Page.captureScreenshot', {
      format: 'png',
      captureBeyondViewport: true,
    })
    await fs.mkdir(path.dirname(screenshot), { recursive: true })
    await fs.writeFile(screenshot, Buffer.from(capture.data, 'base64'))
    evidence.screenshot = screenshot

    const browserErrors = cdp.events.filter((event) => {
      if (event.method === 'Runtime.exceptionThrown') return true
      if (event.method === 'Runtime.consoleAPICalled' && event.params?.type === 'error') return true
      if (event.method !== 'Log.entryAdded' || event.params?.entry?.level !== 'error') return false
      const entry = event.params.entry
      const expectedMissingMuralPlan = entry.url?.endsWith('/mural-plan')
        && entry.text?.includes('404 (Not Found)')
      return !expectedMissingMuralPlan
    })
    assert.deepEqual(browserErrors, [], `browser errors: ${JSON.stringify(browserErrors)}`)
    evidence.browserErrors = 0
    const axeViolations = evidence.axe.flatMap((scan) =>
      scan.violations.map((violation) => ({ state: scan.label, ...violation })))
    const gateFailures = []
    if (evidence.reflow.horizontalOverflowPx > 0) {
      gateFailures.push({
        check: 'reflow-320',
        horizontalOverflowPx: evidence.reflow.horizontalOverflowPx,
      })
    }
    if (evidence.keyboard.focusVisibilityViolations.length > 0) {
      gateFailures.push({
        check: 'keyboard-focus-visible',
        nodes: evidence.keyboard.focusVisibilityViolations,
      })
    }
    if (evidence.confirmationFocusVisibilityViolations.length > 0) {
      gateFailures.push({
        check: 'confirmation-focus-visible',
        nodes: evidence.confirmationFocusVisibilityViolations,
      })
    }
    if (axeViolations.length > 0) {
      gateFailures.push({ check: 'axe-wcag-a-aa', violations: axeViolations })
    }
    assert.deepEqual(gateFailures, [], `accessibility gate failures: ${JSON.stringify(gateFailures, null, 2)}`)
    evidence.ok = true
    await fs.mkdir(path.dirname(report), { recursive: true })
    await fs.writeFile(report, `${JSON.stringify(evidence, null, 2)}\n`)
    console.log(JSON.stringify({ ...evidence, axe: evidence.axe.map(({ label, passes, incomplete }) => ({ label, passes, incomplete })) }, null, 2))
  } catch (error) {
    evidence.error = error instanceof Error ? error.message : String(error)
    await fs.mkdir(path.dirname(report), { recursive: true })
    await fs.writeFile(report, `${JSON.stringify(evidence, null, 2)}\n`)
    throw error
  } finally {
    socket.close()
    activeSocket = null
    await fetch(`${devtools}/json/close/${target.id}`).catch(() => undefined)
  }
}

process.on('SIGINT', () => {
  activeSocket?.close()
  process.exit(130)
})

process.on('SIGTERM', () => {
  activeSocket?.close()
  process.exit(143)
})

await main()
