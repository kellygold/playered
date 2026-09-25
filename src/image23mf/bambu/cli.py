"""Bambu Studio CLI slicing validation with retained, structured evidence.

The adapter deliberately treats a successful process exit as necessary but not sufficient.
Validation succeeds only when Bambu Studio writes the requested sliced ``.gcode.3mf`` and that
archive contains at least one non-empty plate G-code stream with extrusion motion.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import dataclasses
import hashlib
import json
import math
import re
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Optional
from xml.etree import ElementTree as ET

from image23mf.bambu.report import (
    SlicerBounds,
    SlicerPlateReport,
    SlicerValidationExpectation,
    SlicerValidationReport,
    build_slicer_validation_report,
    parse_slicer_plate,
)
from image23mf.external.registry import ExternalToolRegistry, ToolId
from image23mf.external.runner import (
    CancellationToken,
    ToolCanceledError,
    ToolRunner,
    ToolRunResult,
    ToolTimeoutError,
    ToolUnavailableError,
)

DEFAULT_BAMBU_SLICE_TIMEOUT_SECONDS = 10 * 60.0
MAX_GCODE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
PLATE_GCODE_PATTERN = re.compile(r"^Metadata/plate_[1-9][0-9]*\.gcode$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROJECT_SETTINGS_MEMBER = "Metadata/project_settings.config"
MURAL_MANIFEST_MEMBER = "Metadata/image23mf_mural.json"
SLICE_INFO_MEMBER = "Metadata/slice_info.config"
WARNING_PATTERN = re.compile(r"\bwarn(?:ing)?\b", flags=re.IGNORECASE)
GCODE_XYE_PATTERN = re.compile(r"(?:^|\s)([XYE])([-+]?(?:\d+(?:\.\d*)?|\.\d+))")


class BambuValidationStatus(str, Enum):
    VALIDATED = "validated"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"
    UNAVAILABLE = "unavailable"
    PROFILE_ERROR = "profile_error"
    INVALID_ARTIFACT = "invalid_artifact"


@dataclass(frozen=True)
class PinnedBambuProfile:
    """A local Bambu profile whose exact bytes were approved by the caller."""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        if not SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("pinned Bambu profile sha256 must be 64 lowercase hex characters")

    @classmethod
    def pin(cls, path: Path) -> PinnedBambuProfile:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f"Bambu profile is missing or not a file: {resolved}")
        return cls(path=resolved, sha256=_file_sha256(resolved))


@dataclass(frozen=True)
class BambuSliceProfiles:
    """Pinned machine/process profiles and one filament profile per extruder."""

    machine: PinnedBambuProfile
    process: PinnedBambuProfile
    filaments: tuple[PinnedBambuProfile, ...]

    def __post_init__(self) -> None:
        if not self.filaments:
            raise ValueError("at least one pinned Bambu filament profile is required")


@dataclass(frozen=True)
class BambuProfileEvidence:
    role: str
    extruder: Optional[int]
    source_path: Path
    expected_sha256: str
    actual_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class SlicedArtifactEvidence:
    """Stable facts retained after the temporary sliced artifact is removed."""

    name: str
    sha256: str
    size_bytes: int
    gcode_members: tuple[str, ...]
    uncompressed_gcode_bytes: int
    plates: tuple[SlicerPlateReport, ...] = ()
    plate_names: tuple[Optional[str], ...] = ()
    prime_tower_bounds: tuple[Optional[SlicerBounds], ...] = ()


@dataclass(frozen=True)
class BambuValidationResult:
    status: BambuValidationStatus
    source_sha256: Optional[str]
    executable: Optional[Path]
    version: Optional[str]
    command: tuple[str, ...]
    return_code: Optional[int]
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    canceled: bool
    warnings: tuple[str, ...]
    profiles: tuple[BambuProfileEvidence, ...] = ()
    profile_set_sha256: Optional[str] = None
    cache_key: Optional[str] = None
    artifact: Optional[SlicedArtifactEvidence] = None
    report: Optional[SlicerValidationReport] = None
    preparation_command: tuple[str, ...] = ()
    preparation_stdout: str = ""
    preparation_stderr: str = ""


class BambuStudioValidationError(RuntimeError):
    """Base error for a Bambu validation attempt with structured evidence."""

    def __init__(self, message: str, result: BambuValidationResult) -> None:
        super().__init__(message)
        self.result = result


class BambuStudioUnavailableError(BambuStudioValidationError):
    pass


class BambuStudioSliceError(BambuStudioValidationError):
    pass


class BambuStudioProfileError(BambuStudioValidationError):
    pass


class BambuStudioTimeoutError(BambuStudioValidationError):
    pass


class BambuStudioCanceledError(BambuStudioValidationError):
    pass


class InvalidSlicedArtifactError(BambuStudioValidationError):
    pass


@dataclass(frozen=True)
class _StagedProfiles:
    machine: Path
    process: Path
    filaments: tuple[Path, ...]


class _ProfilePreparationError(ValueError):
    def __init__(self, message: str, evidence: tuple[BambuProfileEvidence, ...] = ()) -> None:
        super().__init__(message)
        self.evidence = evidence


class BambuStudioCliValidator:
    """Slice one 3MF in an isolated workspace and retain validation evidence."""

    def __init__(
        self,
        *,
        runner: Optional[ToolRunner] = None,
        registry: Optional[ExternalToolRegistry] = None,
        timeout_seconds: float = DEFAULT_BAMBU_SLICE_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.runner = runner or ToolRunner()
        self.registry = registry or ExternalToolRegistry(runner=self.runner)
        self.timeout_seconds = timeout_seconds

    def validate(
        self,
        source: Path,
        *,
        profiles: Optional[BambuSliceProfiles] = None,
        expectation: Optional[SlicerValidationExpectation] = None,
        cancellation: Optional[CancellationToken] = None,
        project_output: Optional[Path] = None,
        sliced_output: Optional[Path] = None,
    ) -> BambuValidationResult:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"source 3MF is missing or not a file: {source}")
        if cancellation is not None and cancellation.canceled:
            result = _canceled_before_launch_result()
            raise BambuStudioCanceledError(
                "Bambu Studio slicing was canceled before launch.", result
            )

        detection = self.registry.detect(ToolId.BAMBU_STUDIO)
        if cancellation is not None and cancellation.canceled:
            result = _canceled_before_launch_result(
                executable=detection.path,
                version=detection.version,
            )
            raise BambuStudioCanceledError(
                "Bambu Studio slicing was canceled before launch.", result
            )
        if not detection.available or detection.path is None:
            reason = detection.unavailable_reason or "Bambu Studio is unavailable."
            result = BambuValidationResult(
                status=BambuValidationStatus.UNAVAILABLE,
                source_sha256=None,
                executable=detection.path,
                version=detection.version,
                command=(),
                return_code=None,
                stdout="",
                stderr="",
                duration_seconds=0,
                timed_out=False,
                canceled=False,
                warnings=(),
            )
            raise BambuStudioUnavailableError(reason, result)
        if profiles is None:
            result = BambuValidationResult(
                status=BambuValidationStatus.PROFILE_ERROR,
                source_sha256=None,
                executable=detection.path,
                version=detection.version,
                command=(),
                return_code=None,
                stdout="",
                stderr="",
                duration_seconds=0,
                timed_out=False,
                canceled=False,
                warnings=(),
            )
            raise BambuStudioProfileError(
                "Bambu slicing requires pinned machine, process, and filament profiles.",
                result,
            )

        with self.runner.temporary_workspace(prefix="bambu-slice-") as workspace:
            source_sha256, staged_source = _stage_source(source, workspace)
            try:
                tower_arguments = _single_plate_tower_arguments(staged_source)
                extruder_count = _project_extruder_count(staged_source)
                staged_profiles, profile_evidence, profile_set_sha256 = _stage_profiles(
                    profiles,
                    extruder_count=extruder_count,
                    workspace=workspace,
                )
                staged_profiles = _apply_mural_process_overrides(
                    staged_source,
                    staged_profiles,
                    workspace=workspace,
                )
            except _ProfilePreparationError as error:
                result = BambuValidationResult(
                    status=BambuValidationStatus.PROFILE_ERROR,
                    source_sha256=source_sha256,
                    executable=detection.path,
                    version=detection.version,
                    command=(),
                    return_code=None,
                    stdout="",
                    stderr="",
                    duration_seconds=0,
                    timed_out=False,
                    canceled=False,
                    warnings=(),
                    profiles=error.evidence,
                )
                raise BambuStudioProfileError(str(error), result) from error
            output_directory = workspace / "output"
            output_directory.mkdir()
            output_name = f"slice-{source_sha256[:24]}.gcode.3mf"
            output_path = output_directory / output_name
            cache_key = _validation_cache_key(
                source_sha256=source_sha256,
                profile_set_sha256=profile_set_sha256,
                bambu_version=detection.version,
            )
            filament_color_arguments: tuple[str, ...] = ()
            project_colors: tuple[str, ...] = ()
            if project_output is not None:
                with zipfile.ZipFile(staged_source) as archive:
                    project_colors = tuple(
                        json.loads(archive.read(PROJECT_SETTINGS_MEMBER)).get("filament_colour", [])
                    )
            colors = project_colors or (expectation.expected_colors if expectation else ())
            if colors:
                # Pinned full filament configs intentionally contain process/material facts,
                # not the user's spool color.  Bambu gives --load-filaments precedence over
                # the 3MF, so restore the project palette at the command-line priority.
                filament_color_arguments = (
                    "--filament-colour",
                    ";".join(colors),
                )
            arguments = (
                "--load-settings",
                f"{staged_profiles.machine};{staged_profiles.process}",
                "--load-filaments",
                ";".join(str(item) for item in staged_profiles.filaments),
                *filament_color_arguments,
                *tower_arguments,
                # Authored single-plate placement must pass Bambu's collision checks.
                # Legacy/mural packages retain their existing validation policy.
                *(("--no-check",) if not tower_arguments else ()),
                # Bambu's implicit auto-arrange can move mural geometry and rotate tile content
                # among otherwise correctly labelled build plates. Validation must preserve the
                # source package's authored plate identity and prime-tower reserve.
                "--arrange",
                "0",
                "--slice",
                "0",
                "--debug",
                "2",
                "--outputdir",
                str(output_directory),
                "--export-3mf",
                output_name,
                str(staged_source),
            )
            preparation = None
            try:
                if project_output is not None:
                    # Bambu's GUI skips project settings from third-party Application tags.
                    # Have Bambu serialize an editable project with resolved profiles, then
                    # validate those exact downloadable bytes WITHOUT profile overrides.
                    prepared = output_directory / "prepared.3mf"
                    prepare_arguments = (
                        *arguments[: arguments.index("--slice")],
                        "--debug",
                        "2",
                        "--outputdir",
                        str(output_directory),
                        "--export-3mf",
                        prepared.name,
                        str(staged_source),
                    )
                    preparation = self.runner.run(
                        detection.path,
                        prepare_arguments,
                        timeout_seconds=self.timeout_seconds,
                        cancellation=cancellation,
                        working_directory=workspace,
                        check=False,
                    )
                    if preparation.return_code != 0:
                        result = _result_from_tool_run(
                            status=BambuValidationStatus.FAILED,
                            source_sha256=source_sha256,
                            executable=detection.path,
                            version=detection.version,
                            tool_result=preparation,
                            profiles=profile_evidence,
                            profile_set_sha256=profile_set_sha256,
                            cache_key=cache_key,
                        )
                        raise BambuStudioSliceError("Bambu project preparation failed.", result)
                    try:
                        inspect_editable_project(
                            prepared,
                            expected_colors=project_colors,
                        )
                    except (ValueError, OSError) as error:
                        result = _result_from_tool_run(
                            status=BambuValidationStatus.INVALID_ARTIFACT,
                            source_sha256=source_sha256,
                            executable=detection.path,
                            version=detection.version,
                            tool_result=preparation,
                            profiles=profile_evidence,
                            profile_set_sha256=profile_set_sha256,
                            cache_key=cache_key,
                        )
                        raise InvalidSlicedArtifactError(str(error), result) from error
                    source_sha256 = _file_sha256(prepared)
                    cache_key = _validation_cache_key(
                        source_sha256=source_sha256,
                        profile_set_sha256=profile_set_sha256,
                        bambu_version=detection.version,
                    )
                    arguments = (
                        "--arrange",
                        "0",
                        "--slice",
                        "0",
                        "--debug",
                        "2",
                        "--outputdir",
                        str(output_directory),
                        "--export-3mf",
                        output_name,
                        str(prepared),
                    )
                tool_result = self.runner.run(
                    detection.path,
                    arguments,
                    timeout_seconds=self.timeout_seconds,
                    cancellation=cancellation,
                    working_directory=workspace,
                    check=False,
                )
            except ToolCanceledError as error:
                result = _result_from_tool_run(
                    status=BambuValidationStatus.CANCELED,
                    source_sha256=source_sha256,
                    executable=detection.path,
                    version=detection.version,
                    tool_result=error.result,
                    profiles=profile_evidence,
                    profile_set_sha256=profile_set_sha256,
                    cache_key=cache_key,
                )
                raise BambuStudioCanceledError(
                    "Bambu Studio slicing was canceled.", result
                ) from error
            except ToolTimeoutError as error:
                result = _result_from_tool_run(
                    status=BambuValidationStatus.TIMED_OUT,
                    source_sha256=source_sha256,
                    executable=detection.path,
                    version=detection.version,
                    tool_result=error.result,
                    profiles=profile_evidence,
                    profile_set_sha256=profile_set_sha256,
                    cache_key=cache_key,
                )
                raise BambuStudioTimeoutError(
                    f"Bambu Studio slicing exceeded {self.timeout_seconds:g} seconds.", result
                ) from error
            except ToolUnavailableError as error:
                result = BambuValidationResult(
                    status=BambuValidationStatus.UNAVAILABLE,
                    source_sha256=source_sha256,
                    executable=detection.path,
                    version=detection.version,
                    command=(str(detection.path), *arguments),
                    return_code=None,
                    stdout="",
                    stderr="",
                    duration_seconds=0,
                    timed_out=False,
                    canceled=False,
                    warnings=(),
                    profiles=profile_evidence,
                    profile_set_sha256=profile_set_sha256,
                    cache_key=cache_key,
                )
                raise BambuStudioUnavailableError(str(error), result) from error

            if tool_result.return_code != 0:
                result = _result_from_tool_run(
                    status=BambuValidationStatus.FAILED,
                    source_sha256=source_sha256,
                    executable=detection.path,
                    version=detection.version,
                    tool_result=tool_result,
                    profiles=profile_evidence,
                    profile_set_sha256=profile_set_sha256,
                    cache_key=cache_key,
                )
                raise BambuStudioSliceError(
                    f"Bambu Studio exited with status {tool_result.return_code}.", result
                )

            try:
                artifact = _inspect_sliced_artifact(output_path)
            except ValueError as error:
                result = _result_from_tool_run(
                    status=BambuValidationStatus.INVALID_ARTIFACT,
                    source_sha256=source_sha256,
                    executable=detection.path,
                    version=detection.version,
                    tool_result=tool_result,
                    profiles=profile_evidence,
                    profile_set_sha256=profile_set_sha256,
                    cache_key=cache_key,
                )
                raise InvalidSlicedArtifactError(str(error), result) from error

            report = build_slicer_validation_report(
                artifact.plates,
                expectation=expectation,
                stdout=tool_result.stdout,
                stderr=tool_result.stderr,
            )
            if report.missing_expected_colors:
                missing = ", ".join(report.missing_expected_colors)
                result = _result_from_tool_run(
                    status=BambuValidationStatus.INVALID_ARTIFACT,
                    source_sha256=source_sha256,
                    executable=detection.path,
                    version=detection.version,
                    tool_result=tool_result,
                    profiles=profile_evidence,
                    profile_set_sha256=profile_set_sha256,
                    cache_key=cache_key,
                    artifact=artifact,
                    report=report,
                )
                raise InvalidSlicedArtifactError(
                    f"Bambu Studio slice is missing expected colors: {missing}.", result
                )

            if sliced_output is not None:
                sliced_output.write_bytes(output_path.read_bytes())
            if project_output is not None:
                # Publish only after both project import and extrusion/color checks pass.
                project_output.write_bytes(prepared.read_bytes())
            result = _result_from_tool_run(
                status=BambuValidationStatus.VALIDATED,
                source_sha256=source_sha256,
                executable=detection.path,
                version=detection.version,
                tool_result=tool_result,
                profiles=profile_evidence,
                profile_set_sha256=profile_set_sha256,
                cache_key=cache_key,
                artifact=artifact,
                report=report,
            )
            if preparation is not None:
                result = dataclasses.replace(
                    result,
                    preparation_command=preparation.command,
                    preparation_stdout=preparation.stdout,
                    preparation_stderr=preparation.stderr,
                )
            return result


def inspect_editable_project(path: Path, *, expected_colors: tuple[str, ...] = ()) -> None:
    """Reject geometry-only or incomplete projects before offering a download.

    This models the GUI import prerequisite, independent of a successful CLI slice.
    Slicing the result without overlays additionally tests the embedded configuration.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("3D/3dmodel.model"))
            config = json.loads(archive.read(PROJECT_SETTINGS_MEMBER))
            application = next(
                (node.text or "" for node in root if node.get("name") == "Application"), ""
            )
            if not re.fullmatch(r"BambuStudio-\d+(?:\.\d+){2,3}", application):
                raise ValueError("3MF settings would be ignored by Bambu Studio's GUI importer")
            if any(PLATE_GCODE_PATTERN.fullmatch(name) for name in archive.namelist()):
                raise ValueError("Download must be an editable project, not sliced G-code")
            for key in (
                "nozzle_diameter",
                "printer_settings_id",
                "print_settings_id",
                "filament_settings_id",
                "filament_colour",
                "machine_start_gcode",
            ):
                if not config.get(key):
                    raise ValueError(f"Editable 3MF is missing embedded {key}")
            colors = tuple(config["filament_colour"])
            if expected_colors and colors != tuple(expected_colors):
                raise ValueError("Editable 3MF palette differs from the requested colors")
            if len(config["filament_settings_id"]) != len(colors):
                raise ValueError("Editable 3MF filament profiles do not match the palette")
            if config.get("extruder_variant_list") and (
                len(config.get("filament_self_index", []))
                != len(config.get("filament_extruder_variant", []))
            ):
                raise ValueError("Editable 3MF has inconsistent filament variant configuration")
    except (zipfile.BadZipFile, KeyError, ET.ParseError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid editable Bambu project: {error}") from error


def _stage_source(source: Path, workspace: Path) -> tuple[str, Path]:
    provisional = workspace / "incoming.3mf"
    digest = hashlib.sha256()
    with source.open("rb") as source_file, provisional.open("xb") as destination:
        while chunk := source_file.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
            destination.write(chunk)
    source_sha256 = digest.hexdigest()
    staged = workspace / f"input-{source_sha256[:24]}.3mf"
    provisional.replace(staged)
    return source_sha256, staged


def _project_extruder_count(source: Path) -> int:
    try:
        with zipfile.ZipFile(source, "r") as archive:
            payload = json.loads(archive.read(PROJECT_SETTINGS_MEMBER))
    except (
        KeyError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as error:
        raise _ProfilePreparationError(
            "The source 3MF does not contain readable Bambu project settings."
        ) from error
    filament_ids = payload.get("filament_settings_id") if isinstance(payload, dict) else None
    if (
        not isinstance(filament_ids, list)
        or not filament_ids
        or any(not isinstance(item, str) or not item for item in filament_ids)
    ):
        raise _ProfilePreparationError(
            "The source 3MF does not declare a valid filament profile for every extruder."
        )
    return len(filament_ids)


def _stage_profiles(
    profiles: BambuSliceProfiles,
    *,
    extruder_count: int,
    workspace: Path,
) -> tuple[_StagedProfiles, tuple[BambuProfileEvidence, ...], str]:
    if len(profiles.filaments) != extruder_count:
        raise _ProfilePreparationError(
            "Pinned filament profile count does not match the source 3MF: "
            f"expected {extruder_count}, received {len(profiles.filaments)}."
        )

    profile_directory = workspace / "profiles"
    profile_directory.mkdir()
    evidence: list[BambuProfileEvidence] = []

    machine = _stage_profile(
        profiles.machine,
        role="machine",
        extruder=None,
        destination_directory=profile_directory,
        evidence=evidence,
    )
    process = _stage_profile(
        profiles.process,
        role="process",
        extruder=None,
        destination_directory=profile_directory,
        evidence=evidence,
    )
    filaments = tuple(
        _stage_profile(
            profile,
            role="filament",
            extruder=index,
            destination_directory=profile_directory,
            evidence=evidence,
        )
        for index, profile in enumerate(profiles.filaments, start=1)
    )
    frozen_evidence = tuple(evidence)
    payload = [
        {
            "actual_sha256": item.actual_sha256,
            "extruder": item.extruder,
            "role": item.role,
            "size_bytes": item.size_bytes,
        }
        for item in frozen_evidence
    ]
    profile_set_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        _StagedProfiles(machine=machine, process=process, filaments=filaments),
        frozen_evidence,
        profile_set_sha256,
    )


def _single_plate_tower_arguments(source: Path) -> tuple[str, ...]:
    """Restore package tower placement after --load-settings applies defaults."""
    try:
        with zipfile.ZipFile(source) as archive:
            if MURAL_MANIFEST_MEMBER in archive.namelist():
                return ()
            settings = json.loads(archive.read(PROJECT_SETTINGS_MEMBER))
        if "wipe_tower_x" not in settings and "wipe_tower_y" not in settings:
            return ()
        axes = [settings[key] for key in ("wipe_tower_x", "wipe_tower_y")]
        if any(not isinstance(axis, list) or len(axis) != 1 for axis in axes):
            raise ValueError("single-plate tower must have one x/y coordinate")
        x, y = (float(axis[0]) for axis in axes)
        if not all(math.isfinite(value) and 0 <= value <= 256 for value in (x, y)):
            raise ValueError("tower coordinates must be inside the P2S bed")
        return ("--wipe-tower-x", str(x), "--wipe-tower-y", str(y))
    except (KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile) as error:
        raise _ProfilePreparationError(
            "Invalid retained single-plate prime-tower position."
        ) from error


def _apply_mural_process_overrides(
    source: Path,
    profiles: _StagedProfiles,
    *,
    workspace: Path,
) -> _StagedProfiles:
    """Give retained mural tower settings precedence over a loaded process profile."""

    try:
        with zipfile.ZipFile(source, "r") as archive:
            try:
                manifest_bytes = archive.read(MURAL_MANIFEST_MEMBER)
            except KeyError:
                return profiles
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError("mural manifest is not an object")
        tower = manifest["prime_tower"]
        plates = manifest["plates"]
        if (
            not isinstance(tower, dict)
            or not isinstance(plates, list)
            or not plates
            or len(plates) > 100
            or any(not isinstance(plate, dict) for plate in plates)
            or tower.get("policy") != "enabled_slicer_generated_tower"
            or tower.get("inserted_geometry") is not False
        ):
            raise ValueError("unsupported mural prime-tower policy")
        x_mm = float(tower.get("tower_x_mm", tower["x_mm"]))
        y_mm = float(tower.get("tower_y_mm", tower["y_mm"]))
        tower_width_mm = float(tower["tower_width_mm"])
        if (
            not all(math.isfinite(value) for value in (x_mm, y_mm, tower_width_mm))
            or x_mm < 0
            or y_mm < 0
            or tower_width_mm <= 0
        ):
            raise ValueError("invalid mural prime-tower coordinates")
        process_payload = json.loads(profiles.process.read_bytes())
        if not isinstance(process_payload, dict) or process_payload.get("type") != "process":
            raise ValueError("staged process profile is not a process object")
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as error:
        raise _ProfilePreparationError(
            "The mural package does not contain valid retained prime-tower settings."
        ) from error

    process_payload.update(
        {
            "enable_prime_tower": "1",
            "prime_tower_width": str(tower_width_mm),
            "wipe_tower_x": [str(x_mm)] * len(plates),
            "wipe_tower_y": [str(y_mm)] * len(plates),
        }
    )
    payload = json.dumps(process_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    destination = workspace / "profiles" / f"process-mural-{digest[:24]}.json"
    destination.write_bytes(payload)
    return _StagedProfiles(
        machine=profiles.machine,
        process=destination,
        filaments=profiles.filaments,
    )


def _stage_profile(
    profile: PinnedBambuProfile,
    *,
    role: str,
    extruder: Optional[int],
    destination_directory: Path,
    evidence: list[BambuProfileEvidence],
) -> Path:
    source = profile.path.expanduser().resolve()
    if not source.is_file():
        raise _ProfilePreparationError(
            f"Pinned Bambu {role} profile is missing or not a file: {source}",
            tuple(evidence),
        )
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise _ProfilePreparationError(
            f"Pinned Bambu {role} profile could not be read: {source}",
            tuple(evidence),
        ) from error
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    item = BambuProfileEvidence(
        role=role,
        extruder=extruder,
        source_path=source,
        expected_sha256=profile.sha256,
        actual_sha256=actual_sha256,
        size_bytes=len(payload),
    )
    evidence.append(item)
    if not payload:
        raise _ProfilePreparationError(
            f"Pinned Bambu {role} profile is empty: {source}", tuple(evidence)
        )
    if actual_sha256 != profile.sha256:
        raise _ProfilePreparationError(
            f"Pinned Bambu {role} profile fingerprint mismatch for {source}: "
            f"expected {profile.sha256}, found {actual_sha256}.",
            tuple(evidence),
        )
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ProfilePreparationError(
            f"Pinned Bambu {role} profile is not valid JSON: {source}", tuple(evidence)
        ) from error
    if not isinstance(decoded, dict):
        raise _ProfilePreparationError(
            f"Pinned Bambu {role} profile must contain a JSON object: {source}",
            tuple(evidence),
        )
    if decoded.get("type") != role:
        raise _ProfilePreparationError(
            f"Pinned Bambu {role} profile declares type {decoded.get('type')!r}: {source}",
            tuple(evidence),
        )
    ordinal = f"-{extruder}" if extruder is not None else ""
    destination = destination_directory / f"{role}{ordinal}-{actual_sha256[:24]}.json"
    destination.write_bytes(payload)
    return destination


def _validation_cache_key(
    *,
    source_sha256: str,
    profile_set_sha256: str,
    bambu_version: Optional[str],
) -> str:
    payload = {
        "bambu_version": bambu_version,
        "profile_set_sha256": profile_set_sha256,
        "schema": "image23mf-bambu-cli-validation-v3",
        "source_sha256": source_sha256,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"image23mf-bambu-cli-validation-v3:{digest}"


def _canceled_before_launch_result(
    *,
    executable: Optional[Path] = None,
    version: Optional[str] = None,
) -> BambuValidationResult:
    return BambuValidationResult(
        status=BambuValidationStatus.CANCELED,
        source_sha256=None,
        executable=executable,
        version=version,
        command=(),
        return_code=None,
        stdout="",
        stderr="",
        duration_seconds=0,
        timed_out=False,
        canceled=True,
        warnings=(),
    )


def _result_from_tool_run(
    *,
    status: BambuValidationStatus,
    source_sha256: str,
    executable: Path,
    version: Optional[str],
    tool_result: Optional[ToolRunResult],
    profiles: tuple[BambuProfileEvidence, ...] = (),
    profile_set_sha256: Optional[str] = None,
    cache_key: Optional[str] = None,
    artifact: Optional[SlicedArtifactEvidence] = None,
    report: Optional[SlicerValidationReport] = None,
) -> BambuValidationResult:
    if tool_result is None:
        return BambuValidationResult(
            status=status,
            source_sha256=source_sha256,
            executable=executable,
            version=version,
            command=(),
            return_code=None,
            stdout="",
            stderr="",
            duration_seconds=0,
            timed_out=status == BambuValidationStatus.TIMED_OUT,
            canceled=status == BambuValidationStatus.CANCELED,
            warnings=(),
            profiles=profiles,
            profile_set_sha256=profile_set_sha256,
            cache_key=cache_key,
            artifact=artifact,
            report=report,
        )
    return BambuValidationResult(
        status=status,
        source_sha256=source_sha256,
        executable=executable,
        version=version,
        command=tool_result.command,
        return_code=tool_result.return_code,
        stdout=tool_result.stdout,
        stderr=tool_result.stderr,
        duration_seconds=tool_result.duration_seconds,
        timed_out=tool_result.timed_out,
        canceled=tool_result.canceled,
        warnings=_warnings(tool_result.stdout, tool_result.stderr),
        profiles=profiles,
        profile_set_sha256=profile_set_sha256,
        cache_key=cache_key,
        artifact=artifact,
        report=report,
    )


def _warnings(stdout: str, stderr: str) -> tuple[str, ...]:
    warnings: list[str] = []
    seen: set[str] = set()
    for line in (*stdout.splitlines(), *stderr.splitlines()):
        normalized = line.strip()
        if normalized and WARNING_PATTERN.search(normalized) and normalized not in seen:
            warnings.append(normalized)
            seen.add(normalized)
    return tuple(warnings)


def _inspect_sliced_artifact(path: Path) -> SlicedArtifactEvidence:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Bambu Studio exited successfully without producing the requested slice.")
    size_bytes = path.stat().st_size
    if size_bytes == 0:
        raise ValueError("Bambu Studio produced an empty sliced artifact.")

    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Bambu Studio produced duplicate sliced artifact members.")
            members = tuple(sorted(name for name in names if PLATE_GCODE_PATTERN.fullmatch(name)))
            if not members:
                raise ValueError("Bambu Studio output contains no plate G-code artifact.")

            total_gcode_bytes = 0
            plates: list[SlicerPlateReport] = []
            prime_tower_bounds: list[Optional[SlicerBounds]] = []
            for member in members:
                info = archive.getinfo(member)
                if info.flag_bits & 0x1:
                    raise ValueError("Bambu Studio produced encrypted plate G-code.")
                if info.file_size <= 0 or info.file_size > MAX_GCODE_MEMBER_BYTES:
                    raise ValueError("Bambu Studio produced an invalid plate G-code size.")
                total_gcode_bytes += info.file_size
                with archive.open(info, "r") as gcode:
                    plate = parse_slicer_plate(gcode, member=member)
                if not plate.has_extrusion:
                    raise ValueError(f"Bambu Studio plate G-code has no extrusion motion: {member}")
                plates.append(plate)
                with archive.open(info, "r") as gcode:
                    prime_tower_bounds.append(_prime_tower_bounds(gcode))
            plate_names = _sliced_plate_names(archive, members)
    except zipfile.BadZipFile as error:
        raise ValueError("Bambu Studio output is not a valid sliced 3MF archive.") from error

    return SlicedArtifactEvidence(
        name=path.name,
        sha256=_file_sha256(path),
        size_bytes=size_bytes,
        gcode_members=members,
        uncompressed_gcode_bytes=total_gcode_bytes,
        plates=tuple(plates),
        plate_names=plate_names,
        prime_tower_bounds=tuple(prime_tower_bounds),
    )


def _sliced_plate_names(
    archive: zipfile.ZipFile,
    gcode_members: tuple[str, ...],
) -> tuple[Optional[str], ...]:
    try:
        data = archive.read(SLICE_INFO_MEMBER)
    except KeyError:
        return (None,) * len(gcode_members)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError(
            "Bambu Studio produced malformed sliced plate identity metadata."
        ) from error
    names_by_index: dict[int, str] = {}
    for plate in root.findall("plate"):
        index_node = plate.find("metadata[@key='index']")
        objects = plate.findall("object")
        if index_node is None or len(objects) != 1:
            raise ValueError("Bambu Studio produced incomplete sliced plate identity metadata.")
        try:
            index = int(index_node.get("value", ""))
        except ValueError as error:
            raise ValueError("Bambu Studio produced an invalid sliced plate index.") from error
        name = objects[0].get("name", "").strip()
        if index in names_by_index or not name:
            raise ValueError("Bambu Studio produced ambiguous sliced plate identity metadata.")
        names_by_index[index] = name
    expected_indices = tuple(
        int(member.removeprefix("Metadata/plate_").removesuffix(".gcode"))
        for member in gcode_members
    )
    if set(names_by_index) != set(expected_indices):
        raise ValueError("Bambu Studio sliced plate identities do not cover every G-code plate.")
    return tuple(names_by_index[index] for index in expected_indices)


def _prime_tower_bounds(gcode: BinaryIO) -> Optional[SlicerBounds]:
    active_feature: Optional[str] = None
    in_grid = False
    xyz_absolute = True
    x = y = z = 0.0
    minimum: Optional[list[float]] = None
    maximum: Optional[list[float]] = None
    for raw in gcode:
        line = raw.decode("utf-8", errors="replace").strip()
        upper = line.upper()
        if upper.startswith("; Z_HEIGHT:"):
            with suppress(ValueError):
                z = float(line.split(":", 1)[1])
            continue
        if upper.startswith("; FEATURE:"):
            active_feature = line.split(":", 1)[1].strip().lower()
            in_grid = False
            continue
        if upper == "; CP EMPTY GRID START":
            in_grid = active_feature == "prime tower"
            continue
        if upper == "; CP EMPTY GRID END":
            in_grid = False
            continue
        code = line.split(";", 1)[0].strip()
        if code == "G90":
            xyz_absolute = True
            continue
        if code == "G91":
            xyz_absolute = False
            continue
        if not re.match(r"^G[01]\b", code, re.IGNORECASE):
            continue
        parameters = {key.upper(): float(value) for key, value in GCODE_XYE_PATTERN.findall(code)}
        if xyz_absolute:
            x = parameters.get("X", x)
            y = parameters.get("Y", y)
        else:
            x += parameters.get("X", 0.0)
            y += parameters.get("Y", 0.0)
        if not in_grid or parameters.get("E", 0.0) <= 0 or not ({"X", "Y"} & parameters.keys()):
            continue
        values = [x, y, z]
        if minimum is None or maximum is None:
            minimum = values.copy()
            maximum = values.copy()
        else:
            minimum = [min(old, new) for old, new in zip(minimum, values)]
            maximum = [max(old, new) for old, new in zip(maximum, values)]
    if minimum is None or maximum is None:
        return None
    return SlicerBounds(
        min_x_mm=minimum[0],
        min_y_mm=minimum[1],
        min_z_mm=minimum[2],
        max_x_mm=maximum[0],
        max_y_mm=maximum[1],
        max_z_mm=maximum[2],
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
