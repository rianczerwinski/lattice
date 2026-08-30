"""Contract and negative-control checks for installed interruption qualification."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ops import qualify_interruption


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
