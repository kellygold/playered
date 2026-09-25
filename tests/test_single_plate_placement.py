import math

import pytest

from image23mf.bambu.placement import single_plate_placement


@pytest.mark.parametrize(
    "width,height,colors,layer",
    [
        (180, 135, 3, 0.2),
        (180, 135, 4, 0.2),
        (180, 135, 6, 0.2),
        (101.25, 180, 6, 0.2),
        (180, 180, 6, 0.2),
        (200, 200, 4, 0.2),
        (170, 170, 6, 0.1),
    ],
)
def test_artwork_and_tower_have_separate_space_inside_bed(width, height, colors, layer):
    x, y, tower = single_plate_placement(width, height, color_count=colors, layer_height_mm=layer)
    assert tower is not None
    tx, ty = tower
    reserve = max(44, math.ceil(math.sqrt(45 * (colors - 1) / layer * 1.5) + 18))
    assert 0 <= x < x + width <= 256
    assert 0 <= y < y + height <= 256
    assert tx - 3 >= 0 and ty - 3 >= 0
    assert tx - 3 + reserve <= 256 and ty - 3 + reserve <= 256
    assert x >= tx - 3 + reserve + 2 or y + height <= ty - 3 - 2


def test_single_color_keeps_centered_full_bed():
    assert single_plate_placement(256, 256, color_count=1, layer_height_mm=0.2) == (0, 0, None)


def test_centered_landscape_does_not_move_unnecessarily():
    x, y, _ = single_plate_placement(180, 120, color_count=4, layer_height_mm=0.2)
    assert (x, y) == (38, 68)


def test_oversize_multi_color_art_has_actionable_error():
    with pytest.raises(ValueError, match="Reduce the canvas size or use mural tiles"):
        single_plate_placement(250, 250, color_count=6, layer_height_mm=0.2)
