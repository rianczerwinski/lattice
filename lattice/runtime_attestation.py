"""Exact application-byte inventories for installed runtime attestation."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Iterable


def _digest(path: Path) -> tuple[str, int]:
    before = path.stat()
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or byte_count != after.st_size:
        raise OSError(f"runtime source changed while hashing: {path}")
    return digest.hexdigest(), byte_count


def tree_inventory(root: Path) -> list[dict[str, str | int]]:
    """Inventory stable package bytes while excluding interpreter-owned caches."""

    root = root.resolve(strict=True)
    values: list[dict[str, str | int]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise OSError(f"runtime package tree contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise OSError(f"runtime package tree contains an unsupported object: {path}")
        digest, byte_count = _digest(path)
        values.append({"path": relative.as_posix(), "sha256": digest, "byte_count": byte_count})
    if not values:
        raise OSError(f"runtime package tree is empty: {root}")
    return values


def mapped_inventory(mappings: Iterable[tuple[Path, str]]) -> list[dict[str, str | int]]:
    """Compose source directories into their exact installed-package paths."""

    values: dict[str, dict[str, str | int]] = {}
    for root, prefix in mappings:
        for item in tree_inventory(root):
            installed_path = f"{prefix}/{item['path']}" if prefix else str(item["path"])
            if installed_path in values:
                raise OSError(f"runtime source mappings collide at {installed_path}")
            values[installed_path] = {**item, "path": installed_path}
    return [values[path] for path in sorted(values)]


def validate_tree_set(value: object) -> dict[str, list[dict[str, str | int]]]:
    if not isinstance(value, dict) or set(value) != {"lattice"}:
        raise ValueError("runtime source-tree attestation requires exactly the Lattice package inventory")
    validated: dict[str, list[dict[str, str | int]]] = {}
    for name, inventory in value.items():
        if not isinstance(inventory, list) or not inventory:
            raise ValueError(f"runtime source-tree attestation is empty for {name}")
        seen: set[str] = set()
        for item in inventory:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "byte_count"}:
                raise ValueError(f"runtime source-tree item is malformed for {name}")
            path = item["path"]
            digest = item["sha256"]
            byte_count = item["byte_count"]
            if not isinstance(path, str) or not path or path.startswith("/") or ".." in Path(path).parts or path in seen:
                raise ValueError(f"runtime source-tree path is invalid for {name}")
            if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError(f"runtime source-tree digest is invalid for {name}:{path}")
            if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
                raise ValueError(f"runtime source-tree byte count is invalid for {name}:{path}")
            seen.add(path)
        if [item["path"] for item in inventory] != sorted(seen):
            raise ValueError(f"runtime source-tree inventory is not canonically ordered for {name}")
        validated[name] = inventory
    return validated


def validate_provider_attestations(value: object) -> list[dict]:
    """Validate exact provider profiles and their observed subscription-CLI bindings."""

    from .execution import DEFAULT_ADAPTERS

    if not isinstance(value, list) or len(value) != len(DEFAULT_ADAPTERS):
        raise ValueError("runtime provider attestation set is incomplete")
    expected = {profile.provider_code: profile.definition() for profile in DEFAULT_ADAPTERS}
    seen: set[str] = set()
    status_keys = {"model_code", "binary", "binary_sha256", "installed", "qualified", "operational", "version", "control_surface_qualified", "control_surface_detail"}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("runtime provider attestation is malformed")
        provider = item.get("provider_code")
        if provider not in expected or provider in seen:
            raise ValueError("runtime provider attestation identity is invalid")
        definition = expected[provider]
        if {key: item.get(key) for key in definition} != definition or set(item) != set(definition) | status_keys:
            raise ValueError(f"runtime provider profile disagrees with source: {provider}")
        binary = item["binary"]
        digest = item["binary_sha256"]
        if not isinstance(item["installed"], bool) or not isinstance(item["qualified"], bool):
            raise ValueError(f"runtime provider status is malformed: {provider}")
        if item["installed"]:
            if not isinstance(binary, str) or not Path(binary).is_absolute() or not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError(f"runtime provider binary attestation is malformed: {provider}")
        elif binary is not None or digest is not None:
            raise ValueError(f"uninstalled runtime provider claims binary evidence: {provider}")
        if item["operational"] not in {True, False, None} or item["control_surface_qualified"] not in {True, False, None}:
            raise ValueError(f"runtime provider qualification state is malformed: {provider}")
        if item["version"] is not None and not isinstance(item["version"], str):
            raise ValueError(f"runtime provider version is malformed: {provider}")
        if item["control_surface_detail"] is not None and not isinstance(item["control_surface_detail"], str):
            raise ValueError(f"runtime provider qualification detail is malformed: {provider}")
        seen.add(provider)
    return value


def verify_provider_attestation_freshness(value: object) -> list[dict]:
    """Reject a runtime whose effective provider executable changed after qualification."""

    attestations = validate_provider_attestations(value)
    for item in attestations:
        current = shutil.which(item["official_cli"])
        if current != item["binary"]:
            raise ValueError(f"runtime provider entrypoint changed after qualification: {item['provider_code']}")
        if current is not None and _digest(Path(current))[0] != item["binary_sha256"]:
            raise ValueError(f"runtime provider executable changed after qualification: {item['provider_code']}")
    return attestations
