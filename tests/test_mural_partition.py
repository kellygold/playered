import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from image23mf.contracts.job import CropConfig
from image23mf.engine.labels import LabelField
from image23mf.engine.transform import CanonicalTransform, MillimetreSize, PixelSize
from image23mf.mural.partition import (
    IndependentSeamMutationError,
    MuralLabelPartition,
    MuralLabelTile,
    MuralPartitionError,
    PartitionWarningCode,
    SeamMutationPolicy,
    TileSide,
    assess_independent_tile_candidate,
    partition_master_labels,
    recompose_visible_tiles,
)
from image23mf.mural.planner import (
    BedEnvelope,
    MuralLayout,
    MuralPlanRequest,
    MuralSourceProvenance,
    build_mural_plan,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
GOLDEN = Path(__file__).parent / "goldens" / "mural_partition_wager_v1.json"


def label_field(width: int, height: int) -> LabelField:
    pixels = bytes(
        ((x // 3) + (y // 2) + (1 if x == width // 2 or y == height // 2 else 0)) % 4
        for y in range(height)
        for x in range(width)
    )
    return LabelField(
        width=width,
        height=height,
        label_values=(0, 1, 2, 3),
        pixels=pixels,
    )


def mural_request(
    width: int,
    height: int,
    *,
    rows: int,
    columns: int,
    panel_width_mm: float = 200,
    panel_height_mm: float = 200,
    horizontal_gap_mm: float = 0,
    vertical_gap_mm: float = 0,
    bleed_mm: float = 0,
) -> MuralPlanRequest:
    size = PixelSize(width=width, height=height)
    transform = CanonicalTransform.from_crop_config(
        original_size=size,
        normalized_size=size,
        exif_orientation=1,
        crop=CropConfig(mode="stretch"),
        working_size=size,
        canvas_size=MillimetreSize(width=200, height=200),
    )
    return MuralPlanRequest(
        source=MuralSourceProvenance(
            source_asset_id="asset_wager",
            source_asset_sha256=HASH_A,
            processed_artifact_id="artifact_wager_master",
            processed_artifact_sha256=HASH_B,
            processed_size=size,
            canonical_transform=transform,
            config_sha256=HASH_C,
            engine_version="0.1.0",
            revision_id="revision_wager",
        ),
        layout=MuralLayout(
            rows=rows,
            columns=columns,
            panel_width_mm=panel_width_mm,
            panel_height_mm=panel_height_mm,
            horizontal_gap_mm=horizontal_gap_mm,
            vertical_gap_mm=vertical_gap_mm,
            bleed_mm=bleed_mm,
        ),
        bed=BedEnvelope(
            printer_id="bambu-p2s",
            plate_id="textured-pei",
            profile_catalog_fingerprint=HASH_D,
            width_mm=256,
            height_mm=256,
        ),
    )


def wager_partition():
    master = label_field(1201, 799)
    request = mural_request(
        master.width,
        master.height,
        rows=2,
        columns=3,
        horizontal_gap_mm=8,
        vertical_gap_mm=12,
        bleed_mm=3,
    )
    return (
        master,
        request,
        partition_master_labels(
            master,
            request,
            build_mural_plan(request),
            authoritative_vector_sha256=HASH_E,
            authoritative_topology_sha256=HASH_F,
        ),
    )


def golden_summary(partition) -> dict:
    manifest = partition.manifest
    return {
        "schema_version": manifest.schema_version,
        "partition_sha256": manifest.partition_sha256,
        "master_labels_sha256": manifest.master_labels.pixels_sha256,
        "layout": manifest.layout.model_dump(mode="json"),
        "warnings": [warning.code.value for warning in manifest.warnings],
        "tiles": [
            {
                "tile_id": tile.tile_id,
                "visible_bounds": tile.visible_master_bounds.model_dump(mode="json"),
                "sampled_bounds": tile.sampled_master_bounds.model_dump(mode="json"),
                "visible_sha256": tile.visible_labels.pixels_sha256,
                "sampled_sha256": tile.sampled_labels.pixels_sha256,
                "protected_sides": [side.value for side in tile.topology.protected_sides],
                "visible_output_bounds_mm": (
                    tile.topology.visible_output_bounds_mm.model_dump(mode="json")
                ),
            }
            for tile in manifest.tiles
        ],
        "seams": [
            {
                "id": seam.id,
                "first_tile_id": seam.first_tile_id,
                "second_tile_id": seam.second_tile_id,
                "first_guard_sha256": seam.first_guard_sha256,
                "second_guard_sha256": seam.second_guard_sha256,
            }
            for seam in manifest.seams
        ],
    }


def test_wager_3x2_partition_matches_golden_and_recomposes_losslessly() -> None:
    master, _request, partition = wager_partition()

    assert recompose_visible_tiles(partition) == master
    assert len(partition.tiles) == 6
    assert len(partition.manifest.seams) == 7
    assert partition.manifest.authoritative_vector_sha256 == HASH_E
    assert partition.manifest.authoritative_topology_sha256 == HASH_F
    assert golden_summary(partition) == json.loads(GOLDEN.read_text(encoding="utf-8"))


@st.composite
def arbitrary_partition_cases(draw):
    rows = draw(st.integers(min_value=1, max_value=8))
    columns = draw(st.integers(min_value=1, max_value=8))
    width = draw(st.integers(min_value=columns, max_value=columns + 32))
    height = draw(st.integers(min_value=rows, max_value=rows + 32))
    return rows, columns, width, height


@settings(max_examples=100, deadline=None)
@given(arbitrary_partition_cases())
def test_arbitrary_non_square_grids_recompose_every_master_pixel_exactly_once(case) -> None:
    rows, columns, width, height = case
    master = label_field(width, height)
    request = mural_request(
        width,
        height,
        rows=rows,
        columns=columns,
        panel_width_mm=37,
        panel_height_mm=53,
    )

    partition = partition_master_labels(master, request, build_mural_plan(request))

    assert recompose_visible_tiles(partition) == master
    assert len(partition.tiles) == rows * columns
    assert len(partition.manifest.seams) == (rows * (columns - 1) + columns * (rows - 1))
    assert (
        sum(tile.visible_labels.width * tile.visible_labels.height for tile in partition.tiles)
        == width * height
    )


def test_bleed_and_assembly_gaps_never_change_nominal_visible_art() -> None:
    master = label_field(53, 37)
    plain_request = mural_request(53, 37, rows=2, columns=3)
    decorated_request = mural_request(
        53,
        37,
        rows=2,
        columns=3,
        horizontal_gap_mm=9,
        vertical_gap_mm=11,
        bleed_mm=4,
    )
    plain = partition_master_labels(master, plain_request, build_mural_plan(plain_request))
    decorated = partition_master_labels(
        master,
        decorated_request,
        build_mural_plan(decorated_request),
    )

    assert [tile.visible_labels for tile in decorated.tiles] == [
        tile.visible_labels for tile in plain.tiles
    ]
    assert recompose_visible_tiles(decorated) == master
    assert any(
        decorated_tile.sampled_labels != plain_tile.sampled_labels
        for decorated_tile, plain_tile in zip(decorated.tiles, plain.tiles)
    )
    assert {warning.code for warning in decorated.manifest.warnings} == {
        PartitionWarningCode.INDEPENDENT_SEAM_CLEANUP_FORBIDDEN,
        PartitionWarningCode.BLEED_SAMPLING_ONLY,
        PartitionWarningCode.ASSEMBLY_GAP_LAYOUT_ONLY,
    }
    for tile in decorated.manifest.tiles:
        assert tile.topology.visible_output_bounds_mm.x == 4
        assert tile.topology.visible_output_bounds_mm.y == 4


def test_tile_local_cleanup_forbids_or_explicitly_warns_on_shared_edge_change() -> None:
    master = label_field(18, 12)
    request = mural_request(18, 12, rows=2, columns=3)
    partition = partition_master_labels(master, request, build_mural_plan(request))
    tile = partition.tile("tile-r01-c01")
    changed = bytearray(tile.visible_labels.pixels)
    protected_index = tile.visible_labels.width - 1
    changed[protected_index] = (changed[protected_index] + 1) % 4
    candidate = LabelField(
        width=tile.visible_labels.width,
        height=tile.visible_labels.height,
        label_values=tile.visible_labels.label_values,
        pixels=bytes(changed),
    )

    with pytest.raises(IndependentSeamMutationError, match="authoritative master"):
        assess_independent_tile_candidate(partition, tile.manifest.tile_id, candidate)

    assessment = assess_independent_tile_candidate(
        partition,
        tile.manifest.tile_id,
        candidate,
        policy=SeamMutationPolicy.WARN,
    )
    assert assessment.accepted is True
    assert assessment.changed_pixel_count == 1
    assert assessment.protected_changed_pixel_count == 1
    assert assessment.warnings[0].code == PartitionWarningCode.PROTECTED_SEAM_CHANGED


def test_interior_tile_candidate_is_safe_but_cannot_impersonate_persisted_tile() -> None:
    master = label_field(18, 12)
    request = mural_request(18, 12, rows=2, columns=3)
    partition = partition_master_labels(master, request, build_mural_plan(request))
    tile = partition.tile("tile-r01-c01")
    changed = bytearray(tile.visible_labels.pixels)
    interior_index = tile.visible_labels.width + 1
    changed[interior_index] = (changed[interior_index] + 1) % 4
    candidate = LabelField(
        width=tile.visible_labels.width,
        height=tile.visible_labels.height,
        label_values=tile.visible_labels.label_values,
        pixels=bytes(changed),
    )

    assessment = assess_independent_tile_candidate(partition, tile.manifest.tile_id, candidate)
    assert assessment.accepted is True
    assert assessment.protected_changed_pixel_count == 0

    substituted = MuralLabelTile(
        manifest=tile.manifest,
        visible_labels=candidate,
        sampled_labels=tile.sampled_labels,
    )
    supplied = (substituted, *partition.tiles[1:])
    with pytest.raises(MuralPartitionError, match="persisted identity"):
        recompose_visible_tiles(partition, supplied)


def test_topology_recipes_use_nominal_crops_and_protect_only_internal_sides() -> None:
    master = label_field(17, 11)
    request = mural_request(17, 11, rows=2, columns=3, bleed_mm=2)
    partition = partition_master_labels(master, request, build_mural_plan(request))

    upper_left = partition.tile("tile-r01-c01").manifest.topology
    center = partition.tile("tile-r01-c02").manifest.topology
    lower_right = partition.tile("tile-r02-c03").manifest.topology
    assert upper_left.protected_sides == (TileSide.RIGHT, TileSide.BOTTOM)
    assert center.protected_sides == (TileSide.RIGHT, TileSide.BOTTOM, TileSide.LEFT)
    assert lower_right.protected_sides == (TileSide.TOP, TileSide.LEFT)
    assert upper_left.cleanup_policy == "master_only"
    assert upper_left.local_canvas_size_mm == MillimetreSize(width=200, height=200)
    assert upper_left.master_pixel_bounds == partition.manifest.tiles[0].visible_master_bounds
    assert upper_left.master_bounds_mm.y == 200
    assert lower_right.master_bounds_mm.y == 0


def test_deleted_seams_with_self_consistent_manifest_hash_fail_runtime_revalidation() -> None:
    _master, _request, partition = wager_partition()
    payload = partition.manifest.model_dump(mode="json", exclude={"partition_sha256"})
    payload["seams"] = []
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    rewritten = partition.manifest.model_validate({**payload, "partition_sha256": digest})

    with pytest.raises(MuralPartitionError, match="canonical derivation"):
        MuralLabelPartition(
            master_labels=partition.master_labels,
            request=partition.request,
            plan=partition.plan,
            manifest=rewritten,
            tiles=partition.tiles,
        )


def test_rewritten_tile_and_manifest_hashes_cannot_impersonate_master_partition() -> None:
    _master, _request, partition = wager_partition()
    original = partition.tiles[0]
    rewritten_tile_manifest = original.manifest.model_copy(update={"build_plate_index": 6})
    rewritten_tile = replace(original, manifest=rewritten_tile_manifest)
    rewritten_tiles = (rewritten_tile, *partition.tiles[1:])
    payload = partition.manifest.model_dump(mode="json", exclude={"partition_sha256"})
    payload["tiles"][0] = rewritten_tile_manifest.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    rewritten_manifest = partition.manifest.model_validate({**payload, "partition_sha256": digest})

    with pytest.raises(MuralPartitionError, match="canonical derivation"):
        MuralLabelPartition(
            master_labels=partition.master_labels,
            request=partition.request,
            plan=partition.plan,
            manifest=rewritten_manifest,
            tiles=rewritten_tiles,
        )


def test_partition_rejects_stale_dimensions_and_noncanonical_plan() -> None:
    master = label_field(17, 11)
    request = mural_request(17, 11, rows=2, columns=3)
    plan = build_mural_plan(request)
    stale_master = label_field(18, 11)
    with pytest.raises(MuralPartitionError, match="dimensions"):
        partition_master_labels(stale_master, request, plan)

    tampered = plan.model_copy(update={"request_fingerprint": "0" * 64})
    with pytest.raises(MuralPartitionError, match="canonical plan"):
        partition_master_labels(master, request, tampered)


def test_recomposition_rejects_missing_and_duplicate_tiles() -> None:
    master = label_field(17, 11)
    request = mural_request(17, 11, rows=2, columns=3)
    partition = partition_master_labels(master, request, build_mural_plan(request))

    with pytest.raises(MuralPartitionError, match="every nominal tile"):
        recompose_visible_tiles(partition, partition.tiles[:-1])
    with pytest.raises(MuralPartitionError, match="duplicate"):
        recompose_visible_tiles(
            partition,
            (*partition.tiles[:-1], partition.tiles[0]),
        )


def test_partition_hash_covers_source_plan_tiles_seams_and_topology_evidence() -> None:
    master, request, first = wager_partition()
    second = partition_master_labels(
        master,
        request,
        build_mural_plan(request),
        authoritative_vector_sha256=HASH_E,
        authoritative_topology_sha256="9" * 64,
    )

    assert first.manifest.partition_sha256 != second.manifest.partition_sha256
    assert first.manifest.master_labels.pixels_sha256 == hashlib.sha256(master.pixels).hexdigest()
