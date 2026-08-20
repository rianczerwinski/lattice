"""Declarative workflow-pack registry; executable adapters are explicitly registered."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .canonical import canonical_bytes, sha256_bytes
from .contracts import WorkflowPackDefinition


class WorkflowAdapter(Protocol):
    def inspect(self, selector: dict[str, Any]) -> dict[str, Any]: ...
    def command(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class WorkflowRegistry:
    def __init__(self, pack_root: Path):
        self.pack_root = pack_root.resolve()
        self.adapters: dict[str, WorkflowAdapter] = {}

    def register(self, pack_id: str, adapter: WorkflowAdapter) -> None:
        if pack_id in self.adapters:
            raise ValueError(f"workflow adapter already registered: {pack_id}")
        self.adapters[pack_id] = adapter

    def load(self, pack_id: str) -> dict[str, Any]:
        path = (self.pack_root / pack_id / "pack.json").resolve()
        if not path.is_relative_to(self.pack_root):
            raise ValueError("workflow pack escaped its root")
        raw = path.read_bytes()
        definition = WorkflowPackDefinition.model_validate_json(raw)
        if definition.pack_id != pack_id:
            raise ValueError("workflow pack identity does not match its path")
        value = definition.model_dump(mode="json", by_alias=True)
        return {"definition": value, "source_sha256": sha256_bytes(raw), "canonical_sha256": sha256_bytes(canonical_bytes(value))}

    def inspect(self, pack_id: str, selector: dict[str, Any]) -> dict[str, Any]:
        self.load(pack_id)
        try:
            return self.adapters[pack_id].inspect(selector)
        except KeyError as error:
            raise ValueError(f"workflow has no registered adapter: {pack_id}") from error

    def command(self, pack_id: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.load(pack_id)
        try:
            return self.adapters[pack_id].command(operation, arguments)
        except KeyError as error:
            raise ValueError(f"workflow has no registered adapter: {pack_id}") from error

    def validate_transition(self, pack_id: str, from_state: str, to_state: str) -> dict[str, Any]:
        definition = self.load(pack_id)["definition"]
        transition = next((item for item in definition["allowed_transitions"] if item["from"] == from_state and item["to"] == to_state), None)
        if transition is None:
            raise ValueError(f"workflow transition is not declared: {from_state} -> {to_state}")
        trigger = next((item for item in definition["triggers"] if item["trigger"] == transition.get("trigger")), None)
        return {**transition, "trigger_definition": trigger}
