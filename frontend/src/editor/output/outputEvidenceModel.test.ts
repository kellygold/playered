import { describe, expect, it } from 'vitest'
import { formatArtifactBytes, validationWarnings } from './outputEvidenceModel'

describe('output evidence model', () => {
  it('reads legacy and structured warning evidence without duplicates', () => {
    expect(validationWarnings({
      warnings: ['Top-level warning'],
      result: {
        warnings: ['Top-level warning', 'Legacy warning'],
        report: {
          warnings: [
            { category: 'floating_region', message: 'Floating region detected.' },
            { category: 'floating_region', message: 'Floating region detected.' },
          ],
        },
      },
      slicer_report: {
        warnings: [{ category: 'support_required', message: 'Support warning.' }],
      },
    })).toEqual([
      { category: null, message: 'Top-level warning' },
      { category: null, message: 'Legacy warning' },
      { category: 'floating region', message: 'Floating region detected.' },
      { category: 'support required', message: 'Support warning.' },
    ])
  })

  it('formats a human size while retaining exact bytes', () => {
    expect(formatArtifactBytes(42)).toBe('42 B (42 bytes)')
    expect(formatArtifactBytes(2048)).toBe('2.00 KiB (2048 bytes)')
    expect(formatArtifactBytes(2 * 1024 * 1024)).toBe('2.00 MiB (2097152 bytes)')
  })
})
