# Lattice execution kernel

This package is the sole operational subscription-model execution authority. Provider adapters compile official consumer-CLI commands; journals, checkpoints, and process bounds stay provider-neutral.

## Recovery contract

A caller resumes only on an explicit request. It loads the checkpoint, verifies the prompt binding and immutable request packet, and chooses:

1. `native` — continue the official provider session by ID using the same isolated provider state;
2. `portable` — start a fresh isolated session with the original task plus the mechanically projected observable work;
3. `auto` — use native continuation when its identity and state survive, otherwise portable continuation.

Neither mode reconstructs hidden model cognition. Both preserve externally manifested work.

## Journal and checkpoint rules

- `stdout.log`, `stderr.log`, and `progress.jsonl` are append-only evidence. They are never rewritten or compacted.
- `continuation.json` is a derived view of the checkpoint and observable stream events, not a model-authored status report.
- Every checkpoint update mints a new generated-object identity, points to its predecessor, and remains bound to the exact initial prompt and, when present, the exact provider-compiled prompt and system-instruction bytes.
- Historical checkpoints may still name producer `axis-model-runner`. Loaders verify request, prompt, and system-prompt hashes rather than producer identity. The checkpoint schema name remains `axis-model-run-checkpoint` so those journals stay readable.

## Bounds

Provider adapters admit at most 64 MiB of input and 64 MiB of combined provider output and observed sidecar bytes by default. The output budget is reserved before bytes reach journals, checkpoints, live callbacks, or in-memory capture. An overrun terminates the process and becomes a typed `output_limit` failure.

## Claude system-instruction channel

Claude exposes its official system-prompt file surface. Exact system instructions are written to a temporary file, journaled and hashed independently from the user-message bytes, and passed only as a file path in process arguments. Other providers reject a separate system channel until their official CLIs expose and pass qualification for an equivalent surface.

## Claude research permissions

The fixed `research-fresh` profile binds exactly `WebSearch` and `WebFetch`; callers cannot add general permission strings, workspace access, or write authority. The adapter compiles those declared tools into both Claude's availability list and its native permission allowlist under `dontAsk`, with session persistence disabled. Availability is not authorization: the official [CLI contract](https://code.claude.com/docs/en/cli-reference) distinguishes `--tools` from `--allowedTools`.

A native terminal receipt denying either declared web tool becomes `tool_permission_denied`, even if Claude also returns valid domain JSON. The original response remains in the native journal, but it cannot be published as a successful review or ordinary source-evidence hold. Denials of undeclared tools remain protective boundary behavior; similarly named fields inside model payloads are only data. The permanent checks in [`test_claude_research.py`](../../tests/test_claude_research.py) cover command compilation, terminal/journal propagation, public execution state, and idempotent replay. Installed live tool use remains a separate qualification obligation.

## Muse response-contract channel

Muse Code exposes prompt-file input and machine-readable JSONL events but no native response-schema flag. Its adapter preserves the original Lattice context as `prompt.txt`, deterministically appends the Draft 2020-12 schema contract in `provider-prompt.txt`, binds both files in `request.json`, and passes only the temporary provider-prompt path to Muse. The final object is accepted only from the single completed terminal event and is then validated by Lattice against the original schema.
