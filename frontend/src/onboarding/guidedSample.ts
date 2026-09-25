export const GUIDED_SAMPLE_FILENAME = 'image23mf-guided-sample.png'
export const GUIDED_SAMPLE_PROJECT_NAME = 'Guided nozzle-aware sample'

const WIDTH = 1024
const HEIGHT = 768

type Point = readonly [x: number, y: number]

function fillPolygon(
  context: CanvasRenderingContext2D,
  color: string,
  points: readonly Point[],
) {
  context.fillStyle = color
  context.beginPath()
  const [first, ...rest] = points
  context.moveTo(first[0], first[1])
  for (const [x, y] of rest) context.lineTo(x, y)
  context.closePath()
  context.fill()
}

/**
 * Builds a deterministic four-color PNG with deliberately broad physical features.
 * It is generated in-browser so the packaged app does not depend on a filesystem path.
 */
export async function createGuidedSampleFile(): Promise<File> {
  const canvas = document.createElement('canvas')
  canvas.width = WIDTH
  canvas.height = HEIGHT
  const context = canvas.getContext('2d', { alpha: false })
  if (!context) throw new Error('This browser could not prepare the guided sample image.')

  context.imageSmoothingEnabled = false
  context.fillStyle = '#eee2c5'
  context.fillRect(0, 0, WIDTH, HEIGHT)

  context.fillStyle = '#e96b32'
  context.beginPath()
  context.arc(805, 154, 88, 0, Math.PI * 2)
  context.fill()

  fillPolygon(context, '#17498b', [
    [0, 565], [215, 295], [395, 565],
  ])
  fillPolygon(context, '#17498b', [
    [155, 565], [402, 225], [650, 565],
  ])
  fillPolygon(context, '#171a1d', [
    [72, 500], [882, 438], [948, 480], [92, 552],
  ])

  context.fillStyle = '#e96b32'
  context.fillRect(238, 536, 44, 174)
  context.fillRect(492, 514, 44, 196)
  context.fillRect(746, 492, 44, 218)

  context.fillStyle = '#17498b'
  context.fillRect(0, 710, WIDTH, 58)
  context.fillStyle = '#171a1d'
  context.fillRect(0, 744, WIDTH, 24)

  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((result) => {
      if (result) resolve(result)
      else reject(new Error('The guided sample PNG could not be encoded.'))
    }, 'image/png')
  })
  return new File([blob], GUIDED_SAMPLE_FILENAME, {
    type: 'image/png',
    lastModified: 0,
  })
}

export async function importGuidedSample(
  upload: (file: File, projectName: string) => Promise<boolean>,
): Promise<boolean> {
  return upload(await createGuidedSampleFile(), GUIDED_SAMPLE_PROJECT_NAME)
}
