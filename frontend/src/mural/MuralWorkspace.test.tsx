import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MuralWorkspace } from './MuralWorkspace'
import type { MuralBuildTransport } from './MuralBuildCoordinator'

vi.mock('./MuralPlanner', () => ({
  MuralPlanner: ({ onPlanStateChange }: {
    onPlanStateChange: (state: unknown) => void
  }) => (
    <button
      type="button"
      onClick={() => onPlanStateChange({
        plan: { generation: 4, plan: { request_fingerprint: 'a'.repeat(64) } },
        dirty: false,
        hydrated: true,
      })}
    >
      Simulate saved mural plan
    </button>
  ),
}))

vi.mock('./MuralBuildWorkflow', () => ({
  MuralBuildWorkflow: ({ plan, planDirty, previewCurrent, previewJobId }: {
    plan: { generation: number } | null
    planDirty: boolean
    previewCurrent: boolean
    previewJobId: string | null
  }) => (
    <div data-testid="mural-build-binding">
      {plan?.generation ?? 'none'} · {String(planDirty)} · {String(previewCurrent)} · {previewJobId}
    </div>
  ),
}))

afterEach(cleanup)

describe('MuralWorkspace', () => {
  it('feeds only a hydrated saved planner generation into the build workflow', () => {
    render(
      <MuralWorkspace
        projectId="project-a"
        processedArtifactId="master-a"
        masterImageUrl="/master.png"
        previewJobId="preview-a"
        expectedDraftGeneration={8}
        previewCurrent
        profile={{
          printer_model: 'Bambu Lab P2S',
          nozzle_diameter_mm: 0.4,
          layer_height_mm: 0.2,
          bed_type: 'Textured PEI Plate',
        }}
        transport={{} as MuralBuildTransport}
      />,
    )

    expect(screen.getByTestId('mural-build-binding')).toHaveTextContent(
      'none · true · true · preview-a',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Simulate saved mural plan' }))
    expect(screen.getByTestId('mural-build-binding')).toHaveTextContent(
      '4 · false · true · preview-a',
    )
  })
})
