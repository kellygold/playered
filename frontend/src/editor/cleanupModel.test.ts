import { describe, expect, it } from 'vitest'
import type { JobConfigV1, ResolvedPrintabilitySettings } from '../contracts'
import {
  applyCleanupPreset,
  backendCleanupOverrideField,
  backendOverrideDelta,
  changedCleanupFields,
  cleanupEditorModelFromBackend,
  detectCleanupPreset,
  equivalentDiameterMm,
  overriddenFieldsForPreset,
  pixelGuidance,
  profileDefaultValues,
  resetCleanupSection,
  validateCleanupNumericInput,
  type CleanupProfileContext,
} from './cleanupModel'

const recommendationValues = {
  minimum_island_area_mm2: 0.282743,
  minimum_island_diameter_mm: 0.6,
  maximum_tiny_hole_area_mm2: 0.502655,
  maximum_tiny_hole_diameter_mm: 0.8,
  minimum_ring_width_mm: 0.6,
  minimum_line_width_mm: 0.48,
  minimum_neck_width_mm: 0.6,
  minimum_gap_width_mm: 0.5,
  long_line_minimum_length_mm: 1.6,
  smoothing_radius_mm: 0,
} as const

function resolved(overrides: Partial<Record<keyof typeof recommendationValues, number>> = {}): ResolvedPrintabilitySettings {
  return {
    profile_id: 'bbl-p2s-0.4-pla',
    profile_display_name: 'Bambu Lab P2S · 0.4 mm · PLA',
    profile_catalog_fingerprint: 'a'.repeat(64),
    evidence_status: 'pending_print_calibration',
    warning: 'Engineering baseline pending a printed coupon.',
    values: Object.entries(recommendationValues).map(([name, profileValue]) => ({
      name: name as keyof typeof recommendationValues,
      value: overrides[name as keyof typeof recommendationValues] ?? profileValue,
      unit: name.includes('area') ? 'mm2' : 'mm',
      source: name in overrides ? 'user_override' : 'profile',
      profile_value: profileValue,
      profile_basis: 'engineering_baseline',
      profile_confidence: 'provisional',
      rationale: `Rationale for ${name}`,
    })),
  }
}

const config: JobConfigV1 = {
  schema_version: 1,
  source_asset_id: 'asset-1',
  canvas: { width_mm: 200, height_mm: 200 },
  crop: { mode: 'contain', x: 0, y: 0, width: 1, height: 1 },
  printer: {
    profile_catalog_id: 'bambu',
    profile_catalog_version: '1',
    printer_id: 'bbl-p2s',
    nozzle_id: '0.4',
    nozzle_mm: 0.4,
    layer_height_mm: 0.2,
    plate_id: 'textured',
  },
  palette: { colors: [] },
  cleanup: {
    min_island_mm2: 0.4,
    max_hole_mm2: 0.502655,
    smoothing_radius_mm: 0,
    merge_policy: 'review',
    preserve_long_lines: true,
    printability_profile_id: 'bbl-p2s-0.4-pla',
    printability_profile_catalog_fingerprint: 'a'.repeat(64),
    override_fields: ['min_island_mm2'],
  },
  geometry: {
    style: 'flush_inlay',
    base_thickness_mm: 0.8,
    art_thickness_mm: 0.4,
    corner_radius_mm: 2,
  },
}

function profile(): CleanupProfileContext {
  return cleanupEditorModelFromBackend(config, resolved(), 0.4).profile
}

describe('cleanup editor model', () => {
  it('maps config and resolved calibration settings without losing explicit override provenance', () => {
    const model = cleanupEditorModelFromBackend(
      config,
      resolved({ minimum_neck_width_mm: 0.72 }),
      0.4,
    )

    expect(model.values.minimum_island_area_mm2).toBe(0.4)
    expect(model.values.minimum_neck_width_mm).toBe(0.72)
    expect(model.overriddenFields).toEqual(expect.arrayContaining([
      'minimum_island_area_mm2',
      'minimum_neck_width_mm',
    ]))
    expect(model.profile.islandDiameterMm).toBe(0.6)
    expect(model.profile.evidenceStatus).toBe('pending_print_calibration')
  })

  it('throws for incomplete resolved settings instead of silently inventing a value', () => {
    const incomplete = resolved()
    incomplete.values = incomplete.values.filter((value) => value.name !== 'minimum_gap_width_mm')
    expect(() => cleanupEditorModelFromBackend(config, incomplete, 0.4)).toThrow(
      'missing minimum_gap_width_mm',
    )
  })

  it('builds deterministic profile, fine-detail, and reliable presets', () => {
    const currentProfile = profile()
    const defaults = applyCleanupPreset('profile', currentProfile)
    const fine = applyCleanupPreset('fine_detail', currentProfile)
    const reliable = applyCleanupPreset('reliable', currentProfile)

    expect(defaults).toEqual(profileDefaultValues(currentProfile))
    expect(fine.minimum_island_area_mm2).toBe(0.212)
    expect(fine.minimum_line_width_mm).toBe(0.36)
    expect(fine.smoothing_radius_mm).toBe(0)
    expect(reliable.minimum_island_area_mm2).toBe(0.353)
    expect(reliable.smoothing_radius_mm).toBe(0.1)
    expect(applyCleanupPreset('reliable', currentProfile)).toEqual(reliable)
  })

  it('detects exact presets and labels arbitrary edits custom', () => {
    const currentProfile = profile()
    const defaults = profileDefaultValues(currentProfile)
    expect(detectCleanupPreset(defaults, currentProfile)).toBe('profile')
    expect(detectCleanupPreset(applyCleanupPreset('fine_detail', currentProfile), currentProfile)).toBe('fine_detail')
    expect(detectCleanupPreset({ ...defaults, minimum_gap_width_mm: 0.77 }, currentProfile)).toBe('custom')
  })

  it('resets only the requested section and reports changed fields', () => {
    const currentProfile = profile()
    const defaults = profileDefaultValues(currentProfile)
    const edited = {
      ...defaults,
      minimum_island_area_mm2: 2,
      minimum_gap_width_mm: 1,
    }
    const reset = resetCleanupSection(edited, 'regions', currentProfile)

    expect(reset.minimum_island_area_mm2).toBe(defaults.minimum_island_area_mm2)
    expect(reset.minimum_gap_width_mm).toBe(1)
    expect(changedCleanupFields(edited, reset)).toEqual(['minimum_island_area_mm2'])
  })

  it('identifies only preset values that differ from profile defaults as overrides', () => {
    const currentProfile = profile()
    const reliable = applyCleanupPreset('reliable', currentProfile)
    const fields = overriddenFieldsForPreset(reliable, currentProfile)
    expect(fields).toContain('minimum_line_width_mm')
    expect(fields).toContain('smoothing_radius_mm')
    expect(fields).not.toContain('preserve_long_lines')
  })

  it('validates finite numeric input and clamps to physical bounds', () => {
    expect(validateCleanupNumericInput('minimum_line_width_mm', '')).toMatchObject({ valid: false })
    expect(validateCleanupNumericInput('minimum_line_width_mm', 'not-a-number')).toMatchObject({ valid: false })
    expect(validateCleanupNumericInput('minimum_line_width_mm', '-4')).toMatchObject({
      valid: true,
      value: 0,
      clamped: true,
    })
    expect(validateCleanupNumericInput('minimum_line_width_mm', '0.477')).toMatchObject({
      value: 0.48,
      clamped: true,
    })
  })

  it('converts area to an equivalent diameter', () => {
    expect(equivalentDiameterMm(Math.PI)).toBeCloseTo(2)
    expect(equivalentDiameterMm(-1)).toBe(0)
  })

  it('derives isotropic and anisotropic pixel guidance from both physical axes', () => {
    expect(pixelGuidance('minimum_line_width_mm', 0.5, {
      mmPerPixelX: 0.1,
      mmPerPixelY: 0.1,
    })).toBe('About 5.0 px in the current preview.')
    expect(pixelGuidance('minimum_line_width_mm', 0.5, {
      mmPerPixelX: 0.1,
      mmPerPixelY: 0.2,
    })).toBe('About 5.0 px horizontally or 2.5 px vertically.')
    expect(pixelGuidance('minimum_island_area_mm2', Math.PI, {
      mmPerPixelX: 0.1,
      mmPerPixelY: 0.2,
    })).toContain('20 px wide × 10 px tall')
    expect(pixelGuidance('minimum_gap_width_mm', 0.5, null)).toBeNull()
  })

  it('maps every editable calibrated field to the backend override contract', () => {
    expect(backendCleanupOverrideField('minimum_island_area_mm2')).toBe('min_island_mm2')
    expect(backendCleanupOverrideField('minimum_gap_width_mm')).toBe('minimum_gap_width_mm')
    expect(backendCleanupOverrideField('preserve_long_lines')).toBeNull()
    expect(backendOverrideDelta(
      ['minimum_island_area_mm2', 'minimum_gap_width_mm'],
      ['smoothing_radius_mm'],
    )).toEqual({
      set: ['min_island_mm2', 'minimum_gap_width_mm'],
      clear: ['smoothing_radius_mm'],
    })
  })
})
