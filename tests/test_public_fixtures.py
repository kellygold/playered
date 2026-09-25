import hashlib
import json
import struct
from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "public-regression"
REQUIRED_REGRESSIONS = {
    "enclosed-ring-grid",
    "long-thin-lines",
    "dense-line-detail",
    "extension-signature-mismatch",
    "small-stroke-counters",
}


def _catalog() -> dict:
    return json.loads((FIXTURE_ROOT / "catalog.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert payload[12:16] == b"IHDR"
    return struct.unpack(">II", payload[16:24])


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    assert payload.startswith(b"\xff\xd8")
    offset = 2
    start_of_frame = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset < len(payload):
        assert payload[offset] == 0xFF
        while payload[offset] == 0xFF:
            offset += 1
        marker = payload[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        length = struct.unpack(">H", payload[offset : offset + 2])[0]
        if marker in start_of_frame:
            height, width = struct.unpack(">HH", payload[offset + 3 : offset + 7])
            return width, height
        offset += length
    raise AssertionError("JPEG is missing a start-of-frame marker")


def test_public_fixture_catalog_is_complete_and_redistributable() -> None:
    catalog = _catalog()

    assert catalog["schema_version"] == 1
    assert catalog["distribution"] == "redistributable"
    assert len(catalog["fixtures"]) == 3
    assert {region["id"] for item in catalog["fixtures"] for region in item["regions"]} == (
        REQUIRED_REGRESSIONS
    )
    for item in catalog["fixtures"]:
        assert item["redistributable"] is True
        assert item["rights"]
        assert item["provenance"]
        assert not Path(item["source_path"]).is_absolute()
        assert ".." not in Path(item["source_path"]).parts


def test_public_fixture_bytes_dimensions_and_regions_match_the_catalog() -> None:
    for item in _catalog()["fixtures"]:
        path = FIXTURE_ROOT / item["file"]
        payload = path.read_bytes()

        assert len(payload) == item["byte_size"]
        assert _sha256(path) == item["sha256"]
        if item["encoded_format"] == "png":
            dimensions = _png_dimensions(payload)
        elif item["encoded_format"] == "jpeg":
            dimensions = _jpeg_dimensions(payload)
        else:  # pragma: no cover - catalog contract restricts current formats
            raise AssertionError(f"unsupported fixture encoding: {item['encoded_format']}")
        assert dimensions == (item["width_px"], item["height_px"])
        assert item["canvas_width_mm"] > 0
        assert item["canvas_height_mm"] > 0

        for region in item["regions"]:
            assert region["protects"]
            assert 0 <= region["x_px"] < item["width_px"]
            assert 0 <= region["y_px"] < item["height_px"]
            assert region["width_px"] > 0
            assert region["height_px"] > 0
            assert region["x_px"] + region["width_px"] <= item["width_px"]
            assert region["y_px"] + region["height_px"] <= item["height_px"]
