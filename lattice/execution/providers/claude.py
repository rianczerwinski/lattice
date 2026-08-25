"""Claude Code provider-specific execution boundary."""

CLAUDE_REQUIRED_OPTIONS = (
    "--print", "--model", "--effort", "--output-format", "--json-schema", "--safe-mode",
    "--setting-sources", "--strict-mcp-config", "--mcp-config", "--disable-slash-commands",
    "--tools", "--allowedTools", "--permission-mode", "--no-chrome", "--no-session-persistence",
    "--resume", "--fork-session", "--system-prompt-file",
)
