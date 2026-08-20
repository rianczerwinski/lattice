"""Read-only workflow adapter for Octopus Platform Foundry state."""

from __future__ import annotations

from typing import Any, Callable

import httpx

from .canonical import canonical_bytes, sha256_bytes, stamp


class PlatformFoundryAdapter:
    """Expose Foundry state to Loom without crossing Foundry's mutation authority."""

    def __init__(self, base_url: str, *, getter: Callable[..., httpx.Response] = httpx.get):
        self.base_url = base_url.rstrip("/")
        self.getter = getter

    def inspect(self, selector: dict[str, Any]) -> dict[str, Any]:
        platform_id = selector.get("platform_id")
        if not isinstance(platform_id, str) or not platform_id:
            raise ValueError("Platform Foundry inspection requires platform_id")
        response = self.getter(f"{self.base_url}/api/platforms/{platform_id}", timeout=30, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        raw = canonical_bytes(payload)
        return {"kind": "platform-foundry-state-receipt", "platform_id": platform_id, "source_ref": str(response.url), "content_sha256": sha256_bytes(raw), "observed_at": stamp(), "state": payload}

    def command(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise ValueError(f"Platform Foundry mutations remain owned by Octopus: {operation}")
