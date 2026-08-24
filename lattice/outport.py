"""Strict consumption of Outport's Lattice service identity."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .canonical import strict_json_loads


@dataclass(frozen=True)
class OutportService:
    project: str
    instance: str
    name: str
    port: int
    hostname: str
    url: str
    up: bool
    frame_origins: tuple[str, ...] = ()

    @property
    def loopback_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _run_outport(axisvi_root: Path) -> bytes:
    result = subprocess.run(
        ["outport", "status", "--json"],
        cwd=axisvi_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or result.stdout.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Outport could not resolve AXISVI: {detail}")
    return result.stdout


def resolve_lattice_service(axisvi_root: Path, *, runner: Callable[[Path], bytes] = _run_outport) -> OutportService:
    root = axisvi_root.expanduser().resolve(strict=True)
    payload = strict_json_loads(runner(root))
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
        raise RuntimeError("Outport status did not return a successful data object")
    data: dict[str, Any] = payload["data"]
    services = data.get("services")
    service = services.get("lattice") if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise RuntimeError("Outport has no lattice service allocation for AXISVI")
    port = service.get("port")
    hostname = service.get("hostname")
    url = service.get("url")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RuntimeError("Outport returned an invalid Lattice port")
    if not isinstance(hostname, str) or not hostname or not isinstance(url, str):
        raise RuntimeError("Outport returned an incomplete Lattice hostname or URL")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise RuntimeError("Outport Lattice URL disagrees with its hostname identity")
    frame_origins: list[str] = []
    panels = services.get("panels") if isinstance(services, dict) else None
    if isinstance(panels, dict) and isinstance(panels.get("url"), str):
        panel_url = panels["url"].rstrip("/")
        parsed_panel = urlparse(panel_url)
        if parsed_panel.scheme not in {"http", "https"} or not parsed_panel.hostname or parsed_panel.path not in {"", "/"} or parsed_panel.query or parsed_panel.fragment:
            raise RuntimeError("Outport Panels URL is invalid")
        frame_origins.append(panel_url)
    return OutportService(
        project=str(data.get("project", "")),
        instance=str(data.get("instance", "")),
        name="lattice",
        port=port,
        hostname=hostname,
        url=url.rstrip("/"),
        up=service.get("up") is True,
        frame_origins=tuple(frame_origins),
    )
