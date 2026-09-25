from dataclasses import replace
from pathlib import Path

import pytest

from image23mf.api.capabilities import discover_capabilities
from image23mf.external import ExternalToolRegistry, ToolId, ToolRunner, ToolSpec
from image23mf.external.registry import DEFAULT_TOOL_SPECS


def executable(tmp_path: Path, output: str, *, exit_code: int = 0) -> Path:
    path = tmp_path / "fixture-tool"
    path.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def spec(path: Path, *, minimum: tuple[int, ...] = (1, 0)) -> ToolSpec:
    return ToolSpec(
        id=ToolId.POTRACE,
        name="Fixture",
        purpose="Contract testing",
        candidates=(str(path),),
        version_arguments=("--version",),
        version_pattern=r"Fixture (\d+(?:\.\d+)+)",
        minimum_version=minimum,
    )


def test_present_tool_reports_path_version_and_compatibility(tmp_path) -> None:
    path = executable(tmp_path, "Fixture 2.3.4")
    registry = ExternalToolRegistry(
        runner=ToolRunner(temp_root=tmp_path / "runs"), specs={ToolId.POTRACE: spec(path)}
    )

    capability = discover_capabilities(registry)[0]

    assert capability == {
        "id": "potrace",
        "name": "Fixture",
        "detected": True,
        "available": True,
        "compatible": True,
        "path": str(path.resolve()),
        "purpose": "Contract testing",
        "version": "2.3.4",
        "unavailable_reason": None,
    }


def test_missing_and_incompatible_tools_never_claim_availability(tmp_path) -> None:
    missing_spec = spec(tmp_path / "missing")
    old_path = executable(tmp_path, "Fixture 1.5")
    incompatible_spec = spec(old_path, minimum=(2, 0))

    missing = ExternalToolRegistry(specs={ToolId.POTRACE: missing_spec}).detect(ToolId.POTRACE)
    incompatible = ExternalToolRegistry(specs={ToolId.POTRACE: incompatible_spec}).detect(
        ToolId.POTRACE
    )

    assert not missing.detected
    assert not missing.available
    assert missing.unavailable_reason == "Executable was not found."
    assert incompatible.detected
    assert not incompatible.available
    assert not incompatible.compatible
    assert "older than required" in (incompatible.unavailable_reason or "")


def test_nonzero_version_probe_is_unavailable_unless_spec_explicitly_allows_it(tmp_path) -> None:
    path = executable(tmp_path, "Fixture 3.0", exit_code=7)
    strict = spec(path)
    allowed = ToolSpec(**{**strict.__dict__, "allow_nonzero_version_probe": True})

    rejected = ExternalToolRegistry(specs={ToolId.POTRACE: strict}).detect(ToolId.POTRACE)
    accepted = ExternalToolRegistry(specs={ToolId.POTRACE: allowed}).detect(ToolId.POTRACE)

    assert not rejected.available
    assert rejected.compatible
    assert rejected.unavailable_reason == "Version probe exited with status 7."
    assert accepted.available
    assert accepted.version == "3.0"


@pytest.mark.parametrize("version,available", [("02.04.00.70", False), ("02.08.02.61", True)])
def test_bambu_beta_requires_supported_slicer_before_building(tmp_path, version, available):
    path = executable(tmp_path, "BambuStudio-" + version, exit_code=1)
    configured = replace(DEFAULT_TOOL_SPECS[ToolId.BAMBU_STUDIO], candidates=(str(path),))
    detected = ExternalToolRegistry(specs={ToolId.BAMBU_STUDIO: configured}).detect(
        ToolId.BAMBU_STUDIO
    )
    assert detected.available is available
    if not available:
        assert "older than required version 2.8" in detected.unavailable_reason
