"""Codex provider-specific execution boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

CODEX_DISABLED_FEATURES = ("hooks", "apps", "plugins", "memories", "multi_agent", "multi_agent_v2")
CODEX_REQUIRED_EXEC_OPTIONS = (
    "--strict-config", "--ignore-user-config", "--ignore-rules", "--output-schema",
    "--output-last-message", "--ephemeral", "--json",
)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Compile valid Draft 2020-12 constants for Codex's stricter schema subset.

    Codex requires every property schema to declare ``type`` even when a
    ``const`` or homogeneous ``enum`` already determines it. The added type is
    semantically redundant, so Lattice can preserve the caller's canonical
    contract while sending provider-compatible transport bytes.
    """

    compiled = deepcopy(schema)
    stack: list[Any] = [compiled]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        if "type" not in node:
            if "const" in node:
                node["type"] = _json_type(node["const"])
            elif isinstance(node.get("enum"), list) and node["enum"]:
                inferred = {_json_type(value) for value in node["enum"]}
                if len(inferred) == 1:
                    node["type"] = inferred.pop()
        stack.extend(node.values())
    return compiled
