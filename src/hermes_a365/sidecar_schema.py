"""Shared Hermes-owned sidecar schema field validation (#193 / PR #269)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class SidecarSchemaError(ValueError):
    """Invalid ``schema`` / ``schemaVersion`` fields; never echo raw on-disk values."""


def _supported_phrase(supported: frozenset[int]) -> str:
    ordered = ", ".join(str(v) for v in sorted(supported))
    return f"non-boolean integer in {{{ordered}}}"


def _reject_null(
    payload: Mapping[str, Any],
    field: str,
    *,
    label: str,
    supported: frozenset[int],
) -> None:
    if field in payload and payload[field] is None:
        raise SidecarSchemaError(
            f"{label} field {field!r} must not be null; expected a {_supported_phrase(supported)}"
        )


def _parse_field(
    payload: Mapping[str, Any],
    field: str,
    *,
    label: str,
    supported: frozenset[int],
) -> int:
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise SidecarSchemaError(
            f"{label} field {field!r} must be a {_supported_phrase(supported)}"
        )
    if value == 0:
        raise SidecarSchemaError(
            f"{label} field {field!r} must be a {_supported_phrase(supported)}"
        )
    if value not in supported:
        raise SidecarSchemaError(
            f"{label} field {field!r} must be a {_supported_phrase(supported)}"
        )
    return value


def validate_conversations_sidecar_schema(payload: Mapping[str, Any]) -> int:
    """Validate dual ``schema`` / ``schemaVersion`` keys; return effective version."""
    label = "conversations.json"
    supported = frozenset({1})
    _reject_null(payload, "schema", label=label, supported=supported)
    _reject_null(payload, "schemaVersion", label=label, supported=supported)

    schema_present = "schema" in payload
    version_present = "schemaVersion" in payload
    schema_val = (
        _parse_field(payload, "schema", label=label, supported=supported)
        if schema_present
        else None
    )
    version_val = (
        _parse_field(payload, "schemaVersion", label=label, supported=supported)
        if version_present
        else None
    )

    if schema_val is not None:
        effective_schema = schema_val
    elif version_val is not None:
        effective_schema = version_val
    else:
        effective_schema = 1
    if version_val is not None:
        effective_version = version_val
    elif schema_val is not None:
        effective_version = schema_val
    else:
        effective_version = 1
    if schema_present and version_present and schema_val != version_val:
        raise SidecarSchemaError(
            f"{label} fields 'schema' and 'schemaVersion' must agree when both are present; "
            f"expected a {_supported_phrase(supported)}"
        )
    if effective_version not in supported or effective_schema not in supported:
        raise SidecarSchemaError(f"{label} schemaVersion must be a {_supported_phrase(supported)}")
    return effective_version


def validate_publish_state_schema_version(payload: Mapping[str, Any]) -> int:
    """Validate publish-state ``schemaVersion`` only."""
    label = "Copilot Chat publish state"
    supported = frozenset({1, 2})
    _reject_null(payload, "schemaVersion", label=label, supported=supported)
    if "schemaVersion" not in payload:
        raise SidecarSchemaError(
            f"{label} field 'schemaVersion' is missing; expected a {_supported_phrase(supported)}"
        )
    return _parse_field(payload, "schemaVersion", label=label, supported=supported)


def publish_unsupported_schema_message(
    *,
    path_label: str,
    expected_v1: int,
    expected_v2: int,
) -> str:
    return (
        f"Copilot Chat publish state at {path_label} has unsupported "
        f"schemaVersion (expected {expected_v1} or {expected_v2}); fix or remove the file "
        "before re-running publish --apply"
    )
