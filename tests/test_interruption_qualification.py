"""Contract and negative-control checks for installed interruption qualification."""

from __future__ import annotations

import sqlite3
import subprocess
import os
from pathlib import Path

import pytest

from ops import qualify_interruption
from lattice.execution.adapters import _codex_capability_probe


@pytest.mark.regression
def test_isolated_server_does_not_inherit_axisvi_outport_identity() -> None:
    assert qualify_interruption._standalone_environment({"AXISVI_ROOT": "/repo", "PATH": "/bin"}) == {"PATH": "/bin"}


@pytest.mark.regression
def test_synthetic_provider_does_not_impersonate_the_installed_provider_attestation(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    isolated_root = tmp_path / "state"

    environment = qualify_interruption._synthetic_provider_environment(
        {"AXISVI_ROOT": "/repo", "PATH": "/real/providers"},
        isolated_root=isolated_root,
        port=4567,
        fake_bin=fake_bin,
        canaries=[b"first", b"second"],
    )

    assert environment["LATTICE_RUNTIME_ID"] == "source"
    assert environment["PATH"].split(os.pathsep) == [str(fake_bin), "/real/providers"]
    assert "AXISVI_ROOT" not in environment


@pytest.mark.regression
def test_isolated_server_uses_the_attested_runtime_not_the_outport_shim(tmp_path: Path) -> None:
    runtime_manifest = tmp_path / "runtime.json"
    runtime_manifest.write_text("{}", encoding="utf-8")
    executable = tmp_path / ".venv" / "bin" / "lattice"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    assert qualify_interruption._runtime_lattice(runtime_manifest) == executable.resolve()


@pytest.mark.regression
def test_interruption_probe_reads_latest_attempt_from_current_schema(tmp_path) -> None:
    database = tmp_path / "lattice.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE attempts(attempt_id TEXT, invocation_id TEXT, attempt_number INTEGER, state TEXT)")
        connection.executemany(
            "INSERT INTO attempts VALUES(?,?,?,?)",
            [("attempt-1", "invocation-1", 1, "interrupted"), ("attempt-2", "invocation-1", 2, "running")],
        )

    assert qualify_interruption._attempt_state(database, "invocation-1") == ("attempt-2", "running")


@pytest.mark.contract
def test_fake_codex_success_writes_only_the_provider_contract(monkeypatch, tmp_path) -> None:
    mode = tmp_path / "mode"
    pid = tmp_path / "pid"
    binary = tmp_path / "codex"
    output = tmp_path / "output.json"
    schema = tmp_path / "schema.json"
    mode.write_text("success", encoding="utf-8")
    schema.write_text("{}", encoding="utf-8")
    qualify_interruption._write_fake_codex(binary, mode, pid)

    completed = subprocess.run([str(binary), "exec", "--output-schema", str(schema), "--output-last-message", str(output), "-"], input=b"synthetic prompt", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    assert completed.returncode == 0
    assert output.read_text(encoding="utf-8") == '{"content": "INTERRUPTION-RECOVERY-SUCCEEDED"}'
    assert not pid.exists()
    assert _codex_capability_probe(str(binary)) == (True, "qualified Codex isolation controls are present")


@pytest.mark.regression
def test_negative_control_canary_scan_rejects_persisted_credential_bytes(tmp_path) -> None:
    tmp_path.joinpath("journal").write_bytes(b"prefix-SYNTHETIC-CREDENTIAL-suffix")

    with pytest.raises(qualify_interruption.InterruptionFailure, match="credential canary"):
        qualify_interruption._scan_canaries(tmp_path, [b"SYNTHETIC-CREDENTIAL"])
