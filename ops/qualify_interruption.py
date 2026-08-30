#!/usr/bin/env python3
"""Crash an isolated installed Lattice during provider work and prove recovery."""

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
from pathlib import Path
from typing import Any

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, strict_json_loads
from lattice.client import LatticeClient


class InterruptionFailure(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_fake_codex(path: Path, mode_path: Path, pid_path: Path) -> None:
    source = f'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

mode_path = pathlib.Path({str(mode_path)!r})
pid_path = pathlib.Path({str(pid_path)!r})
if "--version" in sys.argv:
    print("codex-cli 0.0.0-lattice-interruption-fixture")
    raise SystemExit(0)
if mode_path.read_text(encoding="utf-8").strip() == "block":
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    while True:
        time.sleep(60)
output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text(json.dumps({{"content": "INTERRUPTION-RECOVERY-SUCCEEDED"}}), encoding="utf-8")
print(json.dumps({{"type": "result", "result": "ok"}}))
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _wait_health(base_url: str, *, expected_boot_id: str | None = None, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = "unreachable"
    while time.monotonic() < deadline:
        request = urllib.request.Request(f"{base_url}/api/health", headers={"Host": base_url.removeprefix("http://").removeprefix("https://")})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                value = json.loads(response.read())
            if isinstance(value, dict) and value.get("status") == "ok" and (expected_boot_id is None or value.get("boot_id") != expected_boot_id):
                return value
            last = repr(value)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            last = str(error)
        time.sleep(0.2)
    raise InterruptionFailure(f"isolated Lattice did not become healthy: {last}")


def _server(installed_lattice: Path, environment: dict[str, str], port: int, log_root: Path) -> subprocess.Popen[bytes]:
    stdout = log_root / f"server-{uuid.uuid4().hex}.stdout"
    stderr = log_root / f"server-{uuid.uuid4().hex}.stderr"
    return subprocess.Popen([str(installed_lattice), "serve", "--port", str(port), "--log-level", "warning"], env=environment, stdin=subprocess.DEVNULL, stdout=stdout.open("xb"), stderr=stderr.open("xb"), start_new_session=True)


def _stop(process: subprocess.Popen[bytes], *, crash: bool) -> None:
    if process.poll() is not None:
        return
    os.kill(process.pid, signal.SIGKILL if crash else signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _submission(operation_key: str, marker: str, *, parent_id: str | None = None) -> dict[str, Any]:
    return {
        "contract_version": "ExecutionSubmission/1.0",
        "external_project_key": "lattice.interruption-qualification",
        "operation_key": operation_key,
        "actor": "lattice-interruption-qualification",
        "workflow_role": None,
        "prompt": f"Reply with exact marker {marker}.",
        "context_block_ids": [],
        "parent_invocation_ids": [parent_id] if parent_id else [],
        "route": {"provider": "openai", "model": "gpt-5.6-sol", "harness": "codex", "effort": "high", "execution_profile": "compiled-fresh", "tools": [], "permissions": [], "workspace": None, "sandbox": "read-only", "approval_policy": "never", "network_policy": "disabled"},
        "output_contract": {"contract_version": "OutputContract/2.0", "mode": "text"},
        "idempotency_key": operation_key,
    }


def _attempt_state(database: Path, invocation_id: str) -> tuple[str, str]:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT attempt_id,state FROM attempts WHERE invocation_id=? ORDER BY ordinal DESC LIMIT 1", (invocation_id,)).fetchone()
    if row is None:
        raise InterruptionFailure(f"invocation has no attempt: {invocation_id}")
    return str(row[0]), str(row[1])


def _wait_attempt(database: Path, invocation_id: str, states: set[str], *, timeout: float = 30.0) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = _attempt_state(database, invocation_id)
        except InterruptionFailure:
            time.sleep(0.1)
            continue
        if last[1] in states:
            return last
        time.sleep(0.1)
    raise InterruptionFailure(f"attempt did not enter {sorted(states)}: {last}")


def _scan_canaries(root: Path, canaries: list[bytes]) -> None:
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        content = path.read_bytes()
        if any(canary in content for canary in canaries):
            raise InterruptionFailure(f"credential canary entered isolated durable state: {path}")


def run(*, installed_lattice: Path, runtime_manifest: Path, axisvi_root: Path, isolated_root: Path, output: Path, credential_canary_files: list[Path], timeout: float = 120.0) -> dict[str, Any]:
    runtime_raw = runtime_manifest.read_bytes()
    runtime = strict_json_loads(runtime_raw)
    if not isinstance(runtime, dict) or canonical_bytes(runtime) != runtime_raw or runtime.get("contract_version") != "LatticeInstalledRuntime/4.0":
        raise InterruptionFailure("installed runtime manifest is not exact")
    canaries = [path.read_bytes() for path in credential_canary_files]
    if not canaries or any(not value for value in canaries):
        raise InterruptionFailure("interruption qualification requires nonempty credential canaries")
    isolated_root = isolated_root.expanduser().resolve()
    if isolated_root.exists():
        raise FileExistsError(isolated_root)
    fake_bin = isolated_root / "fake-bin"
    logs = isolated_root / "acceptance-logs"
    fake_bin.mkdir(parents=True, mode=0o700)
    logs.mkdir(mode=0o700)
    mode_path = isolated_root / "provider-mode"
    pid_path = isolated_root / "provider.pid"
    mode_path.write_text("success", encoding="utf-8")
    fake_codex = fake_bin / "codex"
    _write_fake_codex(fake_codex, mode_path, pid_path)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))),
        "AXISVI_ROOT": str(axisvi_root.resolve(strict=True)),
        "LATTICE_DATA_DIR": str(isolated_root),
        "LATTICE_PORT": str(port),
        "LATTICE_RUNTIME_ID": runtime["runtime_id"],
        "LATTICE_QUALIFICATION_CANARY_A": canaries[0].decode("utf-8", errors="strict"),
        "LATTICE_QUALIFICATION_CANARY_B": (canaries[1] if len(canaries) > 1 else canaries[0]).decode("utf-8", errors="strict"),
    }
    first = _server(installed_lattice, environment, port, logs)
    second: subprocess.Popen[bytes] | None = None
    try:
        before_health = _wait_health(base_url)
        token_path = isolated_root / "operator-token"
        client = LatticeClient(base_url, token_path.read_text(encoding="utf-8").strip(), f"127.0.0.1:{port}")
        base_key = f"base:{uuid.uuid4().hex}"
        base = client.submit(_submission(base_key, "INTERRUPTION-RECOVERY-SUCCEEDED"), wait=True, timeout=timeout)
        if base.state.value != "succeeded":
            raise InterruptionFailure("fake-provider recovery baseline did not succeed")
        mode_path.write_text("block", encoding="utf-8")
        blocked_key = f"blocked:{uuid.uuid4().hex}"
        blocked = client.submit(_submission(blocked_key, "MUST-NOT-COMPLETE", parent_id=base.invocation_id), wait=False)
        attempt_id, pre_state = _wait_attempt(isolated_root / "lattice.sqlite3", blocked.invocation_id, {"leased", "running"})
        deadline = time.monotonic() + 20
        while not pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not pid_path.is_file():
            raise InterruptionFailure("blocking provider process did not start")
        provider_pid = int(pid_path.read_text(encoding="ascii"))
        _stop(first, crash=True)
        try:
            os.kill(provider_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        mode_path.write_text("success", encoding="utf-8")
        second = _server(installed_lattice, environment, port, logs)
        after_health = _wait_health(base_url, expected_boot_id=before_health["boot_id"])
        _attempt_id, post_state = _wait_attempt(isolated_root / "lattice.sqlite3", blocked.invocation_id, {"interrupted", "orphaned", "failed"})
        continuation_key = f"continuation:{uuid.uuid4().hex}"
        continuation = client.submit(_submission(continuation_key, "INTERRUPTION-RECOVERY-SUCCEEDED", parent_id=base.invocation_id), wait=True, timeout=timeout)
        if continuation.state.value != "succeeded":
            raise InterruptionFailure("post-restart continuation did not succeed")
        _scan_canaries(isolated_root, canaries)
        value = {
            "generated_object": generated_object(kind="lattice-interruption-receipt", schema_name="lattice-interruption-receipt", subject="lattice:installed-interruption-qualification", role="snapshot", sources=[{"ref": f"file:{runtime_manifest}", "revision": runtime["runtime_id"], "sha256": sha256_bytes(runtime_raw)}], producer_name="lattice-interruption-qualification", producer_version="1.0.0"),
            "contract_version": "LatticeInterruptionReceipt/1.0",
            "runtime_artifact_id": runtime["generated_object"]["artifact_id"],
            "isolated_root": str(isolated_root),
            "pre_restart_boot_id": before_health["boot_id"],
            "post_restart_boot_id": after_health["boot_id"],
            "interrupted_invocation_id": blocked.invocation_id,
            "interrupted_attempt_id": attempt_id,
            "parent_invocation_id": base.invocation_id,
            "pre_restart_state": pre_state,
            "post_restart_state": post_state,
            "continuation_invocation_id": continuation.invocation_id,
            "continuation_manifest_sha256": continuation.manifest_sha256,
            "credential_canary_sha256s": sorted(sha256_bytes(item) for item in canaries),
        }
        raw = canonical_bytes(value)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return value
    finally:
        _stop(first, crash=False)
        if second is not None:
            _stop(second, crash=False)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-lattice", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--axisvi-root", type=Path, required=True)
    parser.add_argument("--isolated-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credential-canary-file", type=Path, action="append", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    value = run(installed_lattice=args.installed_lattice.resolve(strict=True), runtime_manifest=args.runtime_manifest.resolve(strict=True), axisvi_root=args.axisvi_root.resolve(strict=True), isolated_root=args.isolated_root, output=args.output, credential_canary_files=[path.resolve(strict=True) for path in args.credential_canary_file], timeout=args.timeout)
    print(json.dumps({"output": str(args.output.resolve()), "artifact_id": value["generated_object"]["artifact_id"], "post_restart_state": value["post_restart_state"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
