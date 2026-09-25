"""Independent mesh-quality evidence and export gating.

The canonical :class:`Mesh` model rejects malformed geometry at construction time. This
module deliberately re-measures the serialized entities so export has a human-readable,
region-linked report rather than relying on a successful constructor as implicit proof.
"""

# ruff: noqa: UP045 -- Pydantic evaluates these annotations under supported Python 3.9.

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from image23mf.geometry.base import BaseBuildBounds
from image23mf.geometry.model import MESH_TOLERANCE_MM, GeometryDocument, Mesh, Part


class MeshQualitySeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class MeshQualityOptions(BaseModel):
    """Physical thresholds for an export-quality gate."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    build_bounds: Optional[BaseBuildBounds] = None
    minimum_part_thickness_mm: float = Field(gt=0)
    contact_tolerance_mm: float = Field(default=MESH_TOLERANCE_MM, gt=0)
    mesh_tolerance_mm: float = Field(default=MESH_TOLERANCE_MM, gt=0)
    require_single_component_per_part: bool = True


@dataclass(frozen=True)
class MeshBounds:
    minimum_x_mm: float
    minimum_y_mm: float
    minimum_z_mm: float
    maximum_x_mm: float
    maximum_y_mm: float
    maximum_z_mm: float

    @property
    def width_mm(self) -> float:
        return self.maximum_x_mm - self.minimum_x_mm

    @property
    def depth_mm(self) -> float:
        return self.maximum_y_mm - self.minimum_y_mm

    @property
    def height_mm(self) -> float:
        return self.maximum_z_mm - self.minimum_z_mm


@dataclass(frozen=True)
class MeshQualityFinding:
    code: str
    severity: MeshQualitySeverity
    message: str
    part_id: str
    mesh_id: str
    source_geometry_id: str
    source_label_id: Optional[str]


@dataclass(frozen=True)
class PartMeshEvidence:
    part_id: str
    mesh_id: str
    source_geometry_id: str
    source_label_id: Optional[str]
    bounds: Optional[MeshBounds]
    vertex_count: int
    triangle_count: int
    connected_component_count: int
    signed_volume_mm3: float
    surface_area_mm2: float
    minimum_edge_mm: Optional[float]
    minimum_altitude_mm: Optional[float]
    boundary_edge_count: int
    non_manifold_edge_count: int
    winding_conflict_count: int


@dataclass(frozen=True)
class GeometryQualityReport:
    safe_for_export: bool
    evidence: tuple[PartMeshEvidence, ...]
    findings: tuple[MeshQualityFinding, ...]
    total_signed_volume_mm3: float
    fingerprint: str

    def affected_source_geometry_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.source_geometry_id for item in self.findings}))


def validate_geometry_quality(
    document: GeometryDocument,
    options: MeshQualityOptions,
) -> GeometryQualityReport:
    """Measure every owned mesh and gate export on unsafe findings."""

    meshes = {mesh.id: mesh for mesh in document.meshes}
    base_parts = [part for part in document.parts if part.role == "base"]
    base_top_z = None
    if len(base_parts) == 1 and base_parts[0].mesh_id in meshes:
        base_vertices = meshes[base_parts[0].mesh_id].vertices
        if base_vertices:
            base_top_z = max(vertex.z_mm for vertex in base_vertices)
    pairs = [(part, meshes[part.mesh_id]) for part in document.parts if part.mesh_id in meshes]
    return validate_part_meshes(pairs, options=options, base_top_z_mm=base_top_z)


def validate_part_meshes(
    pairs: Iterable[tuple[Part, Mesh]],
    *,
    options: MeshQualityOptions,
    base_top_z_mm: Optional[float],
) -> GeometryQualityReport:
    """Validate part/mesh pairs, including defensively constructed malformed meshes."""

    evidence: list[PartMeshEvidence] = []
    findings: list[MeshQualityFinding] = []
    seen_meshes: set[str] = set()
    for part, mesh in pairs:
        if mesh.id in seen_meshes:
            findings.append(
                _finding(
                    "mesh-owned-more-than-once",
                    MeshQualitySeverity.ERROR,
                    "The mesh is owned by more than one part.",
                    part,
                    mesh,
                )
            )
        seen_meshes.add(mesh.id)
        part_evidence, part_findings = _measure_part(
            part,
            mesh,
            options=options,
            base_top_z_mm=base_top_z_mm,
        )
        evidence.append(part_evidence)
        findings.extend(part_findings)

    evidence.sort(key=lambda item: item.part_id)
    findings.sort(
        key=lambda item: (
            -_severity_rank(item.severity),
            item.part_id,
            item.code,
            item.message,
        )
    )
    safe = not any(item.severity == MeshQualitySeverity.ERROR for item in findings)
    total_volume = round(sum(item.signed_volume_mm3 for item in evidence), 6)
    payload = {
        "evidence": [_evidence_payload(item) for item in evidence],
        "findings": [_finding_payload(item) for item in findings],
        "safe_for_export": safe,
        "total_signed_volume_mm3": total_volume,
        "version": 1,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return GeometryQualityReport(
        safe_for_export=safe,
        evidence=tuple(evidence),
        findings=tuple(findings),
        total_signed_volume_mm3=total_volume,
        fingerprint=fingerprint,
    )


def _measure_part(
    part: Part,
    mesh: Mesh,
    *,
    options: MeshQualityOptions,
    base_top_z_mm: Optional[float],
) -> tuple[PartMeshEvidence, list[MeshQualityFinding]]:
    findings: list[MeshQualityFinding] = []
    vertices = tuple(mesh.vertices)
    triangles = tuple(mesh.triangles)
    if part.mesh_id != mesh.id:
        findings.append(
            _finding(
                "part-mesh-mismatch",
                MeshQualitySeverity.ERROR,
                "Part ownership references a different mesh ID.",
                part,
                mesh,
            )
        )
    if not mesh.watertight:
        findings.append(
            _finding(
                "mesh-not-declared-watertight",
                MeshQualitySeverity.ERROR,
                "Mesh is not declared watertight and cannot pass the export gate.",
                part,
                mesh,
            )
        )
    finite_vertices = all(
        math.isfinite(value)
        for vertex in vertices
        for value in (vertex.x_mm, vertex.y_mm, vertex.z_mm)
    )
    bounds = _mesh_bounds(mesh) if vertices and finite_vertices else None
    if not finite_vertices:
        findings.append(
            _finding(
                "non-finite-vertex",
                MeshQualitySeverity.ERROR,
                "The mesh contains a non-finite vertex coordinate.",
                part,
                mesh,
            )
        )
    coordinate_keys = [
        (vertex.x_mm, vertex.y_mm, vertex.z_mm)
        for vertex in vertices
        if all(math.isfinite(value) for value in (vertex.x_mm, vertex.y_mm, vertex.z_mm))
    ]
    if len(coordinate_keys) != len(set(coordinate_keys)):
        findings.append(
            _finding(
                "duplicate-vertex",
                MeshQualitySeverity.ERROR,
                "Mesh contains duplicate vertex coordinates.",
                part,
                mesh,
            )
        )

    edge_faces: dict[tuple[int, int], list[tuple[int, int]]] = {}
    surface_area = 0.0
    signed_volume = 0.0
    minimum_edge: Optional[float] = None
    minimum_altitude: Optional[float] = None
    valid_faces: list[tuple[int, int, int]] = []
    face_keys: set[frozenset[int]] = set()
    used_vertices: set[int] = set()
    for face_index, triangle in enumerate(triangles):
        indices = triangle.vertices
        if min(indices) < 0 or max(indices) >= len(vertices) or len(set(indices)) != 3:
            findings.append(
                _finding(
                    "invalid-triangle-index",
                    MeshQualitySeverity.ERROR,
                    f"Triangle {face_index} references a missing or repeated vertex.",
                    part,
                    mesh,
                )
            )
            continue
        face_key = frozenset(indices)
        if face_key in face_keys:
            findings.append(
                _finding(
                    "duplicate-face",
                    MeshQualitySeverity.ERROR,
                    f"Triangle {face_index} duplicates another face.",
                    part,
                    mesh,
                )
            )
        face_keys.add(face_key)
        used_vertices.update(indices)
        a, b, c = (vertices[index] for index in indices)
        if not all(
            math.isfinite(value)
            for vertex in (a, b, c)
            for value in (vertex.x_mm, vertex.y_mm, vertex.z_mm)
        ):
            continue
        ab = (b.x_mm - a.x_mm, b.y_mm - a.y_mm, b.z_mm - a.z_mm)
        ac = (c.x_mm - a.x_mm, c.y_mm - a.y_mm, c.z_mm - a.z_mm)
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        double_area = math.sqrt(sum(value * value for value in cross))
        edge_lengths = (
            math.dist((a.x_mm, a.y_mm, a.z_mm), (b.x_mm, b.y_mm, b.z_mm)),
            math.dist((b.x_mm, b.y_mm, b.z_mm), (c.x_mm, c.y_mm, c.z_mm)),
            math.dist((c.x_mm, c.y_mm, c.z_mm), (a.x_mm, a.y_mm, a.z_mm)),
        )
        altitude = 0.0 if max(edge_lengths) == 0 else double_area / max(edge_lengths)
        minimum_edge = (
            min(edge_lengths) if minimum_edge is None else min(minimum_edge, *edge_lengths)
        )
        minimum_altitude = altitude if minimum_altitude is None else min(minimum_altitude, altitude)
        if min(edge_lengths) <= options.mesh_tolerance_mm or altitude <= options.mesh_tolerance_mm:
            findings.append(
                _finding(
                    "degenerate-triangle",
                    MeshQualitySeverity.ERROR,
                    f"Triangle {face_index} is below the configured edge or altitude tolerance.",
                    part,
                    mesh,
                )
            )
        surface_area += double_area / 2
        signed_volume += (
            a.x_mm * (b.y_mm * c.z_mm - b.z_mm * c.y_mm)
            - a.y_mm * (b.x_mm * c.z_mm - b.z_mm * c.x_mm)
            + a.z_mm * (b.x_mm * c.y_mm - b.y_mm * c.x_mm)
        ) / 6
        valid_faces.append(indices)
        for start, end in zip(indices, (*indices[1:], indices[0])):
            edge_faces.setdefault(tuple(sorted((start, end))), []).append((start, end))

    unused_vertex_count = len(vertices) - len(used_vertices)
    if unused_vertex_count:
        findings.append(
            _finding(
                "unused-vertex",
                MeshQualitySeverity.ERROR,
                f"Mesh contains {unused_vertex_count} vertices that are not referenced by a face.",
                part,
                mesh,
            )
        )

    boundary_edges = sum(len(owners) == 1 for owners in edge_faces.values())
    non_manifold_edges = sum(len(owners) > 2 for owners in edge_faces.values())
    winding_conflicts = sum(
        len(owners) == 2 and owners[0] == owners[1] for owners in edge_faces.values()
    )
    if boundary_edges or non_manifold_edges:
        findings.append(
            _finding(
                "non-manifold-mesh",
                MeshQualitySeverity.ERROR,
                f"Mesh has {boundary_edges} boundary and "
                f"{non_manifold_edges} over-connected edges.",
                part,
                mesh,
            )
        )
    if winding_conflicts:
        findings.append(
            _finding(
                "inconsistent-winding",
                MeshQualitySeverity.ERROR,
                f"Mesh has {winding_conflicts} same-direction shared edges.",
                part,
                mesh,
            )
        )

    components = _connected_face_components(valid_faces)
    if options.require_single_component_per_part and components != 1:
        findings.append(
            _finding(
                "disconnected-part",
                MeshQualitySeverity.ERROR,
                f"Part contains {components} disconnected triangle bodies; expected exactly one.",
                part,
                mesh,
            )
        )
    if signed_volume <= options.mesh_tolerance_mm**3:
        findings.append(
            _finding(
                "non-positive-volume",
                MeshQualitySeverity.ERROR,
                f"Mesh signed volume is {signed_volume:.6g} mm³; "
                "outward positive volume is required.",
                part,
                mesh,
            )
        )

    if bounds is not None:
        if bounds.height_mm + options.mesh_tolerance_mm < options.minimum_part_thickness_mm:
            findings.append(
                _finding(
                    "part-too-thin",
                    MeshQualitySeverity.ERROR,
                    f"Part height {bounds.height_mm:.6g} mm is below the "
                    f"{options.minimum_part_thickness_mm:.6g} mm minimum.",
                    part,
                    mesh,
                )
            )
        _check_build_bounds(part, mesh, bounds, options, findings)
        if part.role != "base" and base_top_z_mm is not None:
            delta = bounds.minimum_z_mm - base_top_z_mm
            if delta > options.contact_tolerance_mm:
                findings.append(
                    _finding(
                        "floating-part",
                        MeshQualitySeverity.ERROR,
                        f"Part starts {delta:.6g} mm above the structural base.",
                        part,
                        mesh,
                    )
                )
            elif delta < -options.contact_tolerance_mm:
                findings.append(
                    _finding(
                        "part-below-contact-plane",
                        MeshQualitySeverity.ERROR,
                        f"Part starts {-delta:.6g} mm below the declared base contact plane.",
                        part,
                        mesh,
                    )
                )

    evidence = PartMeshEvidence(
        part_id=part.id,
        mesh_id=mesh.id,
        source_geometry_id=part.source_geometry_id,
        source_label_id=part.source_label_id,
        bounds=bounds,
        vertex_count=len(vertices),
        triangle_count=len(triangles),
        connected_component_count=components,
        signed_volume_mm3=round(signed_volume, 6),
        surface_area_mm2=round(surface_area, 6),
        minimum_edge_mm=None if minimum_edge is None else round(minimum_edge, 6),
        minimum_altitude_mm=None if minimum_altitude is None else round(minimum_altitude, 6),
        boundary_edge_count=boundary_edges,
        non_manifold_edge_count=non_manifold_edges,
        winding_conflict_count=winding_conflicts,
    )
    return evidence, findings


def _check_build_bounds(
    part: Part,
    mesh: Mesh,
    bounds: MeshBounds,
    options: MeshQualityOptions,
    findings: list[MeshQualityFinding],
) -> None:
    build = options.build_bounds
    if build is None:
        return
    minimum_x = build.origin_x_mm + build.clearance_mm
    minimum_y = build.origin_y_mm + build.clearance_mm
    maximum_x = build.origin_x_mm + build.width_mm - build.clearance_mm
    maximum_y = build.origin_y_mm + build.depth_mm - build.clearance_mm
    tolerance = options.mesh_tolerance_mm
    if (
        bounds.minimum_x_mm < minimum_x - tolerance
        or bounds.minimum_y_mm < minimum_y - tolerance
        or bounds.maximum_x_mm > maximum_x + tolerance
        or bounds.maximum_y_mm > maximum_y + tolerance
    ):
        findings.append(
            _finding(
                "outside-build-bounds",
                MeshQualitySeverity.ERROR,
                "Part bounds exceed the printable build area after clearance.",
                part,
                mesh,
            )
        )
    for excluded in build.excluded_rectangles:
        if (
            bounds.maximum_x_mm >= excluded.minimum_x_mm - tolerance
            and bounds.minimum_x_mm <= excluded.maximum_x_mm + tolerance
            and bounds.maximum_y_mm >= excluded.minimum_y_mm - tolerance
            and bounds.minimum_y_mm <= excluded.maximum_y_mm + tolerance
        ):
            findings.append(
                _finding(
                    "intersects-excluded-build-area",
                    MeshQualitySeverity.ERROR,
                    "Part bounds intersect an excluded build-plate area.",
                    part,
                    mesh,
                )
            )


def _mesh_bounds(mesh: Mesh) -> MeshBounds:
    return MeshBounds(
        minimum_x_mm=min(item.x_mm for item in mesh.vertices),
        minimum_y_mm=min(item.y_mm for item in mesh.vertices),
        minimum_z_mm=min(item.z_mm for item in mesh.vertices),
        maximum_x_mm=max(item.x_mm for item in mesh.vertices),
        maximum_y_mm=max(item.y_mm for item in mesh.vertices),
        maximum_z_mm=max(item.z_mm for item in mesh.vertices),
    )


def _connected_face_components(faces: list[tuple[int, int, int]]) -> int:
    if not faces:
        return 0
    edge_faces: dict[tuple[int, int], list[int]] = {}
    for face_index, indices in enumerate(faces):
        for start, end in zip(indices, (*indices[1:], indices[0])):
            edge_faces.setdefault(tuple(sorted((start, end))), []).append(face_index)
    neighbors: list[set[int]] = [set() for _ in faces]
    for owners in edge_faces.values():
        for owner in owners:
            neighbors[owner].update(candidate for candidate in owners if candidate != owner)
    unseen = set(range(len(faces)))
    component_count = 0
    while unseen:
        component_count += 1
        pending = [unseen.pop()]
        while pending:
            current = pending.pop()
            for neighbor in neighbors[current]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    pending.append(neighbor)
    return component_count


def _finding(
    code: str,
    severity: MeshQualitySeverity,
    message: str,
    part: Part,
    mesh: Mesh,
) -> MeshQualityFinding:
    return MeshQualityFinding(
        code=code,
        severity=severity,
        message=message,
        part_id=part.id,
        mesh_id=mesh.id,
        source_geometry_id=part.source_geometry_id,
        source_label_id=part.source_label_id,
    )


def _severity_rank(severity: MeshQualitySeverity) -> int:
    return {
        MeshQualitySeverity.INFO: 1,
        MeshQualitySeverity.WARNING: 2,
        MeshQualitySeverity.ERROR: 3,
    }[severity]


def _evidence_payload(item: PartMeshEvidence) -> dict[str, object]:
    return {
        "boundary_edge_count": item.boundary_edge_count,
        "bounds": None if item.bounds is None else item.bounds.__dict__,
        "connected_component_count": item.connected_component_count,
        "mesh_id": item.mesh_id,
        "minimum_altitude_mm": item.minimum_altitude_mm,
        "minimum_edge_mm": item.minimum_edge_mm,
        "non_manifold_edge_count": item.non_manifold_edge_count,
        "part_id": item.part_id,
        "signed_volume_mm3": item.signed_volume_mm3,
        "source_geometry_id": item.source_geometry_id,
        "source_label_id": item.source_label_id,
        "surface_area_mm2": item.surface_area_mm2,
        "triangle_count": item.triangle_count,
        "vertex_count": item.vertex_count,
        "winding_conflict_count": item.winding_conflict_count,
    }


def _finding_payload(item: MeshQualityFinding) -> dict[str, object]:
    return {
        "code": item.code,
        "mesh_id": item.mesh_id,
        "message": item.message,
        "part_id": item.part_id,
        "severity": item.severity.value,
        "source_geometry_id": item.source_geometry_id,
        "source_label_id": item.source_label_id,
    }
