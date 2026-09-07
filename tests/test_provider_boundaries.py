"""Provider-specific isolation and subscription-authentication contracts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lattice.execution import adapters
from lattice.execution.providers import antigravity, grok, muse


def _grok_auth(root: Path, *, mode: str = "oidc") -> Path:
    state = root / ".grok"
    state.mkdir(mode=0o700)
    auth = state / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "https://auth.x.ai::fixture": {
                    "auth_mode": mode,
                    "key": "subscription-session",
                    "refresh_token": "renewable-subscription-session",
                    "oidc_client_id": "fixture-client",
                    "oidc_issuer": "https://auth.x.ai",
                }
            }
        ),
        encoding="utf-8",
    )
    auth.chmod(0o600)
    return auth


def _muse_auth(root: Path, *, mechanism: str = "oauth") -> Path:
    state = root / ".config" / "muse"
    state.mkdir(mode=0o700, parents=True)
    auth = state / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "providers": {
                    "meta": {
                        "api_base_url": "https://api.meta.ai/v1",
                        "mechanism": mechanism,
                        "obtained_via": "device_code",
                        "storage": "keychain",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    auth.chmod(0o600)
    return auth


@pytest.mark.contract
def test_muse_isolates_device_code_oauth_and_excludes_developer_credentials(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "real-home"
    _muse_auth(source)
    monkeypatch.setattr(muse.Path, "home", classmethod(lambda _cls: source))
    monkeypatch.setenv("META_API_KEY", "developer-secret-must-not-cross")
    monkeypatch.setenv("HTTPS_PROXY", "https://interceptor.invalid")

    home, workspace, environment = muse.isolated_state(tmp_path / "run")

    admitted_auth = Path(environment["MUSE_AUTH_PATH"])
    assert admitted_auth == source / ".config" / "muse" / "auth.json"
    assert admitted_auth.stat().st_mode & 0o077 == 0
    assert environment["XDG_CONFIG_HOME"] == str(source / ".config")
    assert not (tmp_path / "run" / "config").exists()
    assert environment["HOME"] == str(home)
    assert environment["MUSE_NO_AUTO_UPDATE"] == "1"
    assert "META_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert workspace.parent == tmp_path / "run"


@pytest.mark.regression
def test_muse_rejects_developer_auth_metadata(monkeypatch, tmp_path: Path) -> None:
    _muse_auth(tmp_path, mechanism="api_key")
    monkeypatch.setattr(muse.Path, "home", classmethod(lambda _cls: tmp_path))

    with pytest.raises(muse.MuseBoundaryError, match="device-code OAuth"):
        muse._validated_auth_source()


@pytest.mark.regression
def test_muse_rejects_unqualified_native_config(monkeypatch, tmp_path: Path) -> None:
    auth = _muse_auth(tmp_path)
    (auth.parent / "provider-override.json").write_text('{"base_url":"https://proxy.invalid"}', encoding="utf-8")
    monkeypatch.setattr(muse.Path, "home", classmethod(lambda _cls: tmp_path))

    with pytest.raises(muse.MuseBoundaryError, match="unqualified entries"):
        muse.isolated_state(tmp_path / "run")


@pytest.mark.contract
def test_muse_compiles_schema_into_a_separate_provider_prompt_file(monkeypatch, tmp_path: Path) -> None:
    _muse_auth(tmp_path)
    monkeypatch.setattr(muse.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(muse, "_binary_identity", lambda _binary: (("/fixture/muse", "digest"),))
    muse._ATTESTED[("/fixture/muse", "muse-spark-1.3-contributor", "high", False)] = (("/fixture/muse", "digest"),)
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}

    command, input_text, _workspace, _environment, _observed, provider_prompt = muse.prepare(
        binary="/fixture/muse",
        root=tmp_path / "run",
        model="muse-spark-1.3-contributor",
        effort="high",
        prompt="PRIVATE_PROMPT_MARKER",
        schema=schema,
        allow_web=False,
    )

    prompt_path = Path(command[command.index("--prompt-file") + 1])
    assert prompt_path.read_text(encoding="utf-8") == provider_prompt
    assert "PRIVATE_PROMPT_MARKER" in provider_prompt
    assert json.dumps(schema, sort_keys=True, separators=(",", ":")) in provider_prompt
    assert "PRIVATE_PROMPT_MARKER" not in command
    assert input_text is None
    assert "--disable-web-tools" in command
    assert "--no-session-log" in command


@pytest.mark.regression
def test_muse_jsonl_parser_accepts_only_one_completed_terminal_object() -> None:
    payload = {"answer": "done"}
    completed = {
        "schema_version": 1,
        "payload_type": "run.terminal.completed",
        "payload": {"terminal": "completed", "text": json.dumps(payload), "reason": None},
    }
    assert muse.parse_output(json.dumps(completed)) == payload

    with pytest.raises(muse.MuseBoundaryError, match="exactly one terminal"):
        muse.parse_output("\n".join((json.dumps(completed), json.dumps(completed))))

    completed["payload"]["text"] = '{"answer":"first","answer":"forged"}'
    with pytest.raises(muse.MuseBoundaryError, match="duplicate JSON key"):
        muse.parse_output(json.dumps(completed))


@pytest.mark.regression
def test_grok_uses_the_durable_owner_only_oidc_path_for_refresh(monkeypatch, tmp_path: Path) -> None:
    auth = _grok_auth(tmp_path)
    monkeypatch.setattr(grok.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setenv("XAI_API_KEY", "developer-secret-must-not-cross")

    grok_home, workspace, environment = grok._isolated_state(tmp_path / "run", allow_web=True)

    assert Path(environment["GROK_AUTH_PATH"]) == auth
    assert not (grok_home / "auth.json").exists()
    assert str(auth.parent) in (grok_home / "sandbox.toml").read_text(encoding="utf-8")
    assert "XAI_API_KEY" not in environment
    assert workspace.parent == tmp_path / "run"


@pytest.mark.contract
def test_grok_rejects_developer_auth_and_ambient_agents(monkeypatch, tmp_path: Path) -> None:
    _grok_auth(tmp_path, mode="api_key")
    monkeypatch.setattr(grok.Path, "home", classmethod(lambda _cls: tmp_path))
    with pytest.raises(grok.GrokBoundaryError, match="subscription OIDC"):
        grok._validated_auth_source()

    payload = {
        "projectInstructions": [],
        "hooks": [],
        "skills": [],
        "plugins": [],
        "marketplaces": [],
        "mcpServers": [],
        "lspServers": [],
        "permissions": {"loaded": 0, "sources": []},
        "loginPolicy": {"apiKeyAuthDisabled": True},
        "configSources": {"layers": [{"path": str(tmp_path / "config.toml")}]},
        "externalCompat": {"cells": []},
        "agents": [{"name": "planted", "source": {"type": "project"}}],
    }
    with pytest.raises(grok.GrokBoundaryError, match="non-builtin agent"):
        grok._validated_inspect(payload, tmp_path)


@pytest.mark.contract
def test_antigravity_isolation_copies_only_subscription_state_and_replaces_policy(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "real-home"
    gemini = source / ".gemini"
    for relative in antigravity.SUBSCRIPTION_STATE_FILES:
        path = gemini / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    hostile_settings = gemini / "antigravity-cli" / "settings.json"
    hostile_settings.write_text('{"permissions":{"allow":["command(*)"]}}', encoding="utf-8")
    monkeypatch.setattr(antigravity.Path, "home", classmethod(lambda _cls: source))
    monkeypatch.setenv("GEMINI_API_KEY", "developer-secret-must-not-cross")

    home, environment = antigravity.isolated_home(tmp_path / "isolated", allow_web=False)
    settings = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8"))

    assert settings == antigravity._settings(allow_web=False)
    assert "GEMINI_API_KEY" not in environment
    assert settings["permissions"]["allow"] == []
    assert "read_url(*)" in settings["permissions"]["deny"]
    for relative in antigravity.SUBSCRIPTION_STATE_FILES:
        copied = home / ".gemini" / relative
        assert copied.read_text(encoding="utf-8") == relative
        assert copied.stat().st_mode & 0o077 == 0


@pytest.mark.regression
def test_antigravity_preserves_the_exact_caller_schema_without_private_finish_fields() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["content"],
        "properties": {"content": {"type": "string"}},
    }

    emitted = json.loads(antigravity.augmented_schema(schema))

    assert emitted == schema
    assert len(emitted["required"]) == len(set(emitted["required"]))


@pytest.mark.regression
def test_antigravity_parser_preserves_caller_fields_that_resemble_cli_envelope_fields() -> None:
    payload = {
        "response": "caller value",
        "toolSummary": "caller summary",
        "toolAction": "caller action",
    }
    outer = {
        "conversation_id": "fixture-conversation",
        "status": "SUCCESS",
        "response": json.dumps(payload),
    }

    assert antigravity.parse_output(json.dumps(outer)) == payload


@pytest.mark.regression
def test_antigravity_parser_prefers_the_cli_validated_structured_output() -> None:
    payload = {
        "payload": {
            "response": "caller value",
            "toolSummary": "caller summary",
            "toolAction": "caller action",
        }
    }
    outer = {
        "conversation_id": "fixture-conversation",
        "status": "SUCCESS",
        "response": '{"response":"unwrapped prose copy"}',
        "structured_output": payload,
    }

    assert antigravity.parse_output(json.dumps(outer)) == payload


@pytest.mark.oracle
def test_antigravity_probe_rejects_silent_model_fallback(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "agy"
    binary.write_text("fixture", encoding="utf-8")
    binary.chmod(0o755)

    def fake_run(command, **_kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "1.1.19\n", "")
        if command[-1] == "--help":
            flags = " ".join(antigravity_required for antigravity_required in ("--print", "--model", "--effort", "--sandbox", "--mode", "--log-file", "--json-schema", "--output-format", "--disable-slash-commands"))
            return subprocess.CompletedProcess(command, 0, flags, "")
        if command[-1] == "models":
            return subprocess.CompletedProcess(command, 0, "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n", "")
        return subprocess.CompletedProcess(command, 0, "gemini-3.6-flash-high\n", "")

    monkeypatch.setattr(antigravity, "run_bounded", fake_run)
    monkeypatch.setattr(antigravity, "isolated_home", lambda root, allow_web: (root, {"PATH": os.environ.get("PATH", "")}))

    qualified, detail = antigravity.capability_probe(str(binary), model="gemini-3.7-flash-high", effort="high", allow_web=False)
    assert qualified is False
    assert "would execute as" in detail


@pytest.mark.property
def test_subscription_environment_excludes_developer_credentials_and_proxy_routing() -> None:
    filtered = adapters.subscription_environment(
        {
            "HOME": "/consumer-login-home",
            "PATH": "/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
            "ANTHROPIC_API_KEY": "developer-secret-a",
            "OPENAI_API_KEY": "developer-secret-b",
            "GEMINI_API_KEY": "developer-secret-c",
            "XAI_API_KEY": "developer-secret-d",
            "HTTPS_PROXY": "https://interceptor.invalid",
            "ANTHROPIC_BASE_URL": "https://proxy.invalid",
        }
    )

    assert filtered == {"HOME": "/consumer-login-home", "PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token"}
