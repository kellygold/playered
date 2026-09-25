import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CropEditor } from './CropEditor'

afterEach(cleanup)

describe('CropEditor semantic gestures', () => {
  it('previews every pointer move but commits one named step at pointerup', () => {
    const onChange = vi.fn()
    render(
      <CropEditor
        crop={{ mode: 'cover', x: 0, y: 0, width: 0.8, height: 0.8 }}
        filename="art.png"
        imageUrl="/art.png"
        imageWidth={100}
        imageHeight={100}
        onChange={onChange}
      />,
    )
    const stage = screen.getByTestId('crop-stage')
    vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 100,
      width: 100, height: 100, toJSON: () => ({}),
    })
    const frame = screen.getByRole('application', { name: 'Crop selection' })
    expect(frame).toHaveAccessibleDescription(/Alt with arrows to resize/)
    fireEvent.pointerDown(frame, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 15, clientY: 15 })
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 20, clientY: 20 })
    fireEvent.pointerUp(window, { pointerId: 1, clientX: 20, clientY: 20 })

    expect(onChange.mock.calls.filter((call) => call[1] === 'provisional')).toHaveLength(2)
    const commits = onChange.mock.calls.filter((call) => call[1] === 'commit')
    expect(commits).toHaveLength(1)
    expect(commits[0][2]).toBe('Move crop')
    expect(commits[0][0]).toMatchObject({ x: 0.1, y: 0.1 })
  })
})
