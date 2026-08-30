#!/usr/bin/env python3
"""Qualify installed Lattice providers, consumers, and fixed source suites."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, strict_json_loads
from lattice.client import LatticeClient


SUITES: dict[str, tuple[str, list[list[str]], list[str]]] = {
    "lattice-backend": ("axis/chariots/lattice", [["{lattice-python}", "-m", "pytest", "-q"]], ["oracle", "property", "regression", "contract", "smoke", "fuzz", "concurrency"]),
    "lattice-frontend": ("axis/chariots/lattice/web", [["npm", "run", "check"], ["npm", "test", "--", "--run"]], ["contract", "regression"]),
    "lattice-browser": ("axis/chariots/lattice/web", [["npm", "run", "test:browser"]], ["browser-acceptance", "regression"]),
    "canon": ("sui/mission/projects/canon/dev/quaternary-exhaustion", [["uv", "run", "pytest", "-q", "tests/test_quaternary_workflow.py"]], ["oracle", "contract", "regression"]),
    "field-survey": ("sui/mission/typology/abduction/field-survey/research-pipeline", [["uv", "run", "pytest", "-q"]], ["oracle", "property", "contract", "regression", "concurrency"]),
    "slate-mirror": ("axis/tools/slate-mirror", [["node", "--test", "server.test.js"]], ["contract", "regression"]),
}

PROVIDERS = {
    "anthropic": {"model": "opus", "harness": "claude", "effort": "high"},
    "openai": {"model": "gpt-5.6-sol", "harness": "codex", "effort": "high"},
    "google": {"model": "gemini-3.7-flash-high", "harness": "antigravity", "effort": "high"},
    "xai": {"model": "grok-4.5", "harness": "grok", "effort": "high"},
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
}


class QualificationFailure(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_inventory(root: Path) -> list[dict[str, Any]]:
    excluded = {".git", ".venv", "node_modules", "__pycache__", "runtime", "results", "archive", "dist", ".svelte-kit", ".pytest_cache"}
    result = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
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


def run_suites(axisvi_root: Path) -> list[dict[str, Any]]:
    receipts = []
    for suite_id, (cwd, commands, truth_kinds) in _suite_commands(axisvi_root).items():
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        exit_code = 0
        for command in commands:
            completed = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            stdout_parts.append(completed.stdout)
            stderr_parts.append(completed.stderr)
            if completed.returncode != 0:
                exit_code = completed.returncode
                detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-4000:]
                raise QualificationFailure(f"suite failed: {suite_id}: {detail}")
        receipts.append({
            "suite_id": suite_id,
            "truth_kinds": truth_kinds,
            "command": commands,
            "cwd": str(cwd.resolve()),
            "exit_code": exit_code,
            "stdout_sha256": sha256_bytes(canonical_bytes([sha256_bytes(item) for item in stdout_parts])),
            "stderr_sha256": sha256_bytes(canonical_bytes([sha256_bytes(item) for item in stderr_parts])),
        })
    return receipts


def _route(provider: str, *, profile: str, workspace: Path | None = None, parent_id: str | None = None, continuation_mode: str | None = None) -> dict[str, Any]:
    base = PROVIDERS[provider]
    value: dict[str, Any] = {
        "provider": provider,
        "model": base["model"],
        "harness": base["harness"],
        "effort": base["effort"],
        "execution_profile": profile,
        "session_policy": "visible_native_continuation" if profile == "native-continuation" else "fresh",
        "continuation_mode": continuation_mode,
        "native_parent_invocation_id": parent_id,
        "tools": [],
        "permissions": [],
        "workspace": str(workspace) if workspace is not None else None,
        "sandbox": "read-only",
        "approval_policy": "never",
        "network_policy": "disabled",
    }
    if profile == "research-fresh":
        value["tools"] = {"google": ["read_url"], "xai": ["web_search", "web_fetch"]}[provider]
        value["network_policy"] = "provider_managed"
    elif profile in {"workspace-agent", "native-continuation"}:
        if provider == "anthropic":
            value["sandbox"] = "provider-permissions"
        else:
            value["tools"] = ["shell"]
    return value


def _submission(provider: str, profile_label: str, *, route_profile: str, marker: str, workspace: Path | None = None, parent_id: str | None = None, continuation_mode: str | None = None, research: bool = False) -> dict[str, Any]:
    operation_key = f"qualification:{provider}:{profile_label}:{uuid.uuid4().hex}"
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
        "external_project_key": "lattice.release-qualification",
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


def _execute_canary(client: LatticeClient, request: dict[str, Any], *, provider: str, profile_label: str, marker: str, timeout: float) -> tuple[dict[str, Any], Any]:
    result = client.submit(request, wait=True, timeout=timeout)
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
    receipt = {
        "provider": provider,
        "profile": profile_label,
        "state": "succeeded",
        "invocation_id": result.invocation_id,
        "manifest_sha256": result.manifest_sha256,
        "output_sha256": result.output_sha256,
        "provider_profile_id": result.provider_attestation.get("profile_id"),
        "runtime_receipt_sha256": sha256_bytes(canonical_bytes(result.journal_receipt)),
    }
    return receipt, result


def run_provider_canaries(client: LatticeClient, data_root: Path, *, timeout: float) -> list[dict[str, Any]]:
    workspace = data_root / "acceptance" / "qualification" / f"workspace-{time.strftime('%Y%m%dT%H%M%S')}"
    workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
    workspace.joinpath("SYNTHETIC.txt").write_text("Synthetic Lattice release qualification workspace.\n", encoding="utf-8")
    receipts: list[dict[str, Any]] = []
    for provider in ("anthropic", "openai"):
        marker = f"QUAL-{provider.upper()}-FRESH"
        fresh_receipt, fresh = _execute_canary(client, _submission(provider, "compiled-fresh", route_profile="compiled-fresh", marker=marker), provider=provider, profile_label="compiled-fresh", marker=marker, timeout=timeout)
        receipts.append(fresh_receipt)
        marker = f"QUAL-{provider.upper()}-WORKSPACE"
        workspace_receipt, workspace_result = _execute_canary(client, _submission(provider, "workspace-agent", route_profile="workspace-agent", marker=marker, workspace=workspace.resolve(), parent_id=fresh.invocation_id), provider=provider, profile_label="workspace-agent", marker=marker, timeout=timeout)
        receipts.append(workspace_receipt)
        for mode in ("resume", "fork"):
            label = f"native-continuation:{mode}"
            marker = f"QUAL-{provider.upper()}-{mode.upper()}"
            receipt, _result = _execute_canary(client, _submission(provider, label, route_profile="native-continuation", marker=marker, workspace=workspace.resolve(), parent_id=workspace_result.invocation_id, continuation_mode=mode), provider=provider, profile_label=label, marker=marker, timeout=timeout)
            receipts.append(receipt)
        marker = f"QUAL-{provider.upper()}-PORTABLE"
        portable, _result = _execute_canary(client, _submission(provider, "portable-fallback", route_profile="compiled-fresh", marker=marker, parent_id=workspace_result.invocation_id), provider=provider, profile_label="portable-fallback", marker=marker, timeout=timeout)
        receipts.append(portable)
    for provider in ("google", "xai"):
        for route_profile in ("compiled-fresh", "research-fresh"):
            marker = f"QUAL-{provider.upper()}-{route_profile.upper()}"
            receipt, _result = _execute_canary(client, _submission(provider, route_profile, route_profile=route_profile, marker=marker, research=route_profile == "research-fresh"), provider=provider, profile_label=route_profile, marker=marker, timeout=timeout)
            receipts.append(receipt)
    observed = {(item["provider"], item["profile"]) for item in receipts}
    if observed != EXPECTED_CANARY_PROFILES:
        raise QualificationFailure(f"provider canary set is incomplete: {sorted(EXPECTED_CANARY_PROFILES - observed)}")
    return receipts


def _run_json(command: list[str], *, cwd: Path, environment: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-4000:]
        raise QualificationFailure(f"consumer canary failed: {' '.join(command)}: {detail}")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"consumer canary returned no JSON: {' '.join(command)}") from error
    if not isinstance(value, dict):
        raise QualificationFailure("consumer canary output is not an object")
    return value


def run_consumer_canaries(axisvi_root: Path, data_root: Path, installed_lattice: Path, *, timeout: float) -> list[dict[str, Any]]:
    root = data_root / "acceptance" / "qualification" / f"consumers-{time.strftime('%Y%m%dT%H%M%S')}"
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    environment = {**os.environ, "LATTICE_BIN": str(installed_lattice)}
    canon_root = axisvi_root / "sui/mission/projects/canon/dev/quaternary-exhaustion"
    canon = _run_json(["uv", "run", "python", "tools/run_lattice_loop_canary.py", "--output-dir", str(root / "canon"), "--timeout", str(timeout)], cwd=canon_root, environment=environment)
    field_root = axisvi_root / "sui/mission/typology/abduction/field-survey/research-pipeline"
    field_output = root / "field-survey.json"
    field = _run_json([str(field_root / ".venv/bin/python"), "scripts/run-lattice-consumer-canary.py", "--output", str(field_output), "--lattice-bin", str(installed_lattice), "--timeout", str(timeout)], cwd=field_root, environment=environment)
    receipts = [
        {"consumer_id": "canon.quaternary-exhaustion", "external_project_key": canon["external_project_key"], "invocation_id": canon["invocation_id"], "manifest_sha256": canon["manifest_sha256"], "report_path": canon["report_path"], "report_sha256": _sha256_file(Path(canon["report_path"]))},
        {"consumer_id": "field-survey.research-pipeline", "external_project_key": field["external_project_key"], "invocation_id": field["invocation_id"], "manifest_sha256": field["manifest_sha256"], "report_path": str(field_output), "report_sha256": _sha256_file(field_output)},
    ]
    return receipts


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
    if interruption.get("pre_restart_state") not in {"leased", "running"} or interruption.get("post_restart_state") not in {"interrupted", "orphaned", "failed"} or not interruption.get("continuation_invocation_id") or not interruption.get("parent_invocation_id"):
        raise QualificationFailure("qualification receipt requires a completed active-work interruption proof")
    if not credential_canary_files or any(not path.is_file() or not path.read_bytes() for path in credential_canary_files):
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
        "credential_canary_sha256s": sorted(_sha256_file(path) for path in credential_canary_files),
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


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axisvi-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--consumer-registry", type=Path, required=True)
    parser.add_argument("--interruption-receipt", type=Path, required=True)
    parser.add_argument("--credential-canary-file", type=Path, action="append", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9010")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1800.0)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    axisvi_root = args.axisvi_root.resolve(strict=True)
    data_root = args.data_root.expanduser().resolve(strict=True)
    runtime_manifest = args.runtime_manifest.expanduser().resolve(strict=True)
    installed_lattice = runtime_manifest.parent / ".venv/bin/lattice"
    token_path = data_root / "operator-token"
    if not installed_lattice.is_file() or not token_path.is_file() or token_path.stat().st_mode & 0o777 != 0o600:
        raise QualificationFailure("installed Lattice executable or mode-0600 operator capability is unavailable")
    client = LatticeClient(args.base_url.rstrip("/"), token_path.read_text(encoding="utf-8").strip(), args.base_url.removeprefix("http://").removeprefix("https://"))
    suites = run_suites(axisvi_root)
    providers = run_provider_canaries(client, data_root, timeout=args.timeout)
    consumers = run_consumer_canaries(axisvi_root, data_root, installed_lattice, timeout=args.timeout)
    interruption_raw = args.interruption_receipt.read_bytes()
    interruption = strict_json_loads(interruption_raw)
    if not isinstance(interruption, dict) or canonical_bytes(interruption) != interruption_raw:
        raise QualificationFailure("interruption receipt is not exact canonical JSON")
    receipt = write_receipt(args.output, runtime_manifest_path=runtime_manifest, consumer_registry_path=args.consumer_registry.resolve(strict=True), axisvi_root=axisvi_root, suites=suites, provider_canaries=providers, consumer_canaries=consumers, interruption=interruption, interruption_source=args.interruption_receipt.resolve(strict=True), credential_canary_files=[path.resolve(strict=True) for path in args.credential_canary_file])
    print(json.dumps({"output": str(args.output.resolve()), "artifact_id": receipt["generated_object"]["artifact_id"], "provider_canaries": len(providers), "suites": len(suites)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
