"""Qualified Meta subscription execution through the Muse Code CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..process import run_bounded


MINIMUM_VERSION = (1, 0, 2)
OFFICIAL_API_HOST = "api.meta.ai"
NATIVE_CONFIG_NAMES = {"auth.json", "settings.json", "trust.json", ".auth.json.lock", ".settings.json.lock", ".trust.json.lock"}
_ATTESTED: dict[tuple[str, str, str, bool], tuple[tuple[str, str], ...]] = {}


class MuseBoundaryError(RuntimeError):
    pass


def _version_tuple(text: str) -> tuple[int, int, int]:
    match = re.search(r"(?:^|\s)(\d+)\.(\d+)\.(\d+)(?:[-\s.(]|$)", text.strip())
    if not match:
        raise MuseBoundaryError(f"cannot parse Muse Code version: {text.strip()!r}")
    return tuple(int(part) for part in match.groups())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_identity(binary: str) -> tuple[tuple[str, str], ...]:
    try:
        launcher = Path(binary).resolve(strict=True)
        metadata = launcher.stat()
    except OSError as error:
        raise MuseBoundaryError(f"cannot resolve Muse Code executable {binary!r}") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(launcher, os.X_OK):
        raise MuseBoundaryError(f"Muse Code path is not an executable regular file: {launcher}")
    paths = [launcher]
    version_state = launcher.parent / ".muse-version"
    if version_state.is_file():
        version = version_state.read_text(encoding="utf-8").strip()
        active = launcher.parent / f"muse-bin-{version}"
        if not active.is_file() or not os.access(active, os.X_OK):
            raise MuseBoundaryError("Muse Code launcher does not resolve an active executable")
        paths.append(active.resolve(strict=True))
    return tuple((str(path), _sha256_file(path)) for path in paths)


def _validated_auth_source() -> Path:
    source = Path.home() / ".config" / "muse" / "auth.json"
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise MuseBoundaryError("Meta subscription login is missing; run `muse login`") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise MuseBoundaryError("Muse OAuth metadata must be an owner-only regular file")
        raw = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise MuseBoundaryError("Muse OAuth metadata is not valid JSON") from error
    providers = payload.get("providers") if isinstance(payload, dict) else None
    meta = providers.get("meta") if isinstance(providers, dict) else None
    if payload.get("schema_version") != 2 or not isinstance(meta, dict):
        raise MuseBoundaryError("Muse OAuth metadata contains no Meta subscription session")
    if meta.get("mechanism") != "oauth" or meta.get("obtained_via") != "device_code" or meta.get("storage") != "keychain":
        raise MuseBoundaryError("Muse execution requires device-code OAuth stored in the user keychain and refuses developer API credentials")
    endpoint = urlparse(meta.get("api_base_url", ""))
    if endpoint.scheme != "https" or endpoint.hostname != OFFICIAL_API_HOST:
        raise MuseBoundaryError("Muse OAuth metadata does not target the official Meta subscription endpoint")
    parent = source.parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise MuseBoundaryError("Muse OAuth metadata directory must be owner-controlled and non-writable by other users")
    return source


def _validated_native_config(auth_source: Path) -> Path:
    root = auth_source.parent
    unexpected = sorted(path.name for path in root.iterdir() if path.name not in NATIVE_CONFIG_NAMES)
    if unexpected:
        raise MuseBoundaryError(f"Muse native config root contains unqualified entries: {unexpected}")
    settings_path = root / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
            raise MuseBoundaryError("Muse settings are unreadable") from error
        if not isinstance(settings, dict) or not set(settings).issubset({"schema_version", "provider", "model", "tui"}):
            raise MuseBoundaryError("Muse settings contain execution inputs the adapter has not qualified")
    trust_path = root / "trust.json"
    if trust_path.exists():
        try:
            trust = json.loads(trust_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
            raise MuseBoundaryError("Muse trust state is unreadable") from error
        if not isinstance(trust, dict) or not set(trust).issubset({"schema_version", "projects"}) or not isinstance(trust.get("projects", {}), dict):
            raise MuseBoundaryError("Muse trust state has an unqualified shape")
    return root.parent


def isolated_state(root: Path) -> tuple[Path, Path, dict[str, str]]:
    auth_source = _validated_auth_source()
    home = Path.home()
    config = _validated_native_config(auth_source)
    data = root / "data"
    cache = root / "cache"
    state = root / "state"
    workspace = root / "workspace"
    for path in (data, cache, state, workspace):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL"}
    }
    environment.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(config),
        XDG_DATA_HOME=str(data),
        XDG_CACHE_HOME=str(cache),
        XDG_STATE_HOME=str(state),
        MUSE_AUTH_PATH=str(auth_source),
        MUSE_NO_AUTO_UPDATE="1",
        NO_COLOR="1",
        DO_NOT_TRACK="1",
    )
    return home, workspace, environment


def compile_prompt(prompt: str, schema: dict[str, Any]) -> str:
    contract = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (
        f"{prompt}\n\n"
        "LATTICE_PROVIDER_RESPONSE_CONTRACT/1\n"
        "Return exactly one JSON object and no other text or Markdown. The object must satisfy this Draft 2020-12 JSON Schema:\n"
        f"{contract}\n"
        "END_LATTICE_PROVIDER_RESPONSE_CONTRACT\n"
    )


def _strict_json(raw: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise MuseBoundaryError(f"Muse response contains duplicate JSON key {key!r}")
            value[key] = child
        return value

    def reject_constant(value: str) -> None:
        raise MuseBoundaryError(f"Muse response contains non-finite JSON number {value}")

    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise MuseBoundaryError("Muse response was not one JSON object") from error


def terminal_text(text: str) -> str:
    terminals: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        event = _strict_json(line)
        if not isinstance(event, dict):
            raise MuseBoundaryError("Muse JSONL stream contains a non-object event")
        payload_type = event.get("payload_type")
        if isinstance(payload_type, str) and payload_type.startswith("run.terminal."):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                raise MuseBoundaryError("Muse terminal event has no payload")
            terminals.append({"type": payload_type, "payload": payload})
    if len(terminals) != 1:
        raise MuseBoundaryError("Muse JSONL stream does not contain exactly one terminal event")
    terminal = terminals[0]
    payload = terminal["payload"]
    if terminal["type"] != "run.terminal.completed" or payload.get("terminal") != "completed":
        raise MuseBoundaryError(f"Muse run did not complete: {payload.get('reason') or terminal['type']}")
    result = payload.get("text")
    if not isinstance(result, str):
        raise MuseBoundaryError("Muse terminal event contains no response text")
    return result


def parse_output(text: str) -> dict[str, Any]:
    body = terminal_text(text).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", body, flags=re.DOTALL | re.IGNORECASE)
    value = _strict_json(fenced.group(1) if fenced else body)
    if not isinstance(value, dict):
        raise MuseBoundaryError("Muse response is not a JSON object")
    return value


def capability_probe(binary: str, *, model: str, effort: str, allow_web: bool) -> tuple[bool, str]:
    key = (binary, model, effort, allow_web)
    try:
        identity_before = _binary_identity(binary)
        if _ATTESTED.get(key) == identity_before:
            return True, "qualified Muse subscription boundary is attested for this executable"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL"}
        }
        environment.update(MUSE_NO_AUTO_UPDATE="1", NO_COLOR="1")
        version = run_bounded([binary, "--version"], timeout=10, env=environment)
        if version.returncode != 0 or _version_tuple(version.stdout + "\n" + version.stderr) < MINIMUM_VERSION:
            raise MuseBoundaryError("Muse Code 1.0.2+ is required for the qualified isolation contract")
        help_result = run_bounded([binary, "exec", "--help"], timeout=10, env=environment)
        required = {
            "--approval-judge", "--approval-mode", "--disable-shell", "--disable-web-tools",
            "--disable-write", "--json", "--max-model-steps", "--max-tool-output-bytes",
            "--model", "--no-foreign-personal-context", "--no-parallel-tool-calls",
            "--no-session-log", "--prompt-file", "--provider", "--reasoning-effort",
            "--sandbox-network", "--user-input-auto-resolve", "--workspace", "--worktree",
        }
        available = set(re.findall(r"--[a-z-]+", help_result.stdout + "\n" + help_result.stderr))
        if help_result.returncode != 0 or not required.issubset(available):
            raise MuseBoundaryError(f"Muse Code lacks qualified isolation controls: {sorted(required - available)}")
        with tempfile.TemporaryDirectory(prefix="lattice-muse-probe-") as temporary:
            root = Path(temporary)
            _, workspace, isolated_environment = isolated_state(root)
            config = run_bounded([binary, "config", "status"], timeout=10, cwd=workspace, env=isolated_environment)
            states = re.findall(r"\bstate=([a-z_]+)", config.stdout + "\n" + config.stderr)
            if config.returncode != 0 or len(states) != 4 or set(states) != {"absent"}:
                raise MuseBoundaryError("Muse Code discovered an enterprise configuration layer outside the declared route")
            probe_prompt = root / "probe-prompt.txt"
            probe_prompt.write_text("lattice-muse-boundary-probe", encoding="utf-8")
            os.chmod(probe_prompt, 0o600)
            command = _command(
                binary=binary,
                model=model,
                effort=effort,
                prompt_path=probe_prompt,
                workspace=workspace,
                allow_web=allow_web,
                provider="echo",
            )
            command = [item for item in command if item != "--no-parallel-tool-calls"]
            echo = run_bounded(command, timeout=20, cwd=workspace, env=isolated_environment)
            if echo.returncode != 0 or terminal_text(echo.stdout) != "echo: lattice-muse-boundary-probe":
                raise MuseBoundaryError("Muse Code JSONL and isolation-control probe failed")
        identity_after = _binary_identity(binary)
        if identity_after != identity_before:
            raise MuseBoundaryError("Muse Code executable changed during qualification")
        _ATTESTED[key] = identity_after
        return True, "qualified Meta consumer OAuth, Muse isolation controls, and JSONL protocol are present"
    except (MuseBoundaryError, OSError, subprocess.SubprocessError, ValueError) as error:
        return False, str(error)


def _command(
    *,
    binary: str,
    model: str,
    effort: str,
    prompt_path: Path,
    workspace: Path,
    allow_web: bool,
    provider: str = "meta",
) -> list[str]:
    command = [
        binary,
        "exec",
        "--json",
        "--prompt-file",
        str(prompt_path),
        "--provider",
        provider,
        "--workspace",
        str(workspace),
        "--worktree",
        "off",
        "--no-foreign-personal-context",
        "--no-session-log",
        "--approval-mode",
        "untrusted",
        "--approval-judge",
        "off",
        "--sandbox-network",
        "restricted",
        "--disable-write",
        "--disable-shell",
        "--max-model-steps",
        "1",
        "--max-tool-output-bytes",
        str(1024 * 1024),
        "--user-input-auto-resolve",
    ]
    if provider == "meta":
        command[command.index("--workspace"):command.index("--workspace")] = [
            "--model",
            model,
            "--reasoning-effort",
            effort,
            "--no-parallel-tool-calls",
        ]
    if not allow_web:
        command.append("--disable-web-tools")
    return command


def prepare(
    *,
    binary: str,
    root: Path,
    model: str,
    effort: str,
    prompt: str,
    schema: dict[str, Any],
    allow_web: bool,
) -> tuple[list[str], str | None, Path, dict[str, str], dict[str, Path], str]:
    identity = _binary_identity(binary)
    if _ATTESTED.get((binary, model, effort, allow_web)) != identity:
        raise MuseBoundaryError("Muse Code executable has not passed the matching qualification probe")
    _, workspace, environment = isolated_state(root)
    provider_prompt = compile_prompt(prompt, schema)
    prompt_path = root / "provider-prompt.txt"
    prompt_path.write_text(provider_prompt, encoding="utf-8")
    os.chmod(prompt_path, 0o600)
    command = _command(
        binary=binary,
        model=model,
        effort=effort,
        prompt_path=prompt_path,
        workspace=workspace,
        allow_web=allow_web,
    )
    return command, None, workspace, environment, {}, provider_prompt
