"""Read-only workflow adapter for Octopus Platform Foundry state."""

from __future__ import annotations

from typing import Any

import httpx

from .canonical import canonical_bytes, sha256_bytes, stamp, strict_json_loads
from .http_boundary import HTTPBoundaryError, bounded_http_get, canonical_origin, require_opaque_segment


class PlatformFoundryAdapter:
    """Expose Foundry state to Loom without crossing Foundry's mutation authority."""

    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        try:
            self.origin = canonical_origin(self.base_url, origin_only=True)
        except HTTPBoundaryError as error:
            raise ValueError(f"invalid Platform Foundry origin: {error}") from error

    def inspect(self, selector: dict[str, Any]) -> dict[str, Any]:
        platform_id = selector.get("platform_id")
        try:
            platform_id = require_opaque_segment(platform_id, label="Platform Foundry platform ID")
        except HTTPBoundaryError as error:
            raise ValueError(str(error)) from error
        try:
            response = bounded_http_get(f"{self.base_url}/api/platforms/{platform_id}", allowed_origins={self.origin}, max_bytes=16 * 1024 * 1024, timeout=30, transport=self.transport)
        except HTTPBoundaryError as error:
            raise ValueError(f"Platform Foundry inspection failed: {error}") from error
        payload = strict_json_loads(response.content)
        if not isinstance(payload, dict):
            raise ValueError("Platform Foundry state must be a JSON object")
        raw = canonical_bytes(payload)
        return {"kind": "platform-foundry-state-receipt", "platform_id": platform_id, "source_ref": response.url, "content_sha256": sha256_bytes(raw), "observed_at": stamp(), "state": payload}

    def command(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise ValueError(f"Platform Foundry mutations remain owned by Octopus: {operation}")
