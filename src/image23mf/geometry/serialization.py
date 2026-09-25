"""Strict canonical JSON transport for geometry IR documents."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from image23mf.geometry.model import GeometryDocument

MAX_GEOMETRY_IR_BYTES = 256 * 1024 * 1024
MAX_JSON_NESTING = 128


class GeometryIRDecodeError(ValueError):
    """Raised when bytes do not satisfy the canonical geometry IR wire contract."""


def _reject_constant(value: str) -> None:
    raise GeometryIRDecodeError(f"non-finite JSON number {value} is forbidden")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GeometryIRDecodeError(f"duplicate JSON key {key!r} is forbidden")
        result[key] = value
    return result


def dump_geometry_ir(document: GeometryDocument) -> bytes:
    return document.canonical_bytes()


def _validate_json_nesting(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise GeometryIRDecodeError("geometry IR exceeds the JSON nesting limit")
        elif character in "]}":
            depth -= 1


def load_geometry_ir(data: bytes, *, require_canonical: bool = True) -> GeometryDocument:
    if len(data) > MAX_GEOMETRY_IR_BYTES:
        raise GeometryIRDecodeError("geometry IR exceeds the wire-size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GeometryIRDecodeError("geometry IR must be valid UTF-8") from exc
    _validate_json_nesting(text)
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except GeometryIRDecodeError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise GeometryIRDecodeError("geometry IR must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise GeometryIRDecodeError("geometry IR root must be an object")
    try:
        document = GeometryDocument.model_validate(payload)
    except ValidationError as exc:
        raise GeometryIRDecodeError(f"geometry IR schema validation failed: {exc}") from exc
    if require_canonical and data != document.canonical_bytes():
        raise GeometryIRDecodeError("geometry IR bytes are valid but not canonical")
    return document


def geometry_ir_json_schema() -> dict[str, Any]:
    return GeometryDocument.model_json_schema()
