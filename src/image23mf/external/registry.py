"""Version-aware discovery for supported local tools."""

import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from image23mf.external.runner import ToolRunner, ToolRunnerError


class ToolId(str, Enum):
    POTRACE = "potrace"
    OPENSCAD = "openscad"
    BAMBU_STUDIO = "bambu-studio"


@dataclass(frozen=True)
class ToolSpec:
    id: ToolId
    name: str
    purpose: str
    candidates: tuple[str, ...]
    version_arguments: tuple[str, ...]
    version_pattern: str
    minimum_version: tuple[int, ...]
    allow_nonzero_version_probe: bool = False


@dataclass(frozen=True)
class ToolDetection:
    id: ToolId
    name: str
    purpose: str
    detected: bool
    available: bool
    compatible: bool
    path: Optional[Path]
    version: Optional[str]
    unavailable_reason: Optional[str]


DEFAULT_TOOL_SPECS = {
    ToolId.POTRACE: ToolSpec(
        id=ToolId.POTRACE,
        name="Potrace",
        purpose="Binary mask vectorization",
        candidates=("potrace", "/opt/homebrew/bin/potrace"),
        version_arguments=("--version",),
        version_pattern=r"potrace\s+(\d+(?:\.\d+)+)",
        minimum_version=(1, 16),
    ),
    ToolId.OPENSCAD: ToolSpec(
        id=ToolId.OPENSCAD,
        name="OpenSCAD",
        purpose="Reference geometry adapter",
        candidates=("openscad", "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"),
        version_arguments=("--version",),
        version_pattern=r"OpenSCAD\s+version\s+(\d+(?:\.\d+)+)",
        minimum_version=(2021, 1),
    ),
    ToolId.BAMBU_STUDIO: ToolSpec(
        id=ToolId.BAMBU_STUDIO,
        name="Bambu Studio",
        purpose="3MF slice validation",
        candidates=(
            "bambu-studio",
            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
        ),
        version_arguments=("--version",),
        version_pattern=r"BambuStudio-(\d+(?:\.\d+)+)",
        # 2.4 crashes in headless slicing; desktop beta is validated with 2.8.
        minimum_version=(2, 8),
        allow_nonzero_version_probe=True,
    ),
}


class ExternalToolRegistry:
    def __init__(
        self,
        *,
        runner: Optional[ToolRunner] = None,
        specs: Mapping[ToolId, ToolSpec] = DEFAULT_TOOL_SPECS,
        version_timeout_seconds: float = 5,
    ) -> None:
        self.runner = runner or ToolRunner()
        self.specs = dict(specs)
        self.version_timeout_seconds = version_timeout_seconds

    def detect_all(self) -> tuple[ToolDetection, ...]:
        return tuple(self.detect(tool_id) for tool_id in self.specs)

    def detect(self, tool_id: ToolId) -> ToolDetection:
        spec = self.specs[tool_id]
        path = _first_existing(spec.candidates)
        if path is None:
            return ToolDetection(
                id=spec.id,
                name=spec.name,
                purpose=spec.purpose,
                detected=False,
                available=False,
                compatible=False,
                path=None,
                version=None,
                unavailable_reason="Executable was not found.",
            )
        try:
            result = self.runner.run(
                path,
                spec.version_arguments,
                timeout_seconds=self.version_timeout_seconds,
                check=False,
            )
        except ToolRunnerError as error:
            return ToolDetection(
                id=spec.id,
                name=spec.name,
                purpose=spec.purpose,
                detected=True,
                available=False,
                compatible=False,
                path=path,
                version=None,
                unavailable_reason=str(error),
            )
        combined_output = f"{result.stdout}\n{result.stderr}"
        match = re.search(spec.version_pattern, combined_output, flags=re.IGNORECASE)
        if match is None:
            return ToolDetection(
                id=spec.id,
                name=spec.name,
                purpose=spec.purpose,
                detected=True,
                available=False,
                compatible=False,
                path=path,
                version=None,
                unavailable_reason="The executable version could not be determined.",
            )
        version = match.group(1)
        compatible = _version_tuple(version) >= spec.minimum_version
        successful_probe = result.return_code == 0 or spec.allow_nonzero_version_probe
        available = compatible and successful_probe
        if not successful_probe:
            reason = f"Version probe exited with status {result.return_code}."
        elif not compatible:
            required = ".".join(str(part) for part in spec.minimum_version)
            reason = f"Version {version} is older than required version {required}."
        else:
            reason = None
        return ToolDetection(
            id=spec.id,
            name=spec.name,
            purpose=spec.purpose,
            detected=True,
            available=available,
            compatible=compatible,
            path=path,
            version=version,
            unavailable_reason=reason,
        )


def _first_existing(candidates: tuple[str, ...]) -> Optional[Path]:
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            return Path(executable).expanduser().resolve()
        path = Path(candidate).expanduser()
        if path.is_file() and path.stat().st_mode & 0o111:
            return path.resolve()
    return None


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))
