from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

import image23mf.geometry.production as production_module
from image23mf.contracts.job import JobConfig
from image23mf.engine.labels import LabelField
from image23mf.external import CancellationToken
from image23mf.geometry.jobs import GeometryPhase, GeometryStageContext
from image23mf.geometry.model import LineSegment, Path2D, Point2
from image23mf.geometry.production import (
    ProductionGeometryInput,
    production_geometry_adapters,
    production_geometry_request,
)
from image23mf.geometry.serialization import load_geometry_ir
from image23mf.vectorization import PotraceEvidence, PotraceResult


class RectangleVectorizer:
    def vectorize(self, mask, parameters, *, cancellation=None):
        assert mask.shape == (4, 4)
        assert mask.all()
        assert not cancellation.canceled
        path = Path2D.create(
            purpose="construction",
            start=Point2(x_mm=0, y_mm=0),
            segments=(
                LineSegment(end=Point2(x_mm=20, y_mm=0)),
                LineSegment(end=Point2(x_mm=20, y_mm=20)),
                LineSegment(end=Point2(x_mm=0, y_mm=20)),
                LineSegment(end=Point2(x_mm=0, y_mm=0)),
            ),
            closed=True,
        )
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"/>'
        return PotraceResult(
            mask_sha256="1" * 64,
            cache_key="image23mf-potrace-v1:" + "2" * 64,
            parameters=parameters,
            normalized_svg=svg,
            normalized_svg_sha256=hashlib.sha256(svg).hexdigest(),
            paths=(path,),
            evidence=PotraceEvidence(
                executable=Path("/test/potrace"),
                tool_version="1.16",
                command=("potrace",),
                stdout="",
                stderr="",
                duration_seconds=0.01,
                pixel_width_mm=5,
                pixel_height_mm=5,
                turd_size_pixels=0,
                curve_tolerance_pixels=0.02,
            ),
        )


def config() -> JobConfig:
    return JobConfig.model_validate(
        {
            "source_asset_id": "asset-production",
            "canvas": {"width_mm": 20, "height_mm": 20},
            "printer": {
                "nozzle_mm": 0.4,
                "layer_height_mm": 0.2,
            },
            "palette": {
                "colors": [
                    {"id": "bone", "name": "Bone White", "hex": "#cbc6b8"},
                    {"id": "charcoal", "name": "Charcoal", "hex": "#000000"},
                    {"id": "violet", "name": "Absent Violet", "hex": "#7b4ab5"},
                ]
            },
            "geometry": {"base_thickness_mm": 1.2, "art_thickness_mm": 0.6},
        }
    )


def context(tmp_path, phase: GeometryPhase) -> GeometryStageContext:
    return GeometryStageContext(
        job_id="job-production",
        phase=phase,
        workspace=tmp_path,
        cancellation=CancellationToken(),
    )


def test_production_adapters_build_validated_geometry_and_aligned_preview(tmp_path):
    labels = LabelField(
        width=4,
        height=4,
        label_values=(0, 1),
        pixels=bytes(
            [
                0,
                0,
                1,
                1,
                0,
                0,
                1,
                1,
                0,
                0,
                1,
                1,
                0,
                0,
                1,
                1,
            ]
        ),
    )
    payload = ProductionGeometryInput(
        source_asset_id="asset-production",
        source_asset_sha256="a" * 64,
        processed_labels_sha256=hashlib.sha256(labels.pixels).hexdigest(),
        labels=labels,
        config=config(),
    )
    request = production_geometry_request(payload, preview_job_id="job-preview")
    adapters = production_geometry_adapters(vectorizer=RectangleVectorizer())

    vectorized = adapters.vectorize(request, context(tmp_path, GeometryPhase.VECTORIZE))
    topology = adapters.topology(
        request,
        vectorized,
        context(tmp_path, GeometryPhase.TOPOLOGY),
    )
    extruded = adapters.extrude(
        request,
        topology,
        context(tmp_path, GeometryPhase.EXTRUDE),
    )
    validation = adapters.validate(
        request,
        extruded,
        context(tmp_path, GeometryPhase.VALIDATE),
    )
    preview = adapters.preview(
        request,
        extruded,
        validation,
        context(tmp_path, GeometryPhase.PREVIEW),
    )

    document = load_geometry_ir(extruded.geometry_ir.payload)
    assert validation.accepted is True
    assert len(document.parts) == 3  # one base plus one flush part per color region
    assert len(document.meshes) == 3
    assert {item.color_hex for item in document.materials} == {
        "#000000",
        "#7B4AB5",
        "#CBC6B8",
    }
    assert {item.label_index for item in document.source_labels} == {0, 1}
    assert document.palette_color_order == ("bone", "charcoal", "violet")
    assert preview.media_type == "image/png"
    with Image.open(io.BytesIO(preview.payload)) as rendered:
        assert rendered.size == (4, 4)
        assert rendered.convert("RGB").getpixel((0, 0)) == (203, 198, 184)
        assert rendered.convert("RGB").getpixel((3, 0)) == (0, 0, 0)
    assert request.metadata["preview_job_id"] == "job-preview"
    assert (
        request.derivation_key
        == production_geometry_request(
            payload,
            preview_job_id="job-preview",
        ).derivation_key
    )


def test_geometry_cache_reuses_equivalent_preview_and_invalidates_engine_upgrade(
    monkeypatch,
):
    labels = LabelField(width=1, height=1, label_values=(0,), pixels=b"\x00")
    payload = ProductionGeometryInput(
        source_asset_id="asset-production",
        source_asset_sha256="a" * 64,
        processed_labels_sha256=hashlib.sha256(labels.pixels).hexdigest(),
        labels=labels,
        config=config(),
        operations_fingerprint="operations-proof",
    )
    first = production_geometry_request(payload, preview_job_id="preview-first")
    equivalent = production_geometry_request(payload, preview_job_id="preview-second")
    assert equivalent.derivation_key == first.derivation_key

    monkeypatch.setattr(production_module, "__version__", "99.0.0-upgrade-proof")
    upgraded = production_geometry_request(payload, preview_job_id="preview-third")
    assert upgraded.derivation_key != first.derivation_key


def test_production_input_rejects_unverified_or_out_of_palette_labels():
    labels = LabelField(width=1, height=1, label_values=(4,), pixels=b"\x04")
    try:
        ProductionGeometryInput(
            source_asset_id="asset-production",
            source_asset_sha256="a" * 64,
            processed_labels_sha256="b" * 64,
            labels=labels,
            config=config(),
        )
    except ValueError as error:
        assert "SHA-256" in str(error)
    else:  # pragma: no cover - explicit safety assertion
        raise AssertionError("unverified label bytes were accepted")
