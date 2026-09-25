import io
import zipfile

import numpy as np
import pytest
from PIL import Image

from image23mf.bambu.preview import canvas_translation, render_footprints


def render(code):
    return render_footprints(
        io.BytesIO(code.encode()),
        palette=("#FF8800", "#101010"),
        width_mm=10,
        height_mm=10,
        origin_x=20,
        origin_y=30,
        width_px=100,
        height_px=100,
        base_top_mm=1.2,
    )


def test_actual_extrusion_leaves_base_visible_and_ignores_travel_and_tower():
    result = render("""M83
; LINE_WIDTH: 1
G0 X20 Y35 Z1.2
G1 X30 Y35 E1
T1
; LINE_WIDTH: 0.4
G0 X20 Y34 Z1.4
G1 X30 Y34 E1
G0 X20 Y36
G1 X30 Y36 E1
G0 X20 Y35
G1 X30 Y35 E0
G0 X80 Y90
G1 X90 Y90 E1
""")
    image = np.asarray(Image.open(io.BytesIO(result.png)))
    assert tuple(image[50, 50, :3]) == (255, 136, 0)  # gap is not filled from mesh
    assert tuple(image[40, 50, :3]) == (16, 16, 16)
    assert image[10, 50, 3] == 0  # no invented solid canvas
    assert result.drawn_segments == 3
    assert not np.asarray(result.artwork_mask)[150, 150]


def test_absolute_extrusion_reset_and_retraction_do_not_paint_false_paths():
    result = render("""M82
; LINE_WIDTH: 0.4
G0 X20 Y35 Z1.4
G92 E0
G1 X25 Y35 E1
G1 X30 Y35 E0.5
G92 E0
G0 X20 Y36
G1 X25 Y36 E1
""")
    image = np.asarray(Image.open(io.BytesIO(result.png)))
    assert image[50, 20, 3] > 0
    assert image[50, 80, 3] == 0
    assert result.drawn_segments == 2


def test_clockwise_arc_is_curved_not_omitted_or_rendered_as_a_chord():
    result = render("; LINE_WIDTH: 0.4\nG0 X22 Y35 Z1.4\nG2 X28 Y35 I3 J0 E1\n")
    image = np.asarray(Image.open(io.BytesIO(result.png)))
    assert image[20, 50, 3] > 0  # clockwise upper semicircle
    assert image[50, 50, 3] == 0  # no straight chord through the center


def test_unsupported_radius_arc_fails_explicitly():
    with pytest.raises(ValueError, match="I/J centers"):
        render("; LINE_WIDTH: 0.4\nG0 X22 Y35 Z1.4\nG2 X28 Y35 R3 E1\n")


def test_cancellation_is_checked():
    def cancel():
        raise RuntimeError("canceled")

    with pytest.raises(RuntimeError, match="canceled"):
        render_footprints(
            io.BytesIO(b"M83\n"),
            palette=("#FFFFFF",),
            width_mm=10,
            height_mm=10,
            origin_x=0,
            origin_y=0,
            width_px=10,
            height_px=10,
            base_top_mm=1.2,
            check_canceled=cancel,
        )


def test_translation_is_read_from_exact_package_and_rotation_is_rejected(tmp_path):
    path = tmp_path / "project.3mf"

    def package(matrix):
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(
                "3D/3dmodel.model",
                """<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
unit="millimeter"><build>"""
                + f'<item transform="{matrix}"/></build></model>',
            )

    package("1 0 0 0 1 0 0 0 1 38 28 0")
    assert canvas_translation(path) == (38, 28)
    package("0 1 0 -1 0 0 0 0 1 38 28 0")
    with pytest.raises(ValueError, match="unrotated"):
        canvas_translation(path)


def test_full_circle_without_xy_endpoint_is_not_dropped():
    result = render("; LINE_WIDTH: 0.4\nG0 X22 Y35 Z1.4\nG3 I3 J0 E1\n")
    image = np.asarray(Image.open(io.BytesIO(result.png)))
    assert image[20, 50, 3] > 0
    assert image[80, 50, 3] > 0
    assert image[50, 50, 3] == 0


def test_low_resolution_source_does_not_inflate_physical_line_width():
    result = render_footprints(
        io.BytesIO(b"; LINE_WIDTH: 0.42\nG0 X10 Y100 Z1.4\nG1 X190 Y100 E1\n"),
        palette=("#FFFFFF",),
        width_mm=200,
        height_mm=200,
        origin_x=0,
        origin_y=0,
        width_px=400,
        height_px=400,
        base_top_mm=1.2,
    )
    coverage = np.asarray(result.artwork_mask)
    width_mm = coverage[:, coverage.shape[1] // 2].sum() / (coverage.shape[0] / 200)
    assert abs(width_mm - 0.42) < 0.025  # previously .50 mm at this source size


def test_special_tool_purge_off_canvas_does_not_hide_valid_preview():
    result = render(
        "; LINE_WIDTH: 0.4\nG0 X20 Y35 Z1.4\nG1 X30 Y35 E1\nT255\nG0 X80 Y90\nG1 X90 Y90 E1\n"
    )
    assert result.drawn_segments == 1
