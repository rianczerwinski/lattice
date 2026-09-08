"""Declarative workflow-pack registry; executable adapters are explicitly registered."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from .canonical import canonical_bytes, sha256_bytes, strict_json_loads
from .contracts import WorkflowPackDefinition


class WorkflowAdapter(Protocol):
    def inspect(self, selector: dict[str, Any]) -> dict[str, Any]: ...
    def command(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def evidence(self, evidence_kind: str, selector: dict[str, Any], subject: dict[str, Any]) -> dict[str, Any]: ...


class WorkflowRegistry:
    MAX_PACK_BYTES = 2 * 1024 * 1024
    PACK_ID = re.compile(r"^[a-z][a-z0-9.-]*$")

    def __init__(self, pack_roots: Path | Iterable[Path]):
        roots = (pack_roots,) if isinstance(pack_roots, Path) else tuple(pack_roots)
        if not roots:
            raise ValueError("workflow registry requires at least one pack root")
        resolved = tuple(dict.fromkeys(path.resolve() for path in roots))
        missing = [str(path) for path in resolved if not path.is_dir()]
        if missing:
            raise FileNotFoundError(f"workflow pack root is unavailable: {missing[0]}")
        self.pack_roots = resolved
        self.pack_root = resolved[0]
        self.adapters: dict[str, WorkflowAdapter] = {}

    def register(self, pack_id: str, adapter: WorkflowAdapter) -> None:
        if pack_id in self.adapters:
            raise ValueError(f"workflow adapter already registered: {pack_id}")
        self.adapters[pack_id] = adapter

    def list(self) -> list[dict[str, Any]]:
        packs: list[dict[str, Any]] = []
        for pack_id in self._pack_ids():
            loaded = self.load(pack_id)
            packs.append({**loaded, "available": pack_id in self.adapters})
        return packs

    def catalogue(self) -> dict[str, list[dict[str, Any]]]:
        """Describe every pack without letting one optional pack brick conversation."""
        packs: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for pack_id in sorted(self._pack_candidates()):
            try:
                loaded = self.load(pack_id)
            except (FileNotFoundError, OSError, ValueError) as error:
                failures.append({"pack_id": pack_id, "detail": str(error)})
                continue
            packs.append({**loaded, "available": pack_id in self.adapters})
        return {"packs": packs, "failures": failures}

    def load(self, pack_id: str) -> dict[str, Any]:
        path = self._pack_path(pack_id)
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

    def _pack_ids(self) -> list[str]:
        identities = self._pack_candidates()
        duplicate = next((pack_id for pack_id, paths in sorted(identities.items()) if len(set(paths)) > 1), None)
        if duplicate is not None:
            raise ValueError(f"workflow pack identity is declared by more than one root: {duplicate}")
        return sorted(identities)

    def _pack_candidates(self) -> dict[str, list[Path]]:
        identities: dict[str, list[Path]] = {}
        for root in self.pack_roots:
            for path in sorted(root.glob("*/pack.json")):
                identities.setdefault(path.parent.name, []).append(path.resolve())
        return identities

    def _pack_path(self, pack_id: str) -> Path:
        if not self.PACK_ID.fullmatch(pack_id):
            raise ValueError("workflow pack identity is malformed")
        paths = []
        for root in self.pack_roots:
            path = (root / pack_id / "pack.json").resolve()
            if not path.is_relative_to(root):
                raise ValueError("workflow pack escaped its root")
            if path.is_file():
                paths.append(path)
        unique = tuple(dict.fromkeys(paths))
        if not unique:
            raise FileNotFoundError(f"workflow pack is unavailable: {pack_id}")
        if len(unique) > 1:
            raise ValueError(f"workflow pack identity is declared by more than one root: {pack_id}")
        return unique[0]

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

    def evidence(self, pack_id: str, evidence_kind: str, selector: dict[str, Any], subject: dict[str, Any]) -> dict[str, Any]:
        """Ask the registered adapter to derive evidence from its authoritative system."""

        selector = self.resolve_selector(pack_id, selector)
        if not isinstance(evidence_kind, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,119}", evidence_kind):
            raise ValueError("workflow adapter evidence kind is malformed")
        if not isinstance(subject, dict):
            raise ValueError("workflow adapter evidence subject must be an object")
        try:
            adapter = self.adapters[pack_id]
        except KeyError as error:
            raise ValueError(f"workflow has no registered adapter: {pack_id}") from error
        producer = getattr(adapter, "evidence", None)
        if producer is None:
            raise ValueError(f"workflow adapter does not produce governed evidence: {pack_id}")
        return producer(evidence_kind, selector, subject)

    def validate_transition(self, pack_id: str, from_state: str, to_state: str) -> dict[str, Any]:
        definition = self.load(pack_id)["definition"]
        transition = next((item for item in definition["allowed_transitions"] if item["from"] == from_state and item["to"] == to_state), None)
        if transition is None:
            raise ValueError(f"workflow transition is not declared: {from_state} -> {to_state}")
        trigger = next((item for item in definition["triggers"] if item["trigger"] == transition.get("trigger")), None)
        return {**transition, "trigger_definition": trigger}
