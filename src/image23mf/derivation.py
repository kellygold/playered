"""Versioned, explainable identities for every derived artifact pipeline.

Cache keys are deliberately opaque SHA-256 values, but cache decisions must not be opaque.
The same canonical identity is therefore both hashed into the key and persisted in artifact
metadata.  A later engine can compare the persisted identity with its current identity and
explain whether source, configuration, operations, engine, adapters, or dependencies changed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DERIVATION_POLICY_SCHEMA_VERSION = 1
DERIVATION_METADATA_KEY = "derivation_identity"


@dataclass(frozen=True)
class DerivationIdentity:
    pipeline: str
    source_fingerprint: str
    config_fingerprint: str
    operations_fingerprint: str
    engine_version: str
    adapter_versions: Mapping[str, str]
    dependencies: Mapping[str, str]
    policy_schema_version: int = DERIVATION_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("pipeline", self.pipeline),
            ("source_fingerprint", self.source_fingerprint),
            ("config_fingerprint", self.config_fingerprint),
            ("operations_fingerprint", self.operations_fingerprint),
            ("engine_version", self.engine_version),
        ):
            if not str(value).strip():
                raise ValueError(f"derivation {name} cannot be empty")
        if self.policy_schema_version < 1:
            raise ValueError("derivation policy schema version must be positive")
        _validate_versions("adapter_versions", self.adapter_versions, required=True)
        _validate_versions("dependencies", self.dependencies, required=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_schema_version": self.policy_schema_version,
            "pipeline": self.pipeline,
            "source_fingerprint": self.source_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "operations_fingerprint": self.operations_fingerprint,
            "engine_version": self.engine_version,
            "adapter_versions": dict(sorted(self.adapter_versions.items())),
            "dependencies": dict(sorted(self.dependencies.items())),
        }

    def metadata(self) -> dict[str, Any]:
        return {DERIVATION_METADATA_KEY: self.as_dict()}

    def key(self) -> str:
        return hashlib.sha256(_canonical_json(self.as_dict())).hexdigest()


def explain_staleness(
    recorded_metadata: Mapping[str, Any] | None,
    expected: DerivationIdentity,
) -> str:
    """Return a stable, user-actionable explanation for an incompatible artifact."""

    raw = None if recorded_metadata is None else recorded_metadata.get(DERIVATION_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return (
            "The current artifact predates versioned derivation metadata and must be regenerated."
        )
    recorded = dict(raw)
    if recorded.get("policy_schema_version") != expected.policy_schema_version:
        return (
            "The derivation policy was upgraded; regenerate this artifact with the current engine."
        )
    if recorded.get("pipeline") != expected.pipeline:
        return "The artifact was produced by a different pipeline and cannot be reused."
    if recorded.get("engine_version") != expected.engine_version:
        return (
            "The processing engine was upgraded from "
            f"{recorded.get('engine_version', 'an unknown version')} to {expected.engine_version}; "
            "regenerate this artifact."
        )
    recorded_adapters = recorded.get("adapter_versions")
    expected_adapters = dict(sorted(expected.adapter_versions.items()))
    if recorded_adapters != expected_adapters:
        changed = _changed_names(recorded_adapters, expected_adapters)
        return f"Processing adapter versions changed ({changed}); regenerate this artifact."
    for field, label in (
        ("source_fingerprint", "source image"),
        ("config_fingerprint", "configuration"),
        ("operations_fingerprint", "edit operations"),
    ):
        if recorded.get(field) != getattr(expected, field):
            return f"The {label} changed after this artifact was generated."
    if recorded.get("dependencies") != dict(sorted(expected.dependencies.items())):
        changed = _changed_names(recorded.get("dependencies"), expected.dependencies)
        return f"Pinned processing dependencies changed ({changed}); regenerate this artifact."
    return "The artifact key does not match the current derivation and must be regenerated."


def _validate_versions(name: str, values: Mapping[str, str], *, required: bool) -> None:
    if required and not values:
        raise ValueError(f"derivation {name} cannot be empty")
    if any(not str(key).strip() or not str(value).strip() for key, value in values.items()):
        raise ValueError(f"derivation {name} require non-empty names and values")


def _changed_names(recorded: Any, expected: Mapping[str, str]) -> str:
    before = recorded if isinstance(recorded, Mapping) else {}
    names = sorted(
        name for name in set(before) | set(expected) if before.get(name) != expected.get(name)
    )
    return ", ".join(names) if names else "unknown"


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
