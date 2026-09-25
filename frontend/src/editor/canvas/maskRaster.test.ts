import { describe, expect, it, vi } from 'vitest'
import { binaryMaskToRgba, fetchVerifiedBinaryMask } from './maskRaster'

describe('binary mask raster adapter', () => {
  it('maps exact row-major binary bytes to deterministic RGBA pixels', () => {
    expect(
      [...binaryMaskToRgba(new Uint8Array([0, 1, 1, 0]), 2, 2, [9, 8, 7, 255], [1, 2, 3, 0])],
    ).toEqual([
      1, 2, 3, 0,
      9, 8, 7, 255,
      9, 8, 7, 255,
      1, 2, 3, 0,
    ])
  })

  it('rejects wrong byte counts, non-binary values, and invalid colors', () => {
    expect(() => binaryMaskToRgba(new Uint8Array([0]), 2, 2)).toThrow(/expected 4/)
    expect(() => binaryMaskToRgba(new Uint8Array([0, 1, 2, 0]), 2, 2)).toThrow(/expected only 0 or 1/)
    expect(() =>
      binaryMaskToRgba(new Uint8Array([0]), 1, 1, [0, 0, 0, 300]),
    ).toThrow(/RGBA channels/)
  })

  it('downloads and verifies exact bytes before rasterization', async () => {
    const bytes = new Uint8Array([0, 1, 1, 0])
    const fetcher = async () => new Response(bytes, { status: 200 })
    const artifact = {
      kind: 'editor-changed-mask',
      downloadUrl: '/mask',
      sha256: 'd5e2d2ac07b741be58f6b9e50ede5fdcf16f3e8053ecef9350e7744b0d8bd90c',
      width: 2,
      height: 2,
      encoding: 'uint8-binary-row-major' as const,
    }

    const hashBytes = vi.fn(async () => artifact.sha256)
    await expect(fetchVerifiedBinaryMask(artifact, { fetcher, hashBytes })).resolves.toEqual(bytes)
    expect(hashBytes).toHaveBeenCalledWith(bytes)
    await expect(
      fetchVerifiedBinaryMask(
        { ...artifact, sha256: '0'.repeat(64) },
        { fetcher, hashBytes },
      ),
    ).rejects.toThrow(/SHA-256/)
  })
})
