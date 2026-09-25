"""Independent Geometry IR golden consumer using only the Python standard library."""

import json
import re
from pathlib import Path

GOLDEN = Path(__file__).parent / "goldens" / "geometry_ir_v1.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def test_minimal_stdlib_consumer_resolves_golden_contract_and_references() -> None:
    document = json.loads(GOLDEN.read_bytes())

    assert document["schema_version"] == 1
    assert document["contract"]["units"] == "millimetre"
    assert SHA256.fullmatch(document["source_asset_sha256"])
    processed_labels_sha256 = document["processed_labels_sha256"]
    assert SHA256.fullmatch(processed_labels_sha256)
    assert tuple(document["capabilities"]) == (
        "download",
        "geometry_ir",
        "mesh",
        "package_3mf",
        "slicer_validation",
        "topology",
        "vector_geometry",
    )

    collection_names = (
        "paths",
        "contours",
        "materials",
        "source_labels",
        "islands",
        "shared_edges",
        "meshes",
        "parts",
    )
    entities = {name: {item["id"]: item for item in document[name]} for name in collection_names}
    assert all(list(entities[name]) == sorted(entities[name]) for name in collection_names)

    for contour in entities["contours"].values():
        assert contour["path_id"] in entities["paths"]
    for label in entities["source_labels"].values():
        assert label["processed_labels_sha256"] == processed_labels_sha256
        material = entities["materials"][label["material_id"]]
        assert label["palette_color_id"] == material["palette_color_id"]
    for island in entities["islands"].values():
        assert island["exterior_contour_id"] in entities["contours"]
        assert all(item in entities["contours"] for item in island["hole_contour_ids"])
        label = entities["source_labels"][island["source_label_id"]]
        assert island["material_id"] == label["material_id"]
    for edge in entities["shared_edges"].values():
        assert all(item in entities["islands"] for item in edge["adjacent_island_ids"])
        assert edge["owner_island_id"] == min(edge["adjacent_island_ids"])
    for part in entities["parts"].values():
        assert part["mesh_id"] in entities["meshes"]
        assert part["material_id"] in entities["materials"]
        if part["source_geometry_kind"] == "island":
            assert part["source_geometry_id"] in entities["islands"]
        else:
            assert part["source_geometry_id"] == document["base"]["id"]
