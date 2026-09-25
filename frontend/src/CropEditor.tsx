import { useEffect, useId, useRef, type KeyboardEvent, type PointerEvent } from 'react'
import { dragCrop, keyboardCrop, type CropHandle, type CropRect } from './crop'

type DragState = {
  handle: CropHandle
  clientX: number
  clientY: number
  crop: CropRect
  latestCrop: CropRect
}

type CropEditorProps = {
  crop: CropRect
  filename: string
  imageUrl: string
  imageWidth: number
  imageHeight: number
  onChange: (crop: CropRect, phase: 'provisional' | 'commit', label: string) => void
}

export function CropEditor({
  crop,
  filename,
  imageUrl,
  imageWidth,
  imageHeight,
  onChange,
}: CropEditorProps) {
  const helpId = `crop-selection-help-${useId().replaceAll(':', '')}`
  const stageRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<DragState | null>(null)
  const keyboardCommitRef = useRef<number | null>(null)
  const keyboardCropRef = useRef<CropRect | null>(null)

  useEffect(() => {
    const move = (event: globalThis.PointerEvent) => {
      const active = dragRef.current
      const stage = stageRef.current
      if (!active || !stage) return
      const bounds = stage.getBoundingClientRect()
      const next = dragCrop(
          active.crop,
          active.handle,
          (event.clientX - active.clientX) / bounds.width,
          (event.clientY - active.clientY) / bounds.height,
        )
      active.latestCrop = next
      onChange(next, 'provisional', active.handle === 'move' ? 'Move crop' : 'Resize crop')
    }
    const finish = () => {
      const active = dragRef.current
      if (active) {
        onChange(
          active.latestCrop,
          'commit',
          active.handle === 'move' ? 'Move crop' : 'Resize crop',
        )
      }
      dragRef.current = null
      document.body.classList.remove('crop-dragging')
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', finish)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', finish)
    }
  }, [onChange])

  useEffect(() => () => {
    if (keyboardCommitRef.current !== null) window.clearTimeout(keyboardCommitRef.current)
  }, [])

  const startDrag = (handle: CropHandle, event: PointerEvent) => {
    event.preventDefault()
    dragRef.current = { handle, clientX: event.clientX, clientY: event.clientY, crop, latestCrop: crop }
    document.body.classList.add('crop-dragging')
  }

  const useKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!event.key.startsWith('Arrow')) return
    event.preventDefault()
    const next = keyboardCrop(crop, event.key, {
        resize: event.altKey,
        largeStep: event.shiftKey,
      })
    const label = event.altKey ? 'Resize crop' : 'Move crop'
    keyboardCropRef.current = next
    onChange(next, 'provisional', label)
    if (keyboardCommitRef.current !== null) window.clearTimeout(keyboardCommitRef.current)
    keyboardCommitRef.current = window.setTimeout(() => {
      const committed = keyboardCropRef.current
      keyboardCommitRef.current = null
      keyboardCropRef.current = null
      if (committed) onChange(committed, 'commit', label)
    }, 300)
  }

  return (
    <div className="crop-surface">
      <div className="crop-caption">
        <span>Source crop</span>
        <span>
          {imageWidth} × {imageHeight} px
        </span>
      </div>
      <div className="crop-stage-wrap">
        <div
          className="crop-stage"
          ref={stageRef}
          style={{
            aspectRatio: `${imageWidth} / ${imageHeight}`,
            width: `min(100%, ${Math.max(160, 620 * (imageWidth / imageHeight))}px)`,
          }}
          data-testid="crop-stage"
        >
          <img src={imageUrl} alt={`Crop source: ${filename}`} draggable={false} />
          {/* The crop is a composite keyboard widget: arrows move it and Alt+arrows resize it. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
          <div
            className="crop-frame"
            role="application"
            aria-label="Crop selection"
            aria-describedby={helpId}
            tabIndex={0}
            onKeyDown={useKeyboard}
            onPointerDown={(event) => startDrag('move', event)}
            style={{
              left: `${crop.x * 100}%`,
              top: `${crop.y * 100}%`,
              width: `${crop.width * 100}%`,
              height: `${crop.height * 100}%`,
            }}
          >
            <span id={helpId} className="visually-hidden">
              Use arrow keys to move, Shift with arrows for larger steps, and Alt with arrows to resize.
            </span>
            <span className="crop-grid crop-grid-v crop-grid-v-one" aria-hidden="true" />
            <span className="crop-grid crop-grid-v crop-grid-v-two" aria-hidden="true" />
            <span className="crop-grid crop-grid-h crop-grid-h-one" aria-hidden="true" />
            <span className="crop-grid crop-grid-h crop-grid-h-two" aria-hidden="true" />
            {(['north-west', 'north-east', 'south-west', 'south-east'] as CropHandle[]).map(
              (handle) => (
                <button
                  className={`crop-handle crop-handle-${handle}`}
                  type="button"
                  tabIndex={-1}
                  aria-label={`Resize crop from ${handle.replace('-', ' ')}`}
                  key={handle}
                  onPointerDown={(event) => {
                    event.stopPropagation()
                    startDrag(handle, event)
                  }}
                />
              ),
            )}
          </div>
        </div>
      </div>
      <p className="keyboard-hint">
        Arrow keys move · Shift moves faster · Option/Alt + arrows resizes
      </p>
    </div>
  )
}
