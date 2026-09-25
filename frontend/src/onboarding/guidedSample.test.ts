import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createGuidedSampleFile,
  GUIDED_SAMPLE_FILENAME,
  GUIDED_SAMPLE_PROJECT_NAME,
  importGuidedSample,
} from './guidedSample'

afterEach(() => vi.restoreAllMocks())

describe('createGuidedSampleFile', () => {
  it('encodes a deterministic local PNG without fetching an external asset', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const context = {
      imageSmoothingEnabled: true,
      fillStyle: '',
      fillRect: vi.fn(),
      beginPath: vi.fn(),
      arc: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      fill: vi.fn(),
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
      context as unknown as CanvasRenderingContext2D,
    )
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) => {
      callback(new Blob(['safe-sample'], { type: 'image/png' }))
    })

    const file = await createGuidedSampleFile()

    expect(file.name).toBe(GUIDED_SAMPLE_FILENAME)
    expect(file.type).toBe('image/png')
    expect(file.size).toBeGreaterThan(0)
    expect(context.fillRect).toHaveBeenCalled()
    expect(context.arc).toHaveBeenCalledWith(805, 154, 88, 0, Math.PI * 2)
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('returns the upload result so a rejected import cannot remain marked as guided', async () => {
    const context = {
      imageSmoothingEnabled: true,
      fillStyle: '',
      fillRect: vi.fn(),
      beginPath: vi.fn(),
      arc: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      fill: vi.fn(),
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
      context as unknown as CanvasRenderingContext2D,
    )
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) => {
      callback(new Blob(['safe-sample'], { type: 'image/png' }))
    })
    const upload = vi.fn().mockResolvedValue(false)

    await expect(importGuidedSample(upload)).resolves.toBe(false)
    expect(upload).toHaveBeenCalledWith(
      expect.objectContaining({ name: GUIDED_SAMPLE_FILENAME, type: 'image/png' }),
      GUIDED_SAMPLE_PROJECT_NAME,
    )
  })
})
