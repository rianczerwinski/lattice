"""Opt-in subscription-CLI conformance for one exact native tool decision."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import threading
import time

import pytest

from lattice import native_approvals as ledger
from lattice.contracts import ProviderRoute
from lattice.execution.adapters import DEFAULT_ADAPTERS, build_command, subscription_environment
from lattice.execution import sdk_runner
from lattice.execution import native_approval as approval_module
from lattice.execution.native_approval import NativeApprovalCallback
from lattice.execution.process import ProcessCancelled
from lattice.execution.sdk_runner import run_native_approval
from test_native_approvals import approval_env, decide, start


@pytest.mark.authenticated
@pytest.mark.parametrize("choice", ["deny", "allow", "cancel"])
def test_installed_claude_delivers_exact_native_decision(approval_env, choice):
    if os.environ.get("LATTICE_NATIVE_APPROVAL_LIVE") != "1":
        pytest.skip("requires explicit subscription-CLI opt-in")
    home_text = os.environ.get("LATTICE_NATIVE_APPROVAL_PROBE_HOME")
    if not home_text:
        pytest.fail("set LATTICE_NATIVE_APPROVAL_PROBE_HOME to a fresh durable parent")
    home = Path(home_text).resolve() / choice
    home.mkdir(parents=True, exist_ok=False)
    workspace = home / "workspace"
    workspace.mkdir()
    marker = f"native-approval-{choice}-{hashlib.sha256(os.urandom(16)).hexdigest()[:16]}"
    source = workspace / "proof.txt"
    source.write_text(marker + "\n", encoding="utf-8")
    target = workspace / "copied.txt"
    isolated = home / "isolated"
    isolated.mkdir()
    journal = home / "journal"
    journal.mkdir()
    schema = isolated / "response.schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"observed": {"type": "string"}}, "required": ["observed"], "additionalProperties": False}), encoding="utf-8")

    env = approval_env
    env["route"] = ProviderRoute(
        provider="anthropic", harness="claude", model="claude-opus-5",
        execution_profile="workspace-agent", tools=["Bash"], workspace=str(workspace),
        sandbox="provider-permissions", approval_policy="on-request", network_policy="provider_managed",
    )
    attempt = start(env)
    cancel = threading.Event()
    approval = NativeApprovalCallback(env["store"], attempt, owner_boot_id="test-boot", cancel_event=cancel)
    adapter = DEFAULT_ADAPTERS[0].select_model("claude-opus-5")
    policy = {
        "execution_profile": "workspace-agent", "continuation_mode": None,
        "native_session_id": None, "tools": ["Bash"], "permissions": [],
        "workspace": str(workspace), "sandbox": "provider-permissions",
        "approval_policy": "on-request", "network_policy": "provider_managed",
    }
    planted_command = f"cp {source} {target}"
    prompt = f"Use the Bash tool exactly once with this exact command: {planted_command}. Return JSON with observed equal to SUCCESS if it succeeds or DENIED if access is denied. Do not use any other tool."
    command, input_text, _ = build_command(
        adapter, binary="/opt/homebrew/bin/claude", isolated=isolated,
        schema_path=schema, output_path=isolated / "structured-output.json",
        effort="medium", prompt=prompt, effective_policy=policy,
    )
    (home / "start.json").write_text(json.dumps({
        "started_at": time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
        "choice": choice, "attempt_id": attempt, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "command": command, "prompt": prompt, "schema_sha256": hashlib.sha256(schema.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
        "sdk_version": importlib.metadata.version("claude-agent-sdk"),
        "code_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in {
            "probe": Path(__file__), "sdk_runner": Path(sdk_runner.__file__),
            "approval_callback": Path(approval_module.__file__), "approval_ledger": Path(ledger.__file__),
        }.items()},
        "model_invocations": 1, "timeout_seconds": 120,
    }, sort_keys=True), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_native_approval, command, prompt=input_text, cwd=workspace,
            env=subscription_environment(), journal_root=journal,
            timeout=120, cancel_event=cancel, approval=approval,
        )
        try:
            deadline = time.monotonic() + 90
            item = None
            while time.monotonic() < deadline and not future.done():
                requests = ledger.inspect(env["store"], attempt)["requests"]
                if requests:
                    item = requests[0]
                    break
                time.sleep(0.1)
            assert item is not None, "native CLI did not request the planted Bash action"
            assert len(ledger.inspect(env["store"], attempt)["requests"]) == 1
            action = item["binding"]["action"]
            assert action["tool_name"] == "Bash"
            assert action["tool_input"].get("command") == planted_command
            assert item["state"] == "awaiting-operator"
            if choice == "cancel":
                cancel.set()
                with pytest.raises(ProcessCancelled):
                    future.result(timeout=135)
                withdrawn = ledger.inspect(env["store"], attempt)["requests"][0]
                assert withdrawn["state"] == "withdrawn"
                assert withdrawn["release"] is None
                assert not target.exists()
                return
            decide(env, attempt, item, choice)
            try:
                result = future.result(timeout=135)
            except FutureTimeout:
                pytest.fail("native CLI did not terminate after the exact decision")
            requests = ledger.inspect(env["store"], attempt)["requests"]
            assert len(requests) == 1
            released = requests[0]
            assert released["state"] == "released-to-adapter"
            assert released["release"]["decision"] == choice
            assert result.returncode == 0
            if choice == "allow":
                assert target.read_text(encoding="utf-8") == marker + "\n"
            else:
                assert not target.exists()
        finally:
            if not future.done():
                cancel.set()
                try:
                    future.result(timeout=15)
                except Exception:
                    pass
