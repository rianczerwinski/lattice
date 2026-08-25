"""Codex provider-specific execution boundary."""

CODEX_DISABLED_FEATURES = ("hooks", "apps", "plugins", "memories", "multi_agent", "multi_agent_v2")
CODEX_REQUIRED_EXEC_OPTIONS = (
    "--strict-config", "--ignore-user-config", "--ignore-rules", "--output-schema",
    "--output-last-message", "--ephemeral", "--json",
)
