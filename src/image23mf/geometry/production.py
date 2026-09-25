"""Production adapters that turn one verified preview label field into printable geometry.

The lower-level geometry modules deliberately do not know about projects, jobs, or preview
artifacts.  This module is the narrow adapter between those domains: it carries immutable,
verified preview bytes through Potrace construction evidence, authoritative shared-boundary
topology, layer-aligned extrusion, independent mesh validation, and a source-resolution
geometry preview.
"""

from __future__ import annotations

# ruff: noqa: UP045 -- supported Python 3.9 requires Optional rather than PEP 604 unions.
import hashlib
import io
import json
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image

from image23mf import __version__
from image23mf.contracts.job import JobConfig
from image23mf.derivation import DerivationIdentity
from image23mf.engine.labels import LabelField
from image23mf.geometry.base import (
    BaseBuildBounds,
    BaseMeshOptions,
    generate_structural_base,
)
from image23mf.geometry.extrusion import (
    FlushInlayStrategy,
    assemble_mesh_document,
    extrude_artwork_regions,
)
from image23mf.geometry.jobs import (
    BinaryArtifact,
    ExtrusionOutput,
    GeometryJobRequest,
    GeometryPipelineAdapters,
    GeometryStageContext,
    ValidationOutput,
    VectorizationOutput,
)
from image23mf.geometry.model import (
    GEOMETRY_IR_SCHEMA_VERSION,
    Material,
    Point2,
    RectangleBase,
    SourceLabel,
)
from image23mf.geometry.parallel import geometry_worker_count
from image23mf.geometry.raster_diff import rasterize_geometry
from image23mf.geometry.serialization import dump_geometry_ir
from image23mf.geometry.topology import SharedBoundaryTopologyResult, build_shared_boundary_topology
from image23mf.geometry.validation import MeshQualityOptions, validate_geometry_quality
from image23mf.vectorization import (
    POTRACE_ADAPTER_VERSION,
    PotraceParameters,
    PotraceResult,
    PotraceVectorizer,
)

PRODUCTION_GEOMETRY_ADAPTER_VERSION = 1


@dataclass(frozen=True)
class ProductionGeometryInput:
    """All geometry-affecting preview evidence after ownership and hash verification."""

    source_asset_id: str
    source_asset_sha256: str
    processed_labels_sha256: str
    labels: LabelField
    config: JobConfig
    operations_fingerprint: str = "none"

    def __post_init__(self) -> None:
        if hashlib.sha256(self.labels.pixels).hexdigest() != self.processed_labels_sha256:
            raise ValueError("processed label bytes do not match their verified SHA-256")
        if self.labels.width <= 0 or self.labels.height <= 0:
            raise ValueError("production geometry requires a non-empty label field")
        palette_indices = set(range(len(self.config.palette.colors)))
        if not set(self.labels.label_values).issubset(palette_indices):
            raise ValueError("processed labels reference colors outside the configured palette")


@dataclass(frozen=True)
class ProductionVectorization:
    input: ProductionGeometryInput
    result: PotraceResult
    materials: tuple[Material, ...]
    source_labels: tuple[SourceLabel, ...]
    base: RectangleBase


def production_geometry_request(
    payload: ProductionGeometryInput,
    *,
    preview_job_id: str,
) -> GeometryJobRequest:
    settings = {
        "art_thickness_mm": payload.config.geometry.art_thickness_mm,
        "base_thickness_mm": payload.config.geometry.base_thickness_mm,
        "canvas_height_mm": payload.config.canvas.height_mm,
        "canvas_width_mm": payload.config.canvas.width_mm,
        "layer_height_mm": payload.config.printer.layer_height_mm,
        "nozzle_mm": payload.config.printer.nozzle_mm,
        "palette": [item.model_dump(mode="json") for item in payload.config.palette.colors],
    }
    derivation = DerivationIdentity(
        pipeline="geometry",
        source_fingerprint=payload.source_asset_sha256,
        config_fingerprint=payload.config.fingerprint(),
        operations_fingerprint=payload.operations_fingerprint,
        engine_version=__version__,
        adapter_versions={
            "geometry_ir": str(GEOMETRY_IR_SCHEMA_VERSION),
            "geometry_pipeline": "1",
            "potrace": str(POTRACE_ADAPTER_VERSION),
            "production_geometry": str(PRODUCTION_GEOMETRY_ADAPTER_VERSION),
        },
        dependencies={
            "processed_labels": payload.processed_labels_sha256,
            "settings": hashlib.sha256(
                json.dumps(
                    settings,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        },
    )
    return GeometryJobRequest(
        derivation_key=derivation.key(),
        payload=payload,
        expects_preview=True,
        metadata={
            "adapter_version": PRODUCTION_GEOMETRY_ADAPTER_VERSION,
            "config_fingerprint": payload.config.fingerprint(),
            "preview_job_id": preview_job_id,
            "processed_labels_sha256": payload.processed_labels_sha256,
            "source_asset_sha256": payload.source_asset_sha256,
            **derivation.metadata(),
        },
    )


def production_geometry_adapters(
    *,
    vectorizer: Optional[PotraceVectorizer] = None,
) -> GeometryPipelineAdapters:
    adapter = _ProductionGeometryAdapter(vectorizer=vectorizer or PotraceVectorizer())
    return GeometryPipelineAdapters(
        vectorize=adapter.vectorize,
        topology=adapter.topology,
        extrude=adapter.extrude,
        validate=adapter.validate,
        preview=adapter.preview,
    )


class _ProductionGeometryAdapter:
    def __init__(self, *, vectorizer: PotraceVectorizer) -> None:
        self.vectorizer = vectorizer

    def vectorize(
        self,
        request: GeometryJobRequest,
        context: GeometryStageContext,
    ) -> VectorizationOutput:
        payload = _payload(request)
        context.check_canceled()
        colors = payload.config.palette.colors
        materials = tuple(
            sorted(
                (
                    Material.create(
                        name=color.name,
                        color_hex=color.hex.upper(),
                        palette_color_id=color.id,
                        filament_id=color.filament_id,
                    )
                    for color in colors
                ),
                key=lambda item: item.id,
            )
        )
        material_by_palette = {item.palette_color_id: item for item in materials}
        source_labels = tuple(
            sorted(
                (
                    SourceLabel.create(
                        source_asset_id=payload.source_asset_id,
                        processed_labels_sha256=payload.processed_labels_sha256,
                        label_index=index,
                        name=colors[index].name,
                        color_hex=colors[index].hex.upper(),
                        palette_color_id=colors[index].id,
                        material_id=material_by_palette[colors[index].id].id,
                        classification="background" if index == 0 else "artwork",
                    )
                    for index in sorted(set(payload.labels.pixels))
                ),
                key=lambda item: item.id,
            )
        )
        base = RectangleBase.create(
            center=Point2(
                x_mm=payload.config.canvas.width_mm / 2,
                y_mm=payload.config.canvas.height_mm / 2,
            ),
            width_mm=payload.config.canvas.width_mm,
            height_mm=payload.config.canvas.height_mm,
        )
        # Potrace is retained as construction evidence. Pixel ownership remains the exhaustive
        # label field, so a single active-canvas trace cannot create inter-color gaps or overlaps.
        active = np.ones((payload.labels.height, payload.labels.width), dtype=np.bool_)
        result = self.vectorizer.vectorize(
            active,
            PotraceParameters.for_nozzle(
                canvas_width_mm=payload.config.canvas.width_mm,
                canvas_height_mm=payload.config.canvas.height_mm,
                nozzle_diameter_mm=payload.config.printer.nozzle_mm,
            ),
            cancellation=context.cancellation,
        )
        production = ProductionVectorization(
            input=payload,
            result=result,
            materials=materials,
            source_labels=source_labels,
            base=base,
        )
        return VectorizationOutput(
            geometry=production,
            svg=BinaryArtifact(
                payload=result.normalized_svg,
                extension=".svg",
                media_type="image/svg+xml",
                metadata={
                    "potrace_cache_key": result.cache_key,
                    "potrace_version": result.evidence.tool_version,
                    "vector_artifact_sha256": result.normalized_svg_sha256,
                },
            ),
        )

    def topology(
        self,
        request: GeometryJobRequest,
        vectorized: VectorizationOutput,
        context: GeometryStageContext,
    ) -> SharedBoundaryTopologyResult:
        del request
        context.check_canceled()
        production = _vectorized(vectorized)
        return build_shared_boundary_topology(
            production.input.labels,
            source_asset_sha256=production.input.source_asset_sha256,
            source_labels=production.source_labels,
            materials=production.materials,
            base=production.base,
            canvas_width_mm=production.input.config.canvas.width_mm,
            canvas_height_mm=production.input.config.canvas.height_mm,
            vector_paths=tuple(sorted(production.result.paths, key=lambda item: item.id)),
            vector_artifact_sha256=production.result.normalized_svg_sha256,
            palette_color_order=tuple(color.id for color in production.input.config.palette.colors),
        )

    def extrude(
        self,
        request: GeometryJobRequest,
        topology: SharedBoundaryTopologyResult,
        context: GeometryStageContext,
    ) -> ExtrusionOutput:
        payload = _payload(request)
        context.check_canceled()
        layer_height = payload.config.printer.layer_height_mm
        base_layers = _whole_layers(payload.config.geometry.base_thickness_mm, layer_height, "base")
        art_layers = _whole_layers(
            payload.config.geometry.art_thickness_mm,
            layer_height,
            "artwork",
        )
        base_material = next(
            item
            for item in topology.document.materials
            if item.palette_color_id == payload.config.palette.colors[0].id
        )
        base = generate_structural_base(
            topology.document.base,
            material=base_material,
            options=BaseMeshOptions(
                thickness_mm=payload.config.geometry.base_thickness_mm,
                layer_height_mm=layer_height,
                minimum_layers=base_layers,
            ),
            build_bounds=BaseBuildBounds(width_mm=256, depth_mm=256),
        )
        artwork = extrude_artwork_regions(
            topology.document,
            FlushInlayStrategy(
                base_top_z_mm=base.thickness_mm,
                layer_height_mm=layer_height,
                artwork_layers=art_layers,
            ),
            max_workers=geometry_worker_count(
                len(topology.document.islands),
                sum(len(path.segments) for path in topology.document.paths),
            ),
            check_canceled=context.check_canceled,
        )
        assembly = assemble_mesh_document(
            topology.document,
            base_mesh=base.mesh,
            base_part=base.part,
            artwork=artwork,
        )
        geometry_bytes = dump_geometry_ir(assembly.document)
        mesh_bytes = json.dumps(
            {
                "geometry_fingerprint": assembly.document.fingerprint(),
                "meshes": [item.model_dump(mode="json") for item in assembly.document.meshes],
                "parts": [item.model_dump(mode="json") for item in assembly.document.parts],
                "schema_version": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return ExtrusionOutput(
            geometry=assembly.document,
            geometry_ir=BinaryArtifact(
                payload=geometry_bytes,
                extension=".json",
                media_type="application/vnd.image23mf.geometry+json",
                metadata={
                    "geometry_fingerprint": assembly.document.fingerprint(),
                    "mesh_artifact_sha256": assembly.mesh_artifact_sha256,
                },
            ),
            mesh=BinaryArtifact(
                payload=mesh_bytes,
                extension=".json",
                media_type="application/vnd.image23mf.mesh+json",
                metadata={
                    "geometry_fingerprint": assembly.document.fingerprint(),
                    "mesh_count": len(assembly.document.meshes),
                    "part_count": len(assembly.document.parts),
                },
            ),
        )

    def validate(
        self,
        request: GeometryJobRequest,
        extruded: ExtrusionOutput,
        context: GeometryStageContext,
    ) -> ValidationOutput:
        payload = _payload(request)
        context.check_canceled()
        report = validate_geometry_quality(
            extruded.geometry,
            MeshQualityOptions(
                build_bounds=BaseBuildBounds(width_mm=256, depth_mm=256),
                minimum_part_thickness_mm=payload.config.printer.layer_height_mm,
            ),
        )
        report_payload = {
            "evidence": [
                {
                    **item.__dict__,
                    "bounds": item.bounds.__dict__ if item.bounds is not None else None,
                }
                for item in report.evidence
            ],
            "findings": [
                {**item.__dict__, "severity": item.severity.value} for item in report.findings
            ],
            "fingerprint": report.fingerprint,
            "safe_for_export": report.safe_for_export,
            "schema_version": 1,
            "total_signed_volume_mm3": report.total_signed_volume_mm3,
        }
        return ValidationOutput(
            accepted=report.safe_for_export,
            report=BinaryArtifact(
                payload=json.dumps(
                    report_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8"),
                extension=".json",
                media_type="application/json",
                metadata={
                    "geometry_fingerprint": extruded.geometry.fingerprint(),
                    "safe_for_export": report.safe_for_export,
                    "finding_count": len(report.findings),
                },
            ),
        )

    def preview(
        self,
        request: GeometryJobRequest,
        extruded: ExtrusionOutput,
        validation: ValidationOutput,
        context: GeometryStageContext,
    ) -> BinaryArtifact:
        del validation
        payload = _payload(request)
        context.check_canceled()
        raster = rasterize_geometry(extruded.geometry, source="mesh")
        palette = {
            index: _rgb(payload.config.palette.colors[index].hex)
            for index in payload.labels.label_values
        }
        rgba = bytearray(raster.width * raster.height * 4)
        for offset, label in enumerate(raster.assignment.reshape(-1)):
            color = palette.get(int(label), (0, 0, 0))
            rgba[offset * 4 : offset * 4 + 4] = bytes((*color, 255 if label >= 0 else 0))
        image = Image.frombytes("RGBA", (raster.width, raster.height), bytes(rgba))
        encoded = io.BytesIO()
        try:
            image.save(encoded, format="PNG", optimize=False, compress_level=9)
        finally:
            image.close()
        return BinaryArtifact(
            payload=encoded.getvalue(),
            extension=".png",
            media_type="image/png",
            metadata={
                "geometry_fingerprint": extruded.geometry.fingerprint(),
                "height": raster.height,
                "source": raster.source,
                "width": raster.width,
            },
        )


def _payload(request: GeometryJobRequest) -> ProductionGeometryInput:
    if not isinstance(request.payload, ProductionGeometryInput):
        raise TypeError("production geometry request payload has the wrong type")
    return request.payload


def _vectorized(output: VectorizationOutput) -> ProductionVectorization:
    if not isinstance(output.geometry, ProductionVectorization):
        raise TypeError("production vectorization output has the wrong type")
    return output.geometry


def _whole_layers(thickness_mm: float, layer_height_mm: float, label: str) -> int:
    layers = round(thickness_mm / layer_height_mm)
    if layers < 1 or abs(layers * layer_height_mm - thickness_mm) > 0.0001:
        raise ValueError(f"{label} thickness must be a positive whole number of print layers")
    return layers


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]
