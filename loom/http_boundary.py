"""Shared validation for Loom's explicitly authorized read-only HTTP edges."""

from __future__ import annotations

import re
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


OPAQUE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class HTTPBoundaryError(ValueError):
    pass


@dataclass(frozen=True)
class BoundedHTTPResponse:
    url: str
    origin: str
    status_code: int
    content: bytes
    media_type: str | None
    content_encoding: str | None


def canonical_origin(value: str, *, origin_only: bool = False) -> str:
    """Return one unambiguous HTTP origin, rejecting parser-confusion inputs."""
    if not isinstance(value, str) or not value or any(character.isspace() for character in value) or "\\" in value:
        raise HTTPBoundaryError("HTTP authority is malformed")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise HTTPBoundaryError("HTTP authority is malformed") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPBoundaryError("HTTP authority requires an http or https origin")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise HTTPBoundaryError("HTTP authority cannot contain credentials or a fragment")
    if origin_only and (parsed.path not in {"", "/"} or parsed.query):
        raise HTTPBoundaryError("HTTP authority must be an origin, not a resource URL")
    try:
        port = parsed.port
    except ValueError as error:
        raise HTTPBoundaryError("HTTP authority has an invalid port") from error
    raw_host = parsed.hostname.rstrip(".")
    if "%" in raw_host:
        raise HTTPBoundaryError("HTTP authority host cannot contain a zone or escape marker")
    try:
        host = raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise HTTPBoundaryError("HTTP authority host is malformed") from error
    if not host or parsed.hostname.endswith("."):
        raise HTTPBoundaryError("HTTP authority host is malformed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if any(len(label) > 63 or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None for label in labels):
            raise HTTPBoundaryError("HTTP authority host is malformed")
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 80 if parsed.scheme == "http" else 443
    port_suffix = "" if port in {None, default_port} else f":{port}"
    return f"{parsed.scheme}://{rendered_host}{port_suffix}"


def require_authorized_url(url: str, allowed_origins: set[str]) -> str:
    canonical_allowed = {canonical_origin(value, origin_only=True) for value in allowed_origins}
    origin = canonical_origin(url)
    if origin not in canonical_allowed:
        raise HTTPBoundaryError("HTTP resource exceeds its exact origin authority")
    return origin


def require_opaque_segment(value: str, *, label: str) -> str:
    if not isinstance(value, str) or OPAQUE_PATH_SEGMENT.fullmatch(value) is None:
        raise HTTPBoundaryError(f"{label} is not a safe opaque identifier")
    return value


def bounded_http_get(url: str, *, allowed_origins: set[str], max_bytes: int, timeout: float, transport: httpx.BaseTransport | None = None) -> BoundedHTTPResponse:
    """Read one authorized response without redirects or unbounded buffering."""
    requested_origin = require_authorized_url(url, allowed_origins)
    if max_bytes < 1:
        raise HTTPBoundaryError("HTTP byte limit must be positive")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport, headers={"Accept-Encoding": "identity"}, trust_env=False) as client:
            with client.stream("GET", url) as response:
                if not 200 <= response.status_code < 300:
                    raise HTTPBoundaryError(f"HTTP source returned HTTP {response.status_code}: {url}")
                response_origin = require_authorized_url(str(response.url), allowed_origins)
                if response_origin != requested_origin:
                    raise HTTPBoundaryError("HTTP source changed origin")
                content_encoding = response.headers.get("content-encoding")
                if content_encoding is not None and content_encoding.strip().lower() not in {"", "identity"}:
                    raise HTTPBoundaryError("HTTP source ignored identity encoding and was rejected before decompression")
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        parsed_length = int(declared_length)
                    except ValueError as error:
                        raise HTTPBoundaryError("HTTP source returned an invalid content-length") from error
                    if parsed_length < 0:
                        raise HTTPBoundaryError("HTTP source returned an invalid content-length")
                    if parsed_length > max_bytes:
                        raise HTTPBoundaryError(f"HTTP source exceeds its {max_bytes}-byte limit")
                chunks: list[bytes] = []
                total = 0
                stream = (response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in stream:
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPBoundaryError(f"HTTP source exceeds its {max_bytes}-byte limit")
                    chunks.append(chunk)
                return BoundedHTTPResponse(url=str(response.url), origin=response_origin, status_code=response.status_code, content=b"".join(chunks), media_type=response.headers.get("content-type"), content_encoding=content_encoding)
    except httpx.HTTPError as error:
        raise HTTPBoundaryError(f"HTTP source request failed: {url}") from error
