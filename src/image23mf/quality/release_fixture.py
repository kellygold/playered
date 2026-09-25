"""Deterministic compact specimen used by the physical release gate."""

from __future__ import annotations

import hashlib

from image23mf.bambu import BambuProjectSettings, build_bambu_3mf, read_bambu_3mf
from image23mf.bambu import Material as BambuMaterial
from image23mf.bambu import Mesh as BambuMesh
from image23mf.engine.labels import LabelField
from image23mf.geometry import (
    BaseBuildBounds,
    BaseMeshOptions,
    FlushInlayStrategy,
    LineSegment,
    Material,
    MeshQualityOptions,
    Path2D,
    Point2,
    RectangleBase,
    SourceLabel,
    assemble_mesh_document,
    build_shared_boundary_topology,
    extrude_artwork_regions,
    generate_structural_base,
    validate_geometry_quality,
)


def physical_release_labels() -> LabelField:
    """Return the 20 mm label field covering the four historical failure modes."""

    width = height = 100
    pixels = bytearray(width * height)

    def paint(x0: int, y0: int, x1: int, y1: int, value: int = 1) -> None:
        for y in range(y0, y1):
            for x in range(x0, x1):
                pixels[y * width + x] = value

    paint(5, 5, 7, 7)  # 0.4 x 0.4 mm isolated dot.
    paint(10, 15, 30, 35)  # 4 mm ring around a 2 mm square hole.
    paint(15, 20, 25, 30, 0)
    paint(40, 10, 41, 40)  # 0.2 x 6 mm extended line.
    paint(55, 15, 65, 25)  # Two pads with a 0.2 mm neck.
    paint(75, 15, 85, 25)
    paint(65, 19, 75, 20)
    return LabelField(width=width, height=height, label_values=(0, 1), pixels=bytes(pixels))


def build_physical_release_geometry(*, layer_height_mm: float):
    """Build the exact topology, meshes, and quality report for one specimen."""

    labels = physical_release_labels()
    digest = hashlib.sha256(labels.pixels).hexdigest()
    bone = Material.create(name="Bone White", color_hex="#CBC6B8", palette_color_id="bone")
    charcoal = Material.create(name="Charcoal", color_hex="#000000", palette_color_id="charcoal")
    materials = tuple(sorted((bone, charcoal), key=lambda item: item.id))
    source_labels = tuple(
        sorted(
            (
                SourceLabel.create(
                    source_asset_id="release-golden-v1",
                    processed_labels_sha256=digest,
                    label_index=0,
                    name="Bone White",
                    color_hex="#CBC6B8",
                    palette_color_id="bone",
                    material_id=bone.id,
                    classification="background",
                ),
                SourceLabel.create(
                    source_asset_id="release-golden-v1",
                    processed_labels_sha256=digest,
                    label_index=1,
                    name="Charcoal",
                    color_hex="#000000",
                    palette_color_id="charcoal",
                    material_id=charcoal.id,
                    classification="artwork",
                ),
            ),
            key=lambda item: item.id,
        )
    )
    construction = Path2D.create(
        purpose="construction",
        start=Point2(x_mm=0, y_mm=0),
        segments=(
            LineSegment(end=Point2(x_mm=20, y_mm=0)),
            LineSegment(end=Point2(x_mm=20, y_mm=20)),
            LineSegment(end=Point2(x_mm=0, y_mm=20)),
            LineSegment(end=Point2(x_mm=0, y_mm=0)),
        ),
        closed=True,
    )
    shape = RectangleBase.create(center=Point2(x_mm=10, y_mm=10), width_mm=20, height_mm=20)
    topology = build_shared_boundary_topology(
        labels,
        source_asset_sha256="a" * 64,
        source_labels=source_labels,
        materials=materials,
        base=shape,
        canvas_width_mm=20,
        canvas_height_mm=20,
        vector_paths=(construction,),
        vector_artifact_sha256="b" * 64,
    )
    base = generate_structural_base(
        shape,
        material=bone,
        options=BaseMeshOptions(thickness_mm=1.2, layer_height_mm=layer_height_mm),
        build_bounds=BaseBuildBounds(width_mm=256, depth_mm=256),
    )
    artwork = extrude_artwork_regions(
        topology.document,
        FlushInlayStrategy(
            base_top_z_mm=1.2,
            layer_height_mm=layer_height_mm,
            artwork_layers=round(0.4 / layer_height_mm),
        ),
    )
    document = assemble_mesh_document(
        topology.document,
        base_mesh=base.mesh,
        base_part=base.part,
        artwork=artwork,
    ).document
    quality = validate_geometry_quality(
        document,
        MeshQualityOptions(
            build_bounds=BaseBuildBounds(width_mm=256, depth_mm=256),
            minimum_part_thickness_mm=layer_height_mm,
        ),
    )
    if not quality.safe_for_export:
        raise ValueError("physical release fixture failed geometry quality validation")
    return labels, topology, document, quality


def build_physical_release_3mf(*, nozzle_mm: float, layer_height_mm: float) -> bytes:
    """Package and independently parse one Bambu-ready specimen."""

    _labels, _topology, document, _quality = build_physical_release_geometry(
        layer_height_mm=layer_height_mm
    )
    ordered_materials = tuple(document.materials)
    material_index = {item.id: index for index, item in enumerate(ordered_materials)}
    mesh_by_id = {item.id: item for item in document.meshes}
    package = build_bambu_3mf(
        name="Image23MF release gate v1",
        materials=tuple(
            BambuMaterial(
                name=item.name,
                color=item.color_hex,
                extruder=index,
                preset="Bambu PLA Matte",
            )
            for index, item in enumerate(ordered_materials, start=1)
        ),
        meshes=tuple(
            BambuMesh(
                name=f"{part.name} [{part.id[-8:]}]",
                vertices=tuple(
                    (vertex.x_mm, vertex.y_mm, vertex.z_mm)
                    for vertex in mesh_by_id[part.mesh_id].vertices
                ),
                triangles=tuple(
                    triangle.vertices for triangle in mesh_by_id[part.mesh_id].triangles
                ),
                material_index=material_index[part.material_id],
            )
            for part in document.parts
        ),
        settings=BambuProjectSettings(
            nozzle_diameter=nozzle_mm,
            layer_height=layer_height_mm,
            plate_center_x=118,
            plate_center_y=118,
        ),
    )
    read_bambu_3mf(package)
    return package
