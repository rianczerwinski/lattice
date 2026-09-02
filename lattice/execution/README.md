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
- Every checkpoint update mints a new generated-object identity, points to its predecessor, and remains bound to the exact initial prompt and, when present, the exact system-instruction bytes.
- Historical checkpoints may still name producer `axis-model-runner`. Loaders verify request, prompt, and system-prompt hashes rather than producer identity. The checkpoint schema name remains `axis-model-run-checkpoint` so those journals stay readable.

## Bounds

Provider adapters admit at most 64 MiB of input and 64 MiB of combined provider output and observed sidecar bytes by default. The output budget is reserved before bytes reach journals, checkpoints, live callbacks, or in-memory capture. An overrun terminates the process and becomes a typed `output_limit` failure.

## Claude system-instruction channel

Claude exposes its official system-prompt file surface. Exact system instructions are written to a temporary file, journaled and hashed independently from the user-message bytes, and passed only as a file path in process arguments. Other providers reject a separate system channel until their official CLIs expose and pass qualification for an equivalent surface.
