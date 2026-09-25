"""Deterministic, external assembly aids for a tiled mural.

Assembly aids are deliberately separate artifacts. They describe back labels, edge
relationships, orientation, crop marks, and spacer dimensions without adding a
single pixel or triangle to the printable art face.
"""

# ruff: noqa: UP045 -- Python 3.9 is supported and Pydantic evaluates these annotations.

from __future__ import annotations

import hashlib
import html
import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from image23mf.engine.transform import Rect
from image23mf.mural.planner import MuralPlan, MuralPlanRequest
from image23mf.mural.seam_qa import MuralSeamQaReport
from image23mf.mural.topology_partition import MuralTopologyPartition


class AssemblyAidModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MuralAssemblyAidsSettings(AssemblyAidModel):
    enabled: bool = False
    rear_identifiers: bool = True
    edge_identifiers: bool = True
    orientation_marks: bool = True
    crop_marks: bool = True
    alignment_jig_metadata: bool = True

    @model_validator(mode="after")
    def enabled_mode_has_at_least_one_aid(self) -> MuralAssemblyAidsSettings:
        if self.enabled and not any(
            (
                self.rear_identifiers,
                self.edge_identifiers,
                self.orientation_marks,
                self.crop_marks,
                self.alignment_jig_metadata,
            )
        ):
            raise ValueError("enabled assembly aids require at least one selected aid")
        return self


class TileNeighbours(AssemblyAidModel):
    top: Optional[str] = None
    right: Optional[str] = None
    bottom: Optional[str] = None
    left: Optional[str] = None


class TileEdgeIdentifiers(AssemblyAidModel):
    top: Optional[str] = None
    right: Optional[str] = None
    bottom: Optional[str] = None
    left: Optional[str] = None


class AssemblyTileAid(AssemblyAidModel):
    tile_id: str = Field(pattern=r"^tile-r[0-9]{2}-c[0-9]{2}$")
    row: int = Field(ge=1, le=50)
    column: int = Field(ge=1, le=50)
    build_plate_index: int = Field(ge=1, le=2_500)
    assembled_bounds_mm: Rect
    back_identifier: Optional[str] = Field(default=None, max_length=240)
    orientation_mark: Optional[Literal["TOP ↑"]] = None
    print_rotation_degrees: Literal[0, 90]
    neighbours: TileNeighbours
    edge_identifiers: TileEdgeIdentifiers
    visible_labels_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tile_geometry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class AlignmentJigMetadata(AssemblyAidModel):
    panel_width_mm: float = Field(gt=0)
    panel_height_mm: float = Field(gt=0)
    total_panel_thickness_mm: float = Field(gt=0)
    horizontal_spacer_mm: float = Field(ge=0)
    vertical_spacer_mm: float = Field(ge=0)
    outer_frame_width_mm: float = Field(gt=0)
    outer_frame_height_mm: float = Field(gt=0)
    note: str = Field(min_length=1, max_length=500)


class AssemblyArtworkPurity(AssemblyAidModel):
    policy: Literal["external_artifacts_only_zero_print_geometry_mutation"] = (
        "external_artifacts_only_zero_print_geometry_mutation"
    )
    authoritative_master_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recomposed_visible_art_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visible_art_unchanged: Literal[True]
    label_partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    topology_partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MuralAssemblyAidsManifest(AssemblyAidModel):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=120)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    settings: MuralAssemblyAidsSettings
    master_size_mm: dict[Literal["width", "height"], float]
    assembled_size_mm: dict[Literal["width", "height"], float]
    tiles: tuple[AssemblyTileAid, ...] = Field(min_length=1, max_length=2_500)
    alignment_jig: Optional[AlignmentJigMetadata] = None
    artwork_purity: AssemblyArtworkPurity
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def manifest_is_enabled_complete_and_canonical(self) -> MuralAssemblyAidsManifest:
        if not self.settings.enabled:
            raise ValueError("an assembly-aids manifest requires enabled settings")
        if len({item.tile_id for item in self.tiles}) != len(self.tiles):
            raise ValueError("assembly-aid tile identifiers must be unique")
        if self.settings.alignment_jig_metadata != (self.alignment_jig is not None):
            raise ValueError("alignment-jig metadata must match its selected setting")
        expected = _fingerprint(self.model_dump(mode="json", exclude={"manifest_sha256"}))
        if self.manifest_sha256 != expected:
            raise ValueError("assembly-aids manifest fingerprint is not canonical")
        return self


def build_mural_assembly_aids(
    *,
    title: str,
    request: MuralPlanRequest,
    plan: MuralPlan,
    seam_qa: MuralSeamQaReport,
    topology: MuralTopologyPartition,
    settings: MuralAssemblyAidsSettings,
    total_panel_thickness_mm: float,
) -> tuple[MuralAssemblyAidsManifest, bytes]:
    """Build external metadata and an A4 SVG assembly sheet from exact mural evidence."""

    if not settings.enabled:
        raise ValueError("assembly aids were not enabled")
    topology_by_id = {item.evidence.tile_id: item.evidence for item in topology.tiles}
    seam_by_id = {item.tile_id: item for item in seam_qa.tiles}
    plan_by_position = {(item.row, item.column): item for item in plan.tiles}
    tiles = tuple(
        _tile_aid(
            title=title,
            tile=item,
            topology=topology_by_id[item.id],
            seam=seam_by_id[item.id],
            by_position=plan_by_position,
            request=request,
            settings=settings,
        )
        for item in plan.tiles
    )
    jig = (
        AlignmentJigMetadata(
            panel_width_mm=request.layout.panel_width_mm,
            panel_height_mm=request.layout.panel_height_mm,
            total_panel_thickness_mm=total_panel_thickness_mm,
            horizontal_spacer_mm=request.layout.horizontal_gap_mm,
            vertical_spacer_mm=request.layout.vertical_gap_mm,
            outer_frame_width_mm=plan.assembled_size_mm.width,
            outer_frame_height_mm=plan.assembled_size_mm.height,
            note=(
                "Use the recorded spacer widths between neighbouring finished edges. "
                "The guide is not a dimensional calibration coupon; verify printer scale "
                "before fabricating a rigid jig."
            ),
        )
        if settings.alignment_jig_metadata
        else None
    )
    payload = {
        "schema_version": 1,
        "title": title,
        "request_fingerprint": plan.request_fingerprint,
        "settings": settings,
        "master_size_mm": {
            "width": plan.master_size_mm.width,
            "height": plan.master_size_mm.height,
        },
        "assembled_size_mm": {
            "width": plan.assembled_size_mm.width,
            "height": plan.assembled_size_mm.height,
        },
        "tiles": tiles,
        "alignment_jig": jig,
        "artwork_purity": AssemblyArtworkPurity(
            authoritative_master_sha256=seam_qa.artwork.authoritative_master_sha256,
            recomposed_visible_art_sha256=seam_qa.artwork.recomposed_visible_art_sha256,
            visible_art_unchanged=True,
            label_partition_sha256=topology.manifest.label_partition_sha256,
            topology_partition_sha256=topology.manifest.partition_sha256,
        ),
    }
    manifest = MuralAssemblyAidsManifest(
        **payload,
        manifest_sha256=_fingerprint(_json_value(payload)),
    )
    return manifest, render_assembly_sheet_svg(manifest)


def render_assembly_sheet_svg(manifest: MuralAssemblyAidsManifest) -> bytes:
    """Render a deterministic, printable A4 overview with no external resources."""

    page_width = 210.0
    page_height = 297.0
    diagram_x = 20.0
    diagram_y = 48.0
    diagram_width = 170.0
    diagram_height = 190.0
    assembled_width = manifest.assembled_size_mm["width"]
    assembled_height = manifest.assembled_size_mm["height"]
    scale = min(diagram_width / assembled_width, diagram_height / assembled_height)
    used_width = assembled_width * scale
    used_height = assembled_height * scale
    origin_x = diagram_x + (diagram_width - used_width) / 2
    origin_y = diagram_y + (diagram_height - used_height) / 2
    title = html.escape(manifest.title)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{page_width:g}mm" '
            f'height="{page_height:g}mm" viewBox="0 0 {page_width:g} {page_height:g}" '
            'role="img" aria-labelledby="sheet-title sheet-description">'
        ),
        f'<title id="sheet-title">{title} assembly guide</title>',
        (
            '<desc id="sheet-description">Printable tile order, back identifiers, '
            "orientation marks, crop marks, and spacer dimensions.</desc>"
        ),
        '<rect width="210" height="297" fill="#fff"/>',
        '<g fill="#111" font-family="sans-serif">',
        f'<text x="20" y="18" font-size="7" font-weight="700">{title}</text>',
        (
            '<text x="20" y="27" font-size="3.5">Assembly overview · '
            "not a 1:1 dimensional template</text>"
        ),
        (
            f'<text x="20" y="34" font-size="3.2">Finished size: '
            f"{assembled_width:g} × {assembled_height:g} mm · "
            f"{len(manifest.tiles)} panels · TOP ↑</text>"
        ),
        "</g>",
        '<g fill="none" stroke="#111" stroke-width="0.45">',
    ]
    for tile in manifest.tiles:
        bounds = tile.assembled_bounds_mm
        x = origin_x + bounds.x * scale
        y = origin_y + bounds.y * scale
        width = bounds.width * scale
        height = bounds.height * scale
        lines.append(f'<rect x="{x:.4f}" y="{y:.4f}" width="{width:.4f}" height="{height:.4f}"/>')
        if manifest.settings.crop_marks:
            lines.extend(_crop_mark_lines(x, y, width, height))
        lines.append('</g><g fill="#111" font-family="sans-serif">')
        label = html.escape(
            f"R{tile.row:02d}C{tile.column:02d} · plate {tile.build_plate_index:02d}"
        )
        lines.append(
            f'<text x="{x + width / 2:.4f}" y="{y + height / 2:.4f}" '
            f'font-size="{min(4.2, max(2.1, width / 16)):.3f}" text-anchor="middle">{label}</text>'
        )
        if tile.orientation_mark:
            lines.append(
                f'<text x="{x + width / 2:.4f}" y="{y + 5:.4f}" font-size="3" '
                f'font-weight="700" text-anchor="middle">TOP ↑</text>'
            )
        lines.append('</g><g fill="none" stroke="#111" stroke-width="0.45">')
    lines.extend(
        [
            "</g>",
            '<g fill="#111" font-family="sans-serif" font-size="3.2">',
            f'<text x="20" y="250">Guide fingerprint: {manifest.manifest_sha256}</text>',
            (
                '<text x="20" y="258">Apply identifiers to the rear only. '
                "Do not mark the visible art face.</text>"
            ),
        ]
    )
    if manifest.alignment_jig is not None:
        lines.append(
            f'<text x="20" y="266">Spacers: horizontal '
            f"{manifest.alignment_jig.horizontal_spacer_mm:g} mm · vertical "
            f"{manifest.alignment_jig.vertical_spacer_mm:g} mm · panel thickness "
            f"{manifest.alignment_jig.total_panel_thickness_mm:g} mm</text>"
        )
    lines.extend(["</g>", "</svg>"])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _tile_aid(*, title, tile, topology, seam, by_position, request, settings) -> AssemblyTileAid:
    neighbours = TileNeighbours(
        top=_tile_id(by_position.get((tile.row - 1, tile.column))),
        right=_tile_id(by_position.get((tile.row, tile.column + 1))),
        bottom=_tile_id(by_position.get((tile.row + 1, tile.column))),
        left=_tile_id(by_position.get((tile.row, tile.column - 1))),
    )
    edges = TileEdgeIdentifiers(
        top=_edge_label("TOP", tile.id, neighbours.top, settings.edge_identifiers),
        right=_edge_label("RIGHT", tile.id, neighbours.right, settings.edge_identifiers),
        bottom=_edge_label("BOTTOM", tile.id, neighbours.bottom, settings.edge_identifiers),
        left=_edge_label("LEFT", tile.id, neighbours.left, settings.edge_identifiers),
    )
    x = (tile.column - 1) * (request.layout.panel_width_mm + request.layout.horizontal_gap_mm)
    y = (tile.row - 1) * (request.layout.panel_height_mm + request.layout.vertical_gap_mm)
    return AssemblyTileAid(
        tile_id=tile.id,
        row=tile.row,
        column=tile.column,
        build_plate_index=tile.build_plate_index,
        assembled_bounds_mm=Rect(
            x=x,
            y=y,
            width=request.layout.panel_width_mm,
            height=request.layout.panel_height_mm,
        ),
        back_identifier=(
            f"{title} · R{tile.row:02d}C{tile.column:02d} · plate {tile.build_plate_index:02d}"
            if settings.rear_identifiers
            else None
        ),
        orientation_mark="TOP ↑" if settings.orientation_marks else None,
        print_rotation_degrees=tile.bed_fit.rotation_degrees,
        neighbours=neighbours,
        edge_identifiers=edges,
        visible_labels_sha256=seam.visible_labels_sha256,
        tile_geometry_fingerprint=topology.tile_geometry_fingerprint,
    )


def _tile_id(tile) -> Optional[str]:
    return None if tile is None else tile.id


def _edge_label(
    side: str,
    tile_id: str,
    neighbour: Optional[str],
    enabled: bool,
) -> Optional[str]:
    if not enabled:
        return None
    if neighbour is None:
        return f"{side} · outer edge"
    first, second = sorted((tile_id, neighbour))
    return f"JOIN · {first} ↔ {second}"


def _crop_mark_lines(x: float, y: float, width: float, height: float) -> list[str]:
    length = 2.4
    return [
        f'<path d="M {x - length:.4f} {y:.4f} H {x:.4f} M {x:.4f} {y - length:.4f} V {y:.4f}"/>',
        (
            f'<path d="M {x + width:.4f} {y - length:.4f} V {y:.4f} '
            f'M {x + width:.4f} {y:.4f} H {x + width + length:.4f}"/>'
        ),
        (
            f'<path d="M {x - length:.4f} {y + height:.4f} H {x:.4f} '
            f'M {x:.4f} {y + height:.4f} V {y + height + length:.4f}"/>'
        ),
        (
            f'<path d="M {x + width:.4f} {y + height:.4f} '
            f"H {x + width + length:.4f} M {x + width:.4f} {y + height:.4f} "
            f'V {y + height + length:.4f}"/>'
        ),
    ]


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _json_value(value: object) -> object:
    return json.loads(
        json.dumps(
            value,
            default=lambda item: (
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            ),
        )
    )
