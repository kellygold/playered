import hashlib
import json
from collections import Counter
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from image23mf.contracts.job import CropConfig
from image23mf.engine.labels import LabelField
from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize
from image23mf.geometry import topology as topology_core
from image23mf.geometry.model import (
    GeometryDocument,
    LineSegment,
    Material,
    Path2D,
    Point2,
    RectangleBase,
    SourceLabel,
)
from image23mf.geometry.topology import build_shared_boundary_topology
from image23mf.mural.partition import partition_master_labels
from image23mf.mural.planner import (
    BedEnvelope,
    MuralLayout,
    MuralPlanRequest,
    MuralSourceProvenance,
    build_mural_plan,
)
from image23mf.mural.topology_partition import (
    MuralTileTopology,
    MuralTileTopologyEvidence,
    MuralTopologyPartition,
    MuralTopologyPartitionError,
    partition_master_topology,
    recompose_topology_labels,
)
from image23mf.vectorization.potrace import PotraceVectorizer

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
GOLDEN = Path(__file__).parent / "goldens" / "mural_topology_partition_wager_v1.json"


def field(width: int, height: int) -> LabelField:
    pixels = bytes(
        ((x * 4 // width) + (y * 3 // height) + (1 if x == width // 2 and y > height // 3 else 0))
        % 4
        for y in range(height)
        for x in range(width)
    )
    return LabelField(width=width, height=height, label_values=(0, 1, 2, 3), pixels=pixels)


def request_for(
    labels: LabelField,
    *,
    rows: int,
    columns: int,
    panel_width_mm: float = 40,
    panel_height_mm: float = 30,
) -> MuralPlanRequest:
    size = PixelSize(width=labels.width, height=labels.height)
    return MuralPlanRequest(
        source=MuralSourceProvenance(
            source_asset_id="asset_wager",
            source_asset_sha256=HASH_A,
            processed_artifact_id="artifact_wager_master",
            processed_artifact_sha256=HASH_B,
            processed_size=size,
            canonical_transform=CanonicalTransform.from_crop_config(
                original_size=size,
                normalized_size=size,
                exif_orientation=1,
                crop=CropConfig(mode="stretch"),
                working_size=size,
                canvas_size=MillimetreSize(width=200, height=200),
            ),
            config_sha256=HASH_C,
            engine_version="0.1.0",
            revision_id="revision_wager",
        ),
        layout=MuralLayout(
            rows=rows,
            columns=columns,
            panel_width_mm=panel_width_mm,
            panel_height_mm=panel_height_mm,
        ),
        bed=BedEnvelope(
            printer_id="bambu-p2s",
            plate_id="textured-pei",
            profile_catalog_fingerprint=HASH_D,
            width_mm=256,
            height_mm=256,
        ),
    )


def construction_rectangle(width_mm: float, height_mm: float) -> Path2D:
    origin = Point2(x_mm=0, y_mm=0)
    return Path2D.create(
        purpose="construction",
        start=origin,
        segments=(
            LineSegment(end=Point2(x_mm=width_mm, y_mm=0)),
            LineSegment(end=Point2(x_mm=width_mm, y_mm=height_mm)),
            LineSegment(end=Point2(x_mm=0, y_mm=height_mm)),
            LineSegment(end=origin),
        ),
        closed=True,
    )


def authoritative_topology(
    labels: LabelField,
    request: MuralPlanRequest,
    *,
    vector_sha256: str = HASH_E,
):
    plan = build_mural_plan(request)
    colors = ("#111111", "#F99963", "#0078BF", "#CBC6B8")
    materials = tuple(
        sorted(
            (
                Material.create(
                    name=f"Color {index}",
                    color_hex=colors[index],
                    palette_color_id=f"color-{index}",
                    filament_id=f"filament-{index}",
                )
                for index in labels.label_values
            ),
            key=lambda item: item.id,
        )
    )
    material_by_palette = {item.palette_color_id: item for item in materials}
    labels_sha256 = hashlib.sha256(labels.pixels).hexdigest()
    source_labels = tuple(
        sorted(
            (
                SourceLabel.create(
                    source_asset_id=request.source.source_asset_id,
                    processed_labels_sha256=labels_sha256,
                    label_index=index,
                    name=f"Color {index}",
                    color_hex=colors[index],
                    palette_color_id=f"color-{index}",
                    material_id=material_by_palette[f"color-{index}"].id,
                    classification="background" if index == 0 else "artwork",
                )
                for index in labels.label_values
            ),
            key=lambda item: item.id,
        )
    )
    return build_shared_boundary_topology(
        labels,
        source_asset_sha256=request.source.source_asset_sha256,
        source_labels=source_labels,
        materials=materials,
        base=RectangleBase.create(
            center=Point2(
                x_mm=plan.master_size_mm.width / 2,
                y_mm=plan.master_size_mm.height / 2,
            ),
            width_mm=plan.master_size_mm.width,
            height_mm=plan.master_size_mm.height,
        ),
        canvas_width_mm=plan.master_size_mm.width,
        canvas_height_mm=plan.master_size_mm.height,
        vector_paths=(
            construction_rectangle(
                plan.master_size_mm.width,
                plan.master_size_mm.height,
            ),
        ),
        vector_artifact_sha256=vector_sha256,
        palette_color_order=tuple(f"color-{index}" for index in labels.label_values),
    )


def complete_partition(labels: LabelField, request: MuralPlanRequest):
    topology = authoritative_topology(labels, request)
    label_partition = partition_master_labels(
        labels,
        request,
        build_mural_plan(request),
        authoritative_vector_sha256=HASH_E,
        authoritative_topology_sha256=topology.topology_artifact_sha256,
    )
    return topology, label_partition, partition_master_topology(label_partition, topology)


def topology_golden_summary(partition) -> dict:
    manifest = partition.manifest
    return {
        "schema_version": manifest.schema_version,
        "label_partition_sha256": manifest.label_partition_sha256,
        "master_topology_fingerprint": manifest.master_topology_fingerprint,
        "master_topology_artifact_sha256": manifest.master_topology_artifact_sha256,
        "master_vector_artifact_sha256": manifest.master_vector_artifact_sha256,
        "recomposed_labels_sha256": manifest.recomposed_labels_sha256,
        "seam_topology_sha256": manifest.seam_topology_sha256,
        "partition_sha256": manifest.partition_sha256,
        "tiles": [
            {
                "tile_id": tile.tile_id,
                "clipped_vector_artifact_sha256": tile.clipped_vector_artifact_sha256,
                "tile_topology_artifact_sha256": tile.tile_topology_artifact_sha256,
                "tile_geometry_fingerprint": tile.tile_geometry_fingerprint,
                "tile_raster_sha256": tile.nominal_label_crop_sha256,
                "components": [
                    component.model_dump(mode="json") for component in tile.master_components
                ],
            }
            for tile in manifest.tiles
        ],
    }


def test_wager_3x2_topology_partition_recomposes_exactly_without_revectorizing(
    monkeypatch,
) -> None:
    labels = field(121, 79)
    request = request_for(
        labels,
        rows=2,
        columns=3,
        panel_width_mm=200,
        panel_height_mm=200,
    )

    def forbidden_vectorize(*_args, **_kwargs):
        raise AssertionError("tile partition must not invoke Potrace")

    monkeypatch.setattr(PotraceVectorizer, "vectorize", forbidden_vectorize)
    master = authoritative_topology(labels, request)
    label_partition = partition_master_labels(
        labels,
        request,
        build_mural_plan(request),
        authoritative_vector_sha256=HASH_E,
        authoritative_topology_sha256=master.topology_artifact_sha256,
    )

    def forbidden_regeneration(*_args, **_kwargs):
        raise AssertionError("tile partition must clip the master, not rebuild topology")

    monkeypatch.setattr(topology_core, "build_shared_boundary_topology", forbidden_regeneration)
    topology_partition = partition_master_topology(label_partition, master)

    assert recompose_topology_labels(label_partition, topology_partition.tiles) == labels
    assert (
        topology_partition.manifest.recomposed_labels_sha256
        == hashlib.sha256(labels.pixels).hexdigest()
    )
    assert topology_partition.manifest.master_topology_fingerprint == master.fingerprint()
    assert len(topology_partition.tiles) == 6
    assert topology_golden_summary(topology_partition) == json.loads(
        GOLDEN.read_text(encoding="utf-8")
    )
    for tile, label_tile in zip(topology_partition.tiles, label_partition.tiles):
        assert tile.evidence.cleanup_policy == "no_tile_local_cleanup"
        assert tile.evidence.master_vector_path_ids == master.source_vector_path_ids
        assert tile.evidence.vector_clip_bounds_master_mm == (
            label_tile.manifest.topology.master_bounds_mm
        )
        assert tile.evidence.raster_evidence_policy == "separate_nominal_label_recomposition"
        assert tile.topology.coverage.represented_area_mm2 == pytest.approx(40000)


def test_nominal_label_crop_name_preserves_v1_manifest_wire_contract() -> None:
    labels = field(13, 9)
    request = request_for(labels, rows=2, columns=3)
    _master, _label_partition, partition = complete_partition(labels, request)
    evidence = partition.tiles[0].evidence

    legacy_payload = evidence.model_dump(mode="json")
    modern_payload = evidence.model_dump(mode="json", by_alias=False)

    assert "tile_raster_sha256" in legacy_payload
    assert "nominal_label_crop_sha256" not in legacy_payload
    assert "nominal_label_crop_sha256" in modern_payload
    assert "tile_raster_sha256" not in modern_payload
    assert (
        legacy_payload["tile_raster_sha256"]
        == modern_payload["nominal_label_crop_sha256"]
        == evidence.nominal_label_crop_sha256
    )
    assert MuralTileTopologyEvidence.model_validate(legacy_payload) == evidence
    assert MuralTileTopologyEvidence.model_validate(modern_payload) == evidence
    with pytest.warns(DeprecationWarning, match="nominal_label_crop_sha256"):
        assert evidence.tile_raster_sha256 == evidence.nominal_label_crop_sha256

    schema = MuralTileTopologyEvidence.model_json_schema()
    field_schema = schema["properties"]["nominal_label_crop_sha256"]
    assert "nominal visible-label crop" in field_schema["description"]
    assert "not rasterized physical-topology evidence" in field_schema["description"]


@st.composite
def grid_cases(draw):
    rows = draw(st.integers(min_value=1, max_value=4))
    columns = draw(st.integers(min_value=1, max_value=4))
    width = draw(st.integers(min_value=max(columns, 4), max_value=columns + 12))
    height = draw(st.integers(min_value=max(rows, 4), max_value=rows + 12))
    return rows, columns, width, height


@settings(max_examples=30, deadline=None)
@given(grid_cases())
def test_arbitrary_topology_grids_preserve_pixels_components_and_shared_boundaries(case) -> None:
    rows, columns, width, height = case
    labels = field(width, height)
    request = request_for(labels, rows=rows, columns=columns)
    master, label_partition, partition = complete_partition(labels, request)

    assert recompose_topology_labels(label_partition, partition.tiles) == labels
    assert partition.manifest.source_pixel_count == width * height
    assert partition.manifest.represented_pixel_count == width * height
    slice_counts = Counter()
    clipped_areas: Counter[int] = Counter()
    for tile in partition.tiles:
        for component in tile.evidence.master_components:
            slice_counts[component.component_index] += component.pixel_count
            clipped_areas[component.component_index] += component.clipped_area_mm2
    assert slice_counts == Counter(
        {component.component_index: component.pixel_count for component in master.components}
    )
    for component in master.components:
        assert clipped_areas[component.component_index] == pytest.approx(
            component.area_mm2, abs=0.01
        )
    assert all(
        tile.topology.coverage.gap_area_mm2 == 0 and tile.topology.coverage.overlap_area_mm2 == 0
        for tile in partition.tiles
    )


def test_topology_partition_fails_closed_on_wrong_master_vector_or_topology_binding() -> None:
    labels = field(13, 9)
    request = request_for(labels, rows=2, columns=3)
    master = authoritative_topology(labels, request)
    wrong_vector = partition_master_labels(
        labels,
        request,
        build_mural_plan(request),
        authoritative_vector_sha256="9" * 64,
        authoritative_topology_sha256=master.topology_artifact_sha256,
    )
    with pytest.raises(MuralTopologyPartitionError, match="vector artifact"):
        partition_master_topology(wrong_vector, master)

    wrong_topology = partition_master_labels(
        labels,
        request,
        build_mural_plan(request),
        authoritative_vector_sha256=HASH_E,
        authoritative_topology_sha256="9" * 64,
    )
    with pytest.raises(MuralTopologyPartitionError, match="topology artifact"):
        partition_master_topology(wrong_topology, master)


def test_each_tile_records_exact_master_source_label_and_component_lineage() -> None:
    labels = field(17, 11)
    request = request_for(labels, rows=2, columns=3)
    master, _label_partition, partition = complete_partition(labels, request)
    master_labels = {item.label_index: item.id for item in master.document.source_labels}
    master_islands = {item.island_id for item in master.components}

    for tile in partition.tiles:
        assert all(
            lineage.master_source_label_id == master_labels[lineage.label_index]
            for lineage in tile.evidence.source_label_lineage
        )
        assert {
            component.master_island_id for component in tile.evidence.master_components
        }.issubset(master_islands)
        assert (
            sum(component.pixel_count for component in tile.evidence.master_components)
            == tile.evidence.represented_pixel_count
        )
        assert all(component.tile_island_ids for component in tile.evidence.master_components)


def test_equal_size_cross_label_master_component_swap_is_rejected() -> None:
    labels = LabelField(
        width=4,
        height=2,
        label_values=(0, 1, 2, 3),
        pixels=bytes((0, 0, 1, 1, 2, 2, 3, 3)),
    )
    request = request_for(labels, rows=1, columns=2)
    master = authoritative_topology(labels, request)
    first, second, *remaining = master.components
    assert first.pixel_count == second.pixel_count
    assert first.label_index != second.label_index
    swapped = replace(
        master,
        components=(
            replace(
                first,
                island_id=second.island_id,
                exterior_path_id=second.exterior_path_id,
                hole_path_ids=second.hole_path_ids,
            ),
            replace(
                second,
                island_id=first.island_id,
                exterior_path_id=first.exterior_path_id,
                hole_path_ids=first.hole_path_ids,
            ),
            *remaining,
        ),
    )
    labels_partition = partition_master_labels(
        labels,
        request,
        build_mural_plan(request),
        authoritative_vector_sha256=HASH_E,
        authoritative_topology_sha256=master.topology_artifact_sha256,
    )

    with pytest.raises(MuralTopologyPartitionError, match="lineage|ownership"):
        partition_master_topology(labels_partition, swapped)


def test_master_construction_coordinate_changes_are_bound_into_every_tile_derivation() -> None:
    labels = field(13, 9)
    request = request_for(labels, rows=2, columns=3)
    master, label_partition, first = complete_partition(labels, request)
    construction_id = master.source_vector_path_ids[0]
    original_path = next(item for item in master.document.paths if item.id == construction_id)
    changed_path = original_path.model_copy(
        update={"start": Point2(x_mm=0.125, y_mm=original_path.start.y_mm)}
    )
    changed_document = master.document.model_copy(
        update={
            "paths": tuple(
                changed_path if item.id == construction_id else item
                for item in master.document.paths
            )
        }
    )
    changed_master = replace(master, document=changed_document)
    with pytest.raises(MuralTopologyPartitionError, match="stale content-derived identity"):
        partition_master_topology(label_partition, changed_master)
    master_paths = [
        item.model_dump(mode="json")
        for item in master.document.paths
        if item.id in master.source_vector_path_ids
    ]
    expected_digest = hashlib.sha256(
        json.dumps(master_paths, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert {item.evidence.master_vector_paths_sha256 for item in first.tiles} == {expected_digest}


def test_external_vector_bytes_are_honestly_unverified_or_sha_verified() -> None:
    labels = field(13, 9)
    request = request_for(labels, rows=2, columns=3)
    vector_bytes = b"canonical-vector-artifact"
    vector_sha = hashlib.sha256(vector_bytes).hexdigest()
    master = authoritative_topology(labels, request, vector_sha256=vector_sha)
    label_partition = partition_master_labels(
        labels,
        request,
        build_mural_plan(request),
        authoritative_vector_sha256=vector_sha,
        authoritative_topology_sha256=master.topology_artifact_sha256,
    )

    opaque = partition_master_topology(label_partition, master)
    assert {item.evidence.external_vector_artifact_verification for item in opaque.tiles} == {
        "opaque_sha256_unverified"
    }
    verified = partition_master_topology(
        label_partition, master, vector_artifact_bytes=vector_bytes
    )
    assert {item.evidence.external_vector_artifact_verification for item in verified.tiles} == {
        "supplied_bytes_sha256_verified"
    }
    with pytest.raises(MuralTopologyPartitionError, match="bytes do not match"):
        partition_master_topology(label_partition, master, vector_artifact_bytes=b"tampered")


def staircase_field(width: int, height: int) -> LabelField:
    return LabelField(
        width=width,
        height=height,
        label_values=(0, 1),
        pixels=bytes(
            1 if x >= (source_y * width // height) else 0
            for source_y in range(height)
            for x in range(width)
        ),
    )


def label_areas(document: GeometryDocument) -> dict[int, float]:
    paths = {item.id: item for item in document.paths}
    contours = {item.id: item for item in document.contours}
    source_labels = {item.id: item for item in document.source_labels}
    areas: Counter[int] = Counter()
    for island in document.islands:
        area = paths[contours[island.exterior_contour_id].path_id].signed_area_mm2
        area += sum(
            paths[contours[contour_id].path_id].signed_area_mm2
            for contour_id in island.hole_contour_ids
        )
        areas[source_labels[island.source_label_id].label_index] += area
    return dict(areas)


def oracle_clip_areas(labels: LabelField, clip, master_width: float, master_height: float):
    result: Counter[int] = Counter()
    for source_y in range(labels.height):
        geometry_y = labels.height - 1 - source_y
        cell_bottom = Fraction(geometry_y, labels.height) * Fraction(str(master_height))
        cell_top = Fraction(geometry_y + 1, labels.height) * Fraction(str(master_height))
        overlap_y = max(
            Fraction(0),
            min(cell_top, Fraction(str(clip.bottom))) - max(cell_bottom, Fraction(str(clip.y))),
        )
        if not overlap_y:
            continue
        for x in range(labels.width):
            cell_left = Fraction(x, labels.width) * Fraction(str(master_width))
            cell_right = Fraction(x + 1, labels.width) * Fraction(str(master_width))
            overlap_x = max(
                Fraction(0),
                min(cell_right, Fraction(str(clip.right))) - max(cell_left, Fraction(str(clip.x))),
            )
            if overlap_x:
                result[labels.pixels[source_y * labels.width + x]] += float(overlap_x * overlap_y)
    return dict(result)


def outer_intervals(tile, *, axis: str, coordinate: float):
    document = tile.topology.document
    paths = {item.id: item for item in document.paths}
    contours = {item.id: item for item in document.contours}
    source_labels = {item.id: item for item in document.source_labels}
    values = []
    for island in document.islands:
        label = source_labels[island.source_label_id].label_index
        for contour_id in (island.exterior_contour_id, *island.hole_contour_ids):
            path = paths[contours[contour_id].path_id]
            points = (path.start, *(segment.end for segment in path.segments))
            for first, second in zip(points, points[1:]):
                if axis == "x" and first.x_mm == second.x_mm == coordinate:
                    values.append(
                        (min(first.y_mm, second.y_mm), max(first.y_mm, second.y_mm), label)
                    )
                if axis == "y" and first.y_mm == second.y_mm == coordinate:
                    values.append(
                        (min(first.x_mm, second.x_mm), max(first.x_mm, second.x_mm), label)
                    )
    return sorted(values)


def oracle_outer_intervals(
    labels: LabelField,
    clip,
    *,
    side: str,
    master_width: float,
    master_height: float,
):
    """Return the authoritative labels immediately inside one physical clip edge.

    A panel seam may coincide with a master-cell boundary.  In that case the two
    panel faces read different adjacent source cells, so each face needs its own
    oracle instead of assuming the labels on opposite sides are identical.
    """
    width = Fraction(str(master_width))
    height = Fraction(str(master_height))
    clip_left = Fraction(str(clip.x))
    clip_right = Fraction(str(clip.right))
    clip_bottom = Fraction(str(clip.y))
    clip_top = Fraction(str(clip.bottom))

    def merged(intervals):
        result = []
        for start, end, label in intervals:
            if result and result[-1][1] == start and result[-1][2] == label:
                result[-1] = (result[-1][0], end, label)
            else:
                result.append((start, end, label))
        return [
            (round(float(start), 6), round(float(end), 6), label) for start, end, label in result
        ]

    if side in {"bottom", "top"}:
        geometry_rows = [
            geometry_y
            for geometry_y in range(labels.height)
            if Fraction(geometry_y + 1, labels.height) * height > clip_bottom
            and Fraction(geometry_y, labels.height) * height < clip_top
        ]
        geometry_y = geometry_rows[0] if side == "bottom" else geometry_rows[-1]
        source_y = labels.height - 1 - geometry_y
        intervals = []
        for x in range(labels.width):
            cell_left = Fraction(x, labels.width) * width
            cell_right = Fraction(x + 1, labels.width) * width
            start = max(cell_left, clip_left)
            end = min(cell_right, clip_right)
            if end > start:
                intervals.append(
                    (
                        start - clip_left,
                        end - clip_left,
                        labels.pixels[source_y * labels.width + x],
                    )
                )
        return merged(intervals)

    if side in {"left", "right"}:
        columns = [
            x
            for x in range(labels.width)
            if Fraction(x + 1, labels.width) * width > clip_left
            and Fraction(x, labels.width) * width < clip_right
        ]
        x = columns[0] if side == "left" else columns[-1]
        intervals = []
        for geometry_y in range(labels.height):
            cell_bottom = Fraction(geometry_y, labels.height) * height
            cell_top = Fraction(geometry_y + 1, labels.height) * height
            start = max(cell_bottom, clip_bottom)
            end = min(cell_top, clip_top)
            if end > start:
                source_y = labels.height - 1 - geometry_y
                intervals.append(
                    (
                        start - clip_bottom,
                        end - clip_bottom,
                        labels.pixels[source_y * labels.width + x],
                    )
                )
        return merged(intervals)

    raise AssertionError(f"unsupported clip side: {side}")


def interval_coverage(intervals):
    merged = []
    for start, end, _label in sorted(intervals):
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@pytest.mark.parametrize(
    ("width", "height", "panel_width", "panel_height"),
    ((13, 9, 40, 30), (1024, 1024, 200, 200)),
)
def test_fractional_master_cells_clip_at_exact_panel_seams_without_rescaling(
    width, height, panel_width, panel_height
) -> None:
    labels = staircase_field(width, height)
    request = request_for(
        labels,
        rows=2,
        columns=3,
        panel_width_mm=panel_width,
        panel_height_mm=panel_height,
    )
    master, label_partition, partition = complete_partition(labels, request)
    master_width = request.layout.master_size.width
    master_height = request.layout.master_size.height

    for label_tile, tile in zip(label_partition.tiles, partition.tiles):
        clip = label_tile.manifest.topology.master_bounds_mm
        assert clip.x == (label_tile.manifest.column - 1) * panel_width
        assert clip.y == (2 - label_tile.manifest.row) * panel_height
        document = tile.topology.document
        assert GeometryDocument.model_validate(document.model_dump(mode="json")) == document
        assert abs(sum(label_areas(document).values()) - panel_width * panel_height) <= 0.01
        expected = oracle_clip_areas(labels, clip, master_width, master_height)
        actual = label_areas(document)
        for label in set(expected) | set(actual):
            assert abs(expected.get(label, 0) - actual.get(label, 0)) <= 0.01
        for path in document.paths:
            if path.purpose != "boundary":
                continue
            for point in (path.start, *(segment.end for segment in path.segments)):
                master_x = point.x_mm + clip.x
                master_y = point.y_mm + clip.y
                x_grid = master_x * width / master_width
                y_grid = master_y * height / master_height
                assert (
                    min(
                        abs(x_grid - round(x_grid)),
                        abs(master_x - clip.x),
                        abs(master_x - clip.right),
                    )
                    <= 1e-5
                )
                assert (
                    min(
                        abs(y_grid - round(y_grid)),
                        abs(master_y - clip.y),
                        abs(master_y - clip.bottom),
                    )
                    <= 1e-5
                )

    by_position = {
        (item.manifest.row, item.manifest.column): tile
        for item, tile in zip(label_partition.tiles, partition.tiles)
    }
    clips_by_position = {
        (item.manifest.row, item.manifest.column): item.manifest.topology.master_bounds_mm
        for item in label_partition.tiles
    }
    for row in (1, 2):
        for column in (1, 2):
            left = by_position[(row, column)]
            right = by_position[(row, column + 1)]
            left_intervals = outer_intervals(left, axis="x", coordinate=panel_width)
            right_intervals = outer_intervals(right, axis="x", coordinate=0)
            assert left_intervals == oracle_outer_intervals(
                labels,
                clips_by_position[(row, column)],
                side="right",
                master_width=master_width,
                master_height=master_height,
            )
            assert right_intervals == oracle_outer_intervals(
                labels,
                clips_by_position[(row, column + 1)],
                side="left",
                master_width=master_width,
                master_height=master_height,
            )
            assert interval_coverage(left_intervals) == interval_coverage(right_intervals)
    for column in (1, 2, 3):
        upper = by_position[(1, column)]
        lower = by_position[(2, column)]
        upper_intervals = outer_intervals(upper, axis="y", coordinate=0)
        lower_intervals = outer_intervals(lower, axis="y", coordinate=panel_height)
        assert upper_intervals == oracle_outer_intervals(
            labels,
            clips_by_position[(1, column)],
            side="bottom",
            master_width=master_width,
            master_height=master_height,
        )
        assert lower_intervals == oracle_outer_intervals(
            labels,
            clips_by_position[(2, column)],
            side="top",
            master_width=master_width,
            master_height=master_height,
        )
        assert interval_coverage(upper_intervals) == interval_coverage(lower_intervals)
    assert sum(
        sum(label_areas(tile.topology.document).values()) for tile in partition.tiles
    ) == pytest.approx(master.coverage.source_area_mm2, abs=0.01)


def test_self_hashed_tile_lineage_rewrite_fails_canonical_runtime_recomputation() -> None:
    labels = field(13, 9)
    request = request_for(labels, rows=2, columns=3)
    master, label_partition, partition = complete_partition(labels, request)
    original_tile = partition.tiles[0]
    rewritten_evidence = original_tile.evidence.model_copy(
        update={"clipped_vector_artifact_sha256": "9" * 64}
    )
    rewritten_tile = MuralTileTopology(
        evidence=rewritten_evidence,
        topology=original_tile.topology,
    )
    rewritten_tiles = (rewritten_tile, *partition.tiles[1:])
    payload = partition.manifest.model_dump(mode="json", exclude={"partition_sha256"})
    payload["tiles"][0] = rewritten_evidence.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    rewritten_manifest = partition.manifest.model_validate({**payload, "partition_sha256": digest})

    with pytest.raises(MuralTopologyPartitionError, match="canonical clip"):
        MuralTopologyPartition(
            labels=label_partition,
            master=master,
            manifest=rewritten_manifest,
            tiles=rewritten_tiles,
        )
