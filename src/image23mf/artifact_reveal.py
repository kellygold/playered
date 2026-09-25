"""Safe, project-scoped reveal of retained export packages in macOS Finder."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from image23mf.external.runner import ToolRunner, ToolRunnerError
from image23mf.storage import (
    ArtifactRepository,
    ContentAddressedStore,
    RecordNotFoundError,
    StoredBlob,
)

FINDER_EXECUTABLE = Path("/usr/bin/open")
REVEALABLE_ARTIFACT_KINDS = frozenset({"bambu-3mf"})


class ArtifactRevealError(RuntimeError):
    """Base class for safe reveal failures."""


class ArtifactNotRevealableError(ArtifactRevealError):
    """The artifact is owned by the project but is not a retained export package."""


class ArtifactBlobUnavailableError(ArtifactRevealError):
    """The retained artifact bytes are missing or fail integrity verification."""


class FinderRevealFailedError(ArtifactRevealError):
    """Finder could not reveal an otherwise valid retained export package."""


@dataclass(frozen=True)
class ArtifactRevealResult:
    artifact_id: str
    supported: bool
    revealed: bool
    reason: str | None = None


class FinderOpener:
    """Invoke Finder without a shell or accepting a browser-supplied path."""

    def __init__(self, runner: ToolRunner | None = None) -> None:
        self.runner = runner or ToolRunner()

    def reveal(self, path: Path) -> None:
        try:
            self.runner.run(
                FINDER_EXECUTABLE,
                ("-R", "--", str(path.resolve())),
                timeout_seconds=10,
            )
        except ToolRunnerError as error:
            raise FinderRevealFailedError("Finder could not reveal the export package.") from error


def reveal_artifact_in_finder(
    *,
    connection,
    store: ContentAddressedStore,
    project_id: str,
    artifact_id: str,
    platform_name: str | None = None,
    opener: FinderOpener | None = None,
) -> ArtifactRevealResult:
    """Verify ownership and bytes before asking Finder to reveal one package."""

    artifacts = ArtifactRepository(connection)
    artifact = artifacts.get(artifact_id)
    if artifacts.owner_project_id(artifact_id) != project_id:
        # Match the ordinary artifact download boundary and do not leak foreign IDs.
        raise RecordNotFoundError(f"artifact not found: {artifact_id}")
    if artifact.kind not in REVEALABLE_ARTIFACT_KINDS:
        raise ArtifactNotRevealableError(
            f"artifact kind is not a revealable export package: {artifact.kind}"
        )

    blob = StoredBlob(
        sha256=artifact.sha256,
        relative_path=artifact.relative_path,
        byte_size=artifact.byte_size,
        media_type=artifact.media_type,
        extension=Path(artifact.relative_path).suffix,
    )
    try:
        valid = store.verify(blob)
    except FileNotFoundError:
        valid = False
    if not valid:
        raise ArtifactBlobUnavailableError(
            "The export record exists but its retained package is missing or corrupt."
        )

    effective_platform = platform_name if platform_name is not None else sys.platform
    if effective_platform != "darwin":
        return ArtifactRevealResult(
            artifact_id=artifact_id,
            supported=False,
            revealed=False,
            reason="Reveal in Finder is available only on macOS.",
        )

    (opener or FinderOpener()).reveal(store.path_for(artifact.relative_path))
    return ArtifactRevealResult(artifact_id=artifact_id, supported=True, revealed=True)
