"""Canonical exhaustive label fields and deterministic bounded edits."""

from dataclasses import dataclass
from typing import Literal, Optional

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field


class LabelContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PixelRegion(LabelContract):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


class ReplaceLabelEdit(LabelContract):
    schema_version: Literal[1] = 1
    operation: Literal["replace_label"] = "replace_label"
    region: PixelRegion
    target_label: int = Field(ge=0, le=255)
    source_label: Optional[int] = Field(default=None, ge=0, le=255)


@dataclass(frozen=True)
class LabelField:
    width: int
    height: int
    label_values: tuple[int, ...]
    pixels: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("label field dimensions must be positive")
        if len(self.pixels) != self.width * self.height:
            raise ValueError("label byte count does not match field dimensions")
        if not self.label_values or len(self.label_values) != len(set(self.label_values)):
            raise ValueError("label values must be present and unique")
        if any(value < 0 or value > 255 for value in self.label_values):
            raise ValueError("label values must fit in an unsigned byte")
        unknown = set(self.pixels) - set(self.label_values)
        if unknown:
            raise ValueError(f"label field contains undeclared values: {sorted(unknown)}")

    @classmethod
    def from_image(cls, image: Image.Image, label_values: tuple[int, ...]) -> "LabelField":
        if image.mode != "L":
            raise ValueError("label image must use 8-bit L mode")
        return cls(
            width=image.width,
            height=image.height,
            label_values=label_values,
            pixels=image.tobytes(),
        )

    def to_image(self) -> Image.Image:
        return Image.frombytes("L", (self.width, self.height), self.pixels)

    def mask(self, label: int) -> bytes:
        self._require_label(label)
        return bytes(1 if value == label else 0 for value in self.pixels)

    def masks(self) -> dict[int, bytes]:
        return {label: self.mask(label) for label in self.label_values}

    def apply(self, edit: ReplaceLabelEdit) -> "LabelField":
        self._require_label(edit.target_label)
        if edit.source_label is not None:
            self._require_label(edit.source_label)
        if edit.region.right > self.width or edit.region.bottom > self.height:
            raise ValueError("edit region must remain inside the label field")
        updated = bytearray(self.pixels)
        for y in range(edit.region.y, edit.region.bottom):
            offset = y * self.width
            for x in range(edit.region.x, edit.region.right):
                index = offset + x
                if edit.source_label is None or updated[index] == edit.source_label:
                    updated[index] = edit.target_label
        return LabelField(
            width=self.width,
            height=self.height,
            label_values=self.label_values,
            pixels=bytes(updated),
        )

    def apply_all(self, edits: tuple[ReplaceLabelEdit, ...]) -> "LabelField":
        result = self
        for edit in edits:
            result = result.apply(edit)
        return result

    def _require_label(self, label: int) -> None:
        if label not in self.label_values:
            raise ValueError(f"label {label} is absent from the field palette")
