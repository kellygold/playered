from functools import lru_cache
from typing import Optional

from image23mf.external import ExternalToolRegistry


def discover_capabilities(
    registry: Optional[ExternalToolRegistry] = None,
) -> list[dict[str, object]]:
    if registry is None:
        return [dict(item) for item in _cached_capabilities()]
    return [_serialize(item) for item in registry.detect_all()]


@lru_cache(maxsize=1)
def _cached_capabilities() -> tuple[tuple[tuple[str, object], ...], ...]:
    return tuple(tuple(_serialize(item).items()) for item in ExternalToolRegistry().detect_all())


def clear_capability_cache() -> None:
    _cached_capabilities.cache_clear()


def _serialize(detection) -> dict[str, object]:
    return {
        "id": detection.id.value,
        "name": detection.name,
        "detected": detection.detected,
        "available": detection.available,
        "compatible": detection.compatible,
        "path": str(detection.path) if detection.path else None,
        "purpose": detection.purpose,
        "version": detection.version,
        "unavailable_reason": detection.unavailable_reason,
    }
