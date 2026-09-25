from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from image23mf.engine import LabelField, analyze_regions, derive_region_lineage


def _field(rows: list[list[int]], labels: tuple[int, ...] = (0, 1)) -> LabelField:
    array = np.asarray(rows, dtype=np.uint8)
    return LabelField(
        width=array.shape[1],
        height=array.shape[0],
        label_values=labels,
        pixels=array.tobytes(),
    )


def _analyze(
    rows: list[list[int]],
    *,
    width_mm: float | None = None,
    height_mm: float | None = None,
    active: bytes | None = None,
):
    field = _field(rows)
    return analyze_regions(
        field,
        colors={0: "#ffffff", 1: "#000000"},
        width_mm=width_mm or field.width,
        height_mm=height_mm or field.height,
        active=active,
    )


def test_known_square_has_exact_geometry_border_adjacency_and_width() -> None:
    analysis = _analyze(
        [
            [0, 0, 0, 0, 0],
            [0, 1, 1, 1, 0],
            [0, 1, 1, 1, 0],
            [0, 1, 1, 1, 0],
            [0, 0, 0, 0, 0],
        ]
    )
    graph = analysis.graph
    center = next(region for region in graph.regions if region.label == 1)
    outside = next(region for region in graph.regions if region.label == 0)

    assert graph.active_pixel_count == 25
    assert center.color == "#000000"
    assert center.pixel_count == 9
    assert center.area_mm2 == 9
    assert center.perimeter_edge_count == 12
    assert center.perimeter_mm == 12
    assert center.compactness == pytest.approx(math.pi / 4)
    assert center.pixel_bounds.model_dump() == {"x": 1, "y": 1, "width": 3, "height": 3}
    assert center.physical_bounds.model_dump() == {
        "x_mm": 1,
        "y_mm": 1,
        "width_mm": 3,
        "height_mm": 3,
    }
    assert center.border_contact.total_mm == 0
    assert center.width_estimate.minimum_mm == 3
    assert center.width_estimate.median_mm == 3
    assert center.width_estimate.maximum_mm == 3
    assert outside.border_contact.total_mm == 20
    assert outside.perimeter_mm == 32
    assert center.neighbor_region_ids == (outside.id,)
    assert outside.neighbor_region_ids == (center.id,)
    assert len(graph.adjacency) == 1
    assert graph.adjacency[0].boundary_edge_count == 12
    assert graph.adjacency[0].boundary_length_mm == 12
    assert len(graph.fingerprint()) == 64


def test_anisotropic_pixels_measure_boundary_lengths_in_physical_axes() -> None:
    analysis = _analyze([[1, 1]], width_mm=6, height_mm=2)
    region = analysis.graph.regions[0]

    assert region.area_mm2 == 12
    assert region.perimeter_edge_count == 6
    assert region.perimeter_mm == 16
    assert region.border_contact.top_mm == 6
    assert region.border_contact.right_mm == 2
    assert region.border_contact.bottom_mm == 6
    assert region.border_contact.left_mm == 2
    assert region.width_estimate.minimum_mm == 2
    assert region.width_estimate.maximum_mm == 6


def test_region_ids_do_not_renumber_when_an_unrelated_component_changes() -> None:
    before = _analyze([[1, 0, 0, 1], [1, 0, 0, 1]])
    after = _analyze([[1, 0, 0, 0], [1, 0, 0, 0]])
    unchanged_before = next(
        region
        for region in before.graph.regions
        if region.label == 1 and region.pixel_bounds.x == 0
    )
    unchanged_after = next(
        region for region in after.graph.regions if region.label == 1 and region.pixel_bounds.x == 0
    )

    assert unchanged_before.id == unchanged_after.id
    assert before.graph.fingerprint() != after.graph.fingerprint()


def test_transparency_excludes_inactive_pixels_and_can_produce_an_empty_graph() -> None:
    field = _field([[0, 1], [1, 0]])
    active = bytes((1, 0, 0, 0))
    single = analyze_regions(
        field,
        colors={0: "#FFFFFF", 1: "#000000"},
        width_mm=2,
        height_mm=2,
        active=active,
    )
    empty = analyze_regions(
        field,
        colors={0: "#FFFFFF", 1: "#000000"},
        width_mm=2,
        height_mm=2,
        active=bytes(4),
    )

    assert single.graph.active_pixel_count == 1
    assert len(single.graph.regions) == 1
    assert np.count_nonzero(single.assignment >= 0) == 1
    assert empty.graph.active_pixel_count == 0
    assert empty.graph.regions == ()
    assert empty.graph.adjacency == ()
    assert np.all(empty.assignment == -1)


def test_lineage_reports_unchanged_modified_split_merge_created_and_deleted() -> None:
    unchanged = _analyze([[1, 1, 1]])
    assert [event.kind for event in derive_region_lineage(unchanged, unchanged).events] == [
        "unchanged"
    ]

    modified = _analyze([[0, 0, 0]])
    modified_events = derive_region_lineage(unchanged, modified).events
    assert {event.kind for event in modified_events} == {"modified"}

    before_split = _analyze([[1, 1, 1]], active=bytes((1, 1, 1)))
    after_split = _analyze([[1, 1, 1]], active=bytes((1, 0, 1)))
    split = derive_region_lineage(before_split, after_split)
    assert [event.kind for event in split.events] == ["split"]
    assert len(split.overlaps) == 2
    assert split.events[0].overlap_pixel_count == 2
    assert [event.kind for event in derive_region_lineage(after_split, before_split).events] == [
        "merged"
    ]

    deleted = _analyze([[1]], active=b"\x01")
    created = _analyze([[1]], active=b"\x00")
    assert [event.kind for event in derive_region_lineage(deleted, created).events] == ["deleted"]
    assert [event.kind for event in derive_region_lineage(created, deleted).events] == ["created"]
    assert len(split.fingerprint()) == 64


def test_invalid_inputs_and_incompatible_lineage_are_rejected() -> None:
    field = _field([[0, 1]])
    with pytest.raises(ValueError, match="exactly one"):
        analyze_regions(field, colors={0: "#FFFFFF"}, width_mm=2, height_mm=1)
    with pytest.raises(ValueError, match="invalid color"):
        analyze_regions(
            field,
            colors={0: "white", 1: "#000000"},
            width_mm=2,
            height_mm=1,
        )
    with pytest.raises(ValueError, match="active byte count"):
        analyze_regions(
            field,
            colors={0: "#FFFFFF", 1: "#000000"},
            width_mm=2,
            height_mm=1,
            active=b"\x01",
        )
    first = _analyze([[0, 1]])
    second = _analyze([[0], [1]])
    with pytest.raises(ValueError, match="matching pixel"):
        derive_region_lineage(first, second)


@given(
    width=st.integers(min_value=1, max_value=12),
    height=st.integers(min_value=1, max_value=12),
    values=st.data(),
)
@settings(max_examples=60, deadline=None)
def test_every_active_pixel_has_exactly_one_region_and_graph_is_deterministic(
    width: int, height: int, values: st.DataObject
) -> None:
    pixels = values.draw(
        st.lists(
            st.integers(min_value=0, max_value=2),
            min_size=width * height,
            max_size=width * height,
        )
    )
    active_values = values.draw(
        st.lists(st.booleans(), min_size=width * height, max_size=width * height)
    )
    field = LabelField(
        width=width,
        height=height,
        label_values=(0, 1, 2),
        pixels=bytes(pixels),
    )
    kwargs = {
        "colors": {0: "#000000", 1: "#777777", 2: "#FFFFFF"},
        "width_mm": width * 0.4,
        "height_mm": height * 0.6,
        "active": bytes(active_values),
    }

    first = analyze_regions(field, **kwargs)
    second = analyze_regions(field, **kwargs)

    assert first.graph == second.graph
    assert np.array_equal(first.assignment, second.assignment)
    assert first.graph.active_pixel_count == sum(active_values)
    assert sum(region.pixel_count for region in first.graph.regions) == sum(active_values)
    assert np.count_nonzero(first.assignment >= 0) == sum(active_values)
    assert len({region.id for region in first.graph.regions}) == len(first.graph.regions)
    for edge in first.graph.adjacency:
        assert edge.first_region_id < edge.second_region_id
