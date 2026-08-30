"""Contract and negative-control checks for installed interruption qualification."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from ops import qualify_interruption


@pytest.mark.regression
def test_isolated_server_does_not_inherit_axisvi_outport_identity() -> None:
    assert qualify_interruption._standalone_environment({"AXISVI_ROOT": "/repo", "PATH": "/bin"}) == {"PATH": "/bin"}


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


@pytest.mark.regression
def test_negative_control_canary_scan_rejects_persisted_credential_bytes(tmp_path) -> None:
    tmp_path.joinpath("journal").write_bytes(b"prefix-SYNTHETIC-CREDENTIAL-suffix")

    with pytest.raises(qualify_interruption.InterruptionFailure, match="credential canary"):
        qualify_interruption._scan_canaries(tmp_path, [b"SYNTHETIC-CREDENTIAL"])
