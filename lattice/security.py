"""Same-origin operator authority for Lattice's locally powerful mutation surface."""

from __future__ import annotations

import fcntl
import hmac
import os
import secrets
import stat
import time
from urllib.parse import urlparse
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .http_boundary import HTTPBoundaryError, canonical_origin
from .durability import sync_directory, sync_file


SAFE_METHODS = {"GET", "HEAD"}


def _operator_read_required(path: str) -> bool:
    """Identify API reads which expose provider output or durable project work."""

    # Lattice is a personal-data service.  A new read route must therefore be
    # private by construction instead of depending on an ever-stale allowlist.
    return path == "/api/v1" or path.startswith("/api/v1/")


def configured_development_origin() -> str | None:
    configured = os.environ.get("LATTICE_DEV_ORIGIN")
    if not configured:
        return None
    try:
        return canonical_origin(configured, origin_only=True)
    except HTTPBoundaryError as error:
        raise ValueError(f"LATTICE_DEV_ORIGIN is invalid: {error}") from error


class ServiceInstanceLock:
    """Exclusive process-lifetime ownership of one Lattice canonical data root."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except BlockingIOError as error:
            os.close(descriptor)
            raise RuntimeError(f"another Lattice process owns data root {self.path.parent}") from error
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ensure_cli_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise PermissionError("Lattice operator token must be a non-symlink regular file") from error
        try:
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            if not stat.S_ISREG(metadata.st_mode):
                raise PermissionError("Lattice operator token must be a regular file")
            if mode != 0o600:
                raise PermissionError(f"Lattice operator token must be mode 0600, found {mode:o}")
            if metadata.st_size > 4096:
                raise ValueError("Lattice operator token is malformed")
            raw = os.read(descriptor, metadata.st_size + 1)
        finally:
            os.close(descriptor)
        try:
            token = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as error:
            raise ValueError("Lattice operator token is malformed") from error
        if len(token) < 32:
            raise ValueError("Lattice operator token is malformed")
        return token
    token = secrets.token_urlsafe(48)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
        sync_file(descriptor)
    finally:
        os.close(descriptor)
    sync_directory(path.parent)
    return token


class OperatorSessions:
    def __init__(
        self,
        *,
        ttl_seconds: int = 86_400,
        bootstrap_ttl_seconds: int = 60,
        max_sessions: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 1 or bootstrap_ttl_seconds < 1 or max_sessions < 1:
            raise ValueError("operator session bounds must be positive")
        self.ttl_seconds = ttl_seconds
        self.bootstrap_ttl_seconds = bootstrap_ttl_seconds
        self.max_sessions = max_sessions
        self.clock = clock
        self._sessions: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._bootstrap_tokens: OrderedDict[str, float] = OrderedDict()

    def _prune(self, now: float) -> None:
        expired = [session for session, (_csrf, expires_at) in self._sessions.items() if expires_at <= now]
        for session in expired:
            self._sessions.pop(session, None)
        expired_bootstraps = [token for token, expires_at in self._bootstrap_tokens.items() if expires_at <= now]
        for token in expired_bootstraps:
            self._bootstrap_tokens.pop(token, None)

    def issue_bootstrap(self) -> str:
        """Mint a short-lived, single-use capability for transferring CLI authority to a browser."""

        now = self.clock()
        self._prune(now)
        token = secrets.token_urlsafe(48)
        self._bootstrap_tokens[token] = now + self.bootstrap_ttl_seconds
        while len(self._bootstrap_tokens) > self.max_sessions:
            self._bootstrap_tokens.popitem(last=False)
        return token

    def redeem_bootstrap(self, token: str | None) -> tuple[str, str] | None:
        if not token:
            return None
        now = self.clock()
        self._prune(now)
        matched = next((candidate for candidate in self._bootstrap_tokens if hmac.compare_digest(candidate, token)), None)
        if matched is None:
            return None
        self._bootstrap_tokens.pop(matched, None)
        return self.issue()

    def issue(self) -> tuple[str, str]:
        now = self.clock()
        self._prune(now)
        session = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        self._sessions[session] = (csrf, now + self.ttl_seconds)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
        return session, csrf

    def valid(self, session: str | None, csrf: str | None) -> bool:
        if not session or not csrf:
            return False
        now = self.clock()
        self._prune(now)
        value = self._sessions.get(session)
        return value is not None and hmac.compare_digest(value[0], csrf)

    def csrf_for(self, session: str | None) -> str | None:
        if not session:
            return None
        now = self.clock()
        self._prune(now)
        value = self._sessions.get(session)
        return value[0] if value is not None else None


class OperatorBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, cli_token: str, sessions: OperatorSessions, port: int, public_url: str | None = None, frame_origins: tuple[str, ...] = ()) -> None:
        super().__init__(app)
        self.cli_token = cli_token
        self.sessions = sessions
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}
        self.allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", "http://testserver"}
        if public_url:
            parsed = urlparse(public_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("Lattice public URL is not an exact HTTP origin")
            self.allowed_hosts.add(parsed.netloc)
            self.allowed_origins.add(public_url.rstrip("/"))
        self.frame_origins = ("'self'", "http://127.0.0.1:9000", "http://localhost:9000", *(origin.rstrip("/") for origin in frame_origins))
        configured_dev = configured_development_origin()
        if configured_dev:
            self.allowed_origins.add(configured_dev)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host", "")
        if host not in self.allowed_hosts:
            return Response("unrecognized Lattice host", status_code=421)
        if request.url.path in {"/api/session", "/api/session/redeem"}:
            fetch_site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            if fetch_site not in {None, "none", "same-origin"} or (origin is not None and origin.rstrip("/") not in self.allowed_origins):
                return Response("cross-site operator session bootstrap rejected", status_code=403)
        authorization = request.headers.get("authorization", "")
        bearer = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
        bearer_ok = bearer is not None and hmac.compare_digest(bearer, self.cli_token)
        session_cookie = request.cookies.get("lattice_operator")
        if request.url.path == "/api/session" and not bearer_ok and self.sessions.csrf_for(session_cookie) is None:
            return Response("operator authority required", status_code=403)
        sensitive_read = _operator_read_required(request.url.path)
        if request.method in SAFE_METHODS and sensitive_read and not bearer_ok and self.sessions.csrf_for(session_cookie) is None:
            return Response("operator authority required", status_code=403)
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/") and request.url.path != "/api/session/redeem":
            origin = request.headers.get("origin")
            if origin is not None and origin.rstrip("/") not in self.allowed_origins:
                return Response("unapproved Lattice origin", status_code=403)
            session_ok = self.sessions.valid(session_cookie, request.headers.get("x-lattice-csrf"))
            if not bearer_ok and not session_ok:
                return Response("operator authority required", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = f"default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors {' '.join(self.frame_origins)}"
        return response
