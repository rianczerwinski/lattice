"""Official subscription-CLI adapters for isolated, structured model calls."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .checkpoint import bind_request
from .journal import create_run_journal, finish_run_journal
from .process import MAX_MODEL_OUTPUT_BYTES, ProcessCancelled, ProcessOutputLimit, run_bounded
from .providers import antigravity, claude, codex, grok


FAILURE_KINDS = {
    "authentication",
    "subscription_exhausted",
    "rate_limit",
    "outage",
    "timeout",
    "malformed_output",
    "policy_refusal",
    "semantic_failure",
    "operator_interruption",
    "harness_failure",
    "output_limit",
}

MAX_SCHEMA_BYTES = 1024 * 1024
MAX_CANDIDATE_NODES = 10000
MAX_MODEL_INPUT_BYTES = 64 * 1024 * 1024

# These are provider-owned feature gates whose presence would make a
# compiled-fresh context materially broader than the request and manifest say.
# Keep this list explicit: a renamed or removed Codex feature must fail the
# compatibility probe and be requalified, not silently disappear from policy.
CODEX_DISABLED_FEATURES = codex.CODEX_DISABLED_FEATURES
CODEX_REQUIRED_EXEC_OPTIONS = codex.CODEX_REQUIRED_EXEC_OPTIONS
CLAUDE_REQUIRED_OPTIONS = claude.CLAUDE_REQUIRED_OPTIONS


class SubscriptionRouteUnavailable(RuntimeError):
    """An official consumer-subscription CLI route is not operational."""


class SubscriptionInvocationFailed(RuntimeError):
    """A subscription CLI invocation ended without a valid structured response."""

    def __init__(self, kind: str, message: str, *, journal: Path | None = None) -> None:
        if kind not in FAILURE_KINDS:
            raise ValueError(f"unsupported provider failure kind: {kind}")
        super().__init__(message)
        self.kind = kind
        self.journal = journal


@dataclass(frozen=True)
class ProviderProfile:
    provider_code: str
    harness: str
    executable: str
    model_code: str
    response_mode: str
    isolation: tuple[str, ...]
    proof_limits: tuple[str, ...] = ()
    qualified_profiles: tuple[str, ...] = ("compiled-fresh",)
    exact_context_qualified: bool = True
    qualification_blocker: str | None = None
    supported_models: tuple[str, ...] = ()
    supported_efforts: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")
    capability_compiler: str = "lattice-execution-policy/1"
    parser_revision: str = "lattice-structured-response/1"

    @property
    def cli(self) -> str:
        """Compatibility name for the harness identity stored in legacy manifests."""

        return self.harness

    @property
    def profile_id(self) -> str:
        encoded = json.dumps(self.definition(include_identity=False), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"provider-profile:{hashlib.sha256(encoded).hexdigest()}"

    def definition(self, *, include_identity: bool = True) -> dict[str, Any]:
        value = {
            "contract_version": "ProviderProfile/1.0",
            "provider_code": self.provider_code,
            "harness": self.harness,
            "official_cli": self.executable,
            "default_model": self.model_code,
            "supported_models": list(self.supported_models or (self.model_code,)),
            "supported_efforts": list(self.supported_efforts),
            "auth_surface": "consumer_subscription_cli",
            "response_mode": self.response_mode,
            "isolation": list(self.isolation),
            "proof_limits": list(self.proof_limits),
            "qualified_profiles": list(self.qualified_profiles),
            "exact_context_qualified": self.exact_context_qualified,
            "qualification_blocker": self.qualification_blocker,
            "capability_compiler": self.capability_compiler,
            "parser_revision": self.parser_revision,
        }
        if include_identity:
            value["profile_id"] = self.profile_id
        return value

    def select_model(self, model: str) -> "ProviderProfile":
        if model not in (self.supported_models or (self.model_code,)):
            raise SubscriptionRouteUnavailable(f"{self.provider_code} profile does not admit model {model!r}")
        return ProviderProfile(**{**self.__dict__, "model_code": model})


SubscriptionAdapter = ProviderProfile


DEFAULT_ADAPTERS = (
    ProviderProfile(
        "anthropic",
        "claude",
        "claude",
        "opus",
        "claude_json_envelope",
        (
            "temporary-working-directory",
            "safe-mode",
            "empty-setting-sources",
            "empty-mcp-config",
            "tools-disabled",
            "browser-disabled",
            "session-persistence-disabled",
        ),
        ("provider-managed system context and admin policy are not byte-disclosed",),
        ("compiled-fresh", "research-fresh", "workspace-agent", "native-continuation"),
        supported_models=("opus", "claude-opus-5", "sonnet", "claude-sonnet-4-6"),
    ),
    ProviderProfile(
        "openai",
        "codex",
        "codex",
        "gpt-5.6-sol",
        "output_file",
        (
            "temporary-working-directory",
            "ephemeral-session",
            "user-config-disabled",
            "agent-instruction-file-loading-disabled",
            "hooks-disabled",
            "mcp-disabled",
            "plugins-disabled",
            "apps-disabled",
            "memory-disabled",
            "subagents-disabled",
            "read-only-sandbox",
            "shell-tools-disabled",
        ),
        ("provider-managed system context is not byte-disclosed",),
        ("compiled-fresh", "research-fresh", "workspace-agent", "native-continuation"),
        supported_models=("gpt-5.6-sol",),
    ),
    ProviderProfile(
        "google",
        "antigravity",
        "agy",
        "gemini-3.7-flash-high",
        "antigravity_json_envelope",
        (
            "temporary-home",
            "subscription-state-copied-owner-only",
            "settings-replaced",
            "filesystem-command-mcp-browser-denied",
            "telemetry-disabled",
            "g1-credit-fallback-disabled",
            "sandboxed-plan-mode",
        ),
        ("provider-managed system context and macOS keychain behavior are not byte-disclosed",),
        ("compiled-fresh", "research-fresh"),
        supported_models=("gemini-3.7-flash-high",),
        supported_efforts=("high",),
        capability_compiler="antigravity-isolation/1",
        parser_revision="antigravity-json-envelope/1",
    ),
    ProviderProfile(
        "moonshot",
        "kimi",
        "kimi",
        "kimi-k3",
        "json_stdout",
        ("temporary-working-directory", "tools-disabled", "session-persistence-disabled"),
        ("no official subscription-authenticated local CLI has been qualified",),
        (),
        False,
        "no official subscription-authenticated local CLI has been qualified",
    ),
    ProviderProfile(
        "xai",
        "grok",
        "grok",
        "grok-4.5",
        "json_stdout",
        (
            "temporary-working-directory",
            "tools-disabled",
            "web-disabled",
            "subagents-disabled",
            "memory-disabled",
        ),
        ("provider-managed system context is not byte-disclosed",),
        ("compiled-fresh", "research-fresh"),
        supported_models=("grok-4.5",),
        supported_efforts=("high",),
        capability_compiler="grok-isolation/1",
        parser_revision="grok-structured-output/1",
    ),
)


EXECUTION_PROFILES = {"compiled-fresh", "research-fresh", "workspace-agent", "native-continuation"}
CONTINUATION_MODES = {"resume", "fork"}
SANDBOX_MODES = {"read-only", "workspace-write", "provider-permissions"}
APPROVAL_POLICIES = {"never", "on-request"}
CLAUDE_TOOLS = {
    "Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Read", "Task", "WebFetch", "WebSearch", "Write"
}
CLAUDE_PERMISSION = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:\([^()\r\n,]{1,500}\))?$")


def _cli_version(binary: str) -> str:
    try:
        completed = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20, check=False)
        lines = (completed.stdout + "\n" + completed.stderr).strip().splitlines()
        return lines[0] if lines else "unreported: empty version response"
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"unreported: {error}"


def _codex_capability_probe(binary: str) -> tuple[bool, str]:
    """Confirm the installed CLI still exposes every qualified isolation control."""
    try:
        features = subprocess.run(
            [binary, "features", "list"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        help_result = subprocess.run(
            [binary, "exec", "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"control-surface probe failed: {error}"
    if features.returncode != 0:
        return False, "codex features list failed"
    if help_result.returncode != 0:
        return False, "codex exec --help failed"
    available_features = {
        line.split(maxsplit=1)[0]
        for line in features.stdout.splitlines()
        if line.strip()
    }
    missing_features = sorted(set(CODEX_DISABLED_FEATURES) - available_features)
    help_text = help_result.stdout + "\n" + help_result.stderr
    missing_options = [option for option in CODEX_REQUIRED_EXEC_OPTIONS if option not in help_text]
    if missing_features or missing_options:
        return False, f"missing features={missing_features}; missing exec options={missing_options}"
    return True, "qualified Codex isolation controls are present"


def _claude_capability_probe(binary: str) -> tuple[bool, str]:
    """Confirm the installed Claude CLI still exposes every enforced control."""

    try:
        help_result = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"control-surface probe failed: {error}"
    if help_result.returncode != 0:
        return False, "claude --help failed"
    help_text = help_result.stdout + "\n" + help_result.stderr
    missing_options: list[str] = []
    for option in CLAUDE_REQUIRED_OPTIONS:
        aliases = {option}
        if option.endswith("-file"):
            aliases.add(option.removesuffix("-file") + "[-file]")
        if not any(alias in help_text for alias in aliases):
            missing_options.append(option)
    if missing_options:
        return False, f"missing options={missing_options}"
    return True, "qualified Claude isolation and continuation controls are present"


def installed_adapter_status(
    adapters: tuple[SubscriptionAdapter, ...] = DEFAULT_ADAPTERS,
    *,
    probe_versions: bool = True,
) -> list[dict[str, Any]]:
    binaries = [shutil.which(adapter.executable) for adapter in adapters]
    versions: list[str | None] = [None] * len(adapters)
    if probe_versions:
        present = [(index, binary) for index, binary in enumerate(binaries) if binary]
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(present)))) as executor:
            futures = {index: executor.submit(_cli_version, binary) for index, binary in present}
            for index, future in futures.items():
                versions[index] = future.result()
    result: list[dict[str, Any]] = []
    for adapter, binary, version in zip(adapters, binaries, versions, strict=True):
        binary_sha256 = None
        if binary and Path(binary).is_file():
            digest = hashlib.sha256()
            with Path(binary).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            binary_sha256 = digest.hexdigest()
        operational = None if not probe_versions or not binary else not str(version).startswith("unreported:")
        compatibility: bool | None = None
        compatibility_detail: str | None = None
        if probe_versions and binary and Path(binary).is_file():
            if adapter.provider_code == "openai":
                compatibility, compatibility_detail = _codex_capability_probe(binary)
            elif adapter.provider_code == "anthropic":
                compatibility, compatibility_detail = _claude_capability_probe(binary)
            elif adapter.provider_code == "google":
                compatibility, compatibility_detail = antigravity.capability_probe(binary, model=adapter.model_code, effort="high", allow_web=False)
            elif adapter.provider_code == "xai":
                compatibility, compatibility_detail = grok.capability_probe(binary, model=adapter.model_code, effort="high", allow_web=False)
            if compatibility is not None:
                operational = bool(operational) and compatibility
        result.append({**adapter.definition(), "model_code": adapter.model_code, "binary": binary, "binary_sha256": binary_sha256, "installed": bool(binary), "qualified": bool(binary) and adapter.exact_context_qualified, "operational": operational, "version": version, "control_surface_qualified": compatibility, "control_surface_detail": compatibility_detail})
    return result


def validate_execution_policy(
    adapter: SubscriptionAdapter,
    *,
    execution_profile: str = "compiled-fresh",
    session_policy: str | None = None,
    continuation_mode: str | None = None,
    native_session_id: str | None = None,
    tools: list[str] | tuple[str, ...] = (),
    permissions: list[str] | tuple[str, ...] = (),
    workspace: str | Path | None = None,
    sandbox: str = "read-only",
    approval_policy: str = "never",
    network_policy: str = "disabled",
) -> dict[str, Any]:
    """Admit only execution claims the official harness can enforce and report."""
    if not adapter.exact_context_qualified:
        raise SubscriptionRouteUnavailable(adapter.qualification_blocker or f"{adapter.provider_code} exact-context execution is not qualified")
    if execution_profile not in EXECUTION_PROFILES or execution_profile not in adapter.qualified_profiles:
        raise SubscriptionRouteUnavailable(f"{adapter.provider_code} route has not qualified execution profile {execution_profile}")
    expected_session_policy = "visible_native_continuation" if execution_profile == "native-continuation" else "fresh"
    if session_policy is not None and session_policy != expected_session_policy:
        raise SubscriptionRouteUnavailable(f"{execution_profile} requires session policy {expected_session_policy}")
    if sandbox not in SANDBOX_MODES:
        raise SubscriptionRouteUnavailable(f"unsupported sandbox policy {sandbox}")
    if approval_policy not in APPROVAL_POLICIES:
        raise SubscriptionRouteUnavailable(f"unsupported approval policy {approval_policy}")
    if approval_policy != "never":
        raise SubscriptionRouteUnavailable("interactive approval routing is not yet qualified")
    if network_policy not in {"disabled", "provider_managed", "declared_allowlist"}:
        raise SubscriptionRouteUnavailable(f"unsupported network policy {network_policy}")

    normalized_tools = list(tools)
    normalized_permissions = list(permissions)
    if len(normalized_tools) != len(set(normalized_tools)) or len(normalized_permissions) != len(set(normalized_permissions)):
        raise SubscriptionRouteUnavailable("tools and permissions must be unique")

    resolved_workspace: str | None = None
    if workspace is not None:
        candidate = Path(workspace)
        if not candidate.is_absolute() or not candidate.is_dir():
            raise SubscriptionRouteUnavailable("workspace must be an existing absolute directory")
        resolved_workspace = str(candidate.resolve())

    if execution_profile == "compiled-fresh":
        if normalized_tools or normalized_permissions or resolved_workspace is not None or network_policy != "disabled" or sandbox != "read-only":
            raise SubscriptionRouteUnavailable("compiled-fresh cannot receive tools, permissions, workspace, network, or write authority")
        if continuation_mode is not None or native_session_id is not None:
            raise SubscriptionRouteUnavailable("compiled-fresh cannot carry provider-session continuation state")
    elif execution_profile == "research-fresh":
        if normalized_permissions or resolved_workspace is not None or sandbox != "read-only":
            raise SubscriptionRouteUnavailable("research-fresh cannot receive permissions, workspace, or write authority")
        if continuation_mode is not None or native_session_id is not None:
            raise SubscriptionRouteUnavailable("research-fresh cannot carry provider-session continuation state")
        expected_tools = {
            "anthropic": ["WebSearch", "WebFetch"],
            "openai": ["web_search"],
            "google": ["read_url"],
            "xai": ["web_search", "web_fetch"],
        }.get(adapter.provider_code)
        if expected_tools is None or normalized_tools != expected_tools or network_policy != "provider_managed":
            raise SubscriptionRouteUnavailable(f"{adapter.provider_code} research-fresh requires its exact qualified web-only tool surface")
    else:
        if resolved_workspace is None:
            raise SubscriptionRouteUnavailable(f"{execution_profile} requires an explicit workspace")
        if execution_profile == "native-continuation":
            if continuation_mode not in CONTINUATION_MODES:
                raise SubscriptionRouteUnavailable("native continuation requires an explicit resume or fork mode")
            if not isinstance(native_session_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", native_session_id):
                raise SubscriptionRouteUnavailable("native continuation requires an observed provider session UUID")
        elif continuation_mode is not None or native_session_id is not None:
            raise SubscriptionRouteUnavailable("workspace-agent starts a new provider session and cannot name an ancestor session")

    if adapter.provider_code == "anthropic":
        if execution_profile in {"workspace-agent", "native-continuation"}:
            if sandbox != "provider-permissions":
                raise SubscriptionRouteUnavailable("Claude workspace execution is bounded by explicit provider tool permissions, not a filesystem sandbox")
            unknown = [tool for tool in normalized_tools if tool not in CLAUDE_TOOLS]
            if unknown:
                raise SubscriptionRouteUnavailable(f"Claude tools are not qualified: {', '.join(unknown)}")
            for permission in normalized_permissions:
                if not CLAUDE_PERMISSION.fullmatch(permission):
                    raise SubscriptionRouteUnavailable(f"Claude permission is not in the admitted grammar: {permission}")
                base = permission.split("(", 1)[0]
                if base not in normalized_tools:
                    raise SubscriptionRouteUnavailable(f"Claude permission {permission} enables an undeclared tool")
            network_capable = bool({"Bash", "WebFetch", "WebSearch"} & set(normalized_tools))
            if network_capable and network_policy != "provider_managed":
                raise SubscriptionRouteUnavailable("Claude network-capable tools require an explicit provider-managed network policy")
            if not network_capable and network_policy != "disabled":
                raise SubscriptionRouteUnavailable("Claude cannot claim network authority without a network-capable tool")
    elif adapter.provider_code == "openai":
        if sandbox == "provider-permissions":
            raise SubscriptionRouteUnavailable("Codex requires its explicit read-only or workspace-write sandbox")
        if normalized_permissions:
            raise SubscriptionRouteUnavailable("Codex permissions are expressed by sandbox and approval policy, not free-form permission strings")
        if execution_profile in {"workspace-agent", "native-continuation"} and normalized_tools != ["shell"]:
            if normalized_tools:
                raise SubscriptionRouteUnavailable("Codex workspace execution admits only its sandboxed shell tool surface")
            raise SubscriptionRouteUnavailable("Codex workspace execution must declare its sandboxed shell tool surface")
        if execution_profile == "compiled-fresh" and normalized_tools:
            raise SubscriptionRouteUnavailable("compiled-fresh disables Codex tools")
        if network_policy == "declared_allowlist":
            raise SubscriptionRouteUnavailable("Codex does not expose an exact host allowlist on this route")
    elif execution_profile not in {"compiled-fresh", "research-fresh"}:
        raise SubscriptionRouteUnavailable(f"{adapter.provider_code} workspace and native-session execution are not qualified")

    if execution_profile == "compiled-fresh":
        enforcement = list(adapter.isolation)
    elif execution_profile == "research-fresh":
        enforcement = [
            *adapter.isolation,
            "fresh-provider-session",
            "declared-web-only-tool-surface",
            "provider-managed-network",
            "workspace-context-disabled",
        ]
    elif adapter.provider_code == "anthropic":
        enforcement = [
            "safe-mode",
            "empty-setting-sources",
            "empty-mcp-config",
            "browser-disabled",
            "explicit-workspace",
            "declared-provider-tool-surface",
            "declared-provider-permissions",
            "provider-session-persistence",
        ]
    else:
        enforcement = [
            "user-config-disabled",
            "agent-instruction-file-loading-disabled",
            "hooks-disabled",
            "mcp-disabled",
            "plugins-disabled",
            "apps-disabled",
            "memory-disabled",
            "subagents-disabled",
            "explicit-workspace",
            f"sandbox-{sandbox}",
            f"approval-{approval_policy}",
            "declared-provider-tool-surface",
            "provider-session-persistence",
        ]
    if execution_profile == "native-continuation":
        enforcement.append(f"provider-session-{continuation_mode}")
    return {
        "execution_profile": execution_profile,
        "session_policy": expected_session_policy,
        "continuation_mode": continuation_mode,
        "native_session_id": native_session_id,
        "tools": normalized_tools,
        "permissions": normalized_permissions,
        "workspace": resolved_workspace,
        "sandbox": sandbox,
        "approval_policy": approval_policy,
        "network_policy": network_policy,
        "enforcement": enforcement,
    }


def build_command(
    adapter: SubscriptionAdapter,
    *,
    binary: str,
    isolated: Path,
    schema_path: Path,
    output_path: Path,
    effort: str,
    prompt: str,
    system_prompt: str | None = None,
    effective_policy: dict[str, Any] | None = None,
) -> tuple[list[str], str | None, dict[str, Path]]:
    # Command construction is independently testable for declared-but-unqualified
    # adapters. Invocation always validates before reaching this boundary.
    policy = effective_policy or {
        "execution_profile": "compiled-fresh",
        "continuation_mode": None,
        "native_session_id": None,
        "tools": [],
        "permissions": [],
        "workspace": None,
        "sandbox": "read-only",
    }
    if adapter.provider_code == "anthropic":
        system_prompt_path = None
        if system_prompt is not None:
            system_prompt_path = isolated / "system-prompt.txt"
            system_prompt_path.write_text(system_prompt, encoding="utf-8")
            os.chmod(system_prompt_path, 0o600)
        command = [
                binary,
                "--print",
                "--model",
                adapter.model_code,
                "--effort",
                effort,
                "--output-format",
                "json",
                "--json-schema",
                schema_path.read_text(encoding="utf-8"),
                "--safe-mode",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                json.dumps({"mcpServers": {}}, separators=(",", ":")),
                "--disable-slash-commands",
                "--tools",
                ",".join(policy["tools"]),
                "--allowedTools",
                ",".join(policy["permissions"]),
                "--permission-mode",
                "dontAsk",
                "--no-chrome",
            ]
        if system_prompt_path is not None:
            command.extend(["--system-prompt-file", str(system_prompt_path)])
        if policy["execution_profile"] == "compiled-fresh":
            command.append("--no-session-persistence")
        elif policy["execution_profile"] == "native-continuation":
            command.extend(["--resume", policy["native_session_id"]])
            if policy["continuation_mode"] == "fork":
                command.append("--fork-session")
        return command, prompt, {}
    if system_prompt is not None:
        raise SubscriptionRouteUnavailable(f"{adapter.provider_code} official CLI has no qualified separate system-instruction channel")
    if adapter.provider_code == "openai":
        command = [
                binary,
                "--model",
                adapter.model_code,
                "--sandbox",
                policy["sandbox"],
                "--cd",
                policy["workspace"] or str(isolated),
                "--strict-config",
                "--config",
                f'model_reasoning_effort="{effort}"',
                "--config",
                f'approval_policy="{policy.get("approval_policy", "never")}"',
                "--config",
                "project_doc_max_bytes=0",
                "--config",
                "hooks={}",
                "--config",
                "mcp_servers={}",
                "--config",
                "plugins={}",
                "--config",
                "apps={}",
            ]
        for feature in CODEX_DISABLED_FEATURES:
            command.extend(["--disable", feature])
        if policy["execution_profile"] == "compiled-fresh":
            command.extend(["--disable", "shell_tool", "--disable", "unified_exec"])
        command.append("exec")
        if policy["execution_profile"] == "native-continuation":
            command.extend([policy["continuation_mode"], policy["native_session_id"]])
        if policy["execution_profile"] == "compiled-fresh":
            command.append("--ephemeral")
        command.extend([
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ])
        return command, prompt, {"structured-output": output_path}
    if adapter.provider_code == "google":
        raise SubscriptionRouteUnavailable("Antigravity command construction requires its isolated subscription-home boundary")
    if adapter.provider_code == "xai":
        raise SubscriptionRouteUnavailable("Grok command construction requires its isolated subscription-OIDC boundary")
    raise SubscriptionRouteUnavailable(
        f"{adapter.provider_code} has no qualified official subscription CLI adapter"
    )


def classify_provider_failure(returncode: int, stdout: str, stderr: str) -> str:
    text = f"{stdout}\n{stderr}".casefold()
    if any(
        marker in text
        for marker in (
            "fs_permission_denied",
            "operation not permitted",
            "permission denied",
            "invalid policy rule",
            "failed to initialize in-process app-server client",
            "invalid_json_schema",
            "invalid schema for response_format",
        )
    ):
        return "harness_failure"
    if any(
        marker in text
        for marker in (
            "authentication failed",
            "authentication required",
            "login required",
            "log in to continue",
            "please login",
            "please log in",
            "unauthorized",
            "sign in to continue",
            "not logged in",
        )
    ):
        return "authentication"
    if any(marker in text for marker in ("usage balance exhausted", "payment required", "insufficient balance", "insufficient credits")):
        return "subscription_exhausted"
    if "rate limit" in text or "too many requests" in text or "quota" in text:
        return "rate_limit"
    if "policy" in text or "refus" in text or "not allowed" in text:
        return "policy_refusal"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if returncode != 0:
        return "outage"
    return "malformed_output"


def subscription_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build the minimal process environment needed for file- or keychain-backed consumer login.

    Provider backend selectors, proxy routing, developer credentials, and cloud
    account state are excluded by construction. Consumer OAuth may be read from
    the provider's normal config directory or the one explicitly admitted
    Claude Code subscription token.
    """
    source = os.environ if source is None else source
    exact = {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
        "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "NO_COLOR",
        "FORCE_COLOR", "SSL_CERT_FILE", "SSL_CERT_DIR", "XDG_CONFIG_HOME",
        "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "CODEX_HOME",
        "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME", "GROK_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
    return {
        name: value
        for name, value in source.items()
        if name in exact or name.startswith("LC_")
    }


def _strict_json_loads(raw: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON object key: {key}")
            value[key] = child
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)


def _schema_validator(schema: dict[str, Any]) -> Draft202012Validator:
    try:
        encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"response schema is not finite JSON: {error}") from error
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ValueError(f"response schema exceeds {MAX_SCHEMA_BYTES} bytes")
    stack: list[Any] = [schema]
    visited = 0
    while stack:
        value = stack.pop()
        visited += 1
        if visited > MAX_CANDIDATE_NODES:
            raise ValueError("response schema exceeds its structural node limit")
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#"):
                raise ValueError("response schema cannot resolve external references")
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValueError(f"invalid Draft 2020-12 response schema: {error.message}") from error
    return Draft202012Validator(schema)


def _json_candidates(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack: list[Any] = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > MAX_CANDIDATE_NODES:
            raise ValueError("provider JSON envelope exceeds its structural node limit")
        if isinstance(current, dict):
            found.append(current)
            nested_values: list[Any] = []
            for key in ("structured_output", "result", "response", "message", "content"):
                nested = current.get(key)
                if isinstance(nested, str):
                    try:
                        nested_values.append(_strict_json_loads(nested))
                    except (json.JSONDecodeError, ValueError, RecursionError):
                        continue
                elif isinstance(nested, (dict, list)):
                    nested_values.append(nested)
            stack.extend(reversed(nested_values))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return found


def parse_structured_response(
    adapter: SubscriptionAdapter,
    stdout: str,
    output_path: Path,
    schema: dict[str, Any] | None = None,
    max_output_bytes: int = MAX_MODEL_OUTPUT_BYTES,
) -> dict[str, Any]:
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 1:
        raise ValueError("max_output_bytes must be a positive integer")
    stdout_bytes = len(stdout.encode("utf-8"))
    if stdout_bytes > max_output_bytes:
        raise SubscriptionInvocationFailed("output_limit", f"provider stdout exceeded {max_output_bytes} bytes")
    texts: list[str] = []
    if adapter.response_mode == "output_file" and output_path.exists():
        size = output_path.stat().st_size
        remaining = max_output_bytes - stdout_bytes
        if size > remaining:
            raise SubscriptionInvocationFailed("output_limit", f"provider structured-output file exceeded {max_output_bytes} bytes")
        content = output_path.read_bytes()
        if len(content) != size or len(content) > remaining:
            raise SubscriptionInvocationFailed("output_limit", "provider structured-output file changed while being read")
        try:
            texts.append(content.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as error:
            raise SubscriptionInvocationFailed("malformed_output", "provider structured-output file was not UTF-8") from error
    texts.append(stdout)
    candidates: list[dict[str, Any]] = []
    for text in texts:
        try:
            candidates.extend(_json_candidates(_strict_json_loads(text)))
        except (json.JSONDecodeError, ValueError, RecursionError):
            continue
    if not candidates:
        raise SubscriptionInvocationFailed("malformed_output", "provider returned no JSON object")
    if schema is None:
        return candidates[-1]
    validator = _schema_validator(schema)
    last_error = None
    try:
        for candidate in reversed(candidates):
            last_error = next(validator.iter_errors(candidate), None)
            if last_error is None:
                return candidate
    except RecursionError as error:
        raise SubscriptionInvocationFailed("malformed_output", "provider response exceeded the schema validator's structural depth") from error
    detail = (last_error.message if last_error is not None else "no candidate matched")[:500]
    raise SubscriptionInvocationFailed("malformed_output", f"structured response failed its Draft 2020-12 contract: {detail}")


def invoke_subscription(
    adapter: SubscriptionAdapter,
    *,
    prompt: str,
    system_prompt: str | None = None,
    schema: dict[str, Any],
    runs_dir: Path,
    subject: str,
    effort: str = "high",
    timeout: float = 600,
    cancel_event: threading.Event | None = None,
    on_chunk: Callable[[str, bytes], None] | None = None,
    execution_profile: str = "compiled-fresh",
    session_policy: str | None = None,
    continuation_mode: str | None = None,
    native_session_id: str | None = None,
    tools: list[str] | tuple[str, ...] = (),
    permissions: list[str] | tuple[str, ...] = (),
    workspace: str | Path | None = None,
    sandbox: str = "read-only",
    approval_policy: str = "never",
    network_policy: str = "disabled",
) -> tuple[dict[str, Any], Path]:
    total_input_bytes = len(prompt.encode("utf-8")) + (len(system_prompt.encode("utf-8")) if system_prompt is not None else 0)
    if total_input_bytes > MAX_MODEL_INPUT_BYTES:
        raise ValueError(f"model input exceeds {MAX_MODEL_INPUT_BYTES} bytes")
    if system_prompt is not None and adapter.provider_code != "anthropic":
        raise SubscriptionRouteUnavailable(f"{adapter.provider_code} official CLI has no qualified separate system-instruction channel")
    _schema_validator(schema)
    effective_policy = validate_execution_policy(
        adapter,
        execution_profile=execution_profile,
        session_policy=session_policy,
        continuation_mode=continuation_mode,
        native_session_id=native_session_id,
        tools=tools,
        permissions=permissions,
        workspace=workspace,
        sandbox=sandbox,
        approval_policy=approval_policy,
        network_policy=network_policy,
    )
    if effort not in adapter.supported_efforts:
        raise SubscriptionRouteUnavailable(f"{adapter.provider_code} profile does not admit effort {effort!r}")
    binary = shutil.which(adapter.executable)
    if not binary:
        raise SubscriptionRouteUnavailable(f"official {adapter.executable} CLI is not installed")
    journal = create_run_journal(
        adapter.harness,
        subject=subject,
        model=adapter.model_code,
        effort=effort,
        timeout=timeout,
        runs_dir=runs_dir,
    )
    with tempfile.TemporaryDirectory(prefix=f"lattice-model-{adapter.provider_code}-") as temp:
        isolated = Path(temp)
        schema_path = isolated / "response.schema.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        output_path = isolated / "structured-output.json"
        execution_cwd = Path(effective_policy["workspace"]) if effective_policy["workspace"] else isolated
        execution_environment = subscription_environment()
        if adapter.provider_code == "google":
            allow_web = effective_policy["execution_profile"] == "research-fresh"
            qualified, detail = antigravity.capability_probe(binary, model=adapter.model_code, effort=effort, allow_web=allow_web)
            if not qualified:
                raise SubscriptionRouteUnavailable(detail)
            command, input_text, execution_cwd, execution_environment, observed_files = antigravity.prepare(
                binary=binary,
                root=isolated,
                model=adapter.model_code,
                effort=effort,
                prompt=prompt,
                schema=schema,
                allow_web=allow_web,
                timeout=timeout,
                log_path=journal.root / "driver.log",
            )
        elif adapter.provider_code == "xai":
            allow_web = effective_policy["execution_profile"] == "research-fresh"
            qualified, detail = grok.capability_probe(binary, model=adapter.model_code, effort=effort, allow_web=allow_web)
            if not qualified:
                raise SubscriptionRouteUnavailable(detail)
            command, input_text, execution_cwd, execution_environment, observed_files = grok.prepare(
                binary=binary,
                root=isolated,
                model=adapter.model_code,
                effort=effort,
                prompt=prompt,
                schema=schema,
                allow_web=allow_web,
            )
        else:
            command, input_text, observed_files = build_command(
                adapter,
                binary=binary,
                isolated=isolated,
                schema_path=schema_path,
                output_path=output_path,
                effort=effort,
                prompt=prompt,
                system_prompt=system_prompt,
                effective_policy=effective_policy,
            )
        bind_request(
            journal,
            prompt=prompt,
            system_prompt=system_prompt,
            response_format="json-schema",
            resume_strategy=adapter.harness,
            tool_policy="isolated-subscription-cli",
            metadata={
                "adapter": adapter.definition(),
                "schema": schema,
                "binary": binary,
                "cli_version": _cli_version(binary),
                "isolation": {
                    "working_directory": "explicit-workspace" if effective_policy["workspace"] else "fresh-temporary",
                    "working_directory_path": effective_policy["workspace"] or str(isolated),
                    "environment_policy": "minimal-consumer-subscription",
                    "guarantees": list(effective_policy["enforcement"]),
                },
                "effective_policy": effective_policy,
                "system_instruction_channel": "official-system-prompt-file" if system_prompt is not None else None,
            },
        )
        try:
            result = run_bounded(
                command,
                timeout=timeout,
                input_text=input_text,
                cwd=execution_cwd,
                env=execution_environment,
                stdout_journal=journal.stdout,
                stderr_journal=journal.stderr,
                observed_files=observed_files,
                cancel_event=cancel_event,
                on_chunk=on_chunk,
            )
        except subprocess.TimeoutExpired as error:
            finish_run_journal(journal, "timeout", failure_kind="timeout")
            raise SubscriptionInvocationFailed(
                "timeout", f"{adapter.provider_code} timed out", journal=journal.root
            ) from error
        except KeyboardInterrupt as error:
            finish_run_journal(journal, "interrupted", failure_kind="operator_interruption")
            raise SubscriptionInvocationFailed(
                "operator_interruption", "operator interrupted model call", journal=journal.root
            ) from error
        except ProcessCancelled as error:
            finish_run_journal(journal, "interrupted", failure_kind="operator_interruption")
            raise SubscriptionInvocationFailed(
                "operator_interruption", "operator cancelled model call", journal=journal.root
            ) from error
        except ProcessOutputLimit as error:
            finish_run_journal(journal, "failed", failure_kind="output_limit", max_output_bytes=error.max_output_bytes)
            raise SubscriptionInvocationFailed(
                "output_limit", f"{adapter.provider_code} exceeded the model output limit", journal=journal.root
            ) from error
        except OSError as error:
            finish_run_journal(journal, "failed", failure_kind="harness_failure")
            raise SubscriptionInvocationFailed(
                "harness_failure",
                f"{adapter.provider_code} process could not start: {error}",
                journal=journal.root,
            ) from error
        if result.returncode != 0:
            kind = classify_provider_failure(result.returncode, result.stdout, result.stderr)
            finish_run_journal(journal, "failed", failure_kind=kind, returncode=result.returncode)
            raise SubscriptionInvocationFailed(
                kind,
                f"{adapter.provider_code} CLI exited {result.returncode}",
                journal=journal.root,
            )
        try:
            if adapter.provider_code == "google":
                payload = antigravity.parse_output(result.stdout)
                validation_error = next(_schema_validator(schema).iter_errors(payload), None)
                if validation_error is not None:
                    raise SubscriptionInvocationFailed("malformed_output", f"structured response failed its Draft 2020-12 contract: {validation_error.message[:500]}")
            elif adapter.provider_code == "xai":
                payload = grok.parse_output(result.stdout)
                validation_error = next(_schema_validator(schema).iter_errors(payload), None)
                if validation_error is not None:
                    raise SubscriptionInvocationFailed("malformed_output", f"structured response failed its Draft 2020-12 contract: {validation_error.message[:500]}")
            else:
                payload = parse_structured_response(adapter, result.stdout, output_path, schema)
        except (SubscriptionInvocationFailed, antigravity.AntigravityBoundaryError, grok.GrokBoundaryError) as error:
            if not isinstance(error, SubscriptionInvocationFailed):
                error = SubscriptionInvocationFailed("malformed_output", str(error))
            finish_run_journal(journal, "failed", failure_kind=error.kind)
            error.journal = journal.root
            raise error
        finish_run_journal(journal, "success")
        return payload, journal.root
