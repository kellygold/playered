import { describe, expect, it } from 'vitest'
import { dragCrop, keyboardCrop, normalizeCrop } from './crop'

const crop = { mode: 'cover' as const, x: 0.2, y: 0.2, width: 0.5, height: 0.5 }

describe('crop controls', () => {
  it('clamps movement and resize to normalized source bounds', () => {
    expect(dragCrop(crop, 'move', 1, -1)).toEqual({ ...crop, x: 0.5, y: 0 })
    expect(dragCrop(crop, 'north-west', -1, -1)).toEqual({
      ...crop,
      x: 0,
      y: 0,
      width: 0.7,
      height: 0.7,
    })
    expect(dragCrop(crop, 'south-east', -1, -1)).toMatchObject({
      x: 0.2,
      y: 0.2,
      width: 0.05,
      height: 0.05,
    })
  })

  it('supports precise, accelerated, and resize keyboard operation', () => {
    expect(keyboardCrop(crop, 'ArrowRight', { resize: false, largeStep: false }).x).toBeCloseTo(0.205)
    expect(keyboardCrop(crop, 'ArrowDown', { resize: false, largeStep: true }).y).toBeCloseTo(0.225)
    expect(keyboardCrop(crop, 'ArrowRight', { resize: true, largeStep: false }).width).toBeCloseTo(
      0.505,
    )
    expect(keyboardCrop(crop, 'Enter', { resize: false, largeStep: false })).toBe(crop)
  })

  it('normalizes numeric edits without moving the opposite boundary outside the image', () => {
    expect(normalizeCrop({ ...crop, x: 0.9, width: 0.4 })).toMatchObject({ x: 0.6, width: 0.4 })
    expect(normalizeCrop({ ...crop, width: 0, height: 2 })).toMatchObject({
      width: 0.05,
      height: 1,
    })
  })
})
