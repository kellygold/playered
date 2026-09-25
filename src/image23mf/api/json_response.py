"""Bounded-cost compression for large validated preview JSON responses."""

import gzip
import math

from fastapi import Response
from pydantic import BaseModel


def _accepts_gzip(header: str) -> bool:
    weights: dict[str, float] = {}
    for item in header.lower().split(","):
        coding, *parameters = (part.strip() for part in item.split(";"))
        if coding not in {"gzip", "*"}:
            continue
        quality = 1.0
        for parameter in parameters:
            if parameter.startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        weights[coding] = quality if math.isfinite(quality) and 0 <= quality <= 1 else 0.0
    return weights.get("gzip", weights.get("*", 0.0)) > 0


def preview_json_response(result: BaseModel, accept_encoding: str) -> Response:
    # The service constructs/validates the complete response model, including blob
    # hashes and graph references. Avoid building another large Python object tree.
    content = result.model_dump_json().encode("utf-8")
    headers = {"Vary": "Accept-Encoding"}
    if len(content) >= 4096 and _accepts_gzip(accept_encoding):
        compressed = gzip.compress(content, compresslevel=1, mtime=0)
        if len(compressed) < len(content):
            content = compressed
            headers["Content-Encoding"] = "gzip"
    return Response(content=content, media_type="application/json", headers=headers)
