import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from image23mf.engine.labels import LabelField
from image23mf.engine.regions import RegionAnalysis, analyze_regions, first_mismatched_region


@settings(max_examples=30, deadline=None)
@given(st.lists(st.integers(0, 1), min_size=64, max_size=64), st.integers(0, 63))
def test_linear_validation_matches_exhaustive_region_checks(pixels, changed):
    original = LabelField(width=8, height=8, label_values=(0, 1), pixels=bytes(pixels))
    active = bytes(int(i % 5 != 0) for i in range(64))
    analysis = analyze_regions(
        original, colors={0: "#FFFFFF", 1: "#000000"}, width_mm=8, height_mm=4, active=active
    )
    altered = list(pixels)
    altered[changed] = 1 - altered[changed]
    labels = LabelField(width=8, height=8, label_values=(0, 1), pixels=bytes(altered))
    array = np.asarray(altered).reshape(8, 8)
    expected = next(
        (
            r
            for i, r in enumerate(analysis.graph.regions)
            if not np.all(array[analysis.assignment == i] == r.label)
        ),
        None,
    )
    assert first_mismatched_region(labels, analysis) == expected


def test_fragmented_raster_is_not_compared_once_per_region():
    class CountedAssignment(np.ndarray):
        comparisons = 0

        def __eq__(self, other):
            type(self).comparisons += 1
            return super().__eq__(other)

    pixels = (np.indices((32, 32)).sum(axis=0) % 2).astype(np.uint8)
    labels = LabelField(width=32, height=32, label_values=(0, 1), pixels=pixels.tobytes())
    analysis = analyze_regions(
        labels, colors={0: "#FFFFFF", 1: "#000000"}, width_mm=32, height_mm=32
    )
    assignment = analysis.assignment.view(CountedAssignment)
    assert len(analysis.graph.regions) == 1024
    assert first_mismatched_region(labels, RegionAnalysis(analysis.graph, assignment)) is None
    assert CountedAssignment.comparisons <= 2


def test_validation_rejects_unknown_assignment_instead_of_indexing_outside_graph():
    labels = LabelField(width=2, height=1, label_values=(0,), pixels=b"\x00\x00")
    analysis = analyze_regions(labels, colors={0: "#FFFFFF"}, width_mm=2, height_mm=1)
    invalid = np.asarray([[0, 99]], dtype=np.int32)
    invalid.flags.writeable = False
    with pytest.raises(ValueError, match="unknown region"):
        first_mismatched_region(labels, RegionAnalysis(analysis.graph, invalid))
