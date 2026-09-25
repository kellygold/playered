import io

import pytest
from PIL import Image

from image23mf.prompted_edits.evidence import build_prompted_alternative_evidence


def png(pixels: list[tuple[int, int, int, int]], size: tuple[int, int]) -> bytes:
    image = Image.new("RGBA", size)
    image.putdata(pixels)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_changed_area_evidence_is_exact_and_visually_transparent_elsewhere() -> None:
    parent = png([(1, 2, 3, 255)] * 4, (2, 2))
    candidate = png(
        [(1, 2, 3, 255), (9, 2, 3, 255), (1, 2, 3, 255), (1, 2, 3, 128)],
        (2, 2),
    )

    evidence = build_prompted_alternative_evidence(parent, candidate)

    assert evidence.changed_mask == bytes((0, 1, 0, 1))
    assert evidence.changed_pixel_count == 2
    assert (evidence.width_px, evidence.height_px) == (2, 2)
    with Image.open(io.BytesIO(evidence.changed_mask_preview_png)) as preview:
        assert list(preview.convert("RGBA").getdata()) == [
            (0, 0, 0, 0),
            (255, 92, 72, 210),
            (0, 0, 0, 0),
            (255, 92, 72, 210),
        ]


def test_changed_area_rejects_provider_resizing() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        build_prompted_alternative_evidence(
            png([(0, 0, 0, 255)], (1, 1)),
            png([(0, 0, 0, 255)] * 2, (2, 1)),
        )
