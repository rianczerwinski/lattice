#!/usr/bin/env python3
"""Prove launchd restart, snapshot recovery, and continuation from restored ancestry."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, strict_json_loads
from lattice.client import LatticeClient
from lattice.diagnostics import assess_data_root
from lattice.snapshot import verify_snapshot


SERVICE_LABEL = "com.axisvi.lattice"


class RestoreQualificationFailure(RuntimeError):
    pass


def _standalone_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in environment.items() if key != "AXISVI_ROOT"}


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_health(base_url: str, *, previous_boot_id: str | None = None, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    host = base_url.removeprefix("http://").removeprefix("https://").rstrip("/")
    last = "unreachable"
    while time.monotonic() < deadline:
        request = urllib.request.Request(f"{base_url.rstrip('/')}/api/health", headers={"Host": host})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                value = json.loads(response.read())
            if isinstance(value, dict) and value.get("status") == "ok" and (previous_boot_id is None or value.get("boot_id") != previous_boot_id):
                return value
            last = repr(value)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            last = str(error)
        time.sleep(0.2)
    raise RestoreQualificationFailure(f"Lattice service did not reach the required health generation: {last}")


def _run_json(command: list[str], *, environment: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")[-8000:]
        raise RestoreQualificationFailure(f"installed command failed: {' '.join(command)}: {detail}")
    try:
        value = strict_json_loads(completed.stdout)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise RestoreQualificationFailure(f"installed command returned non-JSON: {' '.join(command)}") from error
    if not isinstance(value, dict):
        raise RestoreQualificationFailure("installed command returned no receipt object")
    return value


def _submission(operation_key: str, marker: str, parent_id: str) -> dict[str, Any]:
    return {
        "contract_version": "ExecutionSubmission/1.0",
        "external_project_key": "lattice.restore-qualification",
        "operation_key": operation_key,
        "actor": "lattice-restore-qualification",
        "workflow_role": None,
        "prompt": f"Reply in one sentence containing exact marker {marker}.",
        "context_block_ids": [],
        "parent_invocation_ids": [parent_id],
        "route": {"provider": "openai", "model": "gpt-5.6-sol", "harness": "codex", "effort": "high", "execution_profile": "compiled-fresh", "tools": [], "permissions": [], "workspace": None, "sandbox": "read-only", "approval_policy": "never", "network_policy": "disabled"},
        "output_contract": {"contract_version": "OutputContract/2.0", "mode": "text"},
        "idempotency_key": operation_key,
    }


def _assert_result(client: LatticeClient, result: Any, marker: str) -> None:
    if result.state.value != "succeeded" or len(result.output_artifact_ids) != 1:
        raise RestoreQualificationFailure(f"continuation did not succeed: {result.state.value}:{result.typed_failure}")
    artifact = client.artifact(result.output_artifact_ids[0])
    content = artifact.get("content")
    if not isinstance(content, str) or marker not in content or sha256_bytes(content.encode("utf-8")) != result.output_sha256:
        raise RestoreQualificationFailure("continuation result omitted its semantic marker or exact output identity")


def _server(installed_lattice: Path, environment: dict[str, str], port: int, log_root: Path) -> subprocess.Popen[bytes]:
    stdout = (log_root / "restored-service.stdout").open("xb")
    stderr = (log_root / "restored-service.stderr").open("xb")
    return subprocess.Popen([str(installed_lattice), "serve", "--port", str(port), "--log-level", "warning"], env=environment, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.kill(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _browser_dormant_parent(browser: dict[str, Any]) -> str:
    actions = browser.get("actions")
    dormant = [item for item in actions if isinstance(item, dict) and item.get("action") == "dormant-branch-return"] if isinstance(actions, list) else []
    if len(dormant) != 1 or not isinstance(dormant[0].get("invocation_ids"), list) or len(dormant[0]["invocation_ids"]) != 1:
        raise RestoreQualificationFailure("browser receipt has no unique dormant branch")
    return str(dormant[0]["invocation_ids"][0])


def build_receipt(
    *,
    runtime_manifest: Path,
    browser_receipt: Path,
    snapshot_root: Path,
    restored_root: Path,
    pre_restart_boot_id: str,
    post_restart_boot_id: str,
    browser_parent_invocation_id: str,
    canonical_continuation: dict[str, Any],
    restored_child: dict[str, Any],
    restored_pre_state_digest: str,
) -> dict[str, Any]:
    runtime_raw = runtime_manifest.read_bytes()
    runtime = strict_json_loads(runtime_raw)
    browser_raw = browser_receipt.read_bytes()
    browser = strict_json_loads(browser_raw)
    snapshot_manifest_path = snapshot_root / "snapshot.json"
    snapshot_raw = snapshot_manifest_path.read_bytes()
    snapshot = verify_snapshot(snapshot_root)
    if not isinstance(runtime, dict) or canonical_bytes(runtime) != runtime_raw or runtime.get("contract_version") != "LatticeInstalledRuntime/4.0":
        raise RestoreQualificationFailure("installed runtime manifest is not exact")
    if not isinstance(browser, dict) or canonical_bytes(browser) != browser_raw or browser.get("contract_version") != "LatticeBrowserAcceptanceReceipt/1.0":
        raise RestoreQualificationFailure("browser acceptance receipt is not exact")
    if _browser_dormant_parent(browser) != browser_parent_invocation_id:
        raise RestoreQualificationFailure("restart continuation does not descend from the browser-qualified dormant branch")
    if pre_restart_boot_id == post_restart_boot_id:
        raise RestoreQualificationFailure("canonical service boot identity did not change")
    source_state_digest = snapshot.get("state_digest")
    if not isinstance(source_state_digest, str) or restored_pre_state_digest != source_state_digest:
        raise RestoreQualificationFailure("restored pre-continuation state does not equal the snapshot")
    restored_report = assess_data_root(restored_root)
    if canonical_continuation.get("state") != "succeeded" or canonical_continuation.get("parent_invocation_id") != browser_parent_invocation_id:
        raise RestoreQualificationFailure("canonical post-restart continuation is not exact")
    if restored_child.get("state") != "succeeded" or restored_child.get("parent_invocation_id") != canonical_continuation.get("invocation_id"):
        raise RestoreQualificationFailure("restored child does not descend from canonical snapshotted ancestry")
    sources = [
        {"ref": f"file:{runtime_manifest}", "revision": runtime["runtime_id"], "sha256": sha256_bytes(runtime_raw)},
        {"ref": f"file:{browser_receipt}", "revision": browser["generated_object"]["artifact_id"], "sha256": sha256_bytes(browser_raw)},
        {"ref": f"file:{snapshot_manifest_path}", "revision": snapshot["generated_object"]["artifact_id"], "sha256": sha256_bytes(snapshot_raw)},
    ]
    return {
        "generated_object": generated_object(kind="lattice-restore-continuation-receipt", schema_name="lattice-restore-continuation-receipt", subject="lattice:installed-restore-continuation:0.3.1", role="snapshot", sources=sources, producer_name="lattice-restore-qualification", producer_version="1.0.0"),
        "contract_version": "LatticeRestoreContinuationReceipt/1.0",
        "runtime_artifact_id": runtime["generated_object"]["artifact_id"],
        "snapshot_root": str(snapshot_root.resolve(strict=True)),
        "snapshot_artifact_id": snapshot["generated_object"]["artifact_id"],
        "snapshot_manifest_sha256": sha256_bytes(snapshot_raw),
        "restored_root": str(restored_root.resolve(strict=True)),
        "source_state_digest": source_state_digest,
        "restored_pre_continuation_state_digest": restored_pre_state_digest,
        "restored_post_continuation_state_digest": restored_report["state_digest"],
        "pre_restart_boot_id": pre_restart_boot_id,
        "post_restart_boot_id": post_restart_boot_id,
        "browser_parent_invocation_id": browser_parent_invocation_id,
        "canonical_continuation_invocation_id": canonical_continuation["invocation_id"],
        "canonical_continuation_manifest_sha256": canonical_continuation["manifest_sha256"],
        "parent_invocation_id": canonical_continuation["invocation_id"],
        "child_invocation_id": restored_child["invocation_id"],
        "child_manifest_sha256": restored_child["manifest_sha256"],
    }


def run(*, installed_lattice: Path, runtime_manifest: Path, browser_receipt: Path, axisvi_root: Path, base_url: str, data_root: Path, backup_root: Path, restored_root: Path, output: Path, timeout: float) -> dict[str, Any]:
    runtime = strict_json_loads(runtime_manifest.read_bytes())
    browser = strict_json_loads(browser_receipt.read_bytes())
    if not isinstance(runtime, dict) or not isinstance(browser, dict):
        raise RestoreQualificationFailure("runtime or browser evidence is malformed")
    parent_id = _browser_dormant_parent(browser)
    canonical_token = (data_root / "operator-token").read_text(encoding="utf-8").strip()
    host = base_url.removeprefix("http://").removeprefix("https://").rstrip("/")
    before_health = _wait_health(base_url, timeout=timeout)
    restarted = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if restarted.returncode != 0:
        raise RestoreQualificationFailure(f"launchd restart failed: {restarted.stderr.decode('utf-8', errors='replace')[-4000:]}")
    after_health = _wait_health(base_url, previous_boot_id=before_health["boot_id"], timeout=timeout)
    canonical_client = LatticeClient(base_url.rstrip("/"), canonical_token, host)
    canonical_marker = f"RESTORE-CANONICAL-{uuid.uuid4().hex}"
    canonical_key = f"canonical:{uuid.uuid4().hex}"
    canonical_result = canonical_client.submit(_submission(canonical_key, canonical_marker, parent_id), wait=True, timeout=timeout)
    _assert_result(canonical_client, canonical_result, canonical_marker)
    environment = {**os.environ, "AXISVI_ROOT": str(axisvi_root.resolve(strict=True)), "LATTICE_RUNTIME_ID": runtime["runtime_id"]}
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = _run_json([str(installed_lattice), "backup", "--to", str(backup_root), "--reason", "execution-closure-restore", "--json"], environment=environment)
    snapshot_root = Path(str(backup["snapshot"])).resolve(strict=True)
    restored = _run_json([str(installed_lattice), "restore", str(snapshot_root), "--to", str(restored_root), "--json"], environment=environment)
    restored_pre = str(restored.get("state_digest", ""))
    port = _free_port()
    restored_base = f"http://127.0.0.1:{port}"
    logs = output.parent / f"restore-service-{uuid.uuid4().hex}"
    logs.mkdir(parents=True, mode=0o700)
    restored_environment = _standalone_environment(environment)
    restored_environment.update({"LATTICE_DATA_DIR": str(restored_root), "LATTICE_PORT": str(port)})
    process = _server(installed_lattice, restored_environment, port, logs)
    try:
        _wait_health(restored_base, timeout=timeout)
        restored_client = LatticeClient(restored_base, (restored_root / "operator-token").read_text(encoding="utf-8").strip(), f"127.0.0.1:{port}")
        child_marker = f"RESTORE-CHILD-{uuid.uuid4().hex}"
        child_key = f"restored:{uuid.uuid4().hex}"
        child_result = restored_client.submit(_submission(child_key, child_marker, canonical_result.invocation_id), wait=True, timeout=timeout)
        _assert_result(restored_client, child_result, child_marker)
    finally:
        _stop(process)
    value = build_receipt(
        runtime_manifest=runtime_manifest,
        browser_receipt=browser_receipt,
        snapshot_root=snapshot_root,
        restored_root=restored_root,
        pre_restart_boot_id=before_health["boot_id"],
        post_restart_boot_id=after_health["boot_id"],
        browser_parent_invocation_id=parent_id,
        canonical_continuation={"state": canonical_result.state.value, "invocation_id": canonical_result.invocation_id, "manifest_sha256": canonical_result.manifest_sha256, "parent_invocation_id": parent_id},
        restored_child={"state": child_result.state.value, "invocation_id": child_result.invocation_id, "manifest_sha256": child_result.manifest_sha256, "parent_invocation_id": canonical_result.invocation_id},
        restored_pre_state_digest=restored_pre,
    )
    raw = canonical_bytes(value)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return value


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-lattice", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--browser-receipt", type=Path, required=True)
    parser.add_argument("--axisvi-root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9010")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--restored-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1800.0)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    value = run(installed_lattice=args.installed_lattice.resolve(strict=True), runtime_manifest=args.runtime_manifest.resolve(strict=True), browser_receipt=args.browser_receipt.resolve(strict=True), axisvi_root=args.axisvi_root.resolve(strict=True), base_url=args.base_url, data_root=args.data_root.expanduser().resolve(strict=True), backup_root=args.backup_root.expanduser().resolve(), restored_root=args.restored_root.expanduser().resolve(), output=args.output.expanduser().resolve(), timeout=args.timeout)
    print(json.dumps({"output": str(args.output.resolve()), "artifact_id": value["generated_object"]["artifact_id"], "restored_root": value["restored_root"], "child_invocation_id": value["child_invocation_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
