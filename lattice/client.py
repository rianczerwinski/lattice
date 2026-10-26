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
from .contracts import ExternalOperationWithdrawalRequestV1, ExternalOperationWithdrawalV1, DurableRunDeclarationV1, DurableRunDeclarationV2, ExecuteRequest, ExecutionResultV1, ExecutionSubmissionV1, HistoricalExecutionImportResultV1, HistoricalExecutionImportV1, InvocationBatchCreateV1, InvocationBatchPreviewV1, ProjectCreate
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

    def conversation_create(self, request) -> dict:
        payload=request.model_dump(mode="json") if hasattr(request,"model_dump") else request
        return self._request("POST","/api/v1/conversations",payload)

    def conversation_list(self, project_id: str, *, owning_task_ref: str | None = None, after: str = "", limit: int = 100):
        query={"project_id":project_id,"after":after,"limit":limit}
        if owning_task_ref is not None:
            query["owning_task_ref"]=owning_task_ref
        return self._request("GET","/api/v1/conversations?"+urllib.parse.urlencode(query))

    def conversation_show(self, conversation_id: str, *, after: int = 0, limit: int = 100):
        return self._request("GET",f"/api/v1/conversations/{urllib.parse.quote(conversation_id,safe='')}?"+urllib.parse.urlencode({"after":after,"limit":limit}))

    def conversation_mutate(self, conversation_id: str, action: str, request):
        if action not in {"send","steer","interrupt","continue","withdraw","adopt","recovery-preview"}:
            raise ValueError("unknown conversation operation")
        payload=request.model_dump(mode="json") if hasattr(request,"model_dump") else request
        return self._request("POST",f"/api/v1/conversations/{urllib.parse.quote(conversation_id,safe='')}/{action}",payload)

    def conversation_preview(self, conversation_id: str, **options):
        return self.conversation_mutate(conversation_id,"recovery-preview",options)

    def conversation_send(self, conversation_id: str, request):
        return self.conversation_mutate(conversation_id,"send",request)

    def conversation_steer(self, conversation_id: str, request):
        return self.conversation_mutate(conversation_id,"steer",request)

    def conversation_interrupt(self, conversation_id: str, request):
        return self.conversation_mutate(conversation_id,"interrupt",request)

    def conversation_continue(self, conversation_id: str, request):
        return self.conversation_mutate(conversation_id,"continue",request)

    def conversation_observations(self, conversation_id: str, *, after: int = 0, limit: int = 100):
        return self._request("GET",f"/api/v1/conversations/{urllib.parse.quote(conversation_id,safe='')}/observations?"+urllib.parse.urlencode({"after":after,"limit":limit}))

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

    def create_project(self, project: ProjectCreate | dict[str, Any]) -> dict[str, Any]:
        value = project if isinstance(project, ProjectCreate) else ProjectCreate.model_validate(project)
        if value.workflow_pack is not None:
            raise ValueError("the manual project endpoint does not accept a workflow pack")
        result = self._request("POST", "/api/v1/projects", value.model_dump(mode="json", exclude={"workflow_pack"}))
        if not isinstance(result, dict) or not isinstance(result.get("project_id"), str):
            raise LatticeClientError("Lattice project creation response is malformed")
        return result

    def project(self, project_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/api/v1/projects/{urllib.parse.quote(project_id, safe='')}")
        if not isinstance(result, dict) or result.get("project_id") != project_id:
            raise LatticeClientError("Lattice project response is malformed")
        return result

    def workflow_pack(self, pack_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/api/v1/workflow-packs/{urllib.parse.quote(pack_id, safe='')}")
        if not isinstance(result, dict) or not isinstance(result.get("canonical_sha256"), str):
            raise LatticeClientError("Lattice workflow-pack response is malformed")
        return result

    def create_workflow_project(self, pack_id: str, request: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", f"/api/v1/workflow-packs/{urllib.parse.quote(pack_id, safe='')}/projects", request)
        project = result.get("project") if isinstance(result, dict) else None
        if not isinstance(project, dict) or not isinstance(project.get("project_id"), str):
            raise LatticeClientError("Lattice workflow-project creation response is malformed")
        return result

    def workflow_project(self, project_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/api/v1/workflow-projects/{urllib.parse.quote(project_id, safe='')}")
        if not isinstance(result, dict) or result.get("project_id") != project_id:
            raise LatticeClientError("Lattice workflow-project response is malformed")
        return result

    def refresh_workflow_project(self, pack_id: str, project_id: str) -> dict[str, Any]:
        result = self._request("POST", f"/api/v1/workflow-packs/{urllib.parse.quote(pack_id, safe='')}/projects/{urllib.parse.quote(project_id, safe='')}/refresh", {})
        workflow = result.get("workflow") if isinstance(result, dict) else None
        blackboard = result.get("blackboard") if isinstance(result, dict) else None
        if not isinstance(workflow, dict) or not isinstance(blackboard, dict) or not isinstance(blackboard.get("block_id"), str):
            raise LatticeClientError("Lattice workflow refresh response is malformed")
        return result

    def create_workflow_adapter_evidence(self, pack_id: str, project_id: str, request: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/api/v1/workflow-packs/{urllib.parse.quote(pack_id, safe='')}/projects/{urllib.parse.quote(project_id, safe='')}/adapter-evidence",
            request,
        )
        if not isinstance(result, dict) or not isinstance(result.get("block_id"), str) or result.get("issuer_kind") != "workflow-adapter":
            raise LatticeClientError("Lattice workflow-adapter evidence response is malformed")
        return result

    def transition_workflow(self, pack_id: str, request: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", f"/api/v1/workflow-packs/{urllib.parse.quote(pack_id, safe='')}/transitions", request)
        workflow = result.get("workflow") if isinstance(result, dict) else None
        if not isinstance(workflow, dict) or not isinstance(workflow.get("revision"), int):
            raise LatticeClientError("Lattice workflow-transition response is malformed")
        return result

    def preview_invocation_batch(self, request: InvocationBatchPreviewV1 | dict[str, Any]) -> dict[str, Any]:
        value = request if isinstance(request, InvocationBatchPreviewV1) else InvocationBatchPreviewV1.model_validate(request)
        result = self._request("POST", "/api/v1/invocation-batches/preview", value.model_dump(mode="json"))
        if not isinstance(result, dict) or result.get("batch_id") != value.batch_id or not isinstance(result.get("preview_sha256"), str):
            raise LatticeClientError("Lattice invocation-batch preview response is malformed")
        return result

    def create_invocation_batch(self, request: InvocationBatchCreateV1 | dict[str, Any]) -> dict[str, Any]:
        value = request if isinstance(request, InvocationBatchCreateV1) else InvocationBatchCreateV1.model_validate(request)
        result = self._request("POST", "/api/v1/invocation-batches", value.model_dump(mode="json"))
        if not isinstance(result, dict) or result.get("batch_id") != value.batch_id or not isinstance(result.get("members"), list):
            raise LatticeClientError("Lattice invocation-batch creation response is malformed")
        return result

    def invocation_batch(self, batch_id: str) -> dict[str, Any]:
        if not batch_id:
            raise ValueError("Lattice invocation-batch identity cannot be empty")
        result = self._request("GET", f"/api/v1/invocation-batches/{urllib.parse.quote(batch_id, safe='')}")
        if not isinstance(result, dict) or result.get("batch_id") != batch_id or not isinstance(result.get("members"), list):
            raise LatticeClientError("Lattice invocation-batch inspection response is malformed")
        return result

    def invocation(self, invocation_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/api/v1/invocations/{urllib.parse.quote(invocation_id, safe='')}")
        if not isinstance(result, dict) or result.get("invocation_id") != invocation_id:
            raise LatticeClientError("Lattice invocation response is malformed")
        return result

    def execute_invocation(self, invocation_id: str, idempotency_key: str) -> dict[str, Any]:
        value = ExecuteRequest(idempotency_key=idempotency_key)
        result = self._request("POST", f"/api/v1/invocations/{urllib.parse.quote(invocation_id, safe='')}/execute", value.model_dump(mode="json"))
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice invocation execution response is malformed")
        return result

    def withdraw_external_operation(self, request: ExternalOperationWithdrawalRequestV1 | dict[str, Any]) -> ExternalOperationWithdrawalV1:
        value = request if isinstance(request, ExternalOperationWithdrawalRequestV1) else ExternalOperationWithdrawalRequestV1.model_validate(request)
        return ExternalOperationWithdrawalV1.model_validate(self._request("POST", "/api/v1/external-operations/withdraw", value.model_dump(mode="json")))

    def result(self, external_project_key: str, operation_key: str) -> ExecutionResultV1:
        query = urllib.parse.urlencode({"external_project_key": external_project_key, "operation_key": operation_key})
        return ExecutionResultV1.model_validate(self._request("GET", f"/api/v1/executions?{query}"))

    def cancel_attempt(self, attempt_id: str) -> dict[str, Any]:
        value = self._request("POST", f"/api/v1/attempts/{urllib.parse.quote(attempt_id, safe='')}/cancel", {})
        if not isinstance(value, dict):
            raise LatticeClientError("Lattice cancellation response is malformed")
        return value

    def routes(self, *, probe: bool = False) -> list[dict[str, Any]]:
        value = self._request("POST", "/api/v1/routes/probe", {}) if probe else self._request("GET", "/api/v1/routes")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise LatticeClientError("Lattice provider catalogue is malformed")
        return value

    def route_amendments(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/role-sessions/{urllib.parse.quote(session_id, safe='')}/route-amendments")

    def request_route_amendment(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        from .contracts import RouteAmendmentRequest
        value = RouteAmendmentRequest.model_validate(payload)
        return self._request("POST", f"/api/v1/role-sessions/{urllib.parse.quote(session_id, safe='')}/route-amendments", value.model_dump(mode="json"))

    def decide_route_amendment(self, session_id: str, amendment_id: str, *, decision: str, note: str) -> dict[str, Any]:
        from .contracts import RouteAmendmentDecision
        value = RouteAmendmentDecision(decision=decision, note=note)
        return self._request("POST", f"/api/v1/role-sessions/{urllib.parse.quote(session_id, safe='')}/route-amendments/{urllib.parse.quote(amendment_id, safe='')}/decision", value.model_dump(mode="json"))

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

    def declare_run(self, declaration: DurableRunDeclarationV1 | DurableRunDeclarationV2 | dict[str, Any]) -> dict[str, Any]:
        if isinstance(declaration, DurableRunDeclarationV1):
            value = declaration
        elif declaration.get("contract_version") == "DurableRunDeclaration/2.0":
            value = DurableRunDeclarationV2.model_validate(declaration)
        else:
            value = DurableRunDeclarationV1.model_validate(declaration)
        result = self._request("POST", "/api/v1/runs", value.model_dump(mode="json"))
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice durable-run declaration response is malformed")
        return result

    def run(self, run_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/api/v1/runs/{urllib.parse.quote(run_id, safe='')}")
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice durable-run status response is malformed")
        return result

    def runs(self, *, project_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({key: value for key, value in {"project_id": project_id, "limit": limit}.items() if value is not None})
        result = self._request("GET", f"/api/v1/runs?{query}")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise LatticeClientError("Lattice durable-run catalogue response is malformed")
        return result

    def admit_run(self, run_id: str) -> dict[str, Any]:
        result = self._request("POST", f"/api/v1/runs/{urllib.parse.quote(run_id, safe='')}/admit", {})
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice durable-run admission response is malformed")
        return result

    def start_run(self, run_id: str, idempotency_key: str) -> dict[str, Any]:
        value = ExecuteRequest(idempotency_key=idempotency_key)
        result = self._request("POST", f"/api/v1/runs/{urllib.parse.quote(run_id, safe='')}/start", value.model_dump(mode="json"))
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice durable-run start response is malformed")
        return result

    def stop_run(self, run_id: str) -> dict[str, Any]:
        result = self._request("POST", f"/api/v1/runs/{urllib.parse.quote(run_id, safe='')}/stop", {})
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice durable-run stop response is malformed")
        return result

    def resume_run(self, run_id: str, idempotency_key: str) -> dict[str, Any]:
        value = ExecuteRequest(idempotency_key=idempotency_key)
        result = self._request("POST", f"/api/v1/runs/{urllib.parse.quote(run_id, safe='')}/resume", value.model_dump(mode="json"))
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice durable-run resume response is malformed")
        return result

    def run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/v1/runs/{urllib.parse.quote(run_id, safe='')}/artifacts")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise LatticeClientError("Lattice durable-run artifact response is malformed")
        return result

    def run_events(self, run_id: str, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"after": after, "limit": limit})
        result = self._request("GET", f"/api/v1/runs/{urllib.parse.quote(run_id, safe='')}/events?{query}")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise LatticeClientError("Lattice durable-run event response is malformed")
        return result

    def run_artifact(self, artifact_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/api/v1/run-artifacts/{urllib.parse.quote(artifact_id, safe='')}")
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice durable-run artifact response is malformed")
        return result

    def run_artifact_bytes(self, artifact_id: str) -> bytes:
        return self._request_bytes(f"/api/v1/run-artifacts/{urllib.parse.quote(artifact_id, safe='')}/content")

    def graft_run_artifact(self, artifact_id: str) -> dict[str, Any]:
        result = self._request("POST", f"/api/v1/run-artifacts/{urllib.parse.quote(artifact_id, safe='')}/context-blocks", {})
        if not isinstance(result, dict):
            raise LatticeClientError("Lattice durable-run artifact graft response is malformed")
        return result

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
