"""Same-origin operator authority for Loom's locally powerful mutation surface."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


SAFE_METHODS = {"GET", "HEAD"}


def ensure_cli_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise PermissionError(f"Loom operator token must be mode 0600, found {mode:o}")
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("Loom operator token is malformed")
        return token
    token = secrets.token_urlsafe(48)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return token


class OperatorSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}

    def issue(self) -> tuple[str, str]:
        session = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        self._sessions[session] = csrf
        return session, csrf

    def valid(self, session: str | None, csrf: str | None) -> bool:
        if not session or not csrf:
            return False
        expected = self._sessions.get(session)
        return expected is not None and hmac.compare_digest(expected, csrf)


class OperatorBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, cli_token: str, sessions: OperatorSessions, port: int) -> None:
        super().__init__(app)
        self.cli_token = cli_token
        self.sessions = sessions
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}
        self.allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", "http://testserver"}
        configured_dev = os.environ.get("LOOM_DEV_ORIGIN")
        if configured_dev:
            self.allowed_origins.add(configured_dev.rstrip("/"))

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host", "")
        if host not in self.allowed_hosts:
            return Response("unrecognized Loom host", status_code=421)
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin is not None and origin.rstrip("/") not in self.allowed_origins:
                return Response("unapproved Loom origin", status_code=403)
            authorization = request.headers.get("authorization", "")
            bearer = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
            bearer_ok = bearer is not None and hmac.compare_digest(bearer, self.cli_token)
            session_ok = self.sessions.valid(request.cookies.get("loom_operator"), request.headers.get("x-loom-csrf"))
            if not bearer_ok and not session_ok:
                return Response("operator authority required", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
        return response
