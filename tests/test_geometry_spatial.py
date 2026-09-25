from hypothesis import given
from hypothesis import strategies as st

from image23mf.geometry.spatial import BoundsIndex


@st.composite
def boxes(draw):
    x, y = draw(st.tuples(st.integers(-100, 100), st.integers(-100, 100)))
    w, h = draw(st.tuples(st.integers(0, 100), st.integers(0, 100)))
    return x, y, x + w, y + h


@given(st.lists(boxes(), max_size=150), boxes(), st.sampled_from((0, 0.00001, 1)))
def test_index_matches_exhaustive_bounds_search(bounds, query, tolerance):
    index = BoundsIndex(bounds, tolerance=tolerance)
    expected = [
        i
        for i, b in enumerate(bounds)
        if (
            b[0] <= query[2] + tolerance
            and b[2] >= query[0] - tolerance
            and b[1] <= query[3] + tolerance
            and b[3] >= query[1] - tolerance
        )
    ]
    assert index.query(query) == expected
