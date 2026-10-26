#!/usr/bin/env python3
"""Qualify installed Lattice providers, consumers, and fixed source suites.

Every expensive unit is checkpointed in a qualification ledger bound to the
exact installed runtime, this file's bytes, the fixed configuration, the data
root, and the exact request bytes. A rerun after interruption reuses each
completed unit through Lattice's external-operation replay boundary, continues
an admitted pending request with identical bytes, and refuses to mint a new
attempt for a failed unit unless the operator passes --retry. Consumer canaries
are continued either through the exact report their entry point wrote, resolved
against the Lattice execution it names, or by rerunning the identical command
under an operation key this ledger supplied, which the consumer's own Lattice
client replays instead of purchasing (see CONSUMERS). Before --retry mints a new
consumer attempt, Lattice is asked whether the previous key already succeeded;
if it did, or if the answer is ambiguous, the retry is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, sha256_json, strict_json_loads
from lattice.client import LatticeClient, LatticeClientError


SUITES: dict[str, tuple[str, list[list[str]], list[str]]] = {
    "lattice-backend": ("axis/chariots/lattice", [["{lattice-python}", "-m", "pytest", "-q"]], ["oracle", "property", "regression", "contract", "smoke", "fuzz", "concurrency"]),
    "lattice-frontend": ("axis/chariots/lattice/web", [["npm", "run", "check"], ["npm", "test", "--", "--run"], ["npm", "run", "build"]], ["contract", "regression"]),
    "lattice-browser": ("axis/chariots/lattice/web", [["npm", "run", "test:browser"]], ["browser-acceptance", "regression"]),
    "canon": ("sui/mission/projects/canon", [["uv", "run", "pytest", "-q", "tests/discovery/reference_cascade/test_quaternary_workflow.py"]], ["oracle", "contract", "regression"]),
    "field-survey": ("sui/mission/typology/abduction/field-survey/research-pipeline", [["uv", "run", "pytest", "-q"]], ["oracle", "property", "contract", "regression", "concurrency"]),
    "octopus": ("axis/soma/octopus", [["uv", "run", "pytest", "-q"], ["npm", "run", "build", "--prefix", "packages/web"]], ["oracle", "property", "contract", "regression", "concurrency"]),
    "slate-mirror": ("axis/tools/slate-mirror", [["node", "--test", "server.test.js"]], ["contract", "regression"]),
}

PROVIDERS = {
    "anthropic": {"model": "opus", "harness": "claude", "effort": "high"},
    "openai": {"model": "gpt-5.6-sol", "harness": "codex", "effort": "high"},
    "google": {"model": "gemini-3.8-flash-high", "harness": "antigravity", "effort": "high"},
    "xai": {"model": "grok-4.5", "harness": "grok", "effort": "high"},
    "meta": {"model": "muse-spark-1.3-contributor", "harness": "muse", "effort": "high"},
}

EXPECTED_CANARY_PROFILES = {
    ("anthropic", "compiled-fresh"),
    ("anthropic", "workspace-agent"),
    ("anthropic", "native-continuation:resume"),
    ("anthropic", "native-continuation:fork"),
    ("anthropic", "portable-fallback"),
    ("openai", "compiled-fresh"),
    ("openai", "workspace-agent"),
    ("openai", "native-continuation:resume"),
    ("openai", "native-continuation:fork"),
    ("openai", "portable-fallback"),
    ("google", "compiled-fresh"),
    ("google", "research-fresh"),
    ("xai", "compiled-fresh"),
    ("xai", "research-fresh"),
    ("meta", "compiled-fresh"),
    ("meta", "research-fresh"),
}

EXTERNAL_PROJECT_KEY = "lattice.release-qualification"
# Fixed dispatch order. Subscription exhaustion is cheapest to discover on the
# two-call providers, so Grok runs first and a depleted balance cannot waste the
# ten deeper Claude and Codex continuity canaries.
PROVIDER_ORDER = ("xai", "meta", "google", "anthropic", "openai")
CONTINUITY_PROFILES = ("compiled-fresh", "workspace-agent", "native-continuation:resume", "native-continuation:fork", "portable-fallback")
RESEARCH_PROFILES = ("compiled-fresh", "research-fresh")
# Consumer canaries are owned by the consumer entry points. The qualifier's
# continuation handle is the consumer's durable report, which names the operation
# key and the invocation that Lattice must still resolve exactly; when no report
# exists, continuation depends on how the entry point derives its operation key.
# The continuation policy is part of the bound configuration:
#   content-bound  the entry point derives its operation key from fixed content
#                  (Canon: countersearch-canary:opus5:<packet sha256>, idempotency
#                  key derived from it, fixed prompt). Rerunning the identical
#                  command resubmits identical bytes, which Lattice replays
#                  (store.reserve_external_submission) instead of purchasing.
#   key-bound      the entry point accepts --operation-key and submits it
#                  unchanged (field-survey, Octopus); its Lattice client derives
#                  the idempotency key from it and every other request field is a
#                  pure function of the key and the bound sources, so rerunning
#                  the identical command under the ledger's key replays the
#                  admitted execution. The ledger can also ask Lattice directly
#                  whether that key already succeeded before any new attempt.
# ``bound_sources`` are the exact files whose sha256 is part of every consumer
# request: the entry point plus the Lattice client module it submits through.
CONSUMERS: dict[str, dict[str, Any]] = {
    "canon.quaternary-exhaustion": {
        "external_project_key": "canon.quaternary-exhaustion.canary",
        "report_contract": "QuaternaryLatticeConsumerCanary/2.0",
        "source_root": "sui/mission/projects/canon",
        "entrypoint": "sui/mission/projects/canon/src/canon/discovery/reference_cascade/run_lattice_loop_canary.py",
        "bound_sources": ["sui/mission/projects/canon/src/canon/discovery/reference_cascade/run_lattice_loop_canary.py"],
        "continuation": "content-bound",
    },
    "field-survey.research-pipeline": {
        "external_project_key": "field-survey.research-pipeline",
        "report_contract": "FieldSurveyLatticeConsumerCanary/1.0",
        "source_root": "sui/mission/typology/abduction/field-survey/research-pipeline",
        "entrypoint": "sui/mission/typology/abduction/field-survey/research-pipeline/scripts/run-lattice-consumer-canary.py",
        "bound_sources": [
            "sui/mission/typology/abduction/field-survey/research-pipeline/scripts/run-lattice-consumer-canary.py",
            "sui/mission/typology/abduction/field-survey/research-pipeline/domain_research/lattice_client.py",
        ],
        "continuation": "key-bound",
    },
    "octopus.model-execution": {
        "external_project_key": "octopus.model-execution",
        "report_contract": "OctopusLatticeCanary/1.0",
        "source_root": "axis/soma/octopus",
        "entrypoint": "axis/soma/octopus/scripts/lattice_execution_canary.py",
        "bound_sources": [
            "axis/soma/octopus/scripts/lattice_execution_canary.py",
            "axis/soma/octopus/packages/model_execution/lattice.py",
        ],
        "continuation": "key-bound",
    },
}
# Octopus validates a caller-supplied key against this limit; the qualifier
# refuses to build a longer one for any consumer so the bytes never differ by consumer.
CONSUMER_OPERATION_KEY_LIMIT = 300
CONSUMER_IDS = tuple(CONSUMERS)
LEDGER_CONTRACT = "LatticeQualificationLedger/1.0"
STATUS_CONTRACT = "LatticeQualificationStatus/1.0"
UNIT_REQUEST_CONTRACT = "LatticeQualificationUnitRequest/1.0"
UNIT_RECEIPT_CONTRACT = "LatticeQualificationUnitReceipt/1.0"
UNIT_FAILURE_CONTRACT = "LatticeQualificationUnitFailure/1.0"
# The execution-consolidation verifier recomputes the marker from the profile
# with exactly this mapping; the qualifier must plant the same string.
MARKER_PROFILES = {"workspace-agent": "WORKSPACE", "portable-fallback": "PORTABLE", "native-continuation:resume": "RESUME", "native-continuation:fork": "FORK"}


class QualificationFailure(RuntimeError):
    """Adjudicated failure of a unit attempt; the ledger records it and --retry may mint the next attempt."""


class QualificationInterrupted(RuntimeError):
    """The attempt is not over and must not be adjudicated failed.

    Raised when Lattice holds admitted or succeeded work under the attempt's key that the
    consumer did not finish evidencing, or when the attempt's report location is occupied
    by content this ledger cannot adopt. Deliberately not a QualificationFailure, so
    run_unit leaves the attempt active and the next run continues it by replay (after the
    operator moves foreign content aside) instead of ever minting a purchasing attempt.
    """


def canary_marker(provider: str, profile: str) -> str:
    """Exact semantic marker the verifier expects for one provider profile."""

    return f"QUAL-{provider.upper()}-{MARKER_PROFILES.get(profile, profile.upper())}"


def operation_key(runtime_id: str, provider: str, profile: str, attempt: int) -> str:
    """Deterministic external operation identity for one provider canary attempt.

    Lattice replays an external submission whose project key and operation key
    match with identical request bytes, so the same attempt resubmitted after an
    interruption continues the admitted execution instead of buying a new one.
    The attempt number is part of the identity so a failed attempt is never
    silently replayed as a retry.
    """

    return f"qualification:{runtime_id}:{provider}:{profile}:a{attempt}"


def consumer_operation_key(runtime_id: str, consumer_id: str, attempt: int) -> str:
    """Deterministic operation key the ledger hands to a key-bound consumer entry point.

    The entry point submits it unchanged, so an identical rerun of the same attempt
    replays the admitted execution. The attempt number keeps a failed attempt from
    being replayed as a retry, exactly as for provider canaries.
    """

    value = f"qualification:{runtime_id}:consumer:{consumer_id}:a{attempt}"
    if value != value.strip() or len(value) > CONSUMER_OPERATION_KEY_LIMIT:
        raise QualificationFailure(f"consumer operation key is not admissible by the consumer entry point: {value!r}")
    return value


def provider_unit_id(provider: str, profile: str) -> str:
    return f"provider:{provider}:{profile}"


def suite_unit_id(suite_id: str) -> str:
    return f"suite:{suite_id}"


def consumer_unit_id(consumer_id: str) -> str:
    return f"consumer:{consumer_id}"


def expected_unit_ids() -> list[str]:
    providers = [provider_unit_id(provider, profile) for provider in PROVIDER_ORDER for profile in (RESEARCH_PROFILES if provider in {"xai", "meta", "google"} else CONTINUITY_PROFILES)]
    suites = [suite_unit_id(suite_id) for suite_id in SUITES]
    consumers = [consumer_unit_id(consumer_id) for consumer_id in CONSUMER_IDS]
    return providers + suites + consumers


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_inventory(root: Path) -> list[dict[str, Any]]:
    excluded = {".git", ".venv", "node_modules", "__pycache__", "runtime", "results", "archive", "dist", ".svelte-kit", ".pytest_cache"}
    git_root_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    paths: list[Path] = []
    if git_root_result.returncode == 0:
        git_root = Path(os.fsdecode(git_root_result.stdout).strip()).resolve(strict=True)
        relative_root = root.resolve(strict=True).relative_to(git_root)
        listed = subprocess.run(
            ["git", "-C", str(git_root), "ls-files", "-co", "--exclude-standard", "-z", "--", relative_root.as_posix()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if listed.returncode != 0:
            detail = listed.stderr.decode("utf-8", errors="replace")[-2000:]
            raise QualificationFailure(f"source inventory failed: {detail}")
        for raw in listed.stdout.split(b"\0"):
            if not raw:
                continue
            candidate = (git_root / Path(os.fsdecode(raw))).resolve()
            if candidate.is_relative_to(root.resolve()) and candidate.is_file() and not candidate.is_symlink():
                relative = candidate.relative_to(root.resolve())
                if not any(part in excluded for part in relative.parts):
                    paths.append(candidate)
    else:
        for current_root, directories, files in os.walk(root):
            current = Path(current_root)
            directories[:] = sorted(item for item in directories if item not in excluded)
            relative_current = current.relative_to(root)
            if any(part in excluded for part in relative_current.parts):
                continue
            paths.extend(current / name for name in sorted(files))
    result = []
    for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in excluded for part in relative.parts) or path.is_symlink() or not path.is_file():
            continue
        result.append({"path": relative.as_posix(), "sha256": _sha256_file(path), "byte_count": path.stat().st_size})
    if not result:
        raise QualificationFailure(f"source inventory is empty: {root}")
    return result


def _suite_commands(axisvi_root: Path) -> dict[str, tuple[Path, list[list[str]], list[str]]]:
    lattice_python = axisvi_root / "axis/chariots/lattice/.venv/bin/python"
    result = {}
    for suite_id, (relative_cwd, commands, truth_kinds) in SUITES.items():
        expanded = [[str(lattice_python) if item == "{lattice-python}" else item for item in command] for command in commands]
        result[suite_id] = (axisvi_root / relative_cwd, expanded, truth_kinds)
    return result


# --- qualification ledger -----------------------------------------------------


def _exact_write(path: Path, value: dict[str, Any]) -> None:
    """Write one canonical JSON record exactly once, atomically, and durably.

    Publication is a hard link from a private pending file, which the filesystem refuses
    atomically when the record already exists; a check-then-rename would let two operators
    on the same ledger silently overwrite each other. A concurrent writer that published
    byte-identical content is accepted as the same record; different bytes refuse. Only the
    writer's own pending file is ever removed.
    """

    raw = canonical_bytes(value)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.pending")
    with temporary.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise QualificationFailure(f"ledger record already exists with different bytes: {path}") from None
    finally:
        os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _exact_read(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        value = strict_json_loads(raw)
    except (ValueError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"ledger record is not exact canonical JSON: {path}") from error
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        raise QualificationFailure(f"ledger record is not exact canonical JSON: {path}")
    return value


def _code_sha256() -> str:
    return _sha256_file(Path(__file__).resolve())


def _configuration_sha256() -> str:
    configuration = {
        "external_project_key": EXTERNAL_PROJECT_KEY,
        "providers": PROVIDERS,
        "provider_order": list(PROVIDER_ORDER),
        "profiles": sorted(list(item) for item in EXPECTED_CANARY_PROFILES),
        "marker_profiles": MARKER_PROFILES,
        "suites": {suite_id: [value[0], value[1], value[2]] for suite_id, value in SUITES.items()},
        "consumers": CONSUMERS,
        "consumer_operation_key_limit": CONSUMER_OPERATION_KEY_LIMIT,
        "unit_ids": expected_unit_ids(),
    }
    return sha256_json(configuration)


class QualificationLedger:
    """Durable per-unit checkpoints bound to one runtime, code, configuration, and data root.

    Layout: ``ledger.json`` (the binding header) and ``units/<unit>.a<n>.{request,receipt,failure}.json``.
    Records are append-only; nothing here ever rewrites or removes a record.
    """

    def __init__(self, root: Path, header: dict[str, Any]) -> None:
        self.root = root
        self.header = header

    @property
    def runtime_id(self) -> str:
        return str(self.header["runtime_id"])

    @property
    def units(self) -> Path:
        return self.root / "units"

    @staticmethod
    def expected_header(*, runtime: dict[str, Any], runtime_manifest_sha256: str, data_root: Path) -> dict[str, Any]:
        return {
            "contract_version": LEDGER_CONTRACT,
            "external_project_key": EXTERNAL_PROJECT_KEY,
            "runtime_id": runtime["runtime_id"],
            "runtime_artifact_id": runtime["generated_object"]["artifact_id"],
            "runtime_manifest_sha256": runtime_manifest_sha256,
            "data_root": str(data_root.resolve()),
            "code_sha256": _code_sha256(),
            "configuration_sha256": _configuration_sha256(),
        }

    @classmethod
    def open(cls, root: Path, *, runtime: dict[str, Any], runtime_manifest_sha256: str, data_root: Path) -> "QualificationLedger":
        expected = cls.expected_header(runtime=runtime, runtime_manifest_sha256=runtime_manifest_sha256, data_root=data_root)
        header_path = root / "ledger.json"
        if header_path.exists():
            existing = _exact_read(header_path)
            if existing != expected:
                drifted = sorted(key for key in set(existing) | set(expected) if existing.get(key) != expected.get(key))
                raise QualificationFailure(f"qualification ledger is bound to a different runtime, code, configuration, or data root: {drifted}")
        else:
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            _exact_write(header_path, expected)
        (root / "units").mkdir(exist_ok=True)
        return cls(root, expected)

    @classmethod
    def inspect(cls, root: Path) -> "QualificationLedger":
        """Open an existing ledger for status only, without rebinding it."""

        header_path = root / "ledger.json"
        if not header_path.is_file():
            raise QualificationFailure(f"qualification ledger does not exist: {root}")
        header = _exact_read(header_path)
        if header.get("contract_version") != LEDGER_CONTRACT:
            raise QualificationFailure(f"qualification ledger header is malformed: {header_path}")
        (root / "units").mkdir(exist_ok=True)
        return cls(root, header)

    @staticmethod
    def _slug(unit_id: str) -> str:
        return unit_id.replace(":", "__")

    def _path(self, unit_id: str, attempt: int, kind: str) -> Path:
        return self.units / f"{self._slug(unit_id)}.a{attempt}.{kind}.json"

    def record(self, unit_id: str) -> dict[str, Any] | None:
        """Latest attempt of one unit, or None when it was never dispatched."""

        pattern = re.compile(re.escape(self._slug(unit_id)) + r"\.a([0-9]+)\.request\.json\Z")
        attempts = sorted(int(match.group(1)) for match in (pattern.fullmatch(path.name) for path in self.units.iterdir()) if match)
        if not attempts:
            return None
        attempt = attempts[-1]
        request = _exact_read(self._path(unit_id, attempt, "request"))
        if request.get("contract_version") != UNIT_REQUEST_CONTRACT or request.get("unit_id") != unit_id or request.get("attempt") != attempt or sha256_json(request.get("request")) != request.get("request_sha256"):
            raise QualificationFailure(f"ledger request record is inconsistent: {unit_id} attempt {attempt}")
        value: dict[str, Any] = {
            "unit_id": unit_id,
            "kind": request.get("kind"),
            "attempt": attempt,
            "state": "active",
            "request": request["request"],
            "request_sha256": request["request_sha256"],
            "receipt": None,
            "operation_key": None,
            "invocation_id": None,
            "failure": None,
        }
        receipt_path = self._path(unit_id, attempt, "receipt")
        failure_path = self._path(unit_id, attempt, "failure")
        if receipt_path.exists():
            stored = _exact_read(receipt_path)
            if stored.get("contract_version") != UNIT_RECEIPT_CONTRACT or stored.get("unit_id") != unit_id or stored.get("attempt") != attempt or stored.get("request_sha256") != request["request_sha256"] or not isinstance(stored.get("receipt"), dict):
                raise QualificationFailure(f"ledger receipt record is inconsistent: {unit_id} attempt {attempt}")
            value.update(state="complete", receipt=stored["receipt"], operation_key=stored.get("operation_key"), invocation_id=stored.get("invocation_id"))
        elif failure_path.exists():
            stored = _exact_read(failure_path)
            if stored.get("contract_version") != UNIT_FAILURE_CONTRACT or stored.get("unit_id") != unit_id or stored.get("attempt") != attempt or stored.get("request_sha256") != request["request_sha256"]:
                raise QualificationFailure(f"ledger failure record is inconsistent: {unit_id} attempt {attempt}")
            value.update(state="failed", failure=stored.get("detail"))
        return value

    def persist_request(self, unit_id: str, attempt: int, request: dict[str, Any], *, kind: str) -> str:
        digest = sha256_json(request)
        _exact_write(self._path(unit_id, attempt, "request"), {"contract_version": UNIT_REQUEST_CONTRACT, "unit_id": unit_id, "kind": kind, "attempt": attempt, "request": request, "request_sha256": digest})
        return digest

    def persist_receipt(self, unit_id: str, attempt: int, *, request_sha256: str, receipt: dict[str, Any], operation_key: str | None, invocation_id: str | None) -> dict[str, Any]:
        value = {"contract_version": UNIT_RECEIPT_CONTRACT, "unit_id": unit_id, "attempt": attempt, "request_sha256": request_sha256, "receipt": receipt, "operation_key": operation_key, "invocation_id": invocation_id}
        _exact_write(self._path(unit_id, attempt, "receipt"), value)
        return value

    def persist_failure(self, unit_id: str, attempt: int, *, request_sha256: str, detail: str) -> None:
        _exact_write(self._path(unit_id, attempt, "failure"), {"contract_version": UNIT_FAILURE_CONTRACT, "unit_id": unit_id, "attempt": attempt, "request_sha256": request_sha256, "detail": detail[-4000:]})

    def require_same_request(self, record: dict[str, Any], request: dict[str, Any]) -> None:
        """Deterministic keys are not enough: the admitted bytes must be reproduced exactly."""

        if canonical_bytes(request) != canonical_bytes(record["request"]) or sha256_json(request) != record["request_sha256"]:
            raise QualificationFailure(f"request bytes drifted from the admitted attempt {record['attempt']}: {record['unit_id']}")

    def status(self) -> dict[str, Any]:
        states: dict[str, list[str]] = {"complete": [], "active": [], "failed": [], "remaining": []}
        for unit_id in expected_unit_ids():
            record = self.record(unit_id)
            states["remaining" if record is None else record["state"]].append(unit_id)
        continuation = {consumer_unit_id(consumer_id): spec["continuation"] for consumer_id, spec in CONSUMERS.items()}
        return {"contract_version": STATUS_CONTRACT, "ledger": str(self.root), "runtime_id": self.runtime_id, "consumer_continuation": continuation, **states}


def run_unit(
    ledger: QualificationLedger,
    unit_id: str,
    *,
    kind: str,
    build_request: Callable[[int], dict[str, Any]],
    execute: Callable[[int, dict[str, Any], bool], dict[str, Any]],
    reuse: Callable[[dict[str, Any]], None],
    resumable: Callable[[dict[str, Any]], bool],
    retry: bool,
    retry_refusal: Callable[[dict[str, Any]], str | None] = lambda _record: None,
) -> dict[str, Any]:
    """Reuse a completed unit, continue an admitted request, or persist a new attempt before dispatch.

    ``build_request(attempt)`` must be deterministic for a fixed runtime and ledger.
    ``execute(attempt, request, resumed)`` returns ``{"receipt", "operation_key", "invocation_id"}``.
    ``reuse(record)`` must raise unless the retained receipt still resolves exactly.
    ``resumable(record)`` says whether an active attempt can be continued without a new dispatch.
    ``retry_refusal(record)`` returns a reason when even --retry must not mint a new attempt,
    because a new dispatch could repurchase work whose identity this ledger cannot name.
    Only a QualificationFailure marks the attempt failed; any other exception (transport, timeout,
    operator interrupt) leaves the attempt active so the next run continues it.
    """

    record = ledger.record(unit_id)
    if record is not None and record["state"] == "complete":
        ledger.require_same_request(record, build_request(record["attempt"]))
        reuse(record)
        return record
    resumed = False
    if record is None:
        attempt = 1
    elif record["state"] == "active" and resumable(record):
        attempt = record["attempt"]
        resumed = True
    else:
        detail = record["failure"] if record["state"] == "failed" else "interrupted before a continuation handle existed"
        refusal = retry_refusal(record)
        if refusal is not None:
            raise QualificationFailure(f"unit {unit_id} attempt {record['attempt']} {record['state']} ({detail}); {refusal}")
        if not retry:
            raise QualificationFailure(f"unit {unit_id} attempt {record['attempt']} {record['state']} ({detail}); rerun with --retry to mint attempt {record['attempt'] + 1}")
        if record["state"] == "active":
            ledger.persist_failure(unit_id, record["attempt"], request_sha256=record["request_sha256"], detail=str(detail))
        attempt = record["attempt"] + 1
    request = build_request(attempt)
    if resumed:
        assert record is not None
        ledger.require_same_request(record, request)
        request_sha256 = record["request_sha256"]
    else:
        request_sha256 = ledger.persist_request(unit_id, attempt, request, kind=kind)
    try:
        outcome = execute(attempt, request, resumed)
    except QualificationFailure as error:
        ledger.persist_failure(unit_id, attempt, request_sha256=request_sha256, detail=str(error))
        raise
    return ledger.persist_receipt(unit_id, attempt, request_sha256=request_sha256, receipt=outcome["receipt"], operation_key=outcome.get("operation_key"), invocation_id=outcome.get("invocation_id"))


# --- suites -------------------------------------------------------------------


def _run_suite(suite_id: str, cwd: Path, commands: list[list[str]], truth_kinds: list[str]) -> dict[str, Any]:
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    for command in commands:
        completed = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        stdout_parts.append(completed.stdout)
        stderr_parts.append(completed.stderr)
        if completed.returncode != 0:
            detail = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace")[-8000:]
            raise QualificationFailure(f"suite failed: {suite_id}: {detail}")
    return {
        "suite_id": suite_id,
        "truth_kinds": truth_kinds,
        "command": commands,
        "cwd": str(cwd.resolve()),
        "exit_code": 0,
        "stdout_sha256": sha256_bytes(canonical_bytes([sha256_bytes(item) for item in stdout_parts])),
        "stderr_sha256": sha256_bytes(canonical_bytes([sha256_bytes(item) for item in stderr_parts])),
    }


def run_suites(axisvi_root: Path, *, ledger: QualificationLedger, retry: bool = False) -> list[dict[str, Any]]:
    receipts = []
    for suite_id, (cwd, commands, truth_kinds) in _suite_commands(axisvi_root).items():

        def build(attempt: int, suite_id: str = suite_id, cwd: Path = cwd, commands: list[list[str]] = commands) -> dict[str, Any]:
            return {"suite_id": suite_id, "cwd": str(cwd.resolve()), "command": commands, "attempt": attempt}

        def execute(_attempt: int, _request: dict[str, Any], _resumed: bool, suite_id: str = suite_id, cwd: Path = cwd, commands: list[list[str]] = commands, truth_kinds: list[str] = truth_kinds) -> dict[str, Any]:
            return {"receipt": _run_suite(suite_id, cwd, commands, truth_kinds)}

        # Suites spend nothing, so an interrupted suite simply reruns under the same attempt.
        record = run_unit(ledger, suite_unit_id(suite_id), kind="suite", build_request=build, execute=execute, reuse=lambda _record: None, resumable=lambda _record: True, retry=retry)
        receipts.append(record["receipt"])
    return receipts


# --- provider canaries --------------------------------------------------------


def _route(provider: str, *, profile: str, workspace: Path | None = None, parent_id: str | None = None, continuation_mode: str | None = None) -> dict[str, Any]:
    base = PROVIDERS[provider]
    native = profile == "native-continuation"
    value: dict[str, Any] = {
        "provider": provider,
        "model": base["model"],
        "harness": base["harness"],
        "effort": base["effort"],
        "execution_profile": profile,
        "session_policy": "visible_native_continuation" if profile == "native-continuation" else "fresh",
        "continuation_mode": continuation_mode if native else None,
        "native_parent_invocation_id": parent_id if native else None,
        "tools": [],
        "permissions": [],
        "workspace": str(workspace) if workspace is not None else None,
        "sandbox": "read-only",
        "approval_policy": "never",
        "network_policy": "disabled",
    }
    if profile == "research-fresh":
        value["tools"] = {"google": ["read_url"], "xai": ["web_search", "web_fetch"], "meta": ["web_search"]}[provider]
        value["network_policy"] = "provider_managed"
    elif profile in {"workspace-agent", "native-continuation"}:
        if provider == "anthropic":
            value["sandbox"] = "provider-permissions"
        else:
            value["tools"] = ["shell"]
    return value


def _submission(provider: str, profile_label: str, *, operation_key: str, route_profile: str, marker: str, workspace: Path | None = None, parent_id: str | None = None, continuation_mode: str | None = None, research: bool = False) -> dict[str, Any]:
    """Exact submission bytes for one canary attempt. Every field is a pure function of its inputs."""

    strict_antigravity_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["response", "toolSummary", "toolAction"],
        "properties": {
            "response": {"type": "string"},
            "toolSummary": {"type": "string"},
            "toolAction": {"type": "string"},
        },
    }
    if provider == "google":
        output_contract = {"contract_version": "OutputContract/2.0", "mode": "json", "json_schema": strict_antigravity_schema}
        prompt = f"Return the required response, toolSummary, and toolAction strings. Put exact marker {marker} in response. " + ("Use the allowed URL reader on https://example.com/ before answering." if research else "Do not use a tool.")
    else:
        output_contract = {"contract_version": "OutputContract/2.0", "mode": "text"}
        prompt = f"Reply in one sentence containing exact marker {marker}. This is synthetic qualification evidence."
        if research:
            prompt += " Use the qualified web-only tool surface to inspect https://example.com/ before answering."
    return {
        "contract_version": "ExecutionSubmission/1.0",
        "external_project_key": EXTERNAL_PROJECT_KEY,
        "operation_key": operation_key,
        "actor": "lattice-release-qualification",
        "workflow_role": None,
        "prompt": prompt,
        "context_block_ids": [],
        "parent_invocation_ids": [parent_id] if parent_id is not None else [],
        "route": _route(provider, profile=route_profile, workspace=workspace, parent_id=parent_id, continuation_mode=continuation_mode),
        "output_contract": output_contract,
        "idempotency_key": operation_key,
    }


def _check_canary_output(client: LatticeClient, result: Any, *, provider: str, profile_label: str, marker: str) -> dict[str, Any]:
    if result.state.value != "succeeded" or len(result.output_artifact_ids) != 1 or not isinstance(result.journal_receipt, dict) or result.journal_receipt.get("kind") != "lattice-model-runtime-receipt":
        raise QualificationFailure(f"provider canary failed: {provider}:{profile_label}: {result.state.value}:{result.typed_failure}")
    artifact = client.artifact(result.output_artifact_ids[0])
    content = artifact.get("content")
    if not isinstance(content, str) or sha256_bytes(content.encode("utf-8")) != result.output_sha256:
        raise QualificationFailure(f"provider canary artifact is not exact: {provider}:{profile_label}")
    if provider == "google":
        try:
            payload = strict_json_loads(content)
        except (ValueError, json.JSONDecodeError) as error:
            raise QualificationFailure("Antigravity canary returned non-JSON content") from error
        if not isinstance(payload, dict) or set(payload) != {"response", "toolSummary", "toolAction"} or marker not in payload["response"]:
            raise QualificationFailure(f"Antigravity strict-schema canary failed: {profile_label}")
    elif marker not in content:
        raise QualificationFailure(f"provider semantic marker is absent: {provider}:{profile_label}")
    return {
        "provider": provider,
        "profile": profile_label,
        "state": "succeeded",
        "invocation_id": result.invocation_id,
        "manifest_sha256": result.manifest_sha256,
        "output_sha256": result.output_sha256,
        "provider_profile_id": result.provider_attestation.get("profile_id"),
        "runtime_receipt_sha256": sha256_bytes(canonical_bytes(result.journal_receipt)),
    }


def _execute_canary(client: LatticeClient, request: dict[str, Any], *, provider: str, profile_label: str, marker: str, timeout: float) -> tuple[dict[str, Any], Any]:
    """Submit exact bytes and wait. A resubmission of an admitted request is a Lattice replay, not a purchase.

    Two different HTTP 409 answers exist and are told apart by the store's exact detail:

    - key conflict (store.reserve_external_submission): the key already carries different bytes, or the
      idempotency key belongs to another operation, because another ledger on the same runtime admitted a
      different request under this attempt's key. Resubmitting can never continue or replay that, so the
      attempt is adjudicated failed visibly, and --retry mints the next attempt under a fresh key.
    - admission refusal (every other LatticeConflict raised after the reservation, e.g. the native
      session-head guard): Lattice reserved the key with exactly these bytes and then refused the request
      content. The reservation stays unsealed, an identical resubmission repeats the refusal, and a new
      attempt under unchanged qualifier code would carry the same content. The attempt is adjudicated failed
      with the store's own detail so the operator corrects the request instead of retrying it.
    """

    try:
        result = client.submit(request, wait=True, timeout=timeout)
    except LatticeClientError as error:
        message = str(error)
        if _KEY_CONFLICT.match(message):
            raise QualificationFailure(
                f"Lattice already holds different bytes under {request['external_project_key']}:{request['operation_key']}, "
                f"so this attempt can neither continue nor replay ({error}); rerun with --retry to mint the next attempt under a new key"
            ) from error
        if _CONFLICT.match(message):
            raise QualificationFailure(
                f"Lattice refused admission of {provider}:{profile_label} under {request['external_project_key']}:{request['operation_key']} ({error}); "
                "the key is now reserved with exactly these bytes, so an identical resubmission repeats the refusal and --retry under unchanged "
                "qualifier code would resubmit the same request content under a new key; correct the request before minting another attempt"
            ) from error
        raise
    if result.external_project_key != request["external_project_key"] or result.operation_key != request["operation_key"]:
        raise QualificationFailure(f"Lattice returned a result for a different operation: {provider}:{profile_label}")
    return _check_canary_output(client, result, provider=provider, profile_label=profile_label, marker=marker), result


def _verify_retained_provider_receipt(client: LatticeClient, record: dict[str, Any]) -> None:
    """Refuse a retained receipt unless Lattice still resolves it to the exact succeeded execution."""

    receipt = record["receipt"]
    operation = record.get("operation_key")
    if not isinstance(operation, str) or receipt.get("state") != "succeeded" or record.get("invocation_id") != receipt.get("invocation_id"):
        raise QualificationFailure(f"retained provider receipt is malformed: {record['unit_id']}")
    # The receipt may only name the operation its own admitted request minted; otherwise a
    # receipt record could point at a succeeded execution of another runtime or attempt and
    # copy that execution's identities, which Lattice would then confirm field by field.
    if operation != record["request"].get("operation_key"):
        raise QualificationFailure(f"retained provider receipt does not belong to its admitted request: {record['unit_id']}")
    try:
        result = client.result(EXTERNAL_PROJECT_KEY, operation)
    except LatticeClientError as error:
        raise QualificationFailure(f"retained provider receipt no longer resolves in Lattice: {record['unit_id']}: {error}") from error
    exact = (
        result.state.value == "succeeded"
        and result.operation_key == operation
        and result.invocation_id == receipt["invocation_id"]
        and result.manifest_sha256 == receipt["manifest_sha256"]
        and result.output_sha256 == receipt["output_sha256"]
        and isinstance(result.journal_receipt, dict)
        and sha256_bytes(canonical_bytes(result.journal_receipt)) == receipt["runtime_receipt_sha256"]
        and result.provider_attestation.get("profile_id") == receipt["provider_profile_id"]
    )
    if not exact:
        raise QualificationFailure(f"retained provider receipt disagrees with Lattice: {record['unit_id']}")


def _provider_unit(client: LatticeClient, ledger: QualificationLedger, *, provider: str, profile_label: str, route_profile: str, workspace: Path | None, parent_id: str | None, continuation_mode: str | None, research: bool, timeout: float, retry: bool) -> dict[str, Any]:
    marker = canary_marker(provider, profile_label)

    def build(attempt: int) -> dict[str, Any]:
        return _submission(provider, profile_label, operation_key=operation_key(ledger.runtime_id, provider, profile_label, attempt), route_profile=route_profile, marker=marker, workspace=workspace, parent_id=parent_id, continuation_mode=continuation_mode, research=research)

    def execute(_attempt: int, request: dict[str, Any], _resumed: bool) -> dict[str, Any]:
        receipt, result = _execute_canary(client, request, provider=provider, profile_label=profile_label, marker=marker, timeout=timeout)
        return {"receipt": receipt, "operation_key": request["operation_key"], "invocation_id": result.invocation_id}

    # A persisted request is always resumable: resubmitting identical bytes either
    # replays the admitted execution or admits it for the first time. Neither path
    # mints a new operation key.
    return run_unit(ledger, provider_unit_id(provider, profile_label), kind="provider", build_request=build, execute=execute, reuse=lambda record: _verify_retained_provider_receipt(client, record), resumable=lambda _record: True, retry=retry)


def run_provider_canaries(client: LatticeClient, data_root: Path, *, ledger: QualificationLedger, timeout: float, retry: bool = False) -> list[dict[str, Any]]:
    workspace = data_root / "acceptance" / "qualification" / f"workspace-{ledger.runtime_id}"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    synthetic = workspace / "SYNTHETIC.txt"
    if not synthetic.exists():
        synthetic.write_text("Synthetic Lattice release qualification workspace.\n", encoding="utf-8")
    workspace = workspace.resolve()
    receipts: list[dict[str, Any]] = []
    for provider in PROVIDER_ORDER:
        if provider in {"xai", "meta", "google"}:
            for route_profile in RESEARCH_PROFILES:
                record = _provider_unit(client, ledger, provider=provider, profile_label=route_profile, route_profile=route_profile, workspace=None, parent_id=None, continuation_mode=None, research=route_profile == "research-fresh", timeout=timeout, retry=retry)
                receipts.append(record["receipt"])
            continue
        fresh = _provider_unit(client, ledger, provider=provider, profile_label="compiled-fresh", route_profile="compiled-fresh", workspace=None, parent_id=None, continuation_mode=None, research=False, timeout=timeout, retry=retry)
        receipts.append(fresh["receipt"])
        # Parent chains are stable across reruns because the parent's invocation id is
        # read back from its retained receipt, so the child's request bytes are reproduced.
        workspace_record = _provider_unit(client, ledger, provider=provider, profile_label="workspace-agent", route_profile="workspace-agent", workspace=workspace, parent_id=fresh["invocation_id"], continuation_mode=None, research=False, timeout=timeout, retry=retry)
        receipts.append(workspace_record["receipt"])
        # Native continuations form one chain, not a fan-out. Lattice admits a native
        # parent only while it is the latest invocation on its provider session
        # (store._assert_actor_route_authorized: "native continuation parent is no longer
        # the provider session head"). A succeeded resume keeps the workspace agent's
        # session and becomes that head, so the fork must continue the resumed
        # invocation; forking from the workspace agent afterwards is refused at
        # admission, and the refusal is not a different-bytes key conflict.
        resume = _provider_unit(client, ledger, provider=provider, profile_label="native-continuation:resume", route_profile="native-continuation", workspace=workspace, parent_id=workspace_record["invocation_id"], continuation_mode="resume", research=False, timeout=timeout, retry=retry)
        receipts.append(resume["receipt"])
        fork = _provider_unit(client, ledger, provider=provider, profile_label="native-continuation:fork", route_profile="native-continuation", workspace=workspace, parent_id=resume["invocation_id"], continuation_mode="fork", research=False, timeout=timeout, retry=retry)
        receipts.append(fork["receipt"])
        # Portable ancestry consumes the workspace agent's durable output without naming a session.
        portable = _provider_unit(client, ledger, provider=provider, profile_label="portable-fallback", route_profile="compiled-fresh", workspace=None, parent_id=workspace_record["invocation_id"], continuation_mode=None, research=False, timeout=timeout, retry=retry)
        receipts.append(portable["receipt"])
    observed = {(item["provider"], item["profile"]) for item in receipts}
    if observed != EXPECTED_CANARY_PROFILES:
        raise QualificationFailure(f"provider canary set is incomplete: {sorted(EXPECTED_CANARY_PROFILES - observed)}")
    return receipts


# --- consumer canaries --------------------------------------------------------


class _ConsumerProcessFailed(RuntimeError):
    """The consumer entry point exited non-zero; whether that is a failure or an interruption depends on Lattice."""


def _run_json(command: list[str], *, cwd: Path, environment: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-4000:]
        raise _ConsumerProcessFailed(f"consumer canary failed: {' '.join(command)}: {detail}")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"consumer canary returned no JSON: {' '.join(command)}") from error
    if not isinstance(value, dict):
        raise QualificationFailure("consumer canary output is not an object")
    return value


def _read_consumer_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise QualificationFailure(f"consumer canary report is missing: {path}")
    try:
        value = strict_json_loads(path.read_bytes())
    except (ValueError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"consumer canary report is not exact JSON: {path}") from error
    if not isinstance(value, dict):
        raise QualificationFailure(f"consumer canary report is not an object: {path}")
    return value


def _consumer_report_bindings(consumer_id: str, report: dict[str, Any], result: Any) -> list[str]:
    """Report fields, per consumer contract, that must agree with the Lattice result the report names."""

    mismatched: list[str] = []

    def expect(field: str, actual: Any) -> None:
        if report.get(field) != actual:
            mismatched.append(field)

    journal = result.journal_receipt
    attestation = result.provider_attestation
    if consumer_id == "canon.quaternary-exhaustion":
        expect("output_artifact_id", result.output_artifact_ids[0])
        expect("attempt_id", result.attempt_id)
        expect("lattice_project_id", result.project_id)
        expect("journal_receipt", journal)
        expect("provider_attestation", attestation)
    elif consumer_id == "field-survey.research-pipeline":
        expect("output_artifact_ids", list(result.output_artifact_ids))
        expect("attempt_id", result.attempt_id)
        expect("project_id", result.project_id)
        expect("runtime_receipt_sha256", sha256_bytes(canonical_bytes(journal)))
        expect("provider_profile_id", attestation.get("profile_id"))
    elif consumer_id == "octopus.model-execution":
        expect("consumer_id", consumer_id)
        expect("output_artifact_id", result.output_artifact_ids[0])
        expect("attempt_id", result.attempt_id)
        expect("journal_receipt_sha256", sha256_bytes(canonical_bytes(journal)))
        expect("provider_attestation_sha256", sha256_bytes(canonical_bytes(attestation)))
    else:
        mismatched.append("consumer_id")
    return mismatched


def _admitted_consumer_report(consumer_id: str, report_path: Path, *, expected_operation_key: str | None) -> dict[str, Any]:
    """The report this ledger may adopt: exact JSON, the consumer's contract and project key, and the supplied key when one was supplied."""

    spec = CONSUMERS.get(consumer_id)
    if spec is None:
        raise QualificationFailure(f"unknown consumer: {consumer_id}")
    report = _read_consumer_report(report_path)
    if report.get("contract_version") != spec["report_contract"] or report.get("external_project_key") != spec["external_project_key"]:
        raise QualificationFailure(f"consumer canary report has the wrong contract or project key: {consumer_id}: {report_path}")
    for key in ("operation_key", "invocation_id", "manifest_sha256", "output_sha256"):
        if not isinstance(report.get(key), str) or not report[key]:
            raise QualificationFailure(f"consumer canary report omits {key}: {consumer_id}")
    if expected_operation_key is not None and report["operation_key"] != expected_operation_key:
        raise QualificationFailure(f"consumer canary report names operation key {report['operation_key']!r}, not the key this ledger supplied: {consumer_id}")
    return report


def _consumer_evidence(client: LatticeClient, consumer_id: str, report_path: Path, *, expected_operation_key: str | None) -> tuple[dict[str, Any], str]:
    """Strict identity of one consumer canary: report bytes, contract, and the exact Lattice execution it names.

    Returns the verifier-shaped receipt and the operation key the report names. For a
    key-bound consumer that key must be the one this ledger supplied. Anything unknown
    or disagreeing refuses; nothing here is best-effort.
    """

    report = _admitted_consumer_report(consumer_id, report_path, expected_operation_key=expected_operation_key)
    spec = CONSUMERS[consumer_id]
    try:
        result = client.result(spec["external_project_key"], report["operation_key"])
    except LatticeClientError as error:
        raise QualificationFailure(f"consumer canary operation does not resolve in Lattice: {consumer_id}: {report['operation_key']}: {error}") from error
    exact = (
        result.state.value == "succeeded"
        and result.external_project_key == spec["external_project_key"]
        and result.operation_key == report["operation_key"]
        and result.invocation_id == report["invocation_id"]
        and result.manifest_sha256 == report["manifest_sha256"]
        and result.output_sha256 == report["output_sha256"]
        and len(result.output_artifact_ids) == 1
        and isinstance(result.journal_receipt, dict)
        and result.journal_receipt.get("kind") == "lattice-model-runtime-receipt"
    )
    if not exact:
        raise QualificationFailure(f"consumer canary report disagrees with the Lattice execution it names: {consumer_id}: {report['invocation_id']}")
    mismatched = _consumer_report_bindings(consumer_id, report, result)
    if mismatched:
        raise QualificationFailure(f"consumer canary report bindings disagree with Lattice: {consumer_id}: {mismatched}")
    # Exactly the field set the execution-consolidation verifier admits for a consumer canary.
    receipt = {"consumer_id": consumer_id, "external_project_key": spec["external_project_key"], "invocation_id": report["invocation_id"], "manifest_sha256": report["manifest_sha256"], "report_path": str(report_path), "report_sha256": _sha256_file(report_path)}
    return receipt, report["operation_key"]


def _verify_retained_consumer_receipt(client: LatticeClient, record: dict[str, Any]) -> None:
    """Refuse a retained consumer receipt unless the report bytes and Lattice still agree with it exactly."""

    receipt = record["receipt"]
    request = record["request"]
    consumer_id = str(receipt.get("consumer_id"))
    if consumer_id != request.get("consumer_id") or str(receipt.get("report_path")) != str(request.get("report_path")):
        raise QualificationFailure(f"retained consumer receipt does not belong to its admitted request: {record['unit_id']}")
    evidence, operation = _consumer_evidence(client, consumer_id, Path(str(receipt.get("report_path"))), expected_operation_key=request.get("operation_key"))
    if evidence != receipt or record.get("operation_key") != operation or record.get("invocation_id") != evidence["invocation_id"]:
        raise QualificationFailure(f"retained consumer receipt disagrees with its report or with Lattice: {record['unit_id']}")


# The installed client renders every HTTP failure as "Lattice returned HTTP <code>: <body>"
# (lattice/client.py). The server maps LatticeNotFound to 404 and LatticeConflict to 409
# (lattice/server.py); a key that was reserved but whose invocation was never sealed answers
# the latter on GET with the fixed detail below, and resubmitting its identical bytes seals
# and dispatches it (lattice/operations.py seal_execution_submission).
# Only two 409 details are key conflicts (store.reserve_external_submission); every other 409
# is an admission refusal of request content raised after the key was reserved.
_NOT_FOUND = re.compile(r"\ALattice returned HTTP 404\b")
_CONFLICT = re.compile(r"\ALattice returned HTTP 409\b")
_KEY_CONFLICT = re.compile(
    r"\ALattice returned HTTP 409\b.*\b(?:external operation key was reused with different bytes|external idempotency key was reused for another operation)\b",
    re.DOTALL,
)
_RESERVED_UNSEALED = re.compile(r"\ALattice returned HTTP 409\b.*\bexternal execution was reserved but has no sealed invocation\b", re.DOTALL)


def _lattice_operation_state(client: LatticeClient, external_project_key: str, operation_key: str) -> str:
    """``absent`` when Lattice has never admitted the key, ``reserved`` when it was admitted but never sealed,
    else the execution state; anything else is ambiguous and refuses."""

    try:
        result = client.result(external_project_key, operation_key)
    except LatticeClientError as error:
        if _NOT_FOUND.match(str(error)):
            return "absent"
        if _RESERVED_UNSEALED.match(str(error)):
            return "reserved"
        raise QualificationFailure(f"Lattice could not say whether {external_project_key}:{operation_key} already succeeded: {error}") from error
    if result.external_project_key != external_project_key or result.operation_key != operation_key:
        raise QualificationFailure(f"Lattice resolved a different operation for {external_project_key}:{operation_key}")
    return str(result.state.value)


def _consumer_handle(record: dict[str, Any]) -> str:
    """How an active consumer attempt continues without purchasing.

    ``report``   the attempt's own report exists and is admissible: adopt it.
    ``replay``   no report yet, and rerunning the identical command cannot purchase a
                 succeeded execution: a key-bound consumer resubmits the ledger's key, a
                 content-bound consumer resubmits its packet-derived key into a clean directory.
    ``occupied`` something this ledger did not admit sits where the report belongs, or a
                 content-bound directory is dirty; nothing is adopted or dispatched.
    """

    request = record["request"]
    report_path = Path(str(request["report_path"]))
    if report_path.exists():
        try:
            _admitted_consumer_report(str(request["consumer_id"]), report_path, expected_operation_key=request.get("operation_key"))
        except QualificationFailure:
            return "occupied"
        return "report"
    if request.get("continuation") == "key-bound":
        return "replay"
    if request.get("continuation") == "content-bound":
        output_dir = report_path.parent
        if not output_dir.exists() or not any(output_dir.iterdir()):
            return "replay"
    return "occupied"


def _consumer_retry_refusal(client: LatticeClient, record: dict[str, Any]) -> str | None:
    """Reasons --retry must not mint a new consumer attempt, because it could purchase succeeded work again.

    An active attempt reaches here only when its report location is occupied by something
    this ledger cannot adopt, so the operator must clear that by hand (never deleted here)
    and continue by replay. A failed key-bound attempt is retried only when Lattice says its
    key never succeeded and is not still in progress; an ambiguous Lattice answer refuses.
    """

    request = record["request"]
    report_path = Path(str(request["report_path"]))
    if record["state"] == "active":
        return (
            f"{report_path} (or its directory) is occupied by content this ledger cannot adopt; a new attempt would dispatch again. "
            "Move that content aside without deleting it (nothing here deletes), then rerun without --retry so the "
            "admitted attempt continues by replay."
        )
    if request.get("continuation") != "key-bound":
        return None
    state = _lattice_operation_state(client, str(request["external_project_key"]), str(request["operation_key"]))
    if state == "succeeded":
        return (
            f"Lattice already holds a succeeded execution under {request['external_project_key']}:{request['operation_key']} "
            f"although the ledger recorded this attempt as failed ({record['failure']}); a new attempt would purchase it again. "
            "Inspect that execution by hand; this ledger will not mint another attempt for it."
        )
    if state not in {"absent", "failed", "interrupted"}:
        return f"Lattice reports {request['external_project_key']}:{request['operation_key']} in state {state}; rerun without --retry to continue it instead of minting a new attempt."
    return None


def _bound_source_sha256s(consumer_id: str, axisvi_root: Path) -> dict[str, str]:
    """Exact identities of the entry point and the Lattice client it submits through."""

    digests: dict[str, str] = {}
    for relative in CONSUMERS[consumer_id]["bound_sources"]:
        path = (axisvi_root / relative).resolve()
        try:
            digests[relative] = _sha256_file(path)
        except OSError as error:
            raise QualificationFailure(f"consumer canary bound source is unreadable: {consumer_id}: {path}") from error
    return digests


def _consumer_request(consumer_id: str, *, attempt: int, runtime_id: str, axisvi_root: Path, root: Path, installed_lattice: Path, timeout: float) -> dict[str, Any]:
    """Exact dispatch bytes for one consumer attempt, bound to the entry point's current source and, for a key-bound consumer, the ledger's key."""

    spec = CONSUMERS[consumer_id]
    cwd = (axisvi_root / spec["source_root"]).resolve()
    entrypoint = (axisvi_root / spec["entrypoint"]).resolve()
    source_sha256s = _bound_source_sha256s(consumer_id, axisvi_root)
    key = consumer_operation_key(runtime_id, consumer_id, attempt) if spec["continuation"] == "key-bound" else None
    if consumer_id == "canon.quaternary-exhaustion":
        output_dir = root / f"canon-a{attempt}"
        command = ["uv", "run", "python", "-m", "canon.discovery.reference_cascade.run_lattice_loop_canary", "--output-dir", str(output_dir), "--timeout", str(timeout)]
        report_path = output_dir / "canary-report.json"
    elif consumer_id == "field-survey.research-pipeline":
        report_path = root / f"field-survey-a{attempt}.json"
        command = [str(cwd / ".venv/bin/python"), "scripts/run-lattice-consumer-canary.py", "--output", str(report_path), "--lattice-bin", str(installed_lattice), "--timeout", str(timeout), "--operation-key", str(key)]
    else:
        report_path = root / f"octopus-a{attempt}.json"
        command = ["uv", "run", "python", "scripts/lattice_execution_canary.py", "--output", str(report_path), "--lattice-bin", str(installed_lattice), "--timeout", str(timeout), "--operation-key", str(key)]
    return {
        "consumer_id": consumer_id,
        "external_project_key": spec["external_project_key"],
        "continuation": spec["continuation"],
        "operation_key": key,
        "cwd": str(cwd),
        "command": command,
        "report_path": str(report_path),
        "entrypoint": str(entrypoint),
        "entrypoint_sha256": source_sha256s[spec["entrypoint"]],
        "source_sha256s": source_sha256s,
        "attempt": attempt,
    }


def run_consumer_canaries(client: LatticeClient, axisvi_root: Path, data_root: Path, installed_lattice: Path, *, ledger: QualificationLedger, timeout: float, retry: bool = False) -> list[dict[str, Any]]:
    # The consumer root is namespaced by the ledger binding, not only the runtime, so a
    # later ledger for the same runtime can never collide with or adopt reports it did
    # not admit.
    root = data_root / "acceptance" / "qualification" / f"consumers-{ledger.runtime_id}-{sha256_json(ledger.header)[:16]}"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve()
    environment = {**os.environ, "LATTICE_BIN": str(installed_lattice)}

    def unit(consumer_id: str) -> dict[str, Any]:
        unit_id = consumer_unit_id(consumer_id)

        def build(attempt: int) -> dict[str, Any]:
            return _consumer_request(consumer_id, attempt=attempt, runtime_id=ledger.runtime_id, axisvi_root=axisvi_root, root=root, installed_lattice=installed_lattice, timeout=timeout)

        def execute(_attempt: int, request: dict[str, Any], _resumed: bool) -> dict[str, Any]:
            report_path = Path(str(request["report_path"]))
            expected_key = request.get("operation_key")
            summary: dict[str, Any] | None = None
            # The handle is inspected on first dispatch too, not only on resume. The consumer
            # root is namespaced by this ledger's exact binding, so a report already there was
            # produced for the same runtime, code, configuration and data root by an equivalent
            # ledger whose records were lost or replaced; it names this attempt's exact key and
            # must still resolve in Lattice below, so adopting it is the same strict evidence
            # path as resume, and dispatching instead would at best replay and at worst, for
            # a content-bound consumer, fail on its occupied directory with no way forward.
            handle = _consumer_handle({"request": request})
            if handle == "report":
                # The consumer finished and wrote its report before this driver stopped; adopt it.
                pass
            elif handle == "occupied":
                # Not a failure: nothing was adjudicated and --retry must not mint a purchasing
                # attempt around foreign content. The attempt stays active; the operator moves the
                # content aside (nothing here deletes) and the next run continues by replay.
                raise QualificationInterrupted(
                    f"consumer report path is occupied by content this ledger cannot adopt: {consumer_id}: {report_path}; "
                    "the attempt stays active; move that content aside without deleting it, then rerun to continue by replay"
                )
            else:
                # First dispatch of this attempt, or a replay of an admitted one: a key-bound
                # consumer resubmits the ledger's key through its own client, a content-bound
                # consumer resubmits its packet-derived key. Identical bytes resolve to the same
                # external operation in Lattice, so neither path purchases succeeded work twice.
                try:
                    summary = _run_json(list(request["command"]), cwd=Path(request["cwd"]), environment=environment)
                except _ConsumerProcessFailed as error:
                    if expected_key is None:
                        raise QualificationFailure(str(error)) from error
                    # The process died; only Lattice knows whether the paid execution already
                    # succeeded under the ledger's key. If it did, this is an interruption and
                    # the attempt stays active for replay. If the key is absent or terminal
                    # without success, nothing succeeded and the attempt is adjudicated failed.
                    state = _lattice_operation_state(client, str(request["external_project_key"]), str(expected_key))
                    if state in {"absent", "failed", "interrupted"}:
                        raise QualificationFailure(f"{error} [lattice state: {state}]") from error
                    raise QualificationInterrupted(f"{error} [lattice state: {state}; attempt stays active, rerun to continue by replay]") from error
            receipt, operation = _consumer_evidence(client, consumer_id, report_path, expected_operation_key=expected_key)
            if summary is not None:
                printed_path = summary.get("report_path")
                if summary.get("invocation_id") != receipt["invocation_id"] or (printed_path is not None and Path(str(printed_path)) != report_path):
                    raise QualificationFailure(f"consumer canary stdout disagrees with its report: {consumer_id}")
            return {"receipt": receipt, "operation_key": operation, "invocation_id": receipt["invocation_id"]}

        return run_unit(
            ledger,
            unit_id,
            kind="consumer",
            build_request=build,
            execute=execute,
            reuse=lambda record: _verify_retained_consumer_receipt(client, record),
            resumable=lambda record: _consumer_handle(record) != "occupied",
            retry=retry,
            retry_refusal=lambda record: _consumer_retry_refusal(client, record),
        )

    receipts = [unit(consumer_id)["receipt"] for consumer_id in CONSUMER_IDS]
    if [item["consumer_id"] for item in receipts] != list(CONSUMER_IDS):
        raise QualificationFailure("consumer canary set is incomplete")
    return receipts


def collect_release_evidence(client: LatticeClient, axisvi_root: Path, data_root: Path, installed_lattice: Path, *, ledger: QualificationLedger, timeout: float, retry: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fail fast on paid-subscription gates before running local suites; every unit is checkpointed."""

    providers = run_provider_canaries(client, data_root, ledger=ledger, timeout=timeout, retry=retry)
    suites = run_suites(axisvi_root, ledger=ledger, retry=retry)
    consumers = run_consumer_canaries(client, axisvi_root, data_root, installed_lattice, ledger=ledger, timeout=timeout, retry=retry)
    return suites, providers, consumers


def _exact_runtime(runtime_manifest_path: Path) -> dict[str, Any]:
    raw = runtime_manifest_path.read_bytes()
    value = strict_json_loads(raw)
    if not isinstance(value, dict) or canonical_bytes(value) != raw or value.get("contract_version") != "LatticeInstalledRuntime/4.0" or not str(value.get("runtime_id", "")).startswith("0.3.1-"):
        raise QualificationFailure("qualification requires an exact installed Lattice 0.3.1 runtime manifest")
    return value


def write_receipt(
    output: Path,
    *,
    runtime_manifest_path: Path,
    consumer_registry_path: Path,
    axisvi_root: Path,
    suites: list[dict[str, Any]],
    provider_canaries: list[dict[str, Any]],
    consumer_canaries: list[dict[str, Any]],
    interruption: dict[str, Any],
    interruption_source: Path,
    credential_canary_files: list[Path],
) -> dict[str, Any]:
    runtime = _exact_runtime(runtime_manifest_path)
    registry_raw = consumer_registry_path.read_bytes()
    registry = strict_json_loads(registry_raw)
    if not isinstance(registry, dict) or canonical_bytes(registry) != registry_raw or registry.get("contract_version") != "ExecutionConsumerRegistry/1.0":
        raise QualificationFailure("consumer registry is not exact canonical ExecutionConsumerRegistry/1.0 JSON")
    if {item.get("suite_id") for item in suites} != set(SUITES) or any(item.get("exit_code") != 0 for item in suites):
        raise QualificationFailure("qualification receipt requires every fixed passing suite")
    observed_profiles = {(item.get("provider"), item.get("profile")) for item in provider_canaries if item.get("state") == "succeeded"}
    if observed_profiles != EXPECTED_CANARY_PROFILES:
        raise QualificationFailure("qualification receipt requires the exact provider-profile canary set")
    expected_consumers = {item["consumer_id"] for item in registry["consumers"]}
    if {item.get("consumer_id") for item in consumer_canaries} != expected_consumers:
        raise QualificationFailure("qualification receipt requires one exact canary per registered consumer")
    if interruption.get("contract_version") != "LatticeInterruptionReceipt/2.0" or interruption.get("runtime_execution_mode") != "installed-bytes-with-synthetic-provider" or interruption.get("pre_restart_state") not in {"leased", "running"} or interruption.get("post_restart_state") not in {"interrupted", "orphaned", "failed"} or not interruption.get("continuation_invocation_id") or not interruption.get("parent_invocation_id"):
        raise QualificationFailure("qualification receipt requires a completed active-work interruption proof")
    canaries = [path.read_bytes().rstrip(b"\r\n") for path in credential_canary_files]
    if not credential_canary_files or any(not path.is_file() or not value for path, value in zip(credential_canary_files, canaries, strict=True)):
        raise QualificationFailure("qualification requires nonempty isolated credential canaries")
    inventories = {item["consumer_id"]: _source_inventory((axisvi_root / item["source_root"]).resolve(strict=True)) for item in registry["consumers"]}
    sources = [
        {"ref": f"file:{runtime_manifest_path}", "revision": runtime["runtime_id"], "sha256": _sha256_file(runtime_manifest_path)},
        {"ref": f"file:{consumer_registry_path}", "revision": registry["generated_object"]["artifact_id"], "sha256": sha256_bytes(registry_raw)},
        {"ref": f"file:{interruption_source}", "revision": interruption.get("generated_object", {}).get("artifact_id"), "sha256": _sha256_file(interruption_source)},
        *[{"ref": f"file:{item['report_path']}", "revision": item["consumer_id"], "sha256": item["report_sha256"]} for item in consumer_canaries],
        *[{"ref": f"lattice:invocation:{item['invocation_id']}", "revision": item["profile"], "sha256": item["manifest_sha256"]} for item in provider_canaries],
    ]
    value = {
        "generated_object": generated_object(kind="lattice-qualification-receipt", schema_name="lattice-qualification-receipt", subject="lattice:release-qualification:0.3.1", role="snapshot", sources=sources, producer_name="lattice-release-qualification", producer_version="1.0.0"),
        "contract_version": "LatticeQualificationReceipt/1.0",
        "runtime_artifact_id": runtime["generated_object"]["artifact_id"],
        "runtime_id": runtime["runtime_id"],
        "suites": suites,
        "consumer_source_inventories": inventories,
        "consumer_canaries": consumer_canaries,
        "provider_canaries": provider_canaries,
        "interruption": interruption,
        "credential_canary_sha256s": sorted(sha256_bytes(item) for item in canaries),
        "kimi": {"state": "unavailable", "reason": "unqualified"},
    }
    raw = canonical_bytes(value)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    with output.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return value


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axisvi-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--consumer-registry", type=Path, required=True)
    parser.add_argument("--interruption-receipt", type=Path, required=True)
    parser.add_argument("--credential-canary-file", type=Path, action="append", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9010")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=None, help="qualification ledger directory; defaults to <output>.ledger")
    parser.add_argument("--status", action="store_true", help="print complete/active/failed/remaining units and exit without dispatching anything")
    parser.add_argument("--retry", action="store_true", help="mint the next attempt for failed units; completed units are never redispatched, interrupted units continue by replay instead, and a key-bound consumer whose key Lattice reports succeeded or in progress is refused")
    parser.add_argument("--timeout", type=float, default=1800.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    axisvi_root = args.axisvi_root.resolve(strict=True)
    data_root = args.data_root.expanduser().resolve(strict=True)
    runtime_manifest = args.runtime_manifest.expanduser().resolve(strict=True)
    runtime = _exact_runtime(runtime_manifest)
    ledger_root = (args.ledger if args.ledger is not None else args.output.with_name(args.output.name + ".ledger")).expanduser().resolve()
    ledger = QualificationLedger.open(ledger_root, runtime=runtime, runtime_manifest_sha256=_sha256_file(runtime_manifest), data_root=data_root)
    if args.status:
        print(json.dumps(ledger.status(), sort_keys=True))
        return 0
    if args.output.exists():
        # Refuse before any unit is touched rather than after every unit has run; the receipt
        # file is write-once. Keep --ledger pointed at the same ledger and choose a new --output.
        raise QualificationFailure(f"qualification receipt already exists: {args.output}; nothing was dispatched. Keep --ledger {ledger.root} and pass a new --output to mint another receipt from the same evidence")
    installed_lattice = runtime_manifest.parent / ".venv/bin/lattice"
    token_path = data_root / "operator-token"
    if not installed_lattice.is_file() or not token_path.is_file() or token_path.stat().st_mode & 0o777 != 0o600:
        raise QualificationFailure("installed Lattice executable or mode-0600 operator capability is unavailable")
    interruption_raw = args.interruption_receipt.read_bytes()
    interruption = strict_json_loads(interruption_raw)
    if not isinstance(interruption, dict) or canonical_bytes(interruption) != interruption_raw:
        raise QualificationFailure("interruption receipt is not exact canonical JSON")
    client = LatticeClient(args.base_url.rstrip("/"), token_path.read_text(encoding="utf-8").strip(), args.base_url.removeprefix("http://").removeprefix("https://"))
    try:
        suites, providers, consumers = collect_release_evidence(client, axisvi_root, data_root, installed_lattice, ledger=ledger, timeout=args.timeout, retry=args.retry)
    except (QualificationFailure, QualificationInterrupted) as error:
        print(json.dumps({"error": str(error), **ledger.status()}, sort_keys=True))
        raise
    receipt = write_receipt(args.output, runtime_manifest_path=runtime_manifest, consumer_registry_path=args.consumer_registry.resolve(strict=True), axisvi_root=axisvi_root, suites=suites, provider_canaries=providers, consumer_canaries=consumers, interruption=interruption, interruption_source=args.interruption_receipt.resolve(strict=True), credential_canary_files=[path.resolve(strict=True) for path in args.credential_canary_file])
    print(json.dumps({"output": str(args.output.resolve()), "artifact_id": receipt["generated_object"]["artifact_id"], "provider_canaries": len(providers), "suites": len(suites), "ledger": str(ledger.root)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
