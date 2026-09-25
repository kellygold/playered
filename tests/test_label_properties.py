import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import composite
from pydantic import ValidationError

from image23mf.contracts import JobConfig, load_job_config
from image23mf.engine import LabelField, PixelRegion, ReplaceLabelEdit

PROPERTY_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)


@composite
def label_fields(draw) -> LabelField:
    width = draw(st.integers(min_value=1, max_value=32))
    height = draw(st.integers(min_value=1, max_value=32))
    label_count = draw(st.integers(min_value=1, max_value=4))
    values = tuple(range(label_count))
    pixels = bytes(
        draw(
            st.lists(
                st.sampled_from(values),
                min_size=width * height,
                max_size=width * height,
            )
        )
    )
    return LabelField(width=width, height=height, label_values=values, pixels=pixels)


@composite
def fields_and_edits(draw) -> tuple[LabelField, ReplaceLabelEdit]:
    field = draw(label_fields())
    x = draw(st.integers(min_value=0, max_value=field.width - 1))
    y = draw(st.integers(min_value=0, max_value=field.height - 1))
    width = draw(st.integers(min_value=1, max_value=field.width - x))
    height = draw(st.integers(min_value=1, max_value=field.height - y))
    source = draw(st.one_of(st.none(), st.sampled_from(field.label_values)))
    target = draw(st.sampled_from(field.label_values))
    edit = ReplaceLabelEdit(
        region=PixelRegion(x=x, y=y, width=width, height=height),
        source_label=source,
        target_label=target,
    )
    return field, edit


@composite
def job_configs(draw) -> JobConfig:
    color_count = draw(st.integers(min_value=2, max_value=8))
    color_values = draw(
        st.lists(
            st.integers(min_value=0, max_value=0xFFFFFF),
            min_size=color_count,
            max_size=color_count,
            unique=True,
        )
    )
    crop_x_tenths = draw(st.integers(min_value=0, max_value=9))
    crop_y_tenths = draw(st.integers(min_value=0, max_value=9))
    crop_width_tenths = draw(st.integers(min_value=1, max_value=10 - crop_x_tenths))
    crop_height_tenths = draw(st.integers(min_value=1, max_value=10 - crop_y_tenths))
    nozzle = draw(st.sampled_from((0.2, 0.4, 0.6, 0.8)))
    layer = draw(
        st.sampled_from(
            tuple(value for value in (0.08, 0.12, 0.16, 0.2, 0.28, 0.4) if value <= nozzle)
        )
    )
    return JobConfig.model_validate(
        {
            "schema_version": 1,
            "source_asset_id": draw(st.from_regex(r"asset_[a-z0-9]{1,24}", fullmatch=True)),
            "canvas": {
                "width_mm": draw(st.integers(min_value=200, max_value=10000)) / 10,
                "height_mm": draw(st.integers(min_value=200, max_value=10000)) / 10,
            },
            "crop": {
                "mode": draw(st.sampled_from(("contain", "cover", "stretch", "extend"))),
                "x": crop_x_tenths / 10,
                "y": crop_y_tenths / 10,
                "width": crop_width_tenths / 10,
                "height": crop_height_tenths / 10,
            },
            "printer": {
                "printer_id": "bambu-p2s",
                "nozzle_mm": nozzle,
                "layer_height_mm": layer,
                "plate_id": draw(st.sampled_from(("textured-pei", "smooth-pei"))),
            },
            "palette": {
                "colors": [
                    {
                        "id": f"color-{index}",
                        "name": f"Color {index}",
                        "hex": f"#{value:06X}",
                        "locked": draw(st.booleans()),
                    }
                    for index, value in enumerate(color_values)
                ]
            },
            "cleanup": {
                "min_island_mm2": draw(st.integers(min_value=0, max_value=250)) / 10,
                "max_hole_mm2": draw(st.integers(min_value=0, max_value=250)) / 10,
                "smoothing_radius_mm": draw(st.integers(min_value=0, max_value=50)) / 10,
                "merge_policy": draw(
                    st.sampled_from(("review", "keep", "dominant_neighbor", "perceptual_neighbor"))
                ),
                "preserve_long_lines": draw(st.booleans()),
            },
            "geometry": {
                "style": draw(st.sampled_from(("flush_inlay", "raised"))),
                "base_thickness_mm": draw(st.integers(min_value=1, max_value=100)) / 10,
                "art_thickness_mm": draw(st.integers(min_value=1, max_value=50)) / 10,
                "corner_radius_mm": draw(st.integers(min_value=0, max_value=1000)) / 10,
            },
        }
    )


@PROPERTY_SETTINGS
@given(label_fields())
def test_masks_are_exhaustive_disjoint_and_reconstruct_every_random_field(
    field: LabelField,
) -> None:
    masks = field.masks()

    assert set(masks) == set(field.label_values)
    assert all(len(mask) == field.width * field.height for mask in masks.values())
    reconstructed = []
    for index in range(field.width * field.height):
        covering = [label for label, mask in masks.items() if mask[index]]
        assert len(covering) == 1
        reconstructed.append(covering[0])
    assert bytes(reconstructed) == field.pixels
    assert LabelField.from_image(field.to_image(), field.label_values) == field


@PROPERTY_SETTINGS
@given(fields_and_edits())
def test_edits_are_deterministic_immutable_idempotent_and_spatially_bounded(
    case: tuple[LabelField, ReplaceLabelEdit],
) -> None:
    field, edit = case
    before = field.pixels

    first = field.apply(edit)
    second = field.apply(edit)

    assert first == second
    assert field.pixels == before
    assert first.apply(edit) == first
    assert set(first.pixels) <= set(field.label_values)
    for index, (old, new) in enumerate(zip(field.pixels, first.pixels)):
        x = index % field.width
        y = index // field.width
        inside = edit.region.x <= x < edit.region.right and edit.region.y <= y < edit.region.bottom
        if not inside:
            assert new == old
        if edit.source_label is not None and old != edit.source_label:
            assert new == old


@PROPERTY_SETTINGS
@given(fields_and_edits())
def test_edit_serialization_and_operation_sequences_round_trip_deterministically(
    case: tuple[LabelField, ReplaceLabelEdit],
) -> None:
    field, edit = case
    restored = ReplaceLabelEdit.model_validate_json(edit.model_dump_json())
    payload = json.loads(edit.model_dump_json())

    assert restored == edit
    assert ReplaceLabelEdit.model_validate(payload) == edit
    assert field.apply_all((edit, restored)) == field.apply(edit)


@PROPERTY_SETTINGS
@given(job_configs())
def test_random_job_configs_round_trip_canonically_with_stable_fingerprints(
    config: JobConfig,
) -> None:
    canonical = config.canonical_json()
    restored = load_job_config(json.loads(canonical))
    reordered = dict(reversed(list(json.loads(canonical).items())))

    assert restored == config
    assert restored.canonical_json() == canonical
    assert restored.fingerprint() == config.fingerprint()
    assert load_job_config(reordered).fingerprint() == config.fingerprint()


@PROPERTY_SETTINGS
@given(label_fields(), st.integers(min_value=0, max_value=255))
def test_unknown_labels_and_out_of_bounds_edits_are_rejected(
    field: LabelField, candidate: int
) -> None:
    if candidate not in field.label_values:
        with pytest.raises(ValueError, match="absent"):
            field.mask(candidate)
    with pytest.raises(ValueError, match="remain inside"):
        field.apply(
            ReplaceLabelEdit(
                region=PixelRegion(x=field.width, y=0, width=1, height=1),
                target_label=field.label_values[0],
            )
        )
    with pytest.raises(ValidationError):
        ReplaceLabelEdit.model_validate(
            {
                "region": {"x": -1, "y": 0, "width": 1, "height": 1},
                "target_label": field.label_values[0],
            }
        )


@PROPERTY_SETTINGS
@given(
    st.integers(min_value=1, max_value=32),
    st.integers(min_value=1, max_value=32),
    st.integers(min_value=1, max_value=4),
)
def test_fields_with_undeclared_pixel_values_are_rejected(
    width: int, height: int, label_count: int
) -> None:
    pixels = bytearray([0] * (width * height))
    pixels[-1] = 255

    with pytest.raises(ValueError, match="undeclared"):
        LabelField(
            width=width,
            height=height,
            label_values=tuple(range(label_count)),
            pixels=bytes(pixels),
        )
