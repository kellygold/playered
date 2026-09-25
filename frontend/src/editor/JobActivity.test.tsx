import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { JobResource } from '../contracts'
import { JobActivity } from './JobActivity'

const job = {
  id: 'job-1', state: 'running', stage: 'slicing', progress: 0.64,
  started_at: '2026-09-16T00:00:00Z', created_at: '2026-09-16T00:00:00Z', finished_at: null,
} as JobResource

afterEach(() => { cleanup(); vi.useRealTimers() })

it('keeps elapsed time moving without inventing progress during a long slicer stage', () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-16T00:01:00Z'))
  const { rerender, unmount } = render(<JobActivity job={job} />)
  expect(screen.getByText('Elapsed · 1m 0s')).toBeVisible()
  expect(screen.getByText(/Bambu Studio is slicing/)).toBeVisible()
  act(() => vi.advanceTimersByTime(45000))
  expect(screen.getByText('Elapsed · 1m 45s')).toBeVisible()
  expect(screen.getByRole('progressbar')).toHaveAttribute('value', '0.64')
  rerender(<JobActivity job={{ ...job, state: 'succeeded', progress: 1, finished_at: '2026-09-16T00:01:47Z' }} />)
  act(() => vi.advanceTimersByTime(10000))
  expect(screen.getByText('Elapsed · 1m 47s')).toBeVisible()
  expect(vi.getTimerCount()).toBe(0)
  unmount()
})

it('does not invent elapsed time or a percentage before a job exists', () => {
  render(<JobActivity job={null} />)
  expect(screen.getByRole('progressbar')).not.toHaveAttribute('value')
  expect(screen.queryByText(/Elapsed ·/)).not.toBeInTheDocument()
})

it('distinguishes queued time from processing and resets for a new job', () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-16T00:01:00Z'))
  const { rerender } = render(<JobActivity job={{ ...job, state: 'queued', started_at: null }} />)
  expect(screen.getByText('Waiting for a worker · 1m 0s')).toBeVisible()
  rerender(<JobActivity job={{ ...job, id: 'job-2', started_at: '2026-09-16T00:00:59Z' }} />)
  expect(screen.getByText('Elapsed · 1s')).toBeVisible()
})
