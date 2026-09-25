"""Measured Bambu Lab P2S profile resolution for generated 3MF projects.

The package writer intentionally does not pretend that its compact
``project_settings.config`` is a complete Bambu Studio preset.  This module records the
installed profiles that must be supplied to Bambu Studio when slicing and the small set
of process overrides that have been verified against the installed application.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

PROFILE_CONTRACT_NAME = "image23mf.bambu.p2s-profile"
PROFILE_CONTRACT_SCHEMA_VERSION = 1
PROFILE_CONTRACT_PATH = "Metadata/image23mf_profile.json"
TESTED_BAMBU_STUDIO_VERSION = "02.07.01.62"
SUPPORTED_PRINTER_MODEL = "Bambu Lab P2S"
SUPPORTED_BED_TYPE = "Textured PEI Plate"

# These are the recommended layer heights in the bundled printer catalog and have exact
# matching system-process profiles in Bambu Studio 02.07.01.62.
PROCESS_PROFILE_NAMES: dict[float, dict[float, str]] = {
    0.2: {
        0.08: "0.08mm High Quality @BBL P2S 0.2 nozzle",
        0.10: "0.10mm Standard @BBL P2S 0.2 nozzle",
        0.12: "0.12mm Balanced Quality @BBL P2S 0.2 nozzle",
    },
    0.4: {
        0.08: "0.08mm High Quality @BBL P2S",
        0.12: "0.12mm High Quality @BBL P2S",
        0.16: "0.16mm Standard @BBL P2S",
        0.20: "0.20mm Standard @BBL P2S",
        0.24: "0.24mm Standard @BBL P2S",
    },
}

# Arachne plus the measured thin-feature controls are the default artwork strategy.  The
# adapter must overlay these keys onto a copy of the installed process profile; a sparse
# user profile does not resolve its Bambu inheritance reliably.
ARTWORK_PROCESS_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("curr_bed_type", SUPPORTED_BED_TYPE),
    ("detect_thin_wall", "1"),
    ("min_bead_width", "70%"),
    # Precise outer-wall spacing exposes the base along adjacent color regions.
    # Use Bambu's normal spacing; validate the resulting sliced coverage separately.
    ("precise_outer_wall", "0"),
    ("wall_generator", "arachne"),
)

SUPPORTED_FILAMENT_PRESETS = frozenset({"Bambu PLA Basic", "Bambu PLA Matte"})


class BambuProfileError(ValueError):
    """Raised when a requested job has no measured P2S profile strategy."""


@runtime_checkable
class MaterialProfileLike(Protocol):
    """Small material contract required to resolve installed filament profiles."""

    color: str
    extruder: int
    filament_type: str
    preset: str


@dataclass(frozen=True)
class InstalledBambuProfile:
    """A system profile identified relative to Bambu Studio's Resources directory."""

    kind: str
    name: str
    relative_path: str

    def resolve(self, resources_root: str | Path) -> Path:
        """Resolve this profile beneath a caller-supplied Bambu Resources directory."""

        return Path(resources_root) / self.relative_path

    def as_contract(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "relative_path": self.relative_path,
            "resolution": "recursive_inheritance_merge",
            "source": "installed_system_profile",
        }


@dataclass(frozen=True)
class InstalledBambuFilamentProfile:
    """One extruder's installed preset plus the requested visual material intent."""

    extruder: int
    base_preset: str
    profile: InstalledBambuProfile
    requested_color: str
    filament_type: str

    def as_contract(self) -> dict[str, object]:
        payload = self.profile.as_contract()
        payload.update(
            {
                "base_preset": self.base_preset,
                "extruder": self.extruder,
                "filament_type": self.filament_type,
                "requested_color": self.requested_color,
            }
        )
        return payload


@dataclass(frozen=True)
class BambuP2SProfilePlan:
    """Resolved, measured P2S slicing strategy for one generated project."""

    printer_model: str
    nozzle_diameter_mm: float
    layer_height_mm: float
    bed_type: str
    machine: InstalledBambuProfile
    process: InstalledBambuProfile
    filaments: tuple[InstalledBambuFilamentProfile, ...]
    process_overrides: tuple[tuple[str, str], ...] = ARTWORK_PROCESS_OVERRIDES

    @property
    def process_override_map(self) -> dict[str, str]:
        return dict(self.process_overrides)

    def as_contract(self) -> dict[str, object]:
        """Return the canonical JSON-compatible profile contract payload."""

        return {
            "authority": {
                "embedded_project_settings": "import_hint",
                "installed_profiles": "slice_authority",
                "requested_filament_colors": "visual_intent",
            },
            "contract": PROFILE_CONTRACT_NAME,
            "evidence": {
                "application": "BambuStudio",
                "platform": "macOS",
                "version": TESTED_BAMBU_STUDIO_VERSION,
            },
            "installed_profiles": {
                "filaments": [filament.as_contract() for filament in self.filaments],
                "machine": self.machine.as_contract(),
                "process": {
                    **self.process.as_contract(),
                    "overrides": self.process_override_map,
                    "override_strategy": "resolve_inheritance_then_overlay",
                },
            },
            "schema_version": PROFILE_CONTRACT_SCHEMA_VERSION,
            "target": {
                "bed_type": self.bed_type,
                "layer_height_mm": self.layer_height_mm,
                "nozzle_diameter_mm": self.nozzle_diameter_mm,
                "printer_model": self.printer_model,
            },
        }


@dataclass(frozen=True)
class ResolvedBambuProfile:
    """Canonical flattened bytes and provenance for one installed Bambu profile."""

    profile: InstalledBambuProfile
    inheritance_chain: tuple[str, ...]
    source_paths: tuple[Path, ...]
    payload: bytes

    @property
    def sha256(self) -> str:
        return sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class ResolvedBambuProfileSet:
    """Fully materialized profiles safe to stage outside Bambu's Resources tree."""

    machine: ResolvedBambuProfile
    process: ResolvedBambuProfile
    filaments: tuple[ResolvedBambuProfile, ...]


def _canonical_float(value: object, supported: Sequence[float], field: str) -> float:
    if isinstance(value, bool):
        raise BambuProfileError(f"{field} must be one of {list(supported)}")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise BambuProfileError(f"{field} must be one of {list(supported)}") from error
    for candidate in supported:
        if math.isclose(number, candidate, rel_tol=0.0, abs_tol=1e-9):
            return candidate
    raise BambuProfileError(f"{field} must be one of {list(supported)}")


def _base_filament_preset(preset: str) -> str:
    for base in sorted(SUPPORTED_FILAMENT_PRESETS):
        if preset == base or preset.startswith(f"{base} @BBL P2S"):
            return base
    raise BambuProfileError(
        "material preset must be a measured Bambu PLA Basic or Bambu PLA Matte P2S preset"
    )


def _machine_profile(nozzle: float) -> InstalledBambuProfile:
    name = f"Bambu Lab P2S {nozzle:.1f} nozzle"
    return InstalledBambuProfile(
        kind="machine",
        name=name,
        relative_path=f"profiles/BBL/machine/{name}.json",
    )


def _process_profile(nozzle: float, layer_height: float) -> InstalledBambuProfile:
    name = PROCESS_PROFILE_NAMES[nozzle][layer_height]
    return InstalledBambuProfile(
        kind="process",
        name=name,
        relative_path=f"profiles/BBL/process/{name}.json",
    )


def _filament_profile(
    material: MaterialProfileLike, nozzle: float, index: int
) -> InstalledBambuFilamentProfile:
    if material.filament_type.upper() != "PLA":
        raise BambuProfileError(f"materials[{index}].filament_type must be PLA")
    base = _base_filament_preset(material.preset)
    suffix = " @BBL P2S 0.2 nozzle" if nozzle == 0.2 else " @BBL P2S"
    name = f"{base}{suffix}"
    return InstalledBambuFilamentProfile(
        extruder=material.extruder,
        base_preset=base,
        profile=InstalledBambuProfile(
            kind="filament",
            name=name,
            relative_path=f"profiles/BBL/filament/{name}.json",
        ),
        requested_color=material.color,
        filament_type="PLA",
    )


def resolve_p2s_profile_plan(
    *,
    printer_model: str,
    nozzle_diameter_mm: float,
    layer_height_mm: float,
    bed_type: str,
    materials: Sequence[MaterialProfileLike],
) -> BambuP2SProfilePlan:
    """Resolve a request to the exact installed P2S profiles measured by 24K-66."""

    if printer_model != SUPPORTED_PRINTER_MODEL:
        raise BambuProfileError(f"printer_model must be {SUPPORTED_PRINTER_MODEL!r}")
    nozzle = _canonical_float(nozzle_diameter_mm, tuple(PROCESS_PROFILE_NAMES), "nozzle_diameter")
    layer = _canonical_float(
        layer_height_mm,
        tuple(PROCESS_PROFILE_NAMES[nozzle]),
        f"layer_height for a {nozzle:.1f} mm nozzle",
    )
    if bed_type != SUPPORTED_BED_TYPE:
        raise BambuProfileError(f"bed_type must be {SUPPORTED_BED_TYPE!r}")
    if not materials:
        raise BambuProfileError("at least one material is required")
    filaments = tuple(
        _filament_profile(material, nozzle, index) for index, material in enumerate(materials)
    )
    return BambuP2SProfilePlan(
        printer_model=printer_model,
        nozzle_diameter_mm=nozzle,
        layer_height_mm=layer,
        bed_type=bed_type,
        machine=_machine_profile(nozzle),
        process=_process_profile(nozzle, layer),
        filaments=filaments,
    )


def _profile_directory(profile: InstalledBambuProfile, resources_root: str | Path) -> Path:
    if profile.kind not in {"filament", "machine", "process"}:
        raise BambuProfileError(f"unsupported Bambu profile kind {profile.kind!r}")
    directory = Path(resources_root) / "profiles" / "BBL" / profile.kind
    if not directory.is_dir():
        raise BambuProfileError(f"Bambu {profile.kind} profile directory is missing: {directory}")
    return directory


def _read_profile_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BambuProfileError(f"Bambu profile is not readable JSON: {path}") from error
    if not isinstance(payload, dict):
        raise BambuProfileError(f"Bambu profile must contain a JSON object: {path}")
    return payload


def _profile_index(directory: Path) -> dict[str, tuple[Path, dict[str, object]]]:
    index: dict[str, tuple[Path, dict[str, object]]] = {}
    for path in sorted(directory.glob("*.json")):
        payload = _read_profile_payload(path)
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            # Bambu ships lookup maps alongside actual profiles in these directories.
            # They are valid resources but are not members of the inheritance graph.
            continue
        if name in index:
            raise BambuProfileError(f"duplicate Bambu profile name {name!r} in {directory}")
        index[name] = (path, payload)
    return index


def resolve_installed_profile(
    profile: InstalledBambuProfile,
    resources_root: str | Path,
    *,
    overrides: dict[str, str] | None = None,
) -> ResolvedBambuProfile:
    """Recursively merge a system profile so it remains complete when staged elsewhere.

    Bambu's CLI accepts a profile path but does not resolve the profile's ``inherits``
    chain when that path is used as an isolated settings input.  Returning canonical
    flattened bytes makes the inherited machine/process/filament settings explicit.
    """

    directory = _profile_directory(profile, resources_root)
    index = _profile_index(directory)
    target = index.get(profile.name)
    expected_path = profile.resolve(resources_root)
    if target is None:
        raise BambuProfileError(f"installed Bambu profile is missing: {expected_path}")
    if target[0].resolve() != expected_path.resolve():
        raise BambuProfileError(
            f"installed Bambu profile path does not match its declared name: {expected_path}"
        )

    merged: dict[str, object] = {}
    chain: list[str] = []
    source_paths: list[Path] = []
    active: set[str] = set()

    def merge_named(name: str) -> None:
        if name in active:
            raise BambuProfileError(f"Bambu profile inheritance cycle at {name!r}")
        item = index.get(name)
        if item is None:
            raise BambuProfileError(
                f"Bambu profile {profile.name!r} inherits missing profile {name!r}"
            )
        path, payload = item
        declared_type = payload.get("type")
        if declared_type != profile.kind:
            raise BambuProfileError(
                f"Bambu {profile.kind} profile {name!r} declares type {declared_type!r}"
            )
        active.add(name)
        inherited = payload.get("inherits")
        if inherited is not None:
            if not isinstance(inherited, str) or not inherited:
                raise BambuProfileError(f"Bambu profile {name!r} has an invalid inherits value")
            merge_named(inherited)
        merged.update(payload)
        chain.append(name)
        source_paths.append(path.resolve())
        active.remove(name)

    merge_named(profile.name)
    if overrides:
        merged.update(overrides)
    payload = (
        json.dumps(merged, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
    return ResolvedBambuProfile(
        profile=profile,
        inheritance_chain=tuple(chain),
        source_paths=tuple(source_paths),
        payload=payload,
    )


def resolve_p2s_profile_set(
    plan: BambuP2SProfilePlan, resources_root: str | Path
) -> ResolvedBambuProfileSet:
    """Materialize every profile in a P2S plan with measured artwork overrides."""

    return ResolvedBambuProfileSet(
        machine=resolve_installed_profile(plan.machine, resources_root),
        process=resolve_installed_profile(
            plan.process,
            resources_root,
            overrides=plan.process_override_map,
        ),
        filaments=tuple(
            resolve_installed_profile(filament.profile, resources_root)
            for filament in plan.filaments
        ),
    )
