"""Declarative workflow-pack registry; executable adapters are explicitly registered."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Protocol

from .canonical import canonical_bytes, sha256_bytes, strict_json_loads
from .contracts import WorkflowPackDefinition


class WorkflowAdapter(Protocol):
    def inspect(self, selector: dict[str, Any]) -> dict[str, Any]: ...
    def command(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class WorkflowRegistry:
    MAX_PACK_BYTES = 2 * 1024 * 1024

    def __init__(self, pack_root: Path):
        self.pack_root = pack_root.resolve()
        self.adapters: dict[str, WorkflowAdapter] = {}

    def register(self, pack_id: str, adapter: WorkflowAdapter) -> None:
        if pack_id in self.adapters:
            raise ValueError(f"workflow adapter already registered: {pack_id}")
        self.adapters[pack_id] = adapter

    def list(self) -> list[dict[str, Any]]:
        packs: list[dict[str, Any]] = []
        for path in sorted(self.pack_root.glob("*/pack.json")):
            loaded = self.load(path.parent.name)
            packs.append({**loaded, "available": path.parent.name in self.adapters})
        return packs

    def load(self, pack_id: str) -> dict[str, Any]:
        path = (self.pack_root / pack_id / "pack.json").resolve()
        if not path.is_relative_to(self.pack_root):
            raise ValueError("workflow pack escaped its root")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("workflow pack is not a regular file")
            if before.st_size > self.MAX_PACK_BYTES:
                raise ValueError(f"workflow pack exceeds {self.MAX_PACK_BYTES} bytes")
            raw = handle.read(self.MAX_PACK_BYTES + 1)
            after = os.fstat(handle.fileno())
        if len(raw) > self.MAX_PACK_BYTES:
            raise ValueError(f"workflow pack exceeds {self.MAX_PACK_BYTES} bytes")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) != after.st_size:
            raise ValueError("workflow pack changed while being read")
        definition = WorkflowPackDefinition.model_validate(strict_json_loads(raw))
        if definition.pack_id != pack_id:
            raise ValueError("workflow pack identity does not match its path")
        value = definition.model_dump(mode="json", by_alias=True)
        return {"definition": value, "source_sha256": sha256_bytes(raw), "canonical_sha256": sha256_bytes(canonical_bytes(value))}

    def inspect(self, pack_id: str, selector: dict[str, Any]) -> dict[str, Any]:
        selector = self.resolve_selector(pack_id, selector)
        try:
            return self.adapters[pack_id].inspect(selector)
        except KeyError as error:
            raise ValueError(f"workflow has no registered adapter: {pack_id}") from error

    def resolve_selector(self, pack_id: str, selector: dict[str, Any]) -> dict[str, Any]:
        definition = self.load(pack_id)["definition"]
        declared = {item["key"]: item for item in definition["project_selector"]}
        extras = sorted(set(selector) - set(declared))
        if extras:
            raise ValueError(f"workflow project selector contains undeclared fields: {extras}")
        resolved = dict(selector)
        for key, field in declared.items():
            if key not in resolved and field["default"] is not None:
                resolved[key] = field["default"]
        missing = sorted(key for key, field in declared.items() if field["required"] and (key not in resolved or resolved[key] == ""))
        if missing:
            raise ValueError(f"workflow project selector lacks required fields: {missing}")
        for key, value in resolved.items():
            expected = declared[key]["value_type"]
            if expected == "string" and not isinstance(value, str):
                raise ValueError(f"workflow project selector {key} must be a string")
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"workflow project selector {key} must be an integer")
        return resolved

    def resolve_command(self, pack_id: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        definition = self.load(pack_id)["definition"]
        command = next((item for item in definition["commands"] if item["operation"] == operation), None)
        if command is None:
            raise ValueError(f"workflow command is not declared by {pack_id}: {operation}")
        declared = {item["key"]: item for item in command["arguments"]}
        extras = sorted(set(arguments) - set(declared))
        if extras:
            raise ValueError(f"workflow command contains undeclared arguments: {extras}")
        resolved = dict(arguments)
        for key, item in declared.items():
            if key not in resolved and item["default"] is not None:
                resolved[key] = item["default"]
        missing = sorted(key for key, item in declared.items() if item["required"] and key not in resolved)
        if missing:
            raise ValueError(f"workflow command lacks required arguments: {missing}")
        for key, value in resolved.items():
            expected = declared[key]["value_type"]
            if expected in {"string", "file"} and not isinstance(value, str):
                raise ValueError(f"workflow command argument {key} must be a string")
            if expected in {"string", "file"} and declared[key]["required"] and not value:
                raise ValueError(f"workflow command argument {key} cannot be empty")
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"workflow command argument {key} must be an integer")
            if expected == "integer" and declared[key]["minimum"] is not None and value < declared[key]["minimum"]:
                raise ValueError(f"workflow command argument {key} is below its minimum")
            if expected == "integer" and declared[key]["maximum"] is not None and value > declared[key]["maximum"]:
                raise ValueError(f"workflow command argument {key} exceeds its maximum")
        return resolved

    def command(self, pack_id: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_command(pack_id, operation, arguments)
        try:
            return self.adapters[pack_id].command(operation, resolved)
        except KeyError as error:
            raise ValueError(f"workflow has no registered adapter: {pack_id}") from error

    def validate_transition(self, pack_id: str, from_state: str, to_state: str) -> dict[str, Any]:
        definition = self.load(pack_id)["definition"]
        transition = next((item for item in definition["allowed_transitions"] if item["from"] == from_state and item["to"] == to_state), None)
        if transition is None:
            raise ValueError(f"workflow transition is not declared: {from_state} -> {to_state}")
        trigger = next((item for item in definition["triggers"] if item["trigger"] == transition.get("trigger")), None)
        return {**transition, "trigger_definition": trigger}
