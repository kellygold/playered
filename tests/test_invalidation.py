from types import SimpleNamespace

from image23mf import __version__
from image23mf.contracts.job import CanvasConfig, JobConfig, PaletteColor, PaletteConfig
from image23mf.invalidation import (
    ReprocessingPlan,
    ReprocessingReason,
    is_strictly_interior_selection,
    mask_sha256,
)
from image23mf.processing import (
    PREVIEW_ADAPTER_VERSIONS,
    _component_fingerprints,
    _plan_reprocessing,
)


def plane(width: int, height: int, points: set[tuple[int, int]]) -> bytes:
    return bytes(1 if (x, y) in points else 0 for y in range(height) for x in range(width))


def test_strict_interior_selection_accepts_only_one_region_away_from_every_boundary() -> None:
    width = height = 7
    labels = bytes([2] * (width * height))
    active = bytes([1] * (width * height))
    center = plane(width, height, {(3, 3)})

    assert is_strictly_interior_selection(
        center,
        labels=labels,
        active=active,
        width=width,
        height=height,
    )
    assert not is_strictly_interior_selection(
        plane(width, height, {(0, 3)}),
        labels=labels,
        active=active,
        width=width,
        height=height,
    )


def test_strict_interior_selection_rejects_adjacent_label_and_transparency_boundaries() -> None:
    width = height = 7
    center = plane(width, height, {(3, 3)})
    labels = bytearray([2] * (width * height))
    labels[3 * width + 4] = 1
    active = bytearray([1] * (width * height))

    assert not is_strictly_interior_selection(
        center,
        labels=bytes(labels),
        active=bytes(active),
        width=width,
        height=height,
    )
    labels[3 * width + 4] = 2
    active[2 * width + 3] = 0
    assert not is_strictly_interior_selection(
        center,
        labels=bytes(labels),
        active=bytes(active),
        width=width,
        height=height,
    )


def test_reprocessing_plan_requires_honest_reuse_evidence() -> None:
    mask = bytes(16)
    plan = ReprocessingPlan(
        mode="full",
        reason=ReprocessingReason.PALETTE_CHANGED,
        message="The palette changed.",
        recomputed_stages=("quantization", "automatic_cleanup"),
        affected_pixel_count=0,
        affected_mask_sha256=mask_sha256(mask),
        output_equivalence="full_recompute",
    )

    assert plan.mode == "full"
    assert plan.reused_stages == ()


def config() -> JobConfig:
    return JobConfig(
        source_asset_id="asset_test",
        canvas=CanvasConfig(width_mm=200, height_mm=200),
        palette=PaletteConfig(
            colors=(
                PaletteColor(id="black", name="Black", hex="#000000"),
                PaletteColor(id="white", name="White", hex="#FFFFFF"),
            )
        ),
    )


def baseline_for(value: JobConfig, *, engine_version: str = __version__) -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job_baseline",
        config_fingerprint=value.fingerprint(),
        component_fingerprints=_component_fingerprints(value),
        engine_version=engine_version,
        adapter_versions=PREVIEW_ADAPTER_VERSIONS,
        dependencies={},
        command_fingerprints=(),
        processed_labels=bytes([0] * 49),
        processed_active=bytes([1] * 49),
    )


def test_palette_crop_and_engine_changes_force_labeled_full_reprocessing() -> None:
    original = config()
    changed_palette = original.model_copy(
        update={
            "palette": PaletteConfig(
                colors=(
                    PaletteColor(id="black", name="Black", hex="#000000"),
                    PaletteColor(id="cream", name="Cream", hex="#E8D9BB"),
                )
            )
        }
    )
    changed_crop = original.model_copy(
        update={"crop": original.crop.model_copy(update={"x": 0.1, "width": 0.9})}
    )

    palette_plan, _ = _plan_reprocessing(
        baseline=baseline_for(original),
        config=changed_palette,
        editor_commands=(),
        transform=None,
        width=7,
        height=7,
    )
    crop_plan, _ = _plan_reprocessing(
        baseline=baseline_for(original),
        config=changed_crop,
        editor_commands=(),
        transform=None,
        width=7,
        height=7,
    )
    engine_plan, _ = _plan_reprocessing(
        baseline=baseline_for(original, engine_version="0.0.0"),
        config=original,
        editor_commands=(),
        transform=None,
        width=7,
        height=7,
    )

    assert (palette_plan.mode, palette_plan.reason) == (
        "full",
        ReprocessingReason.PALETTE_CHANGED,
    )
    assert (crop_plan.mode, crop_plan.reason) == ("full", ReprocessingReason.CROP_CHANGED)
    assert (engine_plan.mode, engine_plan.reason) == (
        "full",
        ReprocessingReason.ENGINE_CHANGED,
    )
