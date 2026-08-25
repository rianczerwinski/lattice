"""Qualified Google subscription execution through the Antigravity CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..process import run_bounded


MINIMUM_VERSION = (1, 1, 10)
SUBSCRIPTION_STATE_FILES = (
    "google_accounts.json",
    "state.json",
    "oauth_creds.json",
    "antigravity/antigravity_state.pbtxt",
    "antigravity-cli/installation_id",
    "antigravity-cli/jetski_state.pbtxt",
)
ENVELOPE_FIELDS = ("toolSummary", "toolAction")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class AntigravityBoundaryError(RuntimeError):
    pass


def _settings(*, allow_web: bool) -> dict[str, Any]:
    denied = [
        "read_file(*)",
        "write_file(*)",
        "command(*)",
        "unsandboxed(*)",
        "mcp(*)",
        "execute_url(*)",
    ]
    if not allow_web:
        denied.insert(0, "read_url(*)")
    return {
        "permissions": {
            "allow": ["read_url(*)"] if allow_web else [],
            "deny": denied,
            "ask": [],
        },
        "allowNonWorkspaceAccess": False,
        "enableTerminalSandbox": True,
        "useG1Credits": False,
        "enableTelemetry": False,
        "showTips": False,
        "showFeedbackSurvey": False,
    }


def isolated_home(root: Path, *, allow_web: bool) -> tuple[Path, dict[str, str]]:
    source_root = Path.home() / ".gemini"
    home = root / "home"
    gemini_root = home / ".gemini"
    settings_root = gemini_root / "antigravity-cli"
    settings_root.mkdir(parents=True, exist_ok=True)
    system_keychain = Path.home() / "Library" / "Keychains"
    if system_keychain.is_dir():
        keychain_parent = home / "Library"
        keychain_parent.mkdir(parents=True, exist_ok=True)
        (keychain_parent / "Keychains").symlink_to(system_keychain, target_is_directory=True)
    for relative in SUBSCRIPTION_STATE_FILES:
        source = source_root / relative
        if not source.is_file():
            continue
        target = gemini_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
    settings = settings_root / "settings.json"
    settings.write_text(json.dumps(_settings(allow_web=allow_web), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(settings, 0o600)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}
    }
    environment.update(
        HOME=str(home),
        AGY_CLI_HIDE_ACCOUNT_INFO="1",
        AGY_CLI_DISABLE_LATEX="1",
        NO_COLOR="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return home, environment


def _version_tuple(text: str) -> tuple[int, int, int]:
    match = re.search(r"(?:^|\s)v?(\d+)\.(\d+)\.(\d+)(?:\s|$)", text.strip())
    if not match:
        raise AntigravityBoundaryError(f"cannot parse Antigravity CLI version: {text.strip()!r}")
    return tuple(int(part) for part in match.groups())


def _effective_model(stdout: str) -> str:
    for line in stdout.splitlines():
        token = line.split("\t", 1)[0].strip()
        if token and " " not in token:
            return token
    return ""


def capability_probe(binary: str, *, model: str, effort: str, allow_web: bool) -> tuple[bool, str]:
    try:
        version = run_bounded([binary, "--version"], timeout=10)
        if version.returncode != 0 or _version_tuple(version.stdout + "\n" + version.stderr) < MINIMUM_VERSION:
            return False, "Antigravity 1.1.10+ is required for effective model selection and permission isolation"
        help_result = run_bounded([binary, "--help"], timeout=10)
        required = {"--print", "--model", "--effort", "--sandbox", "--mode", "--log-file", "--json-schema", "--output-format", "--disable-slash-commands"}
        if help_result.returncode != 0 or not required.issubset(set(re.findall(r"--[a-z-]+", help_result.stdout + "\n" + help_result.stderr))):
            return False, "Antigravity does not expose every qualified isolation control"
        with tempfile.TemporaryDirectory(prefix="lattice-antigravity-probe-") as temporary:
            root = Path(temporary)
            _, environment = isolated_home(root, allow_web=allow_web)
            models = run_bounded([binary, "models"], timeout=30, cwd=root, env=environment)
            if models.returncode != 0 or model not in {_effective_model(line) for line in models.stdout.splitlines()}:
                return False, f"subscription model {model!r} is unavailable"
            selected = run_bounded([binary, "--model", model, "--effort", effort, "-p", "/model"], timeout=30, cwd=root, env=environment)
            effective = _effective_model(selected.stdout)
            if selected.returncode != 0 or effective != model:
                return False, f"requested model {model!r} would execute as {effective or 'unreported'!r}"
        return True, "qualified Antigravity subscription, isolation controls, and exact model selection"
    except (OSError, RuntimeError) as error:
        return False, str(error)


def augmented_schema(schema: dict[str, Any]) -> str:
    value = json.loads(json.dumps(schema))
    properties = value.setdefault("properties", {})
    required = value.setdefault("required", [])
    for field in ENVELOPE_FIELDS:
        properties.setdefault(field, {"type": "string"})
        if field not in required:
            required.append(field)
    if value.get("additionalProperties") is False:
        value["additionalProperties"] = True
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def strip_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in ENVELOPE_FIELDS}


def parse_output(text: str) -> dict[str, Any]:
    body = ANSI.sub("", text).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, flags=re.DOTALL | re.IGNORECASE)
    candidates = [fenced.group(1)] if fenced else [body]
    candidates.extend(body[index:] for index, character in enumerate(body) if character == "{")
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value, _end = decoder.raw_decode(candidate.lstrip())
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if "response" not in value:
            return strip_envelope(value)
        if value.get("error"):
            raise AntigravityBoundaryError(f"Antigravity reported an error: {value['error']}")
        response = value.get("response")
        if isinstance(response, dict):
            return strip_envelope(response)
        if isinstance(response, str):
            return parse_output(response)
    raise AntigravityBoundaryError("Antigravity response did not contain one JSON object")


def prepare(
    *,
    binary: str,
    root: Path,
    model: str,
    effort: str,
    prompt: str,
    schema: dict[str, Any],
    allow_web: bool,
    timeout: float,
    log_path: Path,
) -> tuple[list[str], str | None, Path, dict[str, str], dict[str, Path]]:
    native_root = root / "native-state"
    native_root.mkdir()
    _, environment = isolated_home(native_root, allow_web=allow_web)
    workspace = native_root / "workspace"
    workspace.mkdir()
    command = [
        binary,
        "--model",
        model,
        "--effort",
        effort,
        "--sandbox",
        "--mode",
        "plan",
        "--disable-slash-commands",
        "--json-schema",
        augmented_schema(schema),
        "--output-format",
        "json",
        "--print-timeout",
        f"{max(1, int(timeout))}s",
        "--log-file",
        str(log_path),
        "--print",
        prompt,
    ]
    return command, None, workspace, environment, {"driver.log": log_path}
