import {
  editorCanvasViewMetadata,
  validateEditorCanvasSpace,
  type EditorCanvasSpace,
  type EditorCanvasViewSource,
} from './canvasModel'

export type BinaryMaskArtifact = {
  kind: 'editor-changed-mask' | 'editor-protected-mask' | string
  downloadUrl: string
  sha256: string
  width: number
  height: number
  encoding: 'uint8-binary-row-major'
}

export type BinaryMaskDisplayOptions = {
  foreground?: readonly [number, number, number, number]
  background?: readonly [number, number, number, number]
  fetcher?: typeof fetch
  hashBytes?: (bytes: Uint8Array) => Promise<string>
  signal?: AbortSignal
}

export type EditorCanvasMaskHandle = {
  source: EditorCanvasViewSource
  release: () => void
}

const DEFAULT_FOREGROUND = [71, 223, 185, 255] as const
const DEFAULT_BACKGROUND = [0, 0, 0, 0] as const

function assertColor(color: readonly number[]) {
  if (color.length !== 4 || color.some((channel) => !Number.isInteger(channel) || channel < 0 || channel > 255)) {
    throw new Error('Mask display colors require four integer RGBA channels from 0 through 255.')
  }
}

export function binaryMaskToRgba(
  pixels: Uint8Array,
  width: number,
  height: number,
  foreground: readonly [number, number, number, number] = DEFAULT_FOREGROUND,
  background: readonly [number, number, number, number] = DEFAULT_BACKGROUND,
) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new Error('Binary mask dimensions must be positive integers.')
  }
  if (pixels.byteLength !== width * height) {
    throw new Error(`Binary mask has ${pixels.byteLength} bytes; expected ${width * height}.`)
  }
  assertColor(foreground)
  assertColor(background)
  const rgba = new Uint8ClampedArray(pixels.byteLength * 4)
  for (let index = 0; index < pixels.byteLength; index += 1) {
    const value = pixels[index]
    if (value !== 0 && value !== 1) {
      throw new Error(`Binary mask byte ${index} is ${value}; expected only 0 or 1.`)
    }
    rgba.set(value === 1 ? foreground : background, index * 4)
  }
  return rgba
}

async function sha256Hex(bytes: Uint8Array) {
  const digest = await crypto.subtle.digest('SHA-256', bytes.slice().buffer)
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('')
}

export async function fetchVerifiedBinaryMask(
  artifact: BinaryMaskArtifact,
  options: Pick<BinaryMaskDisplayOptions, 'fetcher' | 'hashBytes' | 'signal'> = {},
) {
  if (artifact.encoding !== 'uint8-binary-row-major') {
    throw new Error(`Unsupported mask encoding: ${artifact.encoding as string}.`)
  }
  const response = await (options.fetcher ?? fetch)(artifact.downloadUrl, {
    signal: options.signal,
  })
  if (!response.ok) throw new Error(`Mask download failed with HTTP ${response.status}.`)
  const bytes = new Uint8Array(await response.arrayBuffer())
  if (bytes.byteLength !== artifact.width * artifact.height) {
    throw new Error('Downloaded mask byte count does not match its declared dimensions.')
  }
  const actualSha256 = await (options.hashBytes ?? sha256Hex)(bytes)
  if (actualSha256 !== artifact.sha256.toLowerCase()) {
    throw new Error('Downloaded mask failed SHA-256 verification.')
  }
  return bytes
}

function rgbaPngBlob(rgba: Uint8ClampedArray, width: number, height: number) {
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d', { alpha: true })
  if (!context) throw new Error('This browser cannot create a mask display canvas.')
  context.imageSmoothingEnabled = false
  const imageData = context.createImageData(width, height)
  imageData.data.set(rgba)
  context.putImageData(imageData, 0, 0)
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob)
      else reject(new Error('The browser could not encode the mask display raster.'))
    }, 'image/png')
  })
}

export async function loadBinaryMaskCanvasSource(
  artifact: BinaryMaskArtifact,
  inputSpace: EditorCanvasSpace,
  options: BinaryMaskDisplayOptions = {},
): Promise<EditorCanvasMaskHandle> {
  const space = validateEditorCanvasSpace(inputSpace)
  if (artifact.width !== space.widthPx || artifact.height !== space.heightPx) {
    throw new Error('Mask dimensions do not match the canonical canvas render space.')
  }
  const bytes = await fetchVerifiedBinaryMask(artifact, options)
  const rgba = binaryMaskToRgba(
    bytes,
    artifact.width,
    artifact.height,
    options.foreground,
    options.background,
  )
  const blob = await rgbaPngBlob(rgba, artifact.width, artifact.height)
  const url = URL.createObjectURL(blob)
  const metadata = editorCanvasViewMetadata('mask')
  return {
    source: {
      view: 'mask',
      label: metadata.label,
      description: metadata.description,
      raster: {
        url,
        sha256: artifact.sha256,
        artifactKind: artifact.kind,
        mediaType: 'image/png',
        interpolation: 'nearest',
      },
      space,
    },
    release: () => URL.revokeObjectURL(url),
  }
}
