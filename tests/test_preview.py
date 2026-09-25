import hashlib
import io

import pytest
from PIL import Image

from image23mf.contracts.job import JobConfig
from image23mf.engine.ingestion import ingest_image
from image23mf.engine.preview import render_preview


def encoded(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def config_for(asset_id: str, *, crop_mode: str = "cover") -> JobConfig:
    return JobConfig.model_validate(
        {
            "source_asset_id": asset_id,
            "canvas": {"width_mm": 200, "height_mm": 100},
            "crop": {"mode": crop_mode, "x": 0, "y": 0, "width": 1, "height": 1},
            "palette": {
                "colors": [
                    {"id": "light", "name": "Light", "hex": "#FFFFFF"},
                    {"id": "dark", "name": "Dark", "hex": "#000000"},
                ]
            },
        }
    )


def test_preview_is_deterministic_versioned_and_uses_physical_canvas_scale() -> None:
    source = Image.new("RGBA", (80, 40), (20, 80, 140, 255))
    ingested = ingest_image(encoded(source), filename="source.png")
    config = config_for("asset-proof")

    first = render_preview(
        ingested.png_bytes,
        source_metadata=ingested.metadata,
        config=config,
        profile_catalog_fingerprint="a" * 64,
        max_dimension=400,
    )
    second = render_preview(
        ingested.png_bytes,
        source_metadata=ingested.metadata,
        config=config,
        profile_catalog_fingerprint="a" * 64,
        max_dimension=400,
    )

    assert first == second
    assert first.statistics.schema_version == 1
    assert first.statistics.preview.model_dump() == {"width": 400, "height": 200}
    assert first.statistics.physical.mm_per_pixel_x == 0.5
    assert first.statistics.physical.mm_per_pixel_y == 0.5
    assert first.statistics.alpha.opaque_pixels == 80_000
    assert first.statistics.alpha.translucent_pixels == 0
    assert first.statistics.alpha.transparent_pixels == 0
    assert first.statistics.alpha.pixel_count == 80_000
    assert first.statistics.preview_sha256 == hashlib.sha256(first.png_bytes).hexdigest()
    assert first.statistics.transform.canvas_size.model_dump() == {
        "width": 200.0,
        "height": 100.0,
    }


def test_contain_preview_records_transparent_extension_without_flattening_alpha() -> None:
    source = Image.new("RGBA", (40, 40), (250, 30, 20, 255))
    source.putpixel((20, 20), (10, 20, 30, 128))
    ingested = ingest_image(encoded(source), filename="alpha.png")

    result = render_preview(
        ingested.png_bytes,
        source_metadata=ingested.metadata,
        config=config_for("asset-alpha", crop_mode="contain"),
        profile_catalog_fingerprint="b" * 64,
        max_dimension=200,
    )

    assert result.statistics.preview.model_dump() == {"width": 200, "height": 100}
    assert result.statistics.alpha.transparent_pixels > 0
    assert result.statistics.alpha.translucent_pixels > 0
    with Image.open(io.BytesIO(result.png_bytes)) as preview:
        assert preview.mode == "RGBA"
        assert preview.getpixel((0, 0))[3] == 0
        assert preview.getpixel((100, 50))[3] > 0


@pytest.mark.parametrize("dimension", [31, 4097])
def test_preview_resolution_is_bounded(dimension: int) -> None:
    ingested = ingest_image(encoded(Image.new("RGB", (2, 2))), filename="tiny.png")
    with pytest.raises(ValueError, match="between 32 and 4096"):
        render_preview(
            ingested.png_bytes,
            source_metadata=ingested.metadata,
            config=config_for("asset-tiny"),
            profile_catalog_fingerprint="c" * 64,
            max_dimension=dimension,
        )
