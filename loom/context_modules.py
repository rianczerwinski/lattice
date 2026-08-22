"""First-party context modules with explicit authority and reproducible receipts."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .canonical import canonical_bytes, sha256_bytes, stamp
from .contracts import ContextModuleDefinition
from .http_boundary import HTTPBoundaryError, bounded_http_get, canonical_origin, require_opaque_segment


class ContextModuleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContextModuleResult:
    content: bytes
    receipt: dict[str, Any]


def read_bounded_local_file(configured: str, *, allowed_roots: tuple[Path, ...], max_bytes: int) -> tuple[Path, bytes]:
    """Read exact regular-file bytes through a no-symlink walk beneath one approved root."""
    source = Path(configured).expanduser().resolve()
    roots = tuple(path.expanduser().resolve() for path in allowed_roots)
    containing = [root for root in roots if source == root or source.is_relative_to(root)]
    if not containing:
        raise ContextModuleError("file path is outside its declared roots")
    root = max(containing, key=lambda item: len(item.parts))
    relative = source.relative_to(root)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, directory_flags)
    try:
        parts = relative.parts
        for component in parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if parts:
            file_descriptor = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = file_descriptor
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ContextModuleError("file source is not a regular file")
        if before.st_size > max_bytes:
            raise ContextModuleError(f"file source exceeds its {max_bytes}-byte limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ContextModuleError(f"file source exceeds its {max_bytes}-byte limit")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or total != after.st_size:
            raise ContextModuleError("file source changed while being captured")
        return source, b"".join(chunks)
    except OSError as error:
        raise ContextModuleError(f"unable to read file source: {source}") from error
    finally:
        os.close(descriptor)


class ContextModuleRunner:
    """Resolve declared inputs; configuration never grants authority implicitly."""

    DEFAULT_MAX_BYTES = 8 * 1024 * 1024
    ABSOLUTE_MAX_BYTES = 64 * 1024 * 1024
    REQUIRED_CAPABILITY = {
        "file": "read-local-file",
        "surface": "read-local-file",
        "octopus": "read-octopus",
        "http_readonly": "read-http",
    }

    def __init__(self, *, allowed_file_roots: tuple[Path, ...], octopus_base_url: str, transport: httpx.BaseTransport | None = None):
        self.allowed_file_roots = tuple(path.expanduser().resolve() for path in allowed_file_roots)
        self.octopus_base_url = octopus_base_url.rstrip("/")
        try:
            self.octopus_origin = canonical_origin(self.octopus_base_url, origin_only=True)
        except HTTPBoundaryError as error:
            raise ContextModuleError(f"invalid Octopus origin: {error}") from error
        self.transport = transport

    def resolve(self, definition: ContextModuleDefinition) -> ContextModuleResult:
        required = self.REQUIRED_CAPABILITY[definition.source_kind]
        if len(definition.capabilities) != 1 or definition.capabilities[0] != required:
            raise ContextModuleError(f"{definition.source_kind} context requires exactly the {required} capability")
        if definition.source_kind in {"file", "surface"}:
            return self._file(definition)
        if definition.source_kind == "octopus":
            return self._octopus(definition)
        if definition.source_kind == "http_readonly":
            return self._http(definition)
        raise ContextModuleError(f"unsupported context module kind: {definition.source_kind}")

    def _file(self, definition: ContextModuleDefinition) -> ContextModuleResult:
        configured = definition.configuration.get("path")
        if not isinstance(configured, str):
            raise ContextModuleError("file module requires a path")
        source, content = read_bounded_local_file(configured, allowed_roots=self.allowed_file_roots, max_bytes=self._max_bytes(definition))
        return self._result(definition, content, source_ref=f"file:{source}")

    def _octopus(self, definition: ContextModuleDefinition) -> ContextModuleResult:
        conversation_id = definition.configuration.get("conversation_id")
        representation = definition.configuration.get("representation", "transcript.txt")
        try:
            conversation_id = require_opaque_segment(conversation_id, label="Octopus conversation ID")
        except HTTPBoundaryError as error:
            raise ContextModuleError(str(error)) from error
        if representation not in {"transcript.txt", "transcript.refs.jsonl"}:
            raise ContextModuleError("invalid Octopus reference")
        url = f"{self.octopus_base_url}/api/conversations/{conversation_id}/{representation}"
        return self._get(definition, url, allowed_origins={self.octopus_origin})

    def _http(self, definition: ContextModuleDefinition) -> ContextModuleResult:
        url = definition.configuration.get("url")
        origins = definition.configuration.get("allowed_origins")
        if not isinstance(url, str) or not isinstance(origins, list) or not origins or not all(isinstance(origin, str) for origin in origins):
            raise ContextModuleError("HTTP module requires a URL and explicit allowed_origins")
        return self._get(definition, url, allowed_origins=set(origins))

    def _get(self, definition: ContextModuleDefinition, url: str, *, allowed_origins: set[str]) -> ContextModuleResult:
        try:
            response = bounded_http_get(url, allowed_origins=allowed_origins, max_bytes=self._max_bytes(definition), timeout=30, transport=self.transport)
        except HTTPBoundaryError as error:
            raise ContextModuleError(str(error)) from error
        details = {"status_code": response.status_code, "media_type": response.media_type, "transport_content_encoding": response.content_encoding, "authorized_origin": response.origin}
        return self._result(definition, response.content, source_ref=response.url, details=details)

    def _max_bytes(self, definition: ContextModuleDefinition) -> int:
        value = definition.configuration.get("max_bytes", self.DEFAULT_MAX_BYTES)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= self.ABSOLUTE_MAX_BYTES:
            raise ContextModuleError(f"context max_bytes must be between 1 and {self.ABSOLUTE_MAX_BYTES}")
        return value

    @staticmethod
    def _result(definition: ContextModuleDefinition, content: bytes, *, source_ref: str, details: dict[str, Any] | None = None) -> ContextModuleResult:
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ContextModuleError("context modules must resolve to exact UTF-8 text") from error
        receipt = {
            "kind": "loom-context-module-receipt",
            "module_id": definition.module_id,
            "module_version": definition.version,
            "definition": definition.model_dump(mode="json"),
            "definition_sha256": sha256_bytes(canonical_bytes(definition.model_dump(mode="json"))),
            "mode": definition.mode.value,
            "source_kind": definition.source_kind,
            "source_ref": source_ref,
            "content_sha256": sha256_bytes(content),
            "byte_count": len(content),
            "resolved_at": stamp(),
            "capabilities": list(definition.capabilities),
        }
        if details:
            receipt["http"] = details
        receipt["receipt_sha256"] = sha256_bytes(canonical_bytes(receipt))
        return ContextModuleResult(content=content, receipt=receipt)
