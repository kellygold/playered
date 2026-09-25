from __future__ import annotations

import shutil
import threading
from pathlib import Path

import numpy as np
import pytest

from image23mf.external import CancellationToken, ExternalToolRegistry, ToolId, ToolRunner, ToolSpec
from image23mf.vectorization import (
    PotraceCanceledError,
    PotraceExecutionError,
    PotraceOutputError,
    PotraceParameters,
    PotraceTimeoutError,
    PotraceUnavailableError,
    PotraceVectorizer,
)


def svg(path_data: str, *, width: float = 20, height: float = 10) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" '
        f'height="{height}mm" viewBox="0 0 {width} {height}">'
        f'<path d="{path_data}"/></svg>'
    )


def fake_potrace(
    tmp_path: Path,
    output: str,
    *,
    version: str = "1.16",
    delay: float = 0,
    stdout_bytes: int = 0,
    exit_code: int = 0,
) -> Path:
    executable = tmp_path / "fake potrace;literal"
    failure = f"raise SystemExit({exit_code})\n" if exit_code else ""
    body = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, time\n"
        f"VERSION = {version!r}\n"
        f"OUTPUT = {output!r}\n"
        "if '--version' in sys.argv:\n"
        "    print('potrace ' + VERSION)\n"
        "    raise SystemExit(0)\n"
        f"time.sleep({delay!r})\n"
        f"print('x' * {stdout_bytes})\n"
        + failure
        + "target = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "target.write_text(OUTPUT, encoding='utf-8')\n"
    )
    executable.write_text(
        body,
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def registry(tmp_path: Path, executable: Path, *, minimum: tuple[int, ...] = (1, 16)):
    runner = ToolRunner(temp_root=tmp_path / "tool runs", max_log_bytes=1024)
    spec = ToolSpec(
        id=ToolId.POTRACE,
        name="Potrace",
        purpose="test",
        candidates=(str(executable),),
        version_arguments=("--version",),
        version_pattern=r"potrace\s+(\d+(?:\.\d+)+)",
        minimum_version=minimum,
    )
    return runner, ExternalToolRegistry(runner=runner, specs={ToolId.POTRACE: spec})


def vectorizer(tmp_path: Path, executable: Path, *, timeout: float = 2) -> PotraceVectorizer:
    runner, tool_registry = registry(tmp_path, executable)
    return PotraceVectorizer(
        runner=runner,
        registry=tool_registry,
        timeout_seconds=timeout,
    )


def parameters(*, width: float = 20, height: float = 10, turd_area: float = 0):
    return PotraceParameters(
        canvas_width_mm=width,
        canvas_height_mm=height,
        turd_area_mm2=turd_area,
        curve_tolerance_mm=0.05,
    )


def test_holes_borders_long_lines_and_y_transform_become_physical_geometry(tmp_path) -> None:
    path_data = "M0 0L20 0L20 10L0 10ZM5 2L5 8L15 8L15 2ZM0 4L20 4L20 4.1L0 4.1Z"
    executable = fake_potrace(tmp_path, svg(path_data))
    mask = np.ones((10, 20), dtype=np.bool_)

    result = vectorizer(tmp_path, executable).vectorize(mask, parameters())

    assert len(result.paths) == 3
    points = {
        (point.x_mm, point.y_mm)
        for path in result.paths
        for point in (path.start, *(segment.end for segment in path.segments))
    }
    assert {(0.0, 0.0), (20.0, 10.0), (0.0, 5.9), (20.0, 6.0)} <= points
    assert b'viewBox="0 0 20 10"' in result.normalized_svg
    assert b'transform="translate(0 10) scale(1 -1)"' in result.normalized_svg
    assert all(path.purpose == "construction" and path.closed for path in result.paths)


def test_physical_turd_and_curve_tolerances_are_converted_and_persisted(tmp_path) -> None:
    executable = fake_potrace(tmp_path, svg("M0 0L1 0L1 1L0 1Z", width=8, height=8))
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[0, :4] = 1

    below = vectorizer(tmp_path, executable).vectorize(
        mask,
        parameters(width=8, height=8, turd_area=3.999999),
    )
    above = vectorizer(tmp_path, executable).vectorize(
        mask,
        parameters(width=8, height=8, turd_area=4.000001),
    )

    assert below.evidence.turd_size_pixels == 3
    assert above.evidence.turd_size_pixels == 4
    assert below.evidence.curve_tolerance_pixels == 0.05
    assert "--turdsize" in below.evidence.command
    assert below.parameters.turd_area_mm2 == 3.999999
    assert below.cache_key != above.cache_key


@pytest.mark.parametrize("nozzle", (0.2, 0.4))
def test_representative_nozzle_masks_preserve_nozzle_aware_parameters(
    tmp_path, nozzle: float
) -> None:
    executable = fake_potrace(tmp_path, svg("M0 0L4 0L4 4L0 4Z", width=4, height=4))
    mask = np.zeros((20, 20), dtype=np.bool_)
    mask[1:-1, 1:-1] = True
    configured = PotraceParameters.for_nozzle(
        canvas_width_mm=4,
        canvas_height_mm=4,
        nozzle_diameter_mm=nozzle,
    )

    result = vectorizer(tmp_path, executable).vectorize(mask, configured)

    assert result.parameters.nozzle_diameter_mm == nozzle
    assert result.parameters.turd_area_mm2 == pytest.approx(nozzle**2)
    assert result.parameters.curve_tolerance_mm == pytest.approx(nozzle / 4)
    assert result.evidence.turd_size_pixels == (1 if nozzle == 0.2 else 4)


def test_missing_and_incompatible_tools_fail_before_vectorization(tmp_path) -> None:
    missing = tmp_path / "missing-potrace"
    runner, missing_registry = registry(tmp_path, missing)
    adapter = PotraceVectorizer(runner=runner, registry=missing_registry)
    with pytest.raises(PotraceUnavailableError, match="not found"):
        adapter.vectorize(np.ones((2, 2), dtype=bool), parameters())

    old = fake_potrace(tmp_path, svg("M0 0L1 0L1 1Z"), version="1.15")
    with pytest.raises(PotraceUnavailableError, match="older than required"):
        vectorizer(tmp_path, old).vectorize(np.ones((2, 2), dtype=bool), parameters())

    failing = fake_potrace(tmp_path, svg("M0 0L1 0L1 1Z"), exit_code=23)
    with pytest.raises(PotraceExecutionError, match="status 23") as raised:
        vectorizer(tmp_path, failing).vectorize(np.ones((2, 2), dtype=bool), parameters())
    assert raised.value.tool_result is not None
    assert raised.value.tool_result.return_code == 23


def test_timeout_and_running_or_prelaunch_cancellation_are_distinct(tmp_path) -> None:
    slow = fake_potrace(tmp_path, svg("M0 0L1 0L1 1Z"), delay=10)
    with pytest.raises(PotraceTimeoutError):
        vectorizer(tmp_path, slow, timeout=0.05).vectorize(
            np.ones((2, 2), dtype=bool), parameters()
        )

    token = CancellationToken()
    timer = threading.Timer(0.05, token.cancel)
    timer.start()
    try:
        with pytest.raises(PotraceCanceledError):
            vectorizer(tmp_path, slow).vectorize(
                np.ones((2, 2), dtype=bool), parameters(), cancellation=token
            )
    finally:
        timer.cancel()

    prelaunch = CancellationToken()
    prelaunch.cancel()
    with pytest.raises(PotraceCanceledError, match="before discovery"):
        vectorizer(tmp_path, slow).vectorize(
            np.ones((2, 2), dtype=bool), parameters(), cancellation=prelaunch
        )


@pytest.mark.parametrize(
    "hostile",
    (
        "<not-svg/>",
        '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg>&xxe;</svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" '
        'viewBox="0 0 20 10"><script/></svg>',
        svg("M0 0A1 1 0 0 0 1 1Z"),
        svg("M0 0L999999999 0L0 1Z"),
    ),
)
def test_malformed_or_hostile_svg_is_rejected(tmp_path, hostile: str) -> None:
    executable = fake_potrace(tmp_path, hostile)
    with pytest.raises(PotraceOutputError):
        vectorizer(tmp_path, executable).vectorize(np.ones((2, 2), dtype=bool), parameters())


def test_normalized_bytes_and_cache_keys_are_deterministic_and_logs_are_bounded(tmp_path) -> None:
    executable = fake_potrace(
        tmp_path,
        svg("M2 2c0 -1 1 -2 2 -2L8 0L8 8L2 8Z"),
        stdout_bytes=5000,
    )
    adapter = vectorizer(tmp_path, executable)
    mask = np.eye(10, dtype=np.bool_)

    first = adapter.vectorize(mask, parameters())
    second = adapter.vectorize(mask.copy(), parameters())

    assert first.cache_key == second.cache_key
    assert first.mask_sha256 == second.mask_sha256
    assert first.normalized_svg == second.normalized_svg
    assert first.normalized_svg_sha256 == second.normalized_svg_sha256
    assert "log bytes omitted" in first.evidence.stdout
    assert first.evidence.tool_version == "1.16"
    assert not (tmp_path / "must-not-exist").exists()


def test_cyclic_svg_start_points_normalize_to_identical_geometry_bytes(tmp_path) -> None:
    mask = np.ones((4, 4), dtype=np.bool_)
    first_tool = fake_potrace(tmp_path, svg("M1 1L9 1L9 9L1 9Z"))
    first = vectorizer(tmp_path, first_tool).vectorize(mask, parameters())
    second_tool = fake_potrace(tmp_path, svg("M9 9L1 9L1 1L9 1Z"))
    second = vectorizer(tmp_path, second_tool).vectorize(mask, parameters())

    assert first.paths == second.paths
    assert first.normalized_svg == second.normalized_svg
    assert first.cache_key == second.cache_key


def test_mask_validation_is_strict(tmp_path) -> None:
    executable = fake_potrace(tmp_path, svg("M0 0L1 0L1 1Z"))
    adapter = vectorizer(tmp_path, executable)
    with pytest.raises(ValueError, match="two-dimensional"):
        adapter.vectorize(np.ones((2, 2, 1)), parameters())
    with pytest.raises(ValueError, match="exactly 0 or 1"):
        adapter.vectorize(np.array([[0, 2]], dtype=np.uint8), parameters())


@pytest.mark.skipif(shutil.which("potrace") is None, reason="Potrace is not installed")
def test_installed_potrace_116_handles_holes_borders_and_physical_turd_threshold() -> None:
    adapter = PotraceVectorizer()
    bordered_hole = np.ones((100, 200), dtype=np.bool_)
    bordered_hole[20:80, 50:150] = False
    traced = adapter.vectorize(
        bordered_hole,
        PotraceParameters(
            canvas_width_mm=20,
            canvas_height_mm=10,
            turd_area_mm2=0,
            curve_tolerance_mm=0.05,
        ),
    )
    assert len(traced.paths) == 2

    one_pixel = np.zeros((20, 20), dtype=np.bool_)
    one_pixel[10, 10] = True
    below = PotraceParameters(
        canvas_width_mm=4,
        canvas_height_mm=4,
        turd_area_mm2=0.039999,
        curve_tolerance_mm=0.05,
    )
    at_threshold = below.model_copy(update={"turd_area_mm2": 0.04})
    assert len(adapter.vectorize(one_pixel, below).paths) == 1
    suppressed = adapter.vectorize(one_pixel, at_threshold)
    assert len(suppressed.paths) == 0
    assert b"<path" not in suppressed.normalized_svg


@pytest.mark.skipif(shutil.which("potrace") is None, reason="Potrace is not installed")
@pytest.mark.parametrize(
    "pixels,canvas",
    [
        ((1024, 683), (180, 120.037)),
        ((1024, 679), (180, 119.362)),
        ((576, 1024), (101.25, 180)),
        ((1000, 1000), (100, 100)),
    ],
)
def test_installed_potrace_rounding_keeps_full_photo_border_on_canvas(pixels, canvas) -> None:
    width_px, height_px = pixels
    width_mm, height_mm = canvas
    result = PotraceVectorizer().vectorize(
        np.ones((height_px, width_px), dtype=np.bool_),
        parameters(width=width_mm, height=height_mm),
    )
    points = [p for path in result.paths for p in (path.start, *(s.end for s in path.segments))]
    assert points
    assert min(p.x_mm for p in points) == 0
    assert max(p.x_mm for p in points) == width_mm
    assert min(p.y_mm for p in points) == 0
    assert max(p.y_mm for p in points) == height_mm


@pytest.mark.parametrize("overshoot", [0.00016, 0.0009])
def test_submicron_outside_points_snap_to_all_canvas_edges(tmp_path, overshoot) -> None:
    d = overshoot
    executable = fake_potrace(
        tmp_path, svg(f"M{-d} {-d}L{20 + d} {-d}L{20 + d} {10 + d}L{-d} {10 + d}Z")
    )
    result = vectorizer(tmp_path, executable).vectorize(np.ones((10, 20), dtype=bool), parameters())
    points = {
        (p.x_mm, p.y_mm)
        for path in result.paths
        for p in (path.start, *(s.end for s in path.segments))
    }
    assert points == {(0, 0), (20, 0), (20, 10), (0, 10)}


@pytest.mark.parametrize("point", ["-0.002 5", "20.002 5", "10 -0.002", "10 10.002"])
def test_real_canvas_escapes_are_still_rejected(tmp_path, point) -> None:
    executable = fake_potrace(tmp_path, svg(f"M{point}L10 5L5 5Z"))
    with pytest.raises(PotraceOutputError, match="escapes the physical canvas"):
        vectorizer(tmp_path, executable).vectorize(np.ones((10, 20), dtype=bool), parameters())


def test_submicron_interior_feature_is_not_moved_by_outside_tolerance(tmp_path) -> None:
    executable = fake_potrace(tmp_path, svg("M0.0004 1L2 1L2 2L0.0004 2Z"))
    result = vectorizer(tmp_path, executable).vectorize(np.ones((10, 20), dtype=bool), parameters())
    points = [p for path in result.paths for p in (path.start, *(s.end for s in path.segments))]
    assert min(p.x_mm for p in points) == 0.0004
