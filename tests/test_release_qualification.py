"""Truth-typed checks for the recoverable release qualifier and its key-bound consumer entry points.

Independent authorities these tests lean on, none of them the qualifier itself:

- ``lattice.contracts.ExecutionResultV1`` and ``InvocationState``: the exact result shape the
  qualifier reads back from Lattice. The fake store below returns real instances of that model.
- ``lattice.store.LatticeStore.reserve_external_submission`` (store.py:2192-2209): an external
  operation whose project key and operation key already exist replays only when the request bytes
  and idempotency key are identical, conflicts otherwise, and a reused idempotency key conflicts.
  ``FakeLattice`` enforces exactly those rules and counts every genuine dispatch, so "no purchase"
  is an assertion on the store, not on the qualifier's own bookkeeping.
- ``lattice.client.LatticeClient``: every HTTP failure surfaces as ``Lattice returned HTTP <code>: <body>``;
  ``lattice/server.py`` maps ``LatticeNotFound`` to 404 and ``LatticeConflict`` to 409, and its
  ``external_result`` answers 409 "external execution was reserved but has no sealed invocation" for a
  key that was reserved but never sealed, which ``operations.seal_execution_submission`` seals on resubmission.
- ``ops/verify_execution_consolidation.py``: the marker mapping (line 727) and the provider and
  consumer receipt field sets (lines 683 and 695) are read from that source with ``ast``.
- The live consumer clients: field-survey derives ``idempotency_key`` as ``field-survey:<sha256(key)>``
  (domain_research/lattice_client.py:185) and Octopus as ``octopus:<sha256(key)>`` with a 300-character
  key limit (packages/model_execution/lattice.py:215-229). The fake clients bound to the owned entry
  points reproduce those derivations.

The owned field-survey and Octopus entry points are exercised as real functions, loaded from the
checkout under test, against those fake clients. No provider, subprocess, or network is touched.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, sha256_json, strict_json_loads
from lattice.client import LatticeClientError
from lattice.contracts import ExecutionResultV1, InvocationState
from ops import qualify_release
from ops.qualify_release import (
    CONSUMERS,
    EXTERNAL_PROJECT_KEY,
    MARKER_PROFILES,
    QualificationFailure,
    QualificationInterrupted,
    QualificationLedger,
    canary_marker,
    consumer_operation_key,
    consumer_unit_id,
    expected_unit_ids,
    operation_key,
    provider_unit_id,
)


CHECKOUT = Path(qualify_release.__file__).resolve().parent.parent
RUNTIME_ID = "0.3.1-fixture"
CANON_ID = "canon.quaternary-exhaustion"
FIELD_SURVEY_ID = "field-survey.research-pipeline"
OCTOPUS_ID = "octopus.model-execution"
CANON_PROJECT_KEY = CONSUMERS[CANON_ID]["external_project_key"]
FIELD_SURVEY_PROJECT_KEY = CONSUMERS[FIELD_SURVEY_ID]["external_project_key"]
OCTOPUS_PROJECT_KEY = CONSUMERS[OCTOPUS_ID]["external_project_key"]
CANON_OPERATION_KEY = f"countersearch-canary:opus5:{sha256_bytes(b'fixture-packet')}"
PROVIDER_UNITS = expected_unit_ids()[:16]
MARKER_PATTERN = re.compile(r"exact marker ([A-Z0-9:-]+)")
SENTINEL_PATTERN = re.compile(r"Reply with exactly (\S+) and nothing else")


# --- fake Lattice store and operator client ----------------------------------


def _respond(request: dict[str, Any]) -> str:
    """Synthetic provider output that satisfies each canary's own semantic check."""

    prompt = str(request["prompt"])
    sentinel = SENTINEL_PATTERN.search(prompt)
    if sentinel is not None:
        return sentinel.group(1)
    marker = MARKER_PATTERN.search(prompt)
    text = f"Synthetic evidence containing {marker.group(1) if marker else 'no marker'}."
    if request.get("route", {}).get("provider") == "google":
        return json.dumps({"response": text, "toolSummary": "none", "toolAction": "none"}, sort_keys=True)
    return text


SESSION_HEAD_REFUSAL = "native continuation parent is no longer the provider session head; use portable ancestry or select the latest native invocation"
RESERVED_UNSEALED_REFUSAL = "external execution was reserved but has no sealed invocation"


class FakeLattice:
    """One durable store shared by every operator handle; replays and conflicts exactly like the real store.

    Two further store behaviours are reproduced because the qualifier's recovery depends on them:

    - Reservation precedes validation (``operations.seal_execution_submission`` reserves, then
      ``store._assert_actor_route_authorized`` validates). A request refused at admission leaves its
      key reserved with exactly its bytes and no invocation: ``lookup`` answers the reserved-unsealed
      409, identical bytes re-validate, and different bytes are a key conflict.
    - Provider sessions have one head (``store._native_session_state``): a workspace agent opens a
      session, a succeeded resume keeps that session and becomes its head, a fork opens a new session,
      and a native continuation whose parent is not its session head is refused with the store's exact
      detail. A refusal is not a dispatch; ``dispatches`` counts paid model calls only.
    """

    def __init__(self) -> None:
        self.operations: dict[tuple[str, str], dict[str, Any]] = {}
        self.idempotency: dict[str, tuple[str, str]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.dispatches: list[tuple[str, str]] = []
        self.fail_keys: set[tuple[str, str]] = set()
        self.visible_state: dict[tuple[str, str], InvocationState] = {}
        self.result_errors: dict[str, LatticeClientError] = {}
        self.before_dispatch: Callable[[dict[str, Any]], None] | None = None
        self.after_admission: Callable[[dict[str, Any]], None] | None = None
        self.sessions: dict[str, str] = {}
        self.heads: dict[tuple[str, str], str] = {}
        self.refusals: list[tuple[tuple[str, str], str]] = []

    def reserved_unsealed(self) -> list[tuple[str, str]]:
        """Keys Lattice reserved and then refused: retained, unsealed, never dispatched."""

        return [identity for identity, record in self.operations.items() if record["result"] is None]

    def _admit(self, identity: tuple[str, str], request: dict[str, Any]) -> None:
        route = request.get("route", {})
        if route.get("execution_profile") != "native-continuation":
            return
        parent = route.get("native_parent_invocation_id")
        detail: str | None = None
        if parent not in request.get("parent_invocation_ids", []):
            detail = "native continuation parent must be explicitly selected as invocation ancestry"
        elif parent not in self.sessions:
            detail = "native continuation parent has no observed provider session identity"
        elif self.heads[(str(route["provider"]), self.sessions[parent])] != parent:
            detail = SESSION_HEAD_REFUSAL
        if detail is not None:
            self.refusals.append((identity, detail))
            raise LatticeClientError(f"Lattice returned HTTP 409: {json.dumps({'detail': detail})}")

    def _record_session(self, request: dict[str, Any], invocation_id: str) -> None:
        route = request.get("route", {})
        profile = route.get("execution_profile")
        if profile == "workspace-agent" or (profile == "native-continuation" and route.get("continuation_mode") == "fork"):
            session = f"session_{len(self.dispatches)}"
        elif profile == "native-continuation":
            session = self.sessions[str(route["native_parent_invocation_id"])]
        else:
            return
        self.sessions[invocation_id] = session
        self.heads[(str(route["provider"]), session)] = invocation_id

    def submit(self, request: dict[str, Any]) -> ExecutionResultV1:
        identity = (str(request["external_project_key"]), str(request["operation_key"]))
        digest = sha256_json(request)
        idempotency = str(request["idempotency_key"])
        existing = self.operations.get(identity)
        if existing is not None:
            if existing["request_sha256"] != digest or existing["idempotency_key"] != idempotency:
                raise LatticeClientError("Lattice returned HTTP 409: external operation key was reused with different bytes")
            if existing["result"] is not None:
                # Replay: a waiting submit returns once the admitted execution is terminal.
                self.visible_state.pop(identity, None)
                return existing["result"]
            # Reserved but never sealed: identical bytes are validated again, nothing new is reserved.
        else:
            if idempotency in self.idempotency:
                raise LatticeClientError("Lattice returned HTTP 409: external idempotency key was reused for another operation")
            if self.before_dispatch is not None:
                self.before_dispatch(request)
                # The real store reserves inside one transaction: whatever another operator admitted
                # while this submission was in flight wins, and this submission becomes its replay.
                existing = self.operations.get(identity)
                if existing is not None:
                    if existing["request_sha256"] != digest or existing["idempotency_key"] != idempotency:
                        raise LatticeClientError("Lattice returned HTTP 409: external operation key was reused with different bytes")
                    if existing["result"] is not None:
                        return existing["result"]
            if identity not in self.operations:
                self.operations[identity] = {"request_sha256": digest, "idempotency_key": idempotency, "result": None}
                self.idempotency[idempotency] = identity
        self._admit(identity, request)
        self.dispatches.append(identity)
        ordinal = len(self.dispatches)
        provider = str(request.get("route", {}).get("provider", "unknown"))
        common = {
            "external_project_key": identity[0],
            "operation_key": identity[1],
            "project_id": f"project_{identity[0]}",
            "invocation_id": f"invocation_{ordinal}",
            "attempt_id": f"attempt_{ordinal}",
            "manifest_sha256": sha256_json({"manifest": request}),
            "provider_attestation": {"profile_id": f"provider-profile:{provider}"},
        }
        if identity in self.fail_keys:
            result = ExecutionResultV1(**common, state=InvocationState.failed, typed_failure="subscription_exhausted")
        else:
            content = _respond(request)
            artifact_id = f"artifact_{ordinal}"
            content_sha256 = sha256_bytes(content.encode("utf-8"))
            self.artifacts[artifact_id] = {"artifact_id": artifact_id, "content": content, "content_sha256": content_sha256}
            result = ExecutionResultV1(
                **common,
                state=InvocationState.succeeded,
                output_block_id=f"block_{ordinal}",
                output_sha256=content_sha256,
                output_artifact_ids=[artifact_id],
                journal_receipt={"kind": "lattice-model-runtime-receipt", "invocation_id": f"invocation_{ordinal}"},
            )
        self.operations[identity]["result"] = result
        if result.state == InvocationState.succeeded:
            self._record_session(request, result.invocation_id)
        if self.after_admission is not None:
            self.after_admission(request)
        return result

    def lookup(self, external_project_key: str, operation_key_value: str) -> ExecutionResultV1:
        error = self.result_errors.get(external_project_key)
        if error is not None:
            raise error
        identity = (external_project_key, operation_key_value)
        existing = self.operations.get(identity)
        if existing is None:
            raise LatticeClientError("Lattice returned HTTP 404: external operation not found")
        if existing["result"] is None:
            raise LatticeClientError(f"Lattice returned HTTP 409: {json.dumps({'detail': RESERVED_UNSEALED_REFUSAL})}")
        override = self.visible_state.get(identity)
        if override is not None:
            return existing["result"].model_copy(update={"state": override})
        return existing["result"]

    def admitted(self, external_project_key: str, operation_key_value: str) -> ExecutionResultV1:
        return self.operations[(external_project_key, operation_key_value)]["result"]

    def count(self, external_project_key: str) -> int:
        return sum(1 for project, _ in self.dispatches if project == external_project_key)


class FakeClient:
    """A fresh operator handle; mirrors only the LatticeClient methods the qualifier calls."""

    def __init__(self, lattice: FakeLattice) -> None:
        self.lattice = lattice

    def submit(self, submission: dict[str, Any], *, wait: bool = False, timeout: float = 1800.0) -> ExecutionResultV1:
        del wait, timeout
        return self.lattice.submit(submission)

    def result(self, external_project_key: str, operation_key_value: str) -> ExecutionResultV1:
        return self.lattice.lookup(external_project_key, operation_key_value)

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        return dict(self.lattice.artifacts[artifact_id])


# --- fixtures ------------------------------------------------------------------


def _source(ref: str) -> list[dict[str, str | None]]:
    return [{"ref": ref, "revision": "fixture", "sha256": None}]


def _runtime(runtime_id: str = RUNTIME_ID) -> dict[str, Any]:
    return {
        "generated_object": generated_object(kind="lattice-installed-runtime", schema_name="lattice-installed-runtime", schema_version=4, subject=f"lattice:installed-runtime:{runtime_id}", sources=_source("fixture:runtime")),
        "contract_version": "LatticeInstalledRuntime/4.0",
        "runtime_id": runtime_id,
    }


class Harness:
    """Owned temporary world: runtime manifest, data root, consumer source tree, shared fake store."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.axisvi_root = tmp_path / "axisvi"
        self.data_root = tmp_path / "data"
        self.data_root.mkdir()
        self.runtime = _runtime()
        self.runtime_manifest = tmp_path / "runtime.json"
        self.runtime_manifest.write_bytes(canonical_bytes(self.runtime))
        self.runtime_manifest_sha256 = sha256_bytes(self.runtime_manifest.read_bytes())
        self.ledger_root = tmp_path / "qualification.json.ledger"
        self.installed_lattice = tmp_path / "runtime" / ".venv" / "bin" / "lattice"
        self.installed_lattice.parent.mkdir(parents=True)
        self.installed_lattice.write_text("#!/bin/sh\n", encoding="utf-8")
        self.lattice = FakeLattice()
        for spec in CONSUMERS.values():
            (self.axisvi_root / spec["source_root"]).mkdir(parents=True, exist_ok=True)
            for relative in spec["bound_sources"]:
                path = self.axisvi_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# fixture bound source {relative}\n", encoding="utf-8")

    def ledger(self, *, runtime: dict[str, Any] | None = None, manifest_sha256: str | None = None, data_root: Path | None = None) -> QualificationLedger:
        return QualificationLedger.open(self.ledger_root, runtime=runtime or self.runtime, runtime_manifest_sha256=manifest_sha256 or self.runtime_manifest_sha256, data_root=data_root or self.data_root)

    def client(self) -> FakeClient:
        return FakeClient(self.lattice)

    def providers(self, *, retry: bool = False, client: FakeClient | None = None) -> list[dict[str, Any]]:
        return qualify_release.run_provider_canaries(client or self.client(), self.data_root, ledger=self.ledger(), timeout=1, retry=retry)

    def consumers(self, *, retry: bool = False, client: FakeClient | None = None) -> list[dict[str, Any]]:
        return qualify_release.run_consumer_canaries(client or self.client(), self.axisvi_root, self.data_root, self.installed_lattice, ledger=self.ledger(), timeout=1, retry=retry)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _unit_of(request: dict[str, Any]) -> tuple[str, int | None] | None:
    key = str(request["operation_key"])
    prefix = f"qualification:{RUNTIME_ID}:"
    if key.startswith(prefix):
        body, _, attempt = key[len(prefix):].rpartition(":a")
        return (body if body.startswith("consumer:") else f"provider:{body}"), int(attempt)
    if request["external_project_key"] == CANON_PROJECT_KEY:
        return consumer_unit_id(CANON_ID), None
    return None


def _require_request_persisted(ledger: QualificationLedger) -> Callable[[dict[str, Any]], None]:
    """A before-dispatch hook: the ledger must already hold the exact request as an active attempt."""

    def hook(request: dict[str, Any]) -> None:
        located = _unit_of(request)
        assert located is not None, request["operation_key"]
        unit_id, attempt = located
        record = ledger.record(unit_id)
        assert record is not None and record["state"] == "active", unit_id
        if attempt is None:
            # Canon mints its own packet-derived key; the ledger binds the project only.
            assert record["request"]["external_project_key"] == request["external_project_key"]
            return
        assert record["attempt"] == attempt
        if unit_id.startswith("consumer:"):
            # The qualifier never builds a consumer's submission; the owned entry point does.
            # Its durable request is the dispatch contract: the supplied key, project and
            # command carrying that key, bound to the entry point and client source hashes.
            persisted = record["request"]
            key = str(persisted["operation_key"])
            assert key == request["operation_key"]
            client_prefix = str(persisted["consumer_id"]).split(".", 1)[0]
            assert request["idempotency_key"] == f"{client_prefix}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
            assert persisted["external_project_key"] == request["external_project_key"]
            assert persisted["command"][-2:] == ["--operation-key", request["operation_key"]]
            assert persisted["entrypoint_sha256"] == sha256_bytes(Path(persisted["entrypoint"]).read_bytes())
            return
        assert canonical_bytes(record["request"]) == canonical_bytes(request)

    return hook


# --- owned consumer entry points ----------------------------------------------


FIELD_SURVEY_CLIENT_STUB = '''"""Fake field-survey Lattice client boundary; HANDLER is bound by the test."""
HANDLER = None


def resolve_lattice_bin(override=None):
    return override or "lattice"


def invoke(**kwargs):
    if HANDLER is None:
        raise RuntimeError("no fake invoke bound")
    return HANDLER(**kwargs)
'''

FIELD_SURVEY_CONFIG_STUB = '''from types import SimpleNamespace

PROFILES = {"sol": SimpleNamespace(name="sol", driver="codex")}
'''

OCTOPUS_CLIENT_STUB = '''"""Fake Octopus Lattice client boundary; HANDLER is bound by the test."""
EXTERNAL_PROJECT_KEY = "octopus.model-execution"
HANDLER = None


def execute(**kwargs):
    if HANDLER is None:
        raise RuntimeError("no fake execute bound")
    return HANDLER(**kwargs)
'''


def _owned_entrypoint(consumer_id: str, environment_variable: str) -> Path | None:
    override = os.environ.get(environment_variable)
    candidates = [Path(override)] if override else []
    scratch = {FIELD_SURVEY_ID: "consumers/field-survey/run-lattice-consumer-canary.py", OCTOPUS_ID: "consumers/octopus/lattice_execution_canary.py"}[consumer_id]
    candidates.append(CHECKOUT / scratch)
    if len(CHECKOUT.parents) > 2:
        candidates.append(CHECKOUT.parents[2] / CONSUMERS[consumer_id]["entrypoint"])
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class OwnedConsumers:
    field_survey: ModuleType
    octopus: ModuleType
    field_survey_client: ModuleType
    octopus_client: ModuleType
    sources: dict[str, Path]


@pytest.fixture
def owned(tmp_path: Path) -> Any:
    """Load the owned entry points as modules over stub client boundaries, then unload them."""

    field_survey_source = _owned_entrypoint(FIELD_SURVEY_ID, "OWNED_FIELD_SURVEY_CANARY")
    octopus_source = _owned_entrypoint(OCTOPUS_ID, "OWNED_OCTOPUS_CANARY")
    if field_survey_source is None or octopus_source is None:
        pytest.skip("owned consumer entry points are not present beside this checkout; set OWNED_FIELD_SURVEY_CANARY and OWNED_OCTOPUS_CANARY")
    field_survey_root = tmp_path / "owned" / "field-survey"
    (field_survey_root / "scripts").mkdir(parents=True)
    (field_survey_root / "domain_research" / "shared").mkdir(parents=True)
    shutil.copyfile(field_survey_source, field_survey_root / "scripts" / "run-lattice-consumer-canary.py")
    (field_survey_root / "domain_research" / "__init__.py").write_text("", encoding="utf-8")
    (field_survey_root / "domain_research" / "lattice_client.py").write_text(FIELD_SURVEY_CLIENT_STUB, encoding="utf-8")
    (field_survey_root / "domain_research" / "shared" / "__init__.py").write_text("", encoding="utf-8")
    (field_survey_root / "domain_research" / "shared" / "config.py").write_text(FIELD_SURVEY_CONFIG_STUB, encoding="utf-8")
    octopus_root = tmp_path / "owned" / "octopus"
    (octopus_root / "scripts").mkdir(parents=True)
    (octopus_root / "packages" / "model_execution").mkdir(parents=True)
    shutil.copyfile(octopus_source, octopus_root / "scripts" / "lattice_execution_canary.py")
    (octopus_root / "packages" / "__init__.py").write_text("", encoding="utf-8")
    (octopus_root / "packages" / "model_execution" / "__init__.py").write_text("", encoding="utf-8")
    (octopus_root / "packages" / "model_execution" / "lattice.py").write_text(OCTOPUS_CLIENT_STUB, encoding="utf-8")
    assert (field_survey_root / "scripts" / "run-lattice-consumer-canary.py").read_bytes() == field_survey_source.read_bytes()
    assert (octopus_root / "scripts" / "lattice_execution_canary.py").read_bytes() == octopus_source.read_bytes()

    saved_modules = set(sys.modules)
    saved_path = list(sys.path)
    for name in [item for item in sys.modules if item == "domain_research" or item.startswith("domain_research.") or item == "packages" or item.startswith("packages.")]:
        del sys.modules[name]
    try:
        field_survey = _load("owned_field_survey_canary", field_survey_root / "scripts" / "run-lattice-consumer-canary.py")
        octopus = _load("owned_octopus_canary", octopus_root / "scripts" / "lattice_execution_canary.py")
        yield OwnedConsumers(
            field_survey=field_survey,
            octopus=octopus,
            field_survey_client=sys.modules["domain_research.lattice_client"],
            octopus_client=sys.modules["packages.model_execution.lattice"],
            sources={FIELD_SURVEY_ID: field_survey_source, OCTOPUS_ID: octopus_source},
        )
    finally:
        for name in [item for item in sys.modules if item not in saved_modules]:
            if name in {"owned_field_survey_canary", "owned_octopus_canary"} or name == "domain_research" or name.startswith("domain_research.") or name == "packages" or name.startswith("packages."):
                del sys.modules[name]
        sys.path[:] = saved_path


def _bind_field_survey(owned: OwnedConsumers, lattice: FakeLattice) -> list[dict[str, Any]]:
    """Fake ``invoke`` with the live client's request derivation; returns the captured calls."""

    calls: list[dict[str, Any]] = []

    def handler(**kwargs: Any) -> Any:
        calls.append(dict(kwargs))
        key = str(kwargs["operation_key"])
        request = {
            "contract_version": "ExecutionSubmission/1.0",
            "external_project_key": FIELD_SURVEY_PROJECT_KEY,
            "operation_key": key,
            "actor": "field-survey",
            "workflow_role": kwargs["workflow_role"],
            "prompt": kwargs["prompt"],
            "context_block_ids": [],
            "parent_invocation_ids": list(kwargs.get("parent_invocation_ids") or []),
            "route": {"provider": "openai", "model": kwargs["profile"].name, "effort": kwargs["effort"], "tools": kwargs["tool_policy"], "subject": kwargs["subject"]},
            "output_contract": {"contract_version": "OutputContract/2.0", "mode": kwargs["output_mode"]},
            "idempotency_key": f"field-survey:{hashlib.sha256(key.encode('utf-8')).hexdigest()}",
        }
        result = lattice.submit(request)
        artifact = lattice.artifacts[result.output_artifact_ids[0]]
        return SimpleNamespace(content=artifact["content"], result=result.model_dump(mode="json"))

    owned.field_survey_client.HANDLER = handler
    return calls


def _bind_octopus(owned: OwnedConsumers, lattice: FakeLattice) -> list[dict[str, Any]]:
    """Fake ``execute`` with the live client's key limit and idempotency derivation; returns the captured calls."""

    calls: list[dict[str, Any]] = []

    def handler(**kwargs: Any) -> Any:
        calls.append(dict(kwargs))
        key = str(kwargs["operation_key"])
        if not key or len(key) > 300:
            raise ValueError("operation key must be 1-300 characters")
        request = {
            "contract_version": "ExecutionSubmission/1.0",
            "external_project_key": OCTOPUS_PROJECT_KEY,
            "operation_key": key,
            "actor": "octopus",
            "workflow_role": kwargs["workflow_role"],
            "prompt": kwargs["prompt"],
            "context_block_ids": [],
            "parent_invocation_ids": [],
            "route": kwargs["route"],
            "output_contract": kwargs["output_contract"],
            "idempotency_key": f"octopus:{hashlib.sha256(key.encode('utf-8')).hexdigest()}",
        }
        result = lattice.submit(request)
        artifact = lattice.artifacts[result.output_artifact_ids[0]]
        return SimpleNamespace(
            invocation_id=result.invocation_id,
            attempt_id=result.attempt_id,
            manifest_sha256=result.manifest_sha256,
            output_text=artifact["content"],
            output_sha256=result.output_sha256,
            output_artifact_id=result.output_artifact_ids[0],
            provider_attestation=result.provider_attestation,
            journal_receipt=result.journal_receipt,
        )

    owned.octopus_client.HANDLER = handler
    return calls


@dataclass
class FakeConsumerProcesses:
    """Stands in for ``_run_json``: parses the exact command the qualifier built and runs the owned functions in-process.

    Any exception from an entry point is what a dead process looks like to the qualifier.
    """

    harness: Harness
    owned: OwnedConsumers
    calls: list[list[str]] = field(default_factory=list)
    exit_nonzero_after_report: set[str] = field(default_factory=set)
    rewrite_report: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = field(default_factory=dict)
    field_calls: list[dict[str, Any]] = field(default_factory=list)
    octopus_calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.field_calls = _bind_field_survey(self.owned, self.harness.lattice)
        self.octopus_calls = _bind_octopus(self.owned, self.harness.lattice)

    @staticmethod
    def _options(command: list[str]) -> dict[str, str]:
        return {token: command[index + 1] for index, token in enumerate(command) if token.startswith("--")}

    def _canon(self, output_dir: Path) -> tuple[dict[str, Any], Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "contract_version": "ExecutionSubmission/1.0",
            "external_project_key": CANON_PROJECT_KEY,
            "operation_key": CANON_OPERATION_KEY,
            "actor": "canon",
            "workflow_role": "canon:countersearch-canary",
            "prompt": "Reply in one sentence containing exact marker CANON-CANARY.",
            "context_block_ids": [],
            "parent_invocation_ids": [],
            "route": {"provider": "anthropic", "model": "opus"},
            "output_contract": {"contract_version": "OutputContract/2.0", "mode": "text"},
            "idempotency_key": f"canon:{hashlib.sha256(CANON_OPERATION_KEY.encode('utf-8')).hexdigest()}",
        }
        result = self.harness.lattice.submit(request)
        report = {
            "contract_version": CONSUMERS[CANON_ID]["report_contract"],
            "external_project_key": CANON_PROJECT_KEY,
            "operation_key": CANON_OPERATION_KEY,
            "invocation_id": result.invocation_id,
            "attempt_id": result.attempt_id,
            "lattice_project_id": result.project_id,
            "manifest_sha256": result.manifest_sha256,
            "output_sha256": result.output_sha256,
            "output_artifact_id": result.output_artifact_ids[0],
            "journal_receipt": result.journal_receipt,
            "provider_attestation": result.provider_attestation,
        }
        path = output_dir / "canary-report.json"
        with path.open("xb") as handle:
            handle.write(canonical_bytes(report))
        return {"invocation_id": result.invocation_id, "report_path": str(path)}, path

    def __call__(self, command: list[str], *, cwd: Path, environment: dict[str, str]) -> dict[str, Any]:
        self.calls.append(list(command))
        options = self._options(command)
        assert environment["LATTICE_BIN"] == str(self.harness.installed_lattice)
        if "scripts/run-lattice-consumer-canary.py" in command:
            consumer_id = FIELD_SURVEY_ID
        elif "scripts/lattice_execution_canary.py" in command:
            consumer_id = OCTOPUS_ID
        elif "canon.discovery.reference_cascade.run_lattice_loop_canary" in command:
            consumer_id = CANON_ID
        else:
            raise AssertionError(f"unexpected consumer command: {command}")
        assert Path(cwd) == (self.harness.axisvi_root / CONSUMERS[consumer_id]["source_root"]).resolve()
        try:
            if consumer_id == FIELD_SURVEY_ID:
                report = self.owned.field_survey.run(Path(options["--output"]), lattice_bin=options["--lattice-bin"], timeout=float(options["--timeout"]), operation_key=options.get("--operation-key"))
                report_path = Path(options["--output"]).resolve()
                summary = dict(report)
            elif consumer_id == OCTOPUS_ID:
                report = self.owned.octopus.run_canary(Path(options["--output"]), lattice_bin=options["--lattice-bin"], timeout=float(options["--timeout"]), operation_key=options.get("--operation-key"))
                report_path = Path(options["--output"]).resolve()
                summary = {"external_project_key": report["external_project_key"], "invocation_id": report["invocation_id"], "manifest_sha256": report["manifest_sha256"], "report_path": str(report_path)}
            else:
                summary, report_path = self._canon(Path(options["--output-dir"]))
        except Exception as error:
            raise qualify_release._ConsumerProcessFailed(f"consumer canary failed: {' '.join(command)}: {error}") from error
        rewrite = self.rewrite_report.get(consumer_id)
        if rewrite is not None:
            report_path.write_bytes(canonical_bytes(rewrite(strict_json_loads(report_path.read_bytes()))))
        if consumer_id in self.exit_nonzero_after_report:
            raise qualify_release._ConsumerProcessFailed(f"consumer canary failed: {' '.join(command)}: exited 1 after writing its report")
        return summary


@pytest.fixture
def processes(harness: Harness, owned: OwnedConsumers, monkeypatch: pytest.MonkeyPatch) -> FakeConsumerProcesses:
    value = FakeConsumerProcesses(harness, owned)
    monkeypatch.setattr(qualify_release, "_run_json", value)
    return value


def _verifier_source() -> str:
    candidates = [Path(qualify_release.__file__).with_name("verify_execution_consolidation.py")]
    override = os.environ.get("LATTICE_VERIFIER_SOURCE")
    if override:
        candidates.insert(0, Path(override))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    pytest.skip("verify_execution_consolidation.py is not beside the qualifier; set LATTICE_VERIFIER_SOURCE")


def _string_sets(source: str) -> set[frozenset[str]]:
    return {frozenset(element.value for element in node.elts) for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Set) and node.elts and all(isinstance(element, ast.Constant) and isinstance(element.value, str) for element in node.elts)}


# --- checkout under test -------------------------------------------------------


@pytest.mark.smoke
def test_the_qualifier_under_test_is_the_one_beside_these_tests() -> None:
    assert Path(qualify_release.__file__).resolve() == CHECKOUT / "ops" / "qualify_release.py"
    assert Path(__file__).resolve().parent.parent == CHECKOUT


@pytest.mark.contract
def test_expected_units_are_providers_then_suites_then_consumers_in_dispatch_order() -> None:
    units = expected_unit_ids()

    assert len(units) == 26
    assert units[:2] == ["provider:xai:compiled-fresh", "provider:xai:research-fresh"]
    assert units[16:23] == [f"suite:{suite_id}" for suite_id in qualify_release.SUITES]
    assert units[23:] == [consumer_unit_id(consumer_id) for consumer_id in CONSUMERS]
    assert {unit for unit in units[:16]} == {provider_unit_id(provider, profile) for provider, profile in qualify_release.EXPECTED_CANARY_PROFILES}


# --- provider units -------------------------------------------------------------


@pytest.mark.contract
def test_provider_requests_are_persisted_before_dispatch_and_a_fresh_operator_rerun_purchases_nothing(harness: Harness) -> None:
    ledger = harness.ledger()
    harness.lattice.before_dispatch = _require_request_persisted(ledger)

    receipts = harness.providers()

    assert len(receipts) == 16 and len(harness.lattice.dispatches) == 16
    assert [key for _, key in harness.lattice.dispatches] == [operation_key(RUNTIME_ID, unit.split(":", 2)[1], unit.split(":", 2)[2], 1) for unit in PROVIDER_UNITS]
    status = ledger.status()
    assert status["complete"] == PROVIDER_UNITS and status["active"] == [] and status["failed"] == []
    assert status["remaining"] == expected_unit_ids()[16:]

    again = harness.providers(client=FakeClient(harness.lattice))

    assert again == receipts
    assert len(harness.lattice.dispatches) == 16


@pytest.mark.contract
def test_interruption_after_a_completed_unit_is_continued_by_a_fresh_operator_without_purchase(harness: Harness) -> None:
    ledger = harness.ledger()
    interrupted_key = operation_key(RUNTIME_ID, "meta", "compiled-fresh", 1)

    def lose_operator(request: dict[str, Any]) -> None:
        if request["operation_key"] == interrupted_key:
            raise ConnectionError("operator process lost after Lattice admitted the request")

    harness.lattice.after_admission = lose_operator
    with pytest.raises(ConnectionError):
        harness.providers()

    status = ledger.status()
    assert status["complete"] == ["provider:xai:compiled-fresh", "provider:xai:research-fresh"]
    assert status["active"] == ["provider:meta:compiled-fresh"]
    assert len(harness.lattice.dispatches) == 3
    admitted = harness.lattice.admitted(EXTERNAL_PROJECT_KEY, interrupted_key).invocation_id

    harness.lattice.after_admission = None
    receipts = harness.providers(client=FakeClient(harness.lattice))

    assert len(receipts) == 16 and len(harness.lattice.dispatches) == 16
    keys = [key for _, key in harness.lattice.dispatches]
    assert len(set(keys)) == 16 and all(key.endswith(":a1") for key in keys)
    record = ledger.record("provider:meta:compiled-fresh")
    assert record is not None and record["state"] == "complete" and record["attempt"] == 1 and record["invocation_id"] == admitted


@pytest.mark.contract
def test_a_failed_provider_unit_stops_the_run_and_only_retry_mints_its_next_attempt(harness: Harness) -> None:
    ledger = harness.ledger()
    failing = operation_key(RUNTIME_ID, "meta", "research-fresh", 1)
    harness.lattice.fail_keys.add((EXTERNAL_PROJECT_KEY, failing))

    with pytest.raises(QualificationFailure, match="provider canary failed: meta:research-fresh"):
        harness.providers()
    assert len(harness.lattice.dispatches) == 4
    assert ledger.status()["failed"] == ["provider:meta:research-fresh"]

    with pytest.raises(QualificationFailure, match=r"rerun with --retry to mint attempt 2"):
        harness.providers()
    assert len(harness.lattice.dispatches) == 4

    harness.lattice.fail_keys.clear()
    receipts = harness.providers(retry=True)

    assert len(receipts) == 16 and len(harness.lattice.dispatches) == 17
    keys = [key for _, key in harness.lattice.dispatches]
    assert keys.count(failing) == 1 and keys.count(operation_key(RUNTIME_ID, "meta", "research-fresh", 2)) == 1
    assert len(set(keys)) == 17
    record = ledger.record("provider:meta:research-fresh")
    assert record is not None and record["attempt"] == 2 and record["state"] == "complete"
    assert ledger.record("provider:xai:compiled-fresh")["attempt"] == 1


@pytest.mark.oracle
def test_request_bytes_that_drift_under_unchanged_keys_are_refused_and_would_conflict_in_lattice(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.providers()
    original = qualify_release._submission

    def drifted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        value = original(*args, **kwargs)
        value["prompt"] = value["prompt"] + " "
        return value

    monkeypatch.setattr(qualify_release, "_submission", drifted)

    with pytest.raises(QualificationFailure, match="request bytes drifted from the admitted attempt 1: provider:xai:compiled-fresh"):
        harness.providers()
    assert len(harness.lattice.dispatches) == 16

    # The store itself would refuse those bytes: same key, different request, no silent replay.
    request = drifted("xai", "compiled-fresh", operation_key=operation_key(RUNTIME_ID, "xai", "compiled-fresh", 1), route_profile="compiled-fresh", marker=canary_marker("xai", "compiled-fresh"))
    with pytest.raises(LatticeClientError, match="HTTP 409"):
        harness.lattice.submit(request)
    assert len(harness.lattice.dispatches) == 16


@pytest.mark.contract
def test_ledger_refuses_runtime_manifest_data_root_code_and_configuration_drift(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.ledger()
    header_path = harness.ledger_root / "ledger.json"
    header = header_path.read_bytes()

    harness.ledger()  # positive control: identical binding reopens

    with pytest.raises(QualificationFailure, match="runtime_id"):
        harness.ledger(runtime=_runtime("0.3.1-other"))
    with pytest.raises(QualificationFailure, match="runtime_manifest_sha256"):
        harness.ledger(manifest_sha256="0" * 64)
    other_root = harness.tmp_path / "other-data"
    other_root.mkdir()
    with pytest.raises(QualificationFailure, match="data_root"):
        harness.ledger(data_root=other_root)
    with monkeypatch.context() as patch:
        patch.setattr(qualify_release, "_code_sha256", lambda: "1" * 64)
        with pytest.raises(QualificationFailure, match="code_sha256"):
            harness.ledger()
    with monkeypatch.context() as patch:
        patch.setattr(qualify_release, "PROVIDERS", {**qualify_release.PROVIDERS, "xai": {**qualify_release.PROVIDERS["xai"], "model": "grok-4.6"}})
        with pytest.raises(QualificationFailure, match="configuration_sha256"):
            harness.ledger()

    assert header_path.read_bytes() == header
    harness.ledger()


@pytest.mark.concurrency
def test_ledger_records_publish_atomically_once_and_only_identical_concurrent_bytes_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "unit.a1.request.json"
    value = {"contract_version": qualify_release.UNIT_REQUEST_CONTRACT, "unit_id": "provider:xai:compiled-fresh", "attempt": 1}

    qualify_release._exact_write(path, value)
    published = path.read_bytes()
    assert published == canonical_bytes(value) and path.stat().st_mode & 0o777 == 0o600

    # A second writer publishing the same record (two operators, one unit) is the same record.
    qualify_release._exact_write(path, dict(value))
    assert path.read_bytes() == published

    # A writer publishing different bytes under the same name is refused and changes nothing.
    with pytest.raises(QualificationFailure, match="already exists with different bytes"):
        qualify_release._exact_write(path, {**value, "attempt": 2})
    assert path.read_bytes() == published
    assert [item.name for item in tmp_path.iterdir()] == [path.name]


@pytest.mark.concurrency
def test_two_operators_racing_one_ledger_unit_purchase_it_once_and_neither_overwrites_the_other(harness: Harness) -> None:
    ledger = harness.ledger()
    raced = operation_key(RUNTIME_ID, "meta", "compiled-fresh", 1)
    second: list[dict[str, Any]] = []

    def other_operator_finishes_everything_first(request: dict[str, Any]) -> None:
        # Operator A has persisted meta:compiled-fresh and its submission is in flight.
        # Operator B, a fresh process on the same ledger, resumes that active attempt and
        # then completes every remaining unit before A's submission reaches the store.
        if request["operation_key"] != raced:
            return
        harness.lattice.before_dispatch = None
        assert ledger.record("provider:meta:compiled-fresh")["state"] == "active"
        second.extend(harness.providers(client=FakeClient(harness.lattice)))

    harness.lattice.before_dispatch = other_operator_finishes_everything_first
    first = harness.providers()

    assert len(second) == 16 and first == second
    assert len(harness.lattice.dispatches) == 16 and len({key for _, key in harness.lattice.dispatches}) == 16
    status = ledger.status()
    assert status["complete"] == PROVIDER_UNITS and status["active"] == [] and status["failed"] == []
    assert ledger.record("provider:meta:compiled-fresh")["invocation_id"] == harness.lattice.admitted(EXTERNAL_PROJECT_KEY, raced).invocation_id
    assert not [item for item in ledger.units.iterdir() if item.name.endswith(".pending")]


@pytest.mark.regression
def test_a_key_already_carrying_different_bytes_in_lattice_fails_visibly_and_retry_mints_a_fresh_key(harness: Harness) -> None:
    # Another ledger on the same runtime (different qualifier bytes) admitted a different prompt
    # under this attempt's deterministic key. Resubmission conflicts in the store, so the attempt
    # must neither hang as active forever nor be mistaken for a replay.
    ledger = harness.ledger()
    collided = operation_key(RUNTIME_ID, "xai", "compiled-fresh", 1)
    foreign = qualify_release._submission("xai", "compiled-fresh", operation_key=collided, route_profile="compiled-fresh", marker=canary_marker("xai", "compiled-fresh"))
    foreign["prompt"] = foreign["prompt"] + " (an earlier qualifier revision)"
    harness.lattice.submit(foreign)
    assert len(harness.lattice.dispatches) == 1

    with pytest.raises(QualificationFailure, match=r"already holds different bytes under lattice\.release-qualification:" + re.escape(collided)):
        harness.providers()
    assert ledger.status()["failed"] == ["provider:xai:compiled-fresh"] and ledger.status()["active"] == []
    assert len(harness.lattice.dispatches) == 1
    with pytest.raises(QualificationFailure, match=r"rerun with --retry to mint attempt 2"):
        harness.providers()

    receipts = harness.providers(retry=True)

    assert len(receipts) == 16 and len(harness.lattice.dispatches) == 17
    keys = [key for _, key in harness.lattice.dispatches]
    assert keys.count(collided) == 1 and keys.count(operation_key(RUNTIME_ID, "xai", "compiled-fresh", 2)) == 1
    assert ledger.record("provider:xai:compiled-fresh")["attempt"] == 2
    assert receipts[0]["invocation_id"] != harness.lattice.admitted(EXTERNAL_PROJECT_KEY, collided).invocation_id


@pytest.mark.regression
def test_a_retained_provider_receipt_pointing_at_another_runtime_s_execution_is_refused_even_though_lattice_confirms_it(harness: Harness) -> None:
    harness.providers()
    ledger = harness.ledger()
    foreign_key = operation_key("0.3.1-other", "xai", "compiled-fresh", 1)
    foreign = harness.lattice.submit(qualify_release._submission("xai", "compiled-fresh", operation_key=foreign_key, route_profile="compiled-fresh", marker=canary_marker("xai", "compiled-fresh")))
    receipt_path = ledger._path("provider:xai:compiled-fresh", 1, "receipt")
    genuine = receipt_path.read_bytes()
    forged = strict_json_loads(genuine)
    forged["operation_key"] = foreign_key
    forged["invocation_id"] = foreign.invocation_id
    forged["receipt"].update(invocation_id=foreign.invocation_id, manifest_sha256=foreign.manifest_sha256, output_sha256=foreign.output_sha256, provider_profile_id=foreign.provider_attestation["profile_id"], runtime_receipt_sha256=sha256_bytes(canonical_bytes(foreign.journal_receipt)))
    receipt_path.write_bytes(canonical_bytes(forged))

    # The forgery is Lattice-consistent field by field; only the binding to the admitted request can catch it.
    confirmed = harness.client().result(EXTERNAL_PROJECT_KEY, foreign_key)
    assert confirmed.state == InvocationState.succeeded and confirmed.invocation_id == forged["receipt"]["invocation_id"] and confirmed.manifest_sha256 == forged["receipt"]["manifest_sha256"]
    with pytest.raises(QualificationFailure, match="does not belong to its admitted request: provider:xai:compiled-fresh"):
        harness.providers()
    assert len(harness.lattice.dispatches) == 17

    receipt_path.write_bytes(genuine)
    assert harness.providers()[0]["invocation_id"] == harness.lattice.admitted(EXTERNAL_PROJECT_KEY, operation_key(RUNTIME_ID, "xai", "compiled-fresh", 1)).invocation_id
    assert len(harness.lattice.dispatches) == 17


@pytest.mark.regression
def test_retained_provider_receipts_are_re_resolved_and_forgeries_are_refused(harness: Harness) -> None:
    harness.providers()
    ledger = harness.ledger()
    first = operation_key(RUNTIME_ID, "xai", "compiled-fresh", 1)

    with pytest.raises(QualificationFailure, match="no longer resolves in Lattice: provider:xai:compiled-fresh"):
        harness.providers(client=FakeClient(FakeLattice()))

    stored = harness.lattice.operations[(EXTERNAL_PROJECT_KEY, first)]
    genuine = stored["result"]
    stored["result"] = genuine.model_copy(update={"output_sha256": "0" * 64})
    with pytest.raises(QualificationFailure, match="disagrees with Lattice: provider:xai:compiled-fresh"):
        harness.providers()
    stored["result"] = genuine
    harness.providers()

    receipt_path = ledger._path("provider:xai:compiled-fresh", 1, "receipt")
    forged = strict_json_loads(receipt_path.read_bytes())
    forged["invocation_id"] = "invocation_forged"
    forged["receipt"]["invocation_id"] = "invocation_forged"
    receipt_path.write_bytes(canonical_bytes(forged))
    with pytest.raises(QualificationFailure, match="disagrees with Lattice: provider:xai:compiled-fresh"):
        harness.providers()

    assert len(harness.lattice.dispatches) == 16


@pytest.mark.oracle
def test_marker_and_receipt_fields_match_the_execution_consolidation_verifier(harness: Harness) -> None:
    source = _verifier_source()
    mappings = [ast.literal_eval(node) for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Dict) and node.keys and all(isinstance(key, ast.Constant) for key in node.keys) and {key.value for key in node.keys} == set(MARKER_PROFILES)]

    assert mappings == [MARKER_PROFILES]
    assert "f\"QUAL-{item['provider'].upper()}-{marker_profile}\"" in source
    for provider, profile in qualify_release.EXPECTED_CANARY_PROFILES:
        assert canary_marker(provider, profile) == f"QUAL-{provider.upper()}-{mappings[0].get(profile, profile.upper())}"
    assert canary_marker("anthropic", "compiled-fresh") == "QUAL-ANTHROPIC-COMPILED-FRESH" != "QUAL-ANTHROPIC-FRESH"

    receipts = harness.providers()
    sets = _string_sets(source)
    assert frozenset(receipts[0]) in sets
    assert frozenset({"consumer_id", "external_project_key", "invocation_id", "manifest_sha256", "report_path", "report_sha256"}) in sets


@pytest.mark.regression
def test_provider_qualification_probes_the_shallow_subscription_gate_first(harness: Harness) -> None:
    harness.providers()

    assert [tuple(key.split(":")[2:4]) for _, key in harness.lattice.dispatches[:6]] == [
        ("xai", "compiled-fresh"),
        ("xai", "research-fresh"),
        ("meta", "compiled-fresh"),
        ("meta", "research-fresh"),
        ("google", "compiled-fresh"),
        ("google", "research-fresh"),
    ]


# --- native continuation chain and admission refusals -----------------------------


def _superseded_provider_chain(harness: Harness, ledger: QualificationLedger) -> None:
    """The superseded qualifier's dispatch order, byte for byte: nine canaries, then a fork from the workspace agent.

    Every request here is built by the same ``_provider_unit`` the corrected loop uses, so the nine
    completed operations carry exactly the bytes a successor ledger will resubmit. Only the fork differs:
    the superseded loop named the workspace agent as the native parent after the resume had already
    become the session head, which Lattice refuses at admission and leaves reserved under ``fork:a1``.
    """

    client = harness.client()
    workspace = harness.data_root / "acceptance" / "qualification" / f"workspace-{RUNTIME_ID}"
    workspace.mkdir(parents=True, exist_ok=True)
    workspace = workspace.resolve()
    for provider in ("xai", "meta", "google"):
        for profile in qualify_release.RESEARCH_PROFILES:
            qualify_release._provider_unit(client, ledger, provider=provider, profile_label=profile, route_profile=profile, workspace=None, parent_id=None, continuation_mode=None, research=profile == "research-fresh", timeout=1, retry=False)
    fresh = qualify_release._provider_unit(client, ledger, provider="anthropic", profile_label="compiled-fresh", route_profile="compiled-fresh", workspace=None, parent_id=None, continuation_mode=None, research=False, timeout=1, retry=False)
    workspace_record = qualify_release._provider_unit(client, ledger, provider="anthropic", profile_label="workspace-agent", route_profile="workspace-agent", workspace=workspace, parent_id=fresh["invocation_id"], continuation_mode=None, research=False, timeout=1, retry=False)
    for mode in ("resume", "fork"):
        qualify_release._provider_unit(client, ledger, provider="anthropic", profile_label=f"native-continuation:{mode}", route_profile="native-continuation", workspace=workspace, parent_id=workspace_record["invocation_id"], continuation_mode=mode, research=False, timeout=1, retry=False)


@pytest.mark.contract
def test_native_fork_continues_from_the_resumed_session_head(harness: Harness) -> None:
    # Falsifier: the superseded loop forked from the workspace agent after the resume had become
    # the session head; the store refuses that, so the run stopped at anthropic:native-continuation:fork.
    ledger = harness.ledger()

    receipts = harness.providers()

    assert len(receipts) == 16 and len(harness.lattice.dispatches) == 16 and harness.lattice.refusals == []
    for provider in ("anthropic", "openai"):
        workspace = ledger.record(provider_unit_id(provider, "workspace-agent"))
        resume = ledger.record(provider_unit_id(provider, "native-continuation:resume"))
        fork = ledger.record(provider_unit_id(provider, "native-continuation:fork"))
        portable = ledger.record(provider_unit_id(provider, "portable-fallback"))
        assert resume["request"]["parent_invocation_ids"] == [workspace["invocation_id"]]
        assert resume["request"]["route"]["native_parent_invocation_id"] == workspace["invocation_id"] and resume["request"]["route"]["continuation_mode"] == "resume"
        assert fork["request"]["parent_invocation_ids"] == [resume["invocation_id"]]
        assert fork["request"]["route"]["native_parent_invocation_id"] == resume["invocation_id"] and fork["request"]["route"]["continuation_mode"] == "fork"
        assert portable["request"]["parent_invocation_ids"] == [workspace["invocation_id"]] and portable["request"]["route"]["native_parent_invocation_id"] is None
        # Store view: the resume advanced the workspace agent's session head; the fork opened its own session.
        workspace_session = harness.lattice.sessions[workspace["invocation_id"]]
        assert harness.lattice.sessions[resume["invocation_id"]] == workspace_session
        assert harness.lattice.heads[(provider, workspace_session)] == resume["invocation_id"]
        assert harness.lattice.sessions[fork["invocation_id"]] != workspace_session
    assert [key for _, key in harness.lattice.dispatches] == [operation_key(RUNTIME_ID, unit.split(":", 2)[1], unit.split(":", 2)[2], 1) for unit in PROVIDER_UNITS]


@pytest.mark.regression
def test_a_lattice_admission_refusal_is_not_reported_as_a_key_conflict(harness: Harness) -> None:
    # Falsifier: the superseded _execute_canary matched every 409 as "different bytes under the key" and
    # recommended --retry, which cannot repair a request Lattice refused on content.
    key = operation_key(RUNTIME_ID, "anthropic", "native-continuation:fork", 1)
    request = qualify_release._submission("anthropic", "native-continuation:fork", operation_key=key, route_profile="native-continuation", marker=canary_marker("anthropic", "native-continuation:fork"), workspace=harness.data_root, parent_id="invocation_workspace", continuation_mode="fork")

    class Refusing(FakeClient):
        def __init__(self, lattice: FakeLattice, message: str) -> None:
            super().__init__(lattice)
            self.message = message

        def submit(self, submission: dict[str, Any], *, wait: bool = False, timeout: float = 1800.0) -> ExecutionResultV1:
            del submission, wait, timeout
            raise LatticeClientError(self.message)

    def execute(message: str) -> QualificationFailure:
        with pytest.raises(QualificationFailure) as refusal:
            qualify_release._execute_canary(Refusing(harness.lattice, message), request, provider="anthropic", profile_label="native-continuation:fork", marker="QUAL-ANTHROPIC-FORK", timeout=1)
        return refusal.value

    admission = execute(f'Lattice returned HTTP 409: {json.dumps({"detail": SESSION_HEAD_REFUSAL})}')
    assert "already holds different bytes" not in str(admission) and "rerun with --retry" not in str(admission)
    assert f"refused admission of anthropic:native-continuation:fork under {EXTERNAL_PROJECT_KEY}:{key}" in str(admission)
    assert SESSION_HEAD_REFUSAL in str(admission) and "reserved with exactly these bytes" in str(admission)

    for detail in ("external operation key was reused with different bytes", "external idempotency key was reused for another operation"):
        conflict = execute(f"Lattice returned HTTP 409: {detail}")
        assert f"already holds different bytes under {EXTERNAL_PROJECT_KEY}:{key}" in str(conflict) and "rerun with --retry" in str(conflict)
        conflict = execute(f'Lattice returned HTTP 409: {json.dumps({"detail": detail})}')
        assert "already holds different bytes" in str(conflict)

    # Anything that is not a 409 is not adjudicated at all: the attempt stays active for replay.
    with pytest.raises(LatticeClientError, match="HTTP 503"):
        qualify_release._execute_canary(Refusing(harness.lattice, "Lattice returned HTTP 503: service unavailable"), request, provider="anthropic", profile_label="native-continuation:fork", marker="QUAL-ANTHROPIC-FORK", timeout=1)
    assert harness.lattice.dispatches == []


@pytest.mark.regression
def test_a_stale_reserved_fork_is_recovered_by_a_successor_ledger_without_repurchase(harness: Harness) -> None:
    # Epoch 1: the superseded qualifier bought nine canaries and had its fork refused at admission.
    superseded = QualificationLedger.open(harness.tmp_path / "superseded.ledger", runtime=harness.runtime, runtime_manifest_sha256=harness.runtime_manifest_sha256, data_root=harness.data_root)
    stale_key = operation_key(RUNTIME_ID, "anthropic", "native-continuation:fork", 1)
    with pytest.raises(QualificationFailure, match="refused admission of anthropic:native-continuation:fork") as refusal:
        _superseded_provider_chain(harness, superseded)
    assert SESSION_HEAD_REFUSAL in str(refusal.value)
    nine = PROVIDER_UNITS[:9]
    assert nine[-1] == "provider:anthropic:native-continuation:resume"
    assert superseded.status()["complete"] == nine and superseded.status()["failed"] == ["provider:anthropic:native-continuation:fork"]
    assert len(harness.lattice.dispatches) == 9
    assert harness.lattice.reserved_unsealed() == [(EXTERNAL_PROJECT_KEY, stale_key)]
    stale_bytes = dict(harness.lattice.operations[(EXTERNAL_PROJECT_KEY, stale_key)])
    assert qualify_release._lattice_operation_state(harness.client(), EXTERNAL_PROJECT_KEY, stale_key) == "reserved"
    resume_id = superseded.record("provider:anthropic:native-continuation:resume")["invocation_id"]

    # Epoch 2: a successor ledger bound to the corrected code. Run 1 replays the nine completed
    # operations without purchase and records the genuine key conflict for its corrected fork:a1,
    # whose bytes (parent = resumed head) differ from the stale reservation under that key.
    successor = QualificationLedger.open(harness.tmp_path / "successor.ledger", runtime=harness.runtime, runtime_manifest_sha256=harness.runtime_manifest_sha256, data_root=harness.data_root)
    with pytest.raises(QualificationFailure, match=r"already holds different bytes under lattice\.release-qualification:" + re.escape(stale_key)):
        qualify_release.run_provider_canaries(FakeClient(harness.lattice), harness.data_root, ledger=successor, timeout=1)
    assert len(harness.lattice.dispatches) == 9
    assert successor.status()["complete"] == nine and successor.status()["failed"] == ["provider:anthropic:native-continuation:fork"]
    assert successor.record("provider:anthropic:native-continuation:fork")["request"]["route"]["native_parent_invocation_id"] == resume_id
    for unit in nine:
        assert successor.record(unit)["invocation_id"] == superseded.record(unit)["invocation_id"]
    with pytest.raises(QualificationFailure, match=r"rerun with --retry to mint attempt 2"):
        qualify_release.run_provider_canaries(FakeClient(harness.lattice), harness.data_root, ledger=successor, timeout=1)
    assert len(harness.lattice.dispatches) == 9

    # Explicit retry advances only the fork, to attempt 2 from the resumed head, then continues.
    receipts = qualify_release.run_provider_canaries(FakeClient(harness.lattice), harness.data_root, ledger=successor, timeout=1, retry=True)

    assert len(receipts) == 16 and {(item["provider"], item["profile"]) for item in receipts} == qualify_release.EXPECTED_CANARY_PROFILES
    keys = [key for _, key in harness.lattice.dispatches]
    assert len(keys) == 16 and len(set(keys)) == 16, "nine cached plus seven new model calls; the refusal and the conflict bought nothing"
    assert keys.count(stale_key) == 0 and keys.count(operation_key(RUNTIME_ID, "anthropic", "native-continuation:fork", 2)) == 1
    assert keys[9:] == [operation_key(RUNTIME_ID, "anthropic", "native-continuation:fork", 2), operation_key(RUNTIME_ID, "anthropic", "portable-fallback", 1)] + [operation_key(RUNTIME_ID, "openai", profile, 1) for profile in qualify_release.CONTINUITY_PROFILES]
    fork = successor.record("provider:anthropic:native-continuation:fork")
    assert fork["attempt"] == 2 and fork["state"] == "complete" and fork["request"]["parent_invocation_ids"] == [resume_id]
    assert all(successor.record(unit)["attempt"] == 1 for unit in nine)
    status = successor.status()
    assert status["complete"] == PROVIDER_UNITS and status["failed"] == [] and status["active"] == []
    # Nothing rewrote the superseded epoch: its ledger still reads as it did, and Lattice still holds
    # the stale reservation unsealed with its original bytes. That row is retained evidence, not deleted here.
    assert superseded.status()["complete"] == nine and superseded.status()["failed"] == ["provider:anthropic:native-continuation:fork"]
    assert harness.lattice.operations[(EXTERNAL_PROJECT_KEY, stale_key)] == stale_bytes
    assert harness.lattice.reserved_unsealed() == [(EXTERNAL_PROJECT_KEY, stale_key)]
    assert len(harness.lattice.operations) == 17 and len(harness.lattice.refusals) == 1


# --- suites ----------------------------------------------------------------------


@pytest.mark.contract
def test_suites_are_checkpointed_and_completed_suites_never_rerun(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = harness.ledger()
    calls: list[str] = []
    failing = {"canon"}

    def fake_suite(suite_id: str, cwd: Path, commands: list[list[str]], truth_kinds: list[str]) -> dict[str, Any]:
        calls.append(suite_id)
        if suite_id in failing:
            raise QualificationFailure(f"suite failed: {suite_id}: fixture")
        return {"suite_id": suite_id, "truth_kinds": truth_kinds, "command": commands, "cwd": str(cwd), "exit_code": 0, "stdout_sha256": "1" * 64, "stderr_sha256": "2" * 64}

    monkeypatch.setattr(qualify_release, "_run_suite", fake_suite)

    with pytest.raises(QualificationFailure, match="suite failed: canon"):
        qualify_release.run_suites(harness.axisvi_root, ledger=ledger)
    assert calls == ["lattice-backend", "lattice-frontend", "lattice-browser", "canon"]
    with pytest.raises(QualificationFailure, match="--retry"):
        qualify_release.run_suites(harness.axisvi_root, ledger=ledger)
    assert len(calls) == 4

    failing.clear()
    receipts = qualify_release.run_suites(harness.axisvi_root, ledger=harness.ledger(), retry=True)
    assert calls == ["lattice-backend", "lattice-frontend", "lattice-browser", "canon", "canon", "field-survey", "octopus", "slate-mirror"]
    assert [item["suite_id"] for item in receipts] == list(qualify_release.SUITES)
    assert ledger.record("suite:canon")["attempt"] == 2

    assert qualify_release.run_suites(harness.axisvi_root, ledger=harness.ledger()) == receipts
    assert len(calls) == 8


@pytest.mark.regression
def test_release_qualification_checks_provider_quota_before_local_suites(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []
    monkeypatch.setattr(qualify_release, "run_provider_canaries", lambda *_args, **_kwargs: observed.append("providers") or [])
    monkeypatch.setattr(qualify_release, "run_suites", lambda *_args, **_kwargs: observed.append("suites") or [])
    monkeypatch.setattr(qualify_release, "run_consumer_canaries", lambda *_args, **_kwargs: observed.append("consumers") or [])

    qualify_release.collect_release_evidence(harness.client(), harness.axisvi_root, harness.data_root, harness.installed_lattice, ledger=harness.ledger(), timeout=1)

    assert observed == ["providers", "suites", "consumers"]


@pytest.mark.contract
def test_status_mode_mints_the_ledger_and_reports_every_unit_remaining_without_a_client(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    output = harness.tmp_path / "status" / "qualification.json"
    argv = [
        "--axisvi-root", str(harness.axisvi_root),
        "--data-root", str(harness.data_root),
        "--runtime-manifest", str(harness.runtime_manifest),
        "--consumer-registry", str(harness.tmp_path / "registry.json"),
        "--interruption-receipt", str(harness.tmp_path / "interruption.json"),
        "--credential-canary-file", str(harness.tmp_path / "canary"),
        "--output", str(output),
        "--status",
    ]

    assert qualify_release.main(argv) == 0

    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["contract_version"] == qualify_release.STATUS_CONTRACT
    assert printed["runtime_id"] == RUNTIME_ID
    assert printed["remaining"] == expected_unit_ids() and printed["complete"] == [] and printed["active"] == [] and printed["failed"] == []
    assert printed["consumer_continuation"] == {consumer_unit_id(CANON_ID): "content-bound", consumer_unit_id(FIELD_SURVEY_ID): "key-bound", consumer_unit_id(OCTOPUS_ID): "key-bound"}
    assert (output.with_name("qualification.json.ledger") / "ledger.json").is_file()
    assert not output.exists()


@pytest.mark.regression
def test_an_existing_receipt_output_is_refused_before_any_unit_is_touched(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    output = harness.tmp_path / "done" / "qualification.json"
    output.parent.mkdir()
    output.write_bytes(b"{}")
    monkeypatch.setattr(qualify_release, "collect_release_evidence", lambda *_args, **_kwargs: pytest.fail("evidence collection must not start"))
    argv = [
        "--axisvi-root", str(harness.axisvi_root),
        "--data-root", str(harness.data_root),
        "--runtime-manifest", str(harness.runtime_manifest),
        "--consumer-registry", str(harness.tmp_path / "registry.json"),
        "--interruption-receipt", str(harness.tmp_path / "interruption.json"),
        "--credential-canary-file", str(harness.tmp_path / "canary"),
        "--output", str(output),
    ]

    with pytest.raises(QualificationFailure, match="qualification receipt already exists") as refusal:
        qualify_release.main(argv)

    assert str(output.with_name("qualification.json.ledger").resolve()) in str(refusal.value)
    assert output.read_bytes() == b"{}" and harness.lattice.dispatches == []


# --- consumer units through the owned entry points ------------------------------


@pytest.mark.contract
def test_consumers_receive_ledger_keys_bind_their_sources_and_a_fresh_operator_rerun_purchases_nothing(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()
    harness.lattice.before_dispatch = _require_request_persisted(ledger)
    field_key = consumer_operation_key(RUNTIME_ID, FIELD_SURVEY_ID, 1)
    octopus_key = consumer_operation_key(RUNTIME_ID, OCTOPUS_ID, 1)

    receipts = harness.consumers()

    assert [item["consumer_id"] for item in receipts] == list(CONSUMERS)
    assert processes.field_calls[0]["operation_key"] == field_key and processes.octopus_calls[0]["operation_key"] == octopus_key
    for consumer_id, project_key in ((CANON_ID, CANON_PROJECT_KEY), (FIELD_SURVEY_ID, FIELD_SURVEY_PROJECT_KEY), (OCTOPUS_ID, OCTOPUS_PROJECT_KEY)):
        assert harness.lattice.count(project_key) == 1, consumer_id
        record = ledger.record(consumer_unit_id(consumer_id))
        assert record is not None and record["state"] == "complete" and record["kind"] == "consumer"
        request = record["request"]
        assert request["entrypoint_sha256"] == sha256_bytes((harness.axisvi_root / CONSUMERS[consumer_id]["entrypoint"]).read_bytes())
        assert set(request["source_sha256s"]) == set(CONSUMERS[consumer_id]["bound_sources"])
        assert Path(request["report_path"]).is_relative_to((harness.data_root / "acceptance").resolve())
        report = strict_json_loads(Path(request["report_path"]).read_bytes())
        assert report["operation_key"] == record["operation_key"] == (request["operation_key"] or CANON_OPERATION_KEY)
        assert report["contract_version"] == CONSUMERS[consumer_id]["report_contract"]
    assert ledger.record(consumer_unit_id(FIELD_SURVEY_ID))["request"]["command"][-2:] == ["--operation-key", field_key]
    assert ledger.record(consumer_unit_id(OCTOPUS_ID))["request"]["command"][-2:] == ["--operation-key", octopus_key]
    assert "--operation-key" not in ledger.record(consumer_unit_id(CANON_ID))["request"]["command"]
    assert set(receipts[1]) == {"consumer_id", "external_project_key", "invocation_id", "manifest_sha256", "report_path", "report_sha256"}
    assert receipts[1]["report_sha256"] == sha256_bytes(Path(receipts[1]["report_path"]).read_bytes())

    before = len(processes.calls)
    again = harness.consumers(client=FakeClient(harness.lattice))

    assert again == receipts
    assert len(processes.calls) == before
    assert len(harness.lattice.dispatches) == 3


@pytest.mark.contract
def test_key_bound_consumer_killed_after_admission_stays_active_and_continues_by_replay(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()
    field_key = consumer_operation_key(RUNTIME_ID, FIELD_SURVEY_ID, 1)

    def kill(request: dict[str, Any]) -> None:
        if request["external_project_key"] == FIELD_SURVEY_PROJECT_KEY:
            raise RuntimeError("consumer process killed after Lattice admitted its request")

    harness.lattice.after_admission = kill
    with pytest.raises(QualificationInterrupted, match=r"lattice state: succeeded; attempt stays active"):
        harness.consumers()

    status = ledger.status()
    assert status["complete"] == [consumer_unit_id(CANON_ID)] and status["active"] == [consumer_unit_id(FIELD_SURVEY_ID)] and status["failed"] == []
    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1
    assert not Path(ledger.record(consumer_unit_id(FIELD_SURVEY_ID))["request"]["report_path"]).exists()
    admitted = harness.lattice.admitted(FIELD_SURVEY_PROJECT_KEY, field_key).invocation_id

    harness.lattice.after_admission = None
    receipts = harness.consumers(client=FakeClient(harness.lattice))

    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1
    assert [call["operation_key"] for call in processes.field_calls] == [field_key, field_key]
    assert receipts[1]["invocation_id"] == admitted
    record = ledger.record(consumer_unit_id(FIELD_SURVEY_ID))
    assert record["state"] == "complete" and record["attempt"] == 1 and record["operation_key"] == field_key


@pytest.mark.contract
def test_key_bound_consumer_killed_before_admission_is_failed_and_retry_purchases_exactly_once(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()

    def kill(request: dict[str, Any]) -> None:
        if request["external_project_key"] == FIELD_SURVEY_PROJECT_KEY:
            raise RuntimeError("consumer process killed before Lattice admitted anything")

    harness.lattice.before_dispatch = kill
    with pytest.raises(QualificationFailure, match=r"\[lattice state: absent\]"):
        harness.consumers()
    assert ledger.status()["failed"] == [consumer_unit_id(FIELD_SURVEY_ID)]
    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 0

    with pytest.raises(QualificationFailure, match="rerun with --retry to mint attempt 2"):
        harness.consumers()
    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 0

    harness.lattice.before_dispatch = None
    receipts = harness.consumers(retry=True)

    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1
    record = ledger.record(consumer_unit_id(FIELD_SURVEY_ID))
    assert record["attempt"] == 2 and record["operation_key"] == consumer_operation_key(RUNTIME_ID, FIELD_SURVEY_ID, 2)
    assert receipts[1]["invocation_id"] == record["invocation_id"]
    assert harness.lattice.count(CANON_PROJECT_KEY) == 1 and harness.lattice.count(OCTOPUS_PROJECT_KEY) == 1


@pytest.mark.contract
def test_key_bound_consumer_killed_while_lattice_still_runs_stays_active_until_replay_observes_the_terminal_result(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()
    octopus_key = consumer_operation_key(RUNTIME_ID, OCTOPUS_ID, 1)

    def kill_while_running(request: dict[str, Any]) -> None:
        if request["external_project_key"] == OCTOPUS_PROJECT_KEY:
            harness.lattice.visible_state[(OCTOPUS_PROJECT_KEY, octopus_key)] = InvocationState.running
            raise RuntimeError("consumer process killed while its execution was still running")

    harness.lattice.after_admission = kill_while_running
    with pytest.raises(QualificationInterrupted, match=r"lattice state: running"):
        harness.consumers()
    assert ledger.status()["active"] == [consumer_unit_id(OCTOPUS_ID)]
    assert harness.lattice.count(OCTOPUS_PROJECT_KEY) == 1

    harness.lattice.after_admission = None
    receipts = harness.consumers(client=FakeClient(harness.lattice))

    assert harness.lattice.count(OCTOPUS_PROJECT_KEY) == 1
    assert receipts[2]["invocation_id"] == harness.lattice.admitted(OCTOPUS_PROJECT_KEY, octopus_key).invocation_id
    assert ledger.record(consumer_unit_id(OCTOPUS_ID))["state"] == "complete"


@pytest.mark.regression
def test_a_report_written_by_a_process_that_then_died_is_adopted_without_rerunning_the_command(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()
    processes.exit_nonzero_after_report.add(OCTOPUS_ID)

    with pytest.raises(QualificationInterrupted, match=r"lattice state: succeeded"):
        harness.consumers()
    report_path = Path(ledger.record(consumer_unit_id(OCTOPUS_ID))["request"]["report_path"])
    assert report_path.is_file() and ledger.status()["active"] == [consumer_unit_id(OCTOPUS_ID)]

    processes.exit_nonzero_after_report.clear()
    before = len(processes.calls)
    receipts = harness.consumers(client=FakeClient(harness.lattice))

    assert len(processes.calls) == before
    assert harness.lattice.count(OCTOPUS_PROJECT_KEY) == 1
    assert receipts[2]["report_path"] == str(report_path) and receipts[2]["report_sha256"] == sha256_bytes(report_path.read_bytes())


@pytest.mark.regression
def test_an_occupied_report_path_refuses_with_and_without_retry_and_nothing_is_deleted(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()

    def kill(request: dict[str, Any]) -> None:
        if request["external_project_key"] == FIELD_SURVEY_PROJECT_KEY:
            raise RuntimeError("killed after admission")

    harness.lattice.after_admission = kill
    with pytest.raises(QualificationInterrupted):
        harness.consumers()
    harness.lattice.after_admission = None
    report_path = Path(ledger.record(consumer_unit_id(FIELD_SURVEY_ID))["request"]["report_path"])
    foreign = canonical_bytes({"contract_version": "SomethingElse/1.0", "operation_key": "not-ours"})
    report_path.write_bytes(foreign)
    calls = len(processes.calls)

    for retry in (False, True):
        with pytest.raises(QualificationFailure, match="occupied by content this ledger cannot adopt"):
            harness.consumers(retry=retry)
    assert report_path.read_bytes() == foreign
    assert len(processes.calls) == calls and harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1
    assert ledger.status()["active"] == [consumer_unit_id(FIELD_SURVEY_ID)]

    report_path.rename(report_path.with_suffix(".foreign"))
    receipts = harness.consumers()

    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1
    assert receipts[1]["invocation_id"] == harness.lattice.admitted(FIELD_SURVEY_PROJECT_KEY, consumer_operation_key(RUNTIME_ID, FIELD_SURVEY_ID, 1)).invocation_id


@pytest.mark.regression
def test_a_report_naming_a_foreign_key_is_refused_and_retry_cannot_repurchase_the_succeeded_key(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()
    processes.rewrite_report[FIELD_SURVEY_ID] = lambda report: {**report, "operation_key": "execution-canary:20261020T000000:deadbeefcafe"}

    with pytest.raises(QualificationFailure, match="not the key this ledger supplied"):
        harness.consumers()
    assert ledger.status()["failed"] == [consumer_unit_id(FIELD_SURVEY_ID)]
    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1

    processes.rewrite_report.clear()
    for retry in (False, True):
        with pytest.raises(QualificationFailure, match="already holds a succeeded execution"):
            harness.consumers(retry=retry)
    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1
    assert ledger.record(consumer_unit_id(FIELD_SURVEY_ID))["attempt"] == 1


@pytest.mark.regression
def test_an_ambiguous_lattice_answer_refuses_visibly_and_never_mints_a_purchasing_attempt(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()

    def kill(request: dict[str, Any]) -> None:
        if request["external_project_key"] == OCTOPUS_PROJECT_KEY:
            raise RuntimeError("killed after admission")

    harness.lattice.after_admission = kill
    harness.lattice.result_errors[OCTOPUS_PROJECT_KEY] = LatticeClientError("Lattice returned HTTP 503: service unavailable")
    with pytest.raises(QualificationFailure, match="could not say whether"):
        harness.consumers()
    harness.lattice.after_admission = None
    assert harness.lattice.count(OCTOPUS_PROJECT_KEY) == 1
    assert ledger.status()["failed"] == [consumer_unit_id(OCTOPUS_ID)]

    with pytest.raises(QualificationFailure, match="could not say whether"):
        harness.consumers(retry=True)
    harness.lattice.result_errors.clear()
    with pytest.raises(QualificationFailure, match="already holds a succeeded execution"):
        harness.consumers(retry=True)

    assert harness.lattice.count(OCTOPUS_PROJECT_KEY) == 1
    assert ledger.record(consumer_unit_id(OCTOPUS_ID))["attempt"] == 1


@pytest.mark.regression
def test_retained_consumer_evidence_is_re_resolved_against_report_bytes_and_lattice(harness: Harness, processes: FakeConsumerProcesses) -> None:
    receipts = harness.consumers()
    report_path = Path(receipts[2]["report_path"])
    genuine = report_path.read_bytes()
    tampered = strict_json_loads(genuine)
    tampered["output_sha256"] = "0" * 64
    report_path.write_bytes(canonical_bytes(tampered))

    with pytest.raises(QualificationFailure, match="disagrees with the Lattice execution it names"):
        harness.consumers()
    report_path.write_bytes(genuine)

    with pytest.raises(QualificationFailure, match="does not resolve in Lattice"):
        harness.consumers(client=FakeClient(FakeLattice()))

    assert harness.consumers() == receipts
    assert len(harness.lattice.dispatches) == 3


@pytest.mark.regression
def test_bound_source_drift_refuses_before_any_dispatch(harness: Harness, processes: FakeConsumerProcesses) -> None:
    harness.consumers()
    calls = len(processes.calls)
    (harness.axisvi_root / CONSUMERS[FIELD_SURVEY_ID]["entrypoint"]).write_text("# a different entry point\n", encoding="utf-8")

    with pytest.raises(QualificationFailure, match="request bytes drifted from the admitted attempt 1: consumer:field-survey.research-pipeline"):
        harness.consumers()

    assert len(processes.calls) == calls and len(harness.lattice.dispatches) == 3


@pytest.mark.contract
def test_content_bound_canon_death_is_failed_and_retry_replays_its_packet_key_without_purchase(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()

    def kill(request: dict[str, Any]) -> None:
        if request["external_project_key"] == CANON_PROJECT_KEY:
            raise RuntimeError("killed after admission")

    harness.lattice.after_admission = kill
    with pytest.raises(QualificationFailure, match="consumer canary failed"):
        harness.consumers()
    assert ledger.status()["failed"] == [consumer_unit_id(CANON_ID)]
    assert harness.lattice.count(CANON_PROJECT_KEY) == 1
    admitted = harness.lattice.admitted(CANON_PROJECT_KEY, CANON_OPERATION_KEY).invocation_id

    harness.lattice.after_admission = None
    receipts = harness.consumers(retry=True)

    assert harness.lattice.count(CANON_PROJECT_KEY) == 1
    record = ledger.record(consumer_unit_id(CANON_ID))
    assert record["attempt"] == 2 and record["operation_key"] == CANON_OPERATION_KEY
    assert receipts[0]["invocation_id"] == admitted
    assert Path(receipts[0]["report_path"]).parent.name == "canon-a2"


@pytest.mark.property
def test_consumer_operation_keys_are_deterministic_and_bounded_by_the_octopus_limit() -> None:
    for consumer_id in (FIELD_SURVEY_ID, OCTOPUS_ID):
        for attempt in (1, 2, 7):
            value = consumer_operation_key(RUNTIME_ID, consumer_id, attempt)
            assert value == f"qualification:{RUNTIME_ID}:consumer:{consumer_id}:a{attempt}"
            assert len(value) <= qualify_release.CONSUMER_OPERATION_KEY_LIMIT
    with pytest.raises(QualificationFailure, match="not admissible"):
        consumer_operation_key("0.3.1-" + "x" * 300, OCTOPUS_ID, 1)


@pytest.mark.regression
def test_a_missing_bound_source_is_refused_before_a_ledger_request_exists(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()
    (harness.axisvi_root / CONSUMERS[OCTOPUS_ID]["bound_sources"][1]).rename(harness.tmp_path / "moved-aside.py")

    with pytest.raises(QualificationFailure, match="bound source is unreadable: octopus.model-execution"):
        harness.consumers()

    assert ledger.record(consumer_unit_id(OCTOPUS_ID)) is None
    assert harness.lattice.count(OCTOPUS_PROJECT_KEY) == 0


@pytest.mark.regression
def test_a_key_reserved_but_never_sealed_stays_active_and_replay_seals_it_without_a_second_purchase(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()
    octopus_key = consumer_operation_key(RUNTIME_ID, OCTOPUS_ID, 1)
    unsealed = LatticeClientError('Lattice returned HTTP 409: {"detail": "external execution was reserved but has no sealed invocation"}')

    def kill_between_reserve_and_seal(request: dict[str, Any]) -> None:
        if request["external_project_key"] == OCTOPUS_PROJECT_KEY:
            harness.lattice.result_errors[OCTOPUS_PROJECT_KEY] = unsealed
            raise RuntimeError("consumer process killed while Lattice held only the reservation")

    harness.lattice.after_admission = kill_between_reserve_and_seal
    with pytest.raises(QualificationInterrupted, match=r"lattice state: reserved; attempt stays active"):
        harness.consumers()
    assert ledger.status()["active"] == [consumer_unit_id(OCTOPUS_ID)] and ledger.status()["failed"] == []
    assert harness.lattice.count(OCTOPUS_PROJECT_KEY) == 1
    assert qualify_release._lattice_operation_state(harness.client(), OCTOPUS_PROJECT_KEY, octopus_key) == "reserved"

    # Resubmitting identical bytes seals the reservation (operations.seal_execution_submission);
    # even with --retry the active attempt continues under its own key and nothing new is minted.
    harness.lattice.after_admission = None
    harness.lattice.result_errors.clear()
    receipts = harness.consumers(client=FakeClient(harness.lattice), retry=True)

    assert harness.lattice.count(OCTOPUS_PROJECT_KEY) == 1
    assert [call["operation_key"] for call in processes.octopus_calls] == [octopus_key, octopus_key]
    assert receipts[2]["invocation_id"] == harness.lattice.admitted(OCTOPUS_PROJECT_KEY, octopus_key).invocation_id
    assert ledger.record(consumer_unit_id(OCTOPUS_ID))["attempt"] == 1


@pytest.mark.regression
def test_foreign_content_at_a_report_path_before_first_dispatch_keeps_the_attempt_active_instead_of_failing_it(harness: Harness, processes: FakeConsumerProcesses) -> None:
    ledger = harness.ledger()
    request = qualify_release._consumer_request(FIELD_SURVEY_ID, attempt=1, runtime_id=RUNTIME_ID, axisvi_root=harness.axisvi_root, root=(harness.data_root / "acceptance" / "qualification" / f"consumers-{RUNTIME_ID}-{sha256_json(ledger.header)[:16]}").resolve(), installed_lattice=harness.installed_lattice, timeout=1)
    report_path = Path(request["report_path"])
    report_path.parent.mkdir(parents=True)
    foreign = canonical_bytes({"contract_version": "SomethingElse/1.0", "operation_key": "not-ours"})
    report_path.write_bytes(foreign)

    with pytest.raises(QualificationInterrupted, match="occupied by content this ledger cannot adopt"):
        harness.consumers()
    status = ledger.status()
    assert status["complete"] == [consumer_unit_id(CANON_ID)] and status["active"] == [consumer_unit_id(FIELD_SURVEY_ID)] and status["failed"] == []
    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 0 and Path(ledger.record(consumer_unit_id(FIELD_SURVEY_ID))["request"]["report_path"]) == report_path
    with pytest.raises(QualificationFailure, match="Move that content aside"):
        harness.consumers(retry=True)
    assert report_path.read_bytes() == foreign and harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 0

    report_path.rename(report_path.with_suffix(".foreign"))
    receipts = harness.consumers()

    assert harness.lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1
    record = ledger.record(consumer_unit_id(FIELD_SURVEY_ID))
    assert record["state"] == "complete" and record["attempt"] == 1 and receipts[1]["invocation_id"] == record["invocation_id"]


@pytest.mark.contract
def test_a_ledger_rebuilt_from_nothing_on_the_same_binding_replays_providers_and_adopts_consumer_reports_without_purchase(harness: Harness, processes: FakeConsumerProcesses) -> None:
    providers = harness.providers()
    consumers = harness.consumers()
    assert len(harness.lattice.dispatches) == 19
    dispatched_processes = len(processes.calls)

    # The operator lost the ledger directory (or chose a new --output without --ledger). The
    # new ledger has identical bindings, so Lattice replays every provider request and the
    # consumer reports already name these exact keys and resolve exactly.
    rebuilt = QualificationLedger.open(harness.tmp_path / "rebuilt.ledger", runtime=harness.runtime, runtime_manifest_sha256=harness.runtime_manifest_sha256, data_root=harness.data_root)
    assert rebuilt.header == harness.ledger().header and rebuilt.status()["remaining"] == expected_unit_ids()

    client = FakeClient(harness.lattice)
    assert qualify_release.run_provider_canaries(client, harness.data_root, ledger=rebuilt, timeout=1) == providers
    assert qualify_release.run_consumer_canaries(client, harness.axisvi_root, harness.data_root, harness.installed_lattice, ledger=rebuilt, timeout=1) == consumers

    assert len(harness.lattice.dispatches) == 19 and len(processes.calls) == dispatched_processes
    status = rebuilt.status()
    assert status["complete"] == PROVIDER_UNITS + [consumer_unit_id(consumer_id) for consumer_id in CONSUMERS] and status["failed"] == [] and status["active"] == []
    for consumer_id in CONSUMERS:
        assert rebuilt.record(consumer_unit_id(consumer_id))["receipt"] == harness.ledger().record(consumer_unit_id(consumer_id))["receipt"]

    # A tampered report is still refused on adoption; adoption is the strict evidence path, not trust.
    report_path = Path(consumers[1]["report_path"])
    genuine = report_path.read_bytes()
    report_path.write_bytes(canonical_bytes({**strict_json_loads(genuine), "invocation_id": "invocation_forged"}))
    third = QualificationLedger.open(harness.tmp_path / "third.ledger", runtime=harness.runtime, runtime_manifest_sha256=harness.runtime_manifest_sha256, data_root=harness.data_root)
    with pytest.raises(QualificationFailure, match="disagrees with the Lattice execution it names"):
        qualify_release.run_consumer_canaries(client, harness.axisvi_root, harness.data_root, harness.installed_lattice, ledger=third, timeout=1)
    report_path.write_bytes(genuine)
    assert len(harness.lattice.dispatches) == 19 and len(processes.calls) == dispatched_processes


# --- owned entry points directly ---------------------------------------------------


@pytest.mark.contract
def test_field_survey_supplied_key_replays_identical_bytes_and_a_different_key_purchases(owned: OwnedConsumers, tmp_path: Path) -> None:
    lattice = FakeLattice()
    calls = _bind_field_survey(owned, lattice)
    key = consumer_operation_key(RUNTIME_ID, FIELD_SURVEY_ID, 1)

    first = owned.field_survey.run(tmp_path / "first.json", lattice_bin="lattice", timeout=1, operation_key=key)
    second = owned.field_survey.run(tmp_path / "second.json", lattice_bin="lattice", timeout=1, operation_key=key)

    assert len(calls) == 2 and calls[0] == calls[1] and calls[0]["operation_key"] == key
    assert lattice.count(FIELD_SURVEY_PROJECT_KEY) == 1
    assert first["operation_key"] == second["operation_key"] == key and first["invocation_id"] == second["invocation_id"]
    report = strict_json_loads((tmp_path / "first.json").read_bytes())
    assert report["contract_version"] == CONSUMERS[FIELD_SURVEY_ID]["report_contract"] and report["external_project_key"] == FIELD_SURVEY_PROJECT_KEY
    assert report["operation_key"] == key and report["retained_model_text_bytes"] == 0
    admitted = lattice.admitted(FIELD_SURVEY_PROJECT_KEY, key)
    assert report["runtime_receipt_sha256"] == sha256_bytes(canonical_bytes(admitted.journal_receipt))
    assert report["provider_profile_id"] == admitted.provider_attestation["profile_id"]

    owned.field_survey.run(tmp_path / "third.json", lattice_bin="lattice", timeout=1, operation_key=key + "x")

    assert lattice.count(FIELD_SURVEY_PROJECT_KEY) == 2


@pytest.mark.regression
def test_field_survey_without_the_option_keeps_minting_fresh_standalone_keys(owned: OwnedConsumers, tmp_path: Path) -> None:
    lattice = FakeLattice()
    calls = _bind_field_survey(owned, lattice)

    first = owned.field_survey.run(tmp_path / "first.json", lattice_bin="lattice", timeout=1)
    second = owned.field_survey.run(tmp_path / "second.json", lattice_bin="lattice", timeout=1)

    assert first["operation_key"] != second["operation_key"]
    assert all(re.fullmatch(r"execution-canary:\d{8}T\d{6}:[0-9a-f]{12}", item["operation_key"]) for item in (first, second))
    assert [call["operation_key"] for call in calls] == [first["operation_key"], second["operation_key"]]
    assert lattice.count(FIELD_SURVEY_PROJECT_KEY) == 2


@pytest.mark.fuzz
@pytest.mark.parametrize("bad", ["", " key", "key ", "\tkey\n", "  "])
def test_field_survey_refuses_an_inadmissible_key_before_invoking_lattice(owned: OwnedConsumers, tmp_path: Path, bad: str) -> None:
    lattice = FakeLattice()
    calls = _bind_field_survey(owned, lattice)

    with pytest.raises(ValueError, match="non-empty string without surrounding whitespace"):
        owned.field_survey.run(tmp_path / "never.json", lattice_bin="lattice", timeout=1, operation_key=bad)

    assert calls == [] and lattice.dispatches == [] and not (tmp_path / "never.json").exists()


@pytest.mark.contract
def test_octopus_supplied_key_binds_the_sentinel_nonce_so_identical_keys_replay(owned: OwnedConsumers, tmp_path: Path) -> None:
    lattice = FakeLattice()
    calls = _bind_octopus(owned, lattice)
    key = consumer_operation_key(RUNTIME_ID, OCTOPUS_ID, 1)

    first = owned.octopus.run_canary(tmp_path / "first.json", lattice_bin="lattice", timeout=1, operation_key=key)
    second = owned.octopus.run_canary(tmp_path / "second.json", lattice_bin="lattice", timeout=1, operation_key=key)

    assert len(calls) == 2 and calls[0]["operation_key"] == calls[1]["operation_key"] == key
    assert calls[0]["prompt"] == calls[1]["prompt"] and calls[0]["route"] == calls[1]["route"] and calls[0]["output_contract"] == calls[1]["output_contract"]
    assert lattice.count(OCTOPUS_PROJECT_KEY) == 1
    nonce = owned.octopus.sentinel_nonce(key)
    assert re.fullmatch(r"[0-9a-f]{32}", nonce)
    assert first["sentinel_sha256"] == second["sentinel_sha256"] == sha256_bytes(f"OCTOPUS-LATTICE-CANARY-{nonce}".encode("utf-8"))
    assert first["invocation_id"] == second["invocation_id"] and first["operation_key"] == key
    report = strict_json_loads((tmp_path / "first.json").read_bytes())
    admitted = lattice.admitted(OCTOPUS_PROJECT_KEY, key)
    assert report["contract_version"] == CONSUMERS[OCTOPUS_ID]["report_contract"] and report["consumer_id"] == OCTOPUS_ID
    assert report["journal_receipt_sha256"] == sha256_bytes(canonical_bytes(admitted.journal_receipt))
    assert report["provider_attestation_sha256"] == sha256_bytes(canonical_bytes(admitted.provider_attestation))

    assert owned.octopus.sentinel_nonce(key + "x") != nonce
    owned.octopus.run_canary(tmp_path / "third.json", lattice_bin="lattice", timeout=1, operation_key=key + "x")
    assert lattice.count(OCTOPUS_PROJECT_KEY) == 2 and calls[2]["prompt"] != calls[0]["prompt"]


@pytest.mark.regression
def test_octopus_without_the_option_keeps_random_sentinels_and_timestamped_keys(owned: OwnedConsumers, tmp_path: Path) -> None:
    lattice = FakeLattice()
    calls = _bind_octopus(owned, lattice)

    first = owned.octopus.run_canary(tmp_path / "first.json", lattice_bin="lattice", timeout=1)
    second = owned.octopus.run_canary(tmp_path / "second.json", lattice_bin="lattice", timeout=1)

    assert first["operation_key"] != second["operation_key"]
    assert all(re.fullmatch(r"canary:\d{8}T\d{6}:[0-9a-f]{32}", item["operation_key"]) for item in (first, second))
    assert calls[0]["prompt"] != calls[1]["prompt"] and first["sentinel_sha256"] != second["sentinel_sha256"]
    assert lattice.count(OCTOPUS_PROJECT_KEY) == 2


@pytest.mark.fuzz
@pytest.mark.parametrize("bad", ["", " key", "key ", "k" * 301])
def test_octopus_refuses_an_inadmissible_key_before_executing(owned: OwnedConsumers, tmp_path: Path, bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
    lattice = FakeLattice()
    calls = _bind_octopus(owned, lattice)

    with pytest.raises(ValueError, match="--operation-key must"):
        owned.octopus.run_canary(tmp_path / "never.json", lattice_bin="lattice", timeout=1, operation_key=bad)
    monkeypatch.setattr(sys, "argv", ["lattice_execution_canary.py", "--output", str(tmp_path / "never.json"), "--operation-key", bad])
    with pytest.raises(SystemExit) as exit_info:
        owned.octopus.main()

    assert exit_info.value.code == 2
    assert calls == [] and lattice.dispatches == [] and not (tmp_path / "never.json").exists()
    assert owned.octopus.validate_operation_key("k" * 300) == "k" * 300


@pytest.mark.oracle
def test_reports_from_the_owned_entry_points_satisfy_the_qualifier_s_strict_bindings(owned: OwnedConsumers, tmp_path: Path) -> None:
    lattice = FakeLattice()
    _bind_field_survey(owned, lattice)
    _bind_octopus(owned, lattice)
    client = FakeClient(lattice)
    field_key = consumer_operation_key(RUNTIME_ID, FIELD_SURVEY_ID, 1)
    octopus_key = consumer_operation_key(RUNTIME_ID, OCTOPUS_ID, 1)
    owned.field_survey.run(tmp_path / "field.json", lattice_bin="lattice", timeout=1, operation_key=field_key)
    owned.octopus.run_canary(tmp_path / "octopus.json", lattice_bin="lattice", timeout=1, operation_key=octopus_key)

    field_receipt, field_operation = qualify_release._consumer_evidence(client, FIELD_SURVEY_ID, tmp_path / "field.json", expected_operation_key=field_key)
    octopus_receipt, octopus_operation = qualify_release._consumer_evidence(client, OCTOPUS_ID, tmp_path / "octopus.json", expected_operation_key=octopus_key)

    assert field_operation == field_key and octopus_operation == octopus_key
    assert field_receipt["invocation_id"] == lattice.admitted(FIELD_SURVEY_PROJECT_KEY, field_key).invocation_id
    assert octopus_receipt["invocation_id"] == lattice.admitted(OCTOPUS_PROJECT_KEY, octopus_key).invocation_id
    with pytest.raises(QualificationFailure, match="not the key this ledger supplied"):
        qualify_release._consumer_evidence(client, FIELD_SURVEY_ID, tmp_path / "field.json", expected_operation_key=consumer_operation_key(RUNTIME_ID, FIELD_SURVEY_ID, 2))
    with pytest.raises(QualificationFailure, match="wrong contract or project key"):
        qualify_release._consumer_evidence(client, OCTOPUS_ID, tmp_path / "field.json", expected_operation_key=field_key)
    lattice.visible_state[(OCTOPUS_PROJECT_KEY, octopus_key)] = InvocationState.running
    with pytest.raises(QualificationFailure, match="disagrees with the Lattice execution it names"):
        qualify_release._consumer_evidence(client, OCTOPUS_ID, tmp_path / "octopus.json", expected_operation_key=octopus_key)


# --- receipt construction (retained from the baseline suite) ----------------------


def _inputs(tmp_path: Path) -> dict:
    runtime = _runtime()
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_bytes(canonical_bytes(runtime))
    consumers = []
    for identity in ("canon.quaternary-exhaustion", "field-survey.research-pipeline", "octopus.model-execution"):
        root = tmp_path / identity
        root.mkdir()
        (root / "client.py").write_text("BOUNDARY = True\n", encoding="utf-8")
        consumers.append({"consumer_id": identity, "source_root": identity, "client_boundary": f"{identity}/client.py", "forbidden_direct_authorities": ["provider-cli", "provider-sdk", "developer-api"]})
    registry = {
        "generated_object": generated_object(kind="lattice-execution-consumer-registry", schema_name="lattice-execution-consumer-registry", subject="lattice:execution-consumers", sources=_source("fixture:registry")),
        "contract_version": "ExecutionConsumerRegistry/1.0",
        "consumers": consumers,
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(canonical_bytes(registry))
    suites = [{"suite_id": identity, "truth_kinds": ["contract"], "command": [["fixture"]], "cwd": str(tmp_path), "exit_code": 0, "stdout_sha256": "1" * 64, "stderr_sha256": "2" * 64} for identity in qualify_release.SUITES]
    providers = [{"provider": provider, "profile": profile, "state": "succeeded", "invocation_id": f"invocation_{index}", "manifest_sha256": "3" * 64, "output_sha256": "4" * 64, "provider_profile_id": "provider-profile:fixture", "runtime_receipt_sha256": "5" * 64} for index, (provider, profile) in enumerate(sorted(qualify_release.EXPECTED_CANARY_PROFILES))]
    consumer_canaries = [{"consumer_id": item["consumer_id"], "external_project_key": f"{item['consumer_id']}.canary", "invocation_id": f"invocation_{item['consumer_id']}", "manifest_sha256": "6" * 64, "report_path": str(tmp_path / f"{item['consumer_id']}.json"), "report_sha256": "7" * 64} for item in consumers]
    for item in consumer_canaries:
        Path(item["report_path"]).write_bytes(b"consumer-report")
        item["report_sha256"] = hashlib.sha256(b"consumer-report").hexdigest()
    interruption_source = tmp_path / "interruption.json"
    interruption_source.write_bytes(b"interruption")
    canary = tmp_path / "credential-canary"
    canary.write_bytes(b"LATTICE-SYNTHETIC-CREDENTIAL-CANARY")
    return {"runtime_manifest_path": runtime_path, "consumer_registry_path": registry_path, "axisvi_root": tmp_path, "suites": suites, "provider_canaries": providers, "consumer_canaries": consumer_canaries, "interruption": {"contract_version": "LatticeInterruptionReceipt/2.0", "runtime_execution_mode": "installed-bytes-with-synthetic-provider", "pre_restart_state": "running", "post_restart_state": "orphaned", "parent_invocation_id": "invocation_parent", "continuation_invocation_id": "invocation_continuation"}, "interruption_source": interruption_source, "credential_canary_files": [canary]}


@pytest.mark.contract
def test_qualification_receipt_is_snapshot_and_exactly_source_bound(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    output = tmp_path / "qualification.json"

    value = qualify_release.write_receipt(output, **inputs)

    assert value["generated_object"]["role"] == "snapshot"
    assert value["contract_version"] == "LatticeQualificationReceipt/1.0"
    assert canonical_bytes(json.loads(output.read_bytes())) == output.read_bytes()
    assert set(value["consumer_source_inventories"]) == {"canon.quaternary-exhaustion", "field-survey.research-pipeline", "octopus.model-execution"}


@pytest.mark.regression
def test_negative_control_qualification_refuses_a_missing_provider_profile(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs["provider_canaries"].pop()

    with pytest.raises(QualificationFailure, match="provider-profile"):
        qualify_release.write_receipt(tmp_path / "must-not-exist.json", **inputs)


@pytest.mark.regression
def test_negative_control_qualification_refuses_a_missing_consumer_canary(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs["consumer_canaries"].pop()

    with pytest.raises(QualificationFailure, match="one exact canary"):
        qualify_release.write_receipt(tmp_path / "must-not-exist.json", **inputs)


@pytest.mark.regression
def test_source_inventory_uses_enclosing_git_and_ignores_cache(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("consumer/.pytest_cache/\n", encoding="utf-8")
    root = tmp_path / "consumer"
    root.mkdir()
    live = root / "client.py"
    live.write_text("BOUNDARY = True\n", encoding="utf-8")
    ignored = root / ".pytest_cache" / "bad.py"
    ignored.parent.mkdir()
    ignored.write_text('subprocess.run(["claude"])\n', encoding="utf-8")

    assert qualify_release._source_inventory(root) == [
        {"path": "client.py", "sha256": qualify_release._sha256_file(live), "byte_count": live.stat().st_size}
    ]


@pytest.mark.contract
def test_antigravity_canary_uses_the_exact_collision_regression_schema() -> None:
    request = qualify_release._submission("google", "compiled-fresh", operation_key="qualification:fixture:google:compiled-fresh:a1", route_profile="compiled-fresh", marker="MARKER")

    schema = request["output_contract"]["json_schema"]
    assert schema["required"] == ["response", "toolSummary", "toolAction"]
    assert set(schema["properties"]) == {"response", "toolSummary", "toolAction"}
    assert request["idempotency_key"] == request["operation_key"]


@pytest.mark.contract
def test_credential_canary_digest_ignores_a_trailing_newline(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    canary = tmp_path / "credential-canary"
    canary.write_bytes(b"LATTICE-SYNTHETIC-CREDENTIAL-CANARY\n")
    inputs["credential_canary_files"] = [canary]
    output = tmp_path / "qualification-canary.json"

    value = qualify_release.write_receipt(output, **inputs)

    assert value["credential_canary_sha256s"] == [sha256_bytes(b"LATTICE-SYNTHETIC-CREDENTIAL-CANARY")]


@pytest.mark.regression
def test_portable_and_workspace_ancestry_does_not_claim_native_session_state() -> None:
    for profile in ("compiled-fresh", "workspace-agent"):
        request = qualify_release._submission("openai", profile, operation_key=f"qualification:fixture:openai:{profile}:a1", route_profile=profile, marker="MARKER", parent_id="invocation_parent")

        assert request["parent_invocation_ids"] == ["invocation_parent"]
        assert request["route"]["native_parent_invocation_id"] is None
        assert request["route"]["continuation_mode"] is None
