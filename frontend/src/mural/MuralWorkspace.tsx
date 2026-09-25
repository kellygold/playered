import { useState } from 'react'
import type { ExportProfileSettings } from '../contracts'
import { MuralBuildWorkflow } from './MuralBuildWorkflow'
import type { MuralBuildTransport } from './MuralBuildCoordinator'
import { muralBuildTransport } from './muralApi'
import { MuralPlanner, type MuralPlannerState } from './MuralPlanner'
import './MuralWorkspace.css'

export type MuralWorkspaceProps = {
  projectId: string
  processedArtifactId: string
  masterImageUrl: string
  previewJobId: string | null
  expectedDraftGeneration: number
  previewCurrent: boolean
  profile: ExportProfileSettings
  disabled?: boolean
  disabledReason?: string
  transport?: MuralBuildTransport
}

const EMPTY_STATE: MuralPlannerState = {
  plan: null,
  dirty: true,
  hydrated: false,
}

export function MuralWorkspace({
  projectId,
  processedArtifactId,
  masterImageUrl,
  previewJobId,
  expectedDraftGeneration,
  previewCurrent,
  profile,
  disabled = false,
  disabledReason,
  transport = muralBuildTransport,
}: MuralWorkspaceProps) {
  const [planner, setPlanner] = useState<MuralPlannerState>(EMPTY_STATE)

  return (
    <div className="mural-workspace">
      <MuralPlanner
        projectId={projectId}
        processedArtifactId={processedArtifactId}
        masterImageUrl={masterImageUrl}
        disabled={disabled}
        onPlanStateChange={setPlanner}
      />
      <MuralBuildWorkflow
        projectId={projectId}
        processedArtifactId={processedArtifactId}
        masterImageUrl={masterImageUrl}
        previewJobId={previewJobId}
        expectedDraftGeneration={expectedDraftGeneration}
        previewCurrent={previewCurrent}
        profile={profile}
        plan={planner.plan}
        planDirty={!planner.hydrated || planner.dirty}
        transport={transport}
        disabled={disabled}
        disabledReason={disabledReason}
      />
    </div>
  )
}
