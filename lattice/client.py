"""Thin authenticated client for Lattice's atomic execution boundary."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_bytes
from .config import LatticeConfig
from .contracts import ExecutionResultV1, ExecutionSubmissionV1, HistoricalExecutionImportResultV1, HistoricalExecutionImportV1
from .outport import resolve_lattice_service
from .security import ensure_cli_token


class LatticeClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class LatticeClient:
    base_url: str
    operator_token: str
    host_header: str

    @classmethod
    def from_environment(cls) -> "LatticeClient":
        config = LatticeConfig.from_environment()
        service = resolve_lattice_service(config.axisvi_root) if config.axisvi_root else None
        base_url = service.loopback_url if service is not None else (config.public_url or f"http://127.0.0.1:{config.port}")
        host = f"127.0.0.1:{service.port}" if service is not None else urllib.parse.urlsplit(base_url).netloc
        return cls(base_url.rstrip("/"), ensure_cli_token(config.operator_token), host)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 35.0) -> Any:
        data = canonical_bytes(payload) if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Host": self.host_header,
                "Authorization": f"Bearer {self.operator_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:4000]
            raise LatticeClientError(f"Lattice returned HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise LatticeClientError(f"Lattice request failed: {error}") from error

    def _request_bytes(self, path: str, *, timeout: float = 35.0) -> bytes:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            method="GET",
            headers={"Host": self.host_header, "Authorization": f"Bearer {self.operator_token}", "Accept": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:4000]
            raise LatticeClientError(f"Lattice returned HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LatticeClientError(f"Lattice request failed: {error}") from error

    def submit(self, submission: ExecutionSubmissionV1 | dict[str, Any], *, wait: bool = False, timeout: float = 1800.0) -> ExecutionResultV1:
        value = submission if isinstance(submission, ExecutionSubmissionV1) else ExecutionSubmissionV1.model_validate(submission)
        result = ExecutionResultV1.model_validate(self._request("POST", "/api/v1/executions", value.model_dump(mode="json")))
        return self.wait(result.external_project_key, result.operation_key, result.project_id, timeout=timeout) if wait and result.state.value not in {"succeeded", "failed", "interrupted"} else result

    def result(self, external_project_key: str, operation_key: str) -> ExecutionResultV1:
        query = urllib.parse.urlencode({"external_project_key": external_project_key, "operation_key": operation_key})
        return ExecutionResultV1.model_validate(self._request("GET", f"/api/v1/executions?{query}"))

    def routes(self, *, probe: bool = False) -> list[dict[str, Any]]:
        value = self._request("POST", "/api/v1/routes/probe", {}) if probe else self._request("GET", "/api/v1/routes")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise LatticeClientError("Lattice provider catalogue is malformed")
        return value

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        value = self._request("GET", f"/api/v1/artifacts/{urllib.parse.quote(artifact_id, safe='')}")
        if not isinstance(value, dict):
            raise LatticeClientError("Lattice artifact response is malformed")
        return value

    def blob(self, content_sha256: str) -> bytes:
        if len(content_sha256) != 64 or any(character not in "0123456789abcdef" for character in content_sha256):
            raise ValueError("Lattice blob identity must be a lowercase SHA-256 digest")
        content = self._request_bytes(f"/api/v1/blobs/{content_sha256}")
        if __import__("hashlib").sha256(content).hexdigest() != content_sha256:
            raise LatticeClientError("Lattice blob bytes disagree with their requested identity")
        return content

    def import_history(self, request: HistoricalExecutionImportV1 | dict[str, Any]) -> HistoricalExecutionImportResultV1:
        value = request if isinstance(request, HistoricalExecutionImportV1) else HistoricalExecutionImportV1.model_validate(request)
        return HistoricalExecutionImportResultV1.model_validate(self._request("POST", "/api/v1/historical-executions", value.model_dump(mode="json", exclude_unset=True)))

    def wait(self, external_project_key: str, operation_key: str, project_id: str, *, timeout: float = 1800.0) -> ExecutionResultV1:
        """Follow the canonical event stream and inspect only when it advances."""

        request = urllib.request.Request(
            f"{self.base_url}/api/v1/projects/{urllib.parse.quote(project_id, safe='')}/events/stream?after=0",
            method="GET",
            headers={"Host": self.host_header, "Authorization": f"Bearer {self.operator_token}", "Accept": "text/event-stream"},
        )
        deadline = time.monotonic() + timeout
        try:
            with urllib.request.urlopen(request, timeout=max(timeout, 1.0)) as response:
                for raw_line in response:
                    if time.monotonic() >= deadline:
                        raise LatticeClientError(f"Lattice execution did not finish within {timeout:g} seconds")
                    if raw_line.startswith((b"data:", b":")):
                        current = self.result(external_project_key, operation_key)
                        if current.state.value in {"succeeded", "failed", "interrupted"}:
                            return current
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:4000]
            raise LatticeClientError(f"Lattice event stream returned HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LatticeClientError(f"Lattice event stream failed: {error}") from error
        raise LatticeClientError("Lattice event stream ended before execution became terminal")
