import type { JobConfigV1 } from './contracts'

export type CropRect = JobConfigV1['crop']
export type CropHandle = 'move' | 'north-west' | 'north-east' | 'south-west' | 'south-east'

export const MIN_CROP_SIZE = 0.05

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.min(maximum, Math.max(minimum, value))

export function normalizeCrop(crop: CropRect): CropRect {
  const width = clamp(crop.width, MIN_CROP_SIZE, 1)
  const height = clamp(crop.height, MIN_CROP_SIZE, 1)
  return {
    ...crop,
    x: clamp(crop.x, 0, 1 - width),
    y: clamp(crop.y, 0, 1 - height),
    width,
    height,
  }
}

export function dragCrop(
  crop: CropRect,
  handle: CropHandle,
  deltaX: number,
  deltaY: number,
): CropRect {
  const right = crop.x + crop.width
  const bottom = crop.y + crop.height
  if (handle === 'move') {
    return normalizeCrop({
      ...crop,
      x: crop.x + deltaX,
      y: crop.y + deltaY,
    })
  }
  const west = handle.endsWith('west')
  const north = handle.startsWith('north')
  const x = west ? clamp(crop.x + deltaX, 0, right - MIN_CROP_SIZE) : crop.x
  const y = north ? clamp(crop.y + deltaY, 0, bottom - MIN_CROP_SIZE) : crop.y
  const nextRight = west ? right : clamp(right + deltaX, crop.x + MIN_CROP_SIZE, 1)
  const nextBottom = north ? bottom : clamp(bottom + deltaY, crop.y + MIN_CROP_SIZE, 1)
  return normalizeCrop({
    ...crop,
    x,
    y,
    width: nextRight - x,
    height: nextBottom - y,
  })
}

export function keyboardCrop(
  crop: CropRect,
  key: string,
  options: { resize: boolean; largeStep: boolean },
): CropRect {
  const step = options.largeStep ? 0.025 : 0.005
  const delta = {
    ArrowLeft: [-step, 0],
    ArrowRight: [step, 0],
    ArrowUp: [0, -step],
    ArrowDown: [0, step],
  }[key]
  if (!delta) return crop
  if (options.resize) {
    return dragCrop(crop, 'south-east', delta[0], delta[1])
  }
  return dragCrop(crop, 'move', delta[0], delta[1])
}

export function replaceCrop(config: JobConfigV1, crop: CropRect): JobConfigV1 {
  return { ...config, crop: normalizeCrop(crop) }
}
