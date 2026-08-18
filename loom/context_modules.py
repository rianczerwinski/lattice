"""First-party context modules with explicit authority and reproducible receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .canonical import canonical_bytes, sha256_bytes, stamp
from .contracts import ContextModuleDefinition


class ContextModuleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContextModuleResult:
    content: bytes
    receipt: dict[str, Any]


class ContextModuleRunner:
    """Resolve declared inputs; configuration never grants authority implicitly."""

    def __init__(self, *, allowed_file_roots: tuple[Path, ...], octopus_base_url: str):
        self.allowed_file_roots = tuple(path.expanduser().resolve() for path in allowed_file_roots)
        self.octopus_base_url = octopus_base_url.rstrip("/")

    def resolve(self, definition: ContextModuleDefinition) -> ContextModuleResult:
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
        source = Path(configured).expanduser().resolve()
        if not any(source == root or source.is_relative_to(root) for root in self.allowed_file_roots):
            raise ContextModuleError("file module path is outside its declared roots")
        try:
            content = source.read_bytes()
        except OSError as error:
            raise ContextModuleError(f"unable to read context source: {source}") from error
        return self._result(definition, content, source_ref=f"file:{source}")

    def _octopus(self, definition: ContextModuleDefinition) -> ContextModuleResult:
        conversation_id = definition.configuration.get("conversation_id")
        representation = definition.configuration.get("representation", "transcript.txt")
        if not isinstance(conversation_id, str) or representation not in {"transcript.txt", "transcript.refs.jsonl"}:
            raise ContextModuleError("invalid Octopus reference")
        url = f"{self.octopus_base_url}/api/conversations/{conversation_id}/{representation}"
        return self._get(definition, url, allowed_hosts={urlparse(self.octopus_base_url).hostname})

    def _http(self, definition: ContextModuleDefinition) -> ContextModuleResult:
        url = definition.configuration.get("url")
        hosts = definition.configuration.get("allowed_hosts")
        if not isinstance(url, str) or not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
            raise ContextModuleError("HTTP module requires a URL and explicit allowed_hosts")
        return self._get(definition, url, allowed_hosts=set(hosts))

    def _get(self, definition: ContextModuleDefinition, url: str, *, allowed_hosts: set[str | None]) -> ContextModuleResult:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed_hosts:
            raise ContextModuleError("HTTP context source exceeds its declared host authority")
        try:
            response = httpx.get(url, timeout=30, follow_redirects=False)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ContextModuleError(f"context source request failed: {url}") from error
        return self._result(definition, response.content, source_ref=url)

    @staticmethod
    def _result(definition: ContextModuleDefinition, content: bytes, *, source_ref: str) -> ContextModuleResult:
        receipt = {
            "kind": "loom-context-module-receipt",
            "module_id": definition.module_id,
            "module_version": definition.version,
            "mode": definition.mode.value,
            "source_kind": definition.source_kind,
            "source_ref": source_ref,
            "content_sha256": sha256_bytes(content),
            "byte_count": len(content),
            "resolved_at": stamp(),
            "capabilities": definition.capabilities,
        }
        receipt["receipt_sha256"] = sha256_bytes(canonical_bytes(receipt))
        return ContextModuleResult(content=content, receipt=receipt)
