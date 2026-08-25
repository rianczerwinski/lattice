"""Official Grok Build subscription boundary with attested isolation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from ..process import run_bounded, terminate_process_group


MINIMUM_VERSION = (0, 2, 118)
WEB_TOOLS = ("web_search", "web_fetch")
SANDBOX_PROFILE = "lattice-execution"
PERMISSION_DENIES = ("Bash", "Edit", "Read", "Grep", "MCPTool")
DISALLOWED_TOOLS = (
    "bash", "run_terminal_cmd", "run_terminal_command", "read_file", "grep", "glob", "list_dir",
    "search_replace", "write_file", "edit_file", "search_tool", "use_tool", "Agent", "Task",
    "ask_user_question", "enter_plan_mode", "exit_plan_mode", "image_edit", "image_gen",
    "image_to_video", "monitor", "reference_to_video", "scheduler_create", "scheduler_delete",
    "scheduler_list", "todo_write", "workflow", "write",
)
SYSTEM_PROMPT = (
    "Use only the supplied task and declared web tools. Local files, commands, MCP, subagents, memory, "
    "plans, and ambient workspace context are unavailable. Return only the native structured output requested."
)
_ATTESTED: dict[tuple[str, str, str, bool], tuple[str, str]] = {}


class GrokBoundaryError(RuntimeError):
    pass


def _version_tuple(text: str) -> tuple[int, int, int]:
    match = re.search(r"(?:^|\s)v?(\d+)\.(\d+)\.(\d+)(?:\s|$)", text.strip())
    if not match:
        raise GrokBoundaryError(f"cannot parse Grok CLI version: {text.strip()!r}")
    return tuple(int(part) for part in match.groups())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_identity(binary: str) -> tuple[str, str]:
    try:
        resolved = Path(binary).resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise GrokBoundaryError(f"cannot resolve Grok CLI executable {binary!r}") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise GrokBoundaryError(f"Grok CLI path is not an executable regular file: {resolved}")
    return str(resolved), _sha256_file(resolved)


def _validated_auth_source() -> Path:
    source = Path.home() / ".grok" / "auth.json"
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise GrokBoundaryError("Grok subscription login is missing; run `grok login --oauth`") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise GrokBoundaryError("Grok OAuth credential must be an owner-only regular file")
        raw = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise GrokBoundaryError("Grok OAuth credential is not valid JSON") from error
    if not isinstance(payload, dict) or not payload:
        raise GrokBoundaryError("Grok OAuth credential contains no subscription session")
    for scope, credential in payload.items():
        if not isinstance(scope, str) or not isinstance(credential, dict) or credential.get("auth_mode") != "oidc":
            raise GrokBoundaryError("Grok execution requires subscription OIDC and refuses developer API credentials")
        if any(not isinstance(credential.get(field), str) or not credential[field].strip() for field in ("key", "refresh_token", "oidc_client_id", "oidc_issuer")):
            raise GrokBoundaryError("Grok subscription credential is incomplete")
    try:
        parent = source.parent.stat(follow_symlinks=False)
    except OSError as error:
        raise GrokBoundaryError("Grok OAuth state directory is unavailable") from error
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise GrokBoundaryError("Grok OAuth state directory must be owner-controlled and non-writable by other users")
    return source


def _config() -> str:
    return """[cli]
auto_update = false

[features]
telemetry = false
feedback = false

[telemetry]
trace_upload = false

[compat.cursor]
skills = false
rules = false
agents = false
mcps = false
hooks = false
sessions = false

[compat.claude]
skills = false
rules = false
agents = false
mcps = false
hooks = false
sessions = false

[compat.codex]
sessions = false
"""


def _tools(allow_web: bool) -> tuple[str, ...]:
    return WEB_TOOLS if allow_web else ()


def _disallowed(allow_web: bool) -> tuple[str, ...]:
    return DISALLOWED_TOOLS if allow_web else tuple(dict.fromkeys((*DISALLOWED_TOOLS, *WEB_TOOLS)))


def _agent_profile(allow_web: bool) -> str:
    tools = "".join(f"  - {tool}\n" for tool in _tools(allow_web))
    denied = "".join(f"  - {tool}\n" for tool in _disallowed(allow_web))
    return (
        "---\nname: lattice-execution\ndescription: Isolated subscription execution.\npromptMode: full\ntools:\n"
        f"{tools}disallowedTools:\n{denied}permissionMode: dontAsk\nagentsMd: false\ndiscoverSkills: false\n"
        "inheritSkills: false\ninjectDefaultTools: true\nmcpInheritance: none\n---\n"
        f"{SYSTEM_PROMPT}\n"
    )


def _isolated_state(root: Path, *, allow_web: bool) -> tuple[Path, Path, dict[str, str]]:
    auth_source = _validated_auth_source()
    home = root / "home"
    grok_home = root / "grok-home"
    workspace = root / "workspace"
    for path in (home, grok_home, workspace):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = {
        grok_home / "config.toml": _config().encode("utf-8"),
        # Refreshing an OIDC token commonly replaces auth.json atomically, which
        # requires write access to its parent. The durable credential remains the
        # sole admitted path outside the disposable execution root.
        grok_home / "sandbox.toml": f'[profiles.{SANDBOX_PROFILE}]\nextends = "strict"\nread_write = [{json.dumps(str(auth_source.parent))}]\n'.encode("utf-8"),
        grok_home / "lattice-agent.md": _agent_profile(allow_web).encode("utf-8"),
    }
    for path, content in files.items():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL"}}
    environment.update(HOME=str(home), GROK_HOME=str(grok_home), GROK_AUTH_PATH=str(auth_source), GROK_DISABLE_API_KEY_AUTH="true", GROK_WEB_FETCH="1" if allow_web else "0", GROK_TELEMETRY_ENABLED="0", GROK_FEEDBACK_ENABLED="0", GROK_TELEMETRY_TRACE_UPLOAD="0", GROK_EXTERNAL_OTEL="0", DO_NOT_TRACK="1", NO_COLOR="1")
    return grok_home, workspace, environment


def _validated_inspect(payload: Any, grok_home: Path) -> None:
    if not isinstance(payload, dict):
        raise GrokBoundaryError("Grok inspect output is not an object")
    for name in ("projectInstructions", "hooks", "skills", "plugins", "marketplaces", "mcpServers", "lspServers"):
        if payload.get(name) != []:
            raise GrokBoundaryError("Grok isolated environment discovered ambient instructions or extensions")
    agents = payload.get("agents")
    if not isinstance(agents, list) or any(not isinstance(agent, dict) or not isinstance(agent.get("source"), dict) or agent["source"].get("type") != "builtin" for agent in agents):
        raise GrokBoundaryError("Grok isolated environment discovered a non-builtin agent")
    permissions = payload.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("loaded") != 0 or permissions.get("sources") != []:
        raise GrokBoundaryError("Grok isolated environment loaded ambient permission policy")
    login = payload.get("loginPolicy")
    if not isinstance(login, dict) or login.get("apiKeyAuthDisabled") is not True:
        raise GrokBoundaryError("Grok cannot prove developer API-key authentication is disabled")
    layers = payload.get("configSources", {}).get("layers") if isinstance(payload.get("configSources"), dict) else None
    paths = [item.get("path") for item in layers if isinstance(item, dict)] if isinstance(layers, list) else []
    if len(paths) != 1 or not isinstance(paths[0], str) or Path(paths[0]).resolve() != (grok_home / "config.toml").resolve():
        raise GrokBoundaryError("Grok loaded an unexpected configuration layer")
    compat = payload.get("externalCompat", {}).get("cells") if isinstance(payload.get("externalCompat"), dict) else None
    if not isinstance(compat, list) or any(not isinstance(cell, dict) or cell.get("enabled") is not False for cell in compat):
        raise GrokBoundaryError("Grok did not disable foreign-harness compatibility")


def _model_efforts(value: Any, model: str) -> set[str]:
    if not isinstance(value, dict) or value.get("currentModelId") != model:
        raise GrokBoundaryError(f"Grok did not select model {model!r}")
    available = value.get("availableModels")
    selected = next((item for item in available if isinstance(item, dict) and item.get("modelId") == model), None) if isinstance(available, list) else None
    choices = selected.get("_meta", {}).get("reasoningEfforts") if isinstance(selected, dict) and isinstance(selected.get("_meta"), dict) else None
    if not isinstance(choices, list):
        raise GrokBoundaryError(f"Grok exposes no reasoning-effort catalogue for {model!r}")
    return {item["value"] for item in choices if isinstance(item, dict) and isinstance(item.get("value"), str)}


def _send(proc: subprocess.Popen[str], value: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise GrokBoundaryError("Grok isolation probe has no input stream")
    proc.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _receive(proc: subprocess.Popen[str], deadline: float) -> dict[str, Any]:
    if proc.stdout is None:
        raise GrokBoundaryError("Grok isolation probe has no output stream")
    ready, _, _ = select.select([proc.stdout], [], [], max(deadline - time.monotonic(), 0))
    if not ready:
        raise GrokBoundaryError("Grok isolation probe timed out")
    line = proc.stdout.readline()
    if not line:
        raise GrokBoundaryError("Grok isolation probe exited before attestation")
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise GrokBoundaryError("Grok isolation probe emitted malformed JSON-RPC") from error
    if not isinstance(value, dict):
        raise GrokBoundaryError("Grok isolation probe emitted a non-object message")
    return value


def _runtime_probe(binary: str, *, model: str, effort: str, grok_home: Path, workspace: Path, environment: dict[str, str], allow_web: bool, timeout: float) -> None:
    command = [binary, "--no-auto-update", "--cwd", str(workspace), "--sandbox", SANDBOX_PROFILE, "--permission-mode", "dontAsk", "--no-subagents", "--no-plan", "agent", "--model", model, "--reasoning-effort", effort, "--agent-profile", str(grok_home / "lattice-agent.md"), "stdio"]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=workspace, env=environment, start_new_session=True, bufsize=1)
    deadline = time.monotonic() + timeout
    tools_seen = False
    session_seen = False
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}})
        while True:
            message = _receive(proc, deadline)
            if message.get("id") != 1:
                continue
            metadata = message.get("result", {}).get("_meta") if isinstance(message.get("result"), dict) else None
            if not isinstance(metadata, dict) or metadata.get("defaultAuthMethodId") != "cached_token" or metadata.get("mcpServers") != []:
                raise GrokBoundaryError("Grok runtime did not select isolated subscription authentication")
            if effort not in _model_efforts(metadata.get("modelState"), model):
                raise GrokBoundaryError("Grok runtime did not advertise the requested effort")
            break
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(workspace), "mcpServers": [], "_meta": {"yoloMode": False, "modelId": model}}})
        while not (tools_seen and session_seen):
            message = _receive(proc, deadline)
            if message.get("method") == "session/update" and isinstance(message.get("params"), dict):
                update = message["params"].get("update")
                if isinstance(update, dict) and update.get("sessionUpdate") == "available_commands_update":
                    names = update.get("_meta", {}).get("tools") if isinstance(update.get("_meta"), dict) else None
                    if not isinstance(names, list) or set(names) != set(_tools(allow_web)):
                        raise GrokBoundaryError("Grok effective tools differ from the declared route")
                    tools_seen = True
            if message.get("id") == 2:
                result = message.get("result")
                if not isinstance(result, dict) or effort not in _model_efforts(result.get("models"), model):
                    raise GrokBoundaryError("Grok session did not apply the requested model and effort")
                session_seen = True
    finally:
        terminate_process_group(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def capability_probe(binary: str, *, model: str, effort: str, allow_web: bool, timeout: float = 30) -> tuple[bool, str]:
    key = (binary, model, effort, allow_web)
    try:
        identity_before = _binary_identity(binary)
        if _ATTESTED.get(key) == identity_before:
            return True, "qualified Grok subscription boundary is attested for this executable"
        version = run_bounded([binary, "--no-auto-update", "--version"], timeout=10)
        if version.returncode != 0 or _version_tuple(version.stdout + "\n" + version.stderr) < MINIMUM_VERSION:
            raise GrokBoundaryError("Grok CLI is too old for the qualified isolation contract")
        help_result = run_bounded([binary, "--help"], timeout=10)
        required = {"--allow", "--deny", "--disallowed-tools", "--json-schema", "--max-turns", "--model", "--no-plan", "--no-subagents", "--output-format", "--permission-mode", "--reasoning-effort", "--sandbox", "--system-prompt-override", "--tools", "--verbatim"}
        if help_result.returncode != 0 or not required.issubset(set(re.findall(r"--[a-z-]+", help_result.stdout + help_result.stderr))):
            raise GrokBoundaryError("Grok CLI lacks required isolation controls")
        with tempfile.TemporaryDirectory(prefix="lattice-grok-preflight-") as temp:
            root = Path(temp)
            grok_home, workspace, environment = _isolated_state(root, allow_web=allow_web)
            inspect = run_bounded([binary, "--no-auto-update", "inspect", "--json"], timeout=10, cwd=workspace, env=environment)
            if inspect.returncode != 0:
                raise GrokBoundaryError("Grok isolated-environment inspection failed")
            _validated_inspect(json.loads(inspect.stdout), grok_home)
            _runtime_probe(binary, model=model, effort=effort, grok_home=grok_home, workspace=workspace, environment=environment, allow_web=allow_web, timeout=timeout)
        identity_after = _binary_identity(binary)
        if identity_after != identity_before:
            raise GrokBoundaryError("Grok CLI changed during qualification")
        _ATTESTED[key] = identity_after
        return True, "qualified Grok subscription and isolation controls are present"
    except (GrokBoundaryError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return False, str(error)


def prepare(*, binary: str, root: Path, model: str, effort: str, prompt: str, schema: dict[str, Any], allow_web: bool) -> tuple[list[str], str | None, Path, dict[str, str], dict[str, Path]]:
    identity = _binary_identity(binary)
    if _ATTESTED.get((binary, model, effort, allow_web)) != identity:
        raise GrokBoundaryError("Grok executable has not passed the matching qualification probe")
    grok_home, workspace, environment = _isolated_state(root, allow_web=allow_web)
    profile = grok_home / "lattice-agent.md"
    allowed = [value for tool in (("WebSearch", "WebFetch") if allow_web else ()) for value in ("--allow", tool)]
    denied_permissions = PERMISSION_DENIES if allow_web else (*PERMISSION_DENIES, "WebSearch", "WebFetch")
    denied = [value for tool in denied_permissions for value in ("--deny", tool)]
    command = [binary, "--no-auto-update", "--cwd", str(workspace), "--model", model, "--agent", str(profile), "--reasoning-effort", effort, "--sandbox", SANDBOX_PROFILE, "--permission-mode", "dontAsk", *allowed, *denied, "--tools", ",".join(_tools(allow_web)), "--disallowed-tools", ",".join(_disallowed(allow_web)), "--no-subagents", "--no-plan", "--max-turns", "80", "--json-schema", json.dumps(schema, ensure_ascii=False, sort_keys=True), "--output-format", "json", "--system-prompt-override", SYSTEM_PROMPT, "--verbatim", "--session-id", str(uuid.uuid4()), "-p", prompt]
    return command, None, workspace, environment, {}


def parse_output(text: str) -> Any:
    body = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).strip()
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as error:
        raise GrokBoundaryError("Grok output was not one JSON envelope") from error
    if not isinstance(envelope, dict) or envelope.get("structuredOutputError"):
        detail = envelope.get("structuredOutputError") if isinstance(envelope, dict) else "non-object envelope"
        raise GrokBoundaryError(f"Grok structured output failed: {detail}")
    if "structuredOutput" not in envelope:
        raise GrokBoundaryError("Grok JSON envelope has no native structured output")
    return envelope["structuredOutput"]
