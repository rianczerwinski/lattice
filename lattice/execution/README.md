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

Process ownership covers the whole same-session process group, not just its leader or standard pipes. Normal completion, cancellation, deadlines, and observation/setup failures all end that ownership before retiring the parent-death watchdog. Cleanup escalates to the group even when its leader has exited, and runs once after a successful stop. Sidecar read/projection failures remain visible while the provider is stopped; they cannot skip cleanup. This is not a sandbox for detached processes or a rollback of actions already performed.

## Bidirectional process input

`run_bounded` accepts either one `input_text` value or a thread-safe `input_messages` FIFO of byte messages, never both. An empty FIFO keeps stdin open; `None` closes it. This permits a provider to request input after receiving the task. The same guardian, raw output journals, output cap, deadline, and cancellation controls apply. Input has a 64 MiB cumulative byte cap by default, checked before each queued message is delivered. A request already cancelled at entry never launches a process.

This is transport, not an approval policy: a queued response is not a native acknowledgement, and the caller still owns message framing, exact-action authorization, durable decisions, and response interpretation. Current production adapters continue to reject interactive approval policies until the complete route is qualified. Native channel and real-pipe evidence are retained under [`assurance/native-approvals-20260912T093656/`](../../assurance/native-approvals-20260912T093656/README.md).

## Official SDK transport

`GuardedSDKTransport` implements the pinned `claude-agent-sdk==0.2.152` transport interface over the same guarded process runner. The SDK owns control messages and permission responses; this asyncio transport owns strict UTF-8 JSON-line framing, durable input acceptance, and process lifetime. The project installs the SDK's Python source distribution; callers supply an explicit, separately qualified native executable. The SDK is not an alternate tool loop or a permission bypass.

Input has both the cumulative byte cap and a 64-entry FIFO, with one entry reserved for EOF. Writers wait for capacity without blocking shutdown; empty strings consume no entries. Input is appended and synced before enqueue. A successful `write` return means the complete message was accepted into the queue, not delivered natively. `stdin.accepted.log` alone cannot establish that claim: a journal failure can leave a partial tail that was never enqueued. Invalid frames and journal failures stop the process. Ordinary SDK reader cancellation does not: the SDK cancels its reader before the transport sends EOF, so normal disconnect retains a graceful-exit interval.

Callers must close the transport on every path and inspect its `failure` and terminal `result` before claiming successful native completion. A successful SDK initialization or an absence of messages is not a successful process result. The transport is asyncio-specific; other AnyIO backends, slow-storage responsiveness, native action enforcement, and installed interactive routing are not qualified by its local tests.

`NativeApprovalCallback` connects the SDK's `can_use_tool` interface to the existing exact-action operator ledger. Run it on the provider worker's asyncio loop, separate from the HTTP loop, and share its cancellation event with the transport. It requests an action, waits through read snapshots, and consumes only a recorded one-use operator decision. Allow responses use persisted arguments, not mutable caller input; native suggestions cannot save rules. A cancelled native callback withdraws only its unreleased action. Cancellation observed after a committed release suppresses the response without restoring replayable authority.

The adapter must finish SDK callback finalizers and call `raise_if_failed()` before reporting success: the SDK can encode a callback exception as a control response and otherwise continue. Callback errors are accumulated independently and signal process cancellation. Synchronous ledger work inherits database/storage delays; the 100 ms polling interval is not a cancellation-latency bound. The callback is an internal integration component, not an enabled production approval route.

## Claude system-instruction channel

Claude exposes its official system-prompt file surface. Exact system instructions are written to a temporary file, journaled and hashed independently from the user-message bytes, and passed only as a file path in process arguments. Other providers reject a separate system channel until their official CLIs expose and pass qualification for an equivalent surface.

## Claude research permissions

The fixed `research-fresh` profile binds exactly `WebSearch` and `WebFetch`; callers cannot add general permission strings, workspace access, or write authority. The adapter compiles those declared tools into both Claude's availability list and its native permission allowlist under `dontAsk`, with session persistence disabled. Availability is not authorization: the official [CLI contract](https://code.claude.com/docs/en/cli-reference) distinguishes `--tools` from `--allowedTools`.

A native terminal receipt denying either declared web tool becomes `tool_permission_denied`, even if Claude also returns valid domain JSON. The original response remains in the native journal, but it cannot be published as a successful review or ordinary source-evidence hold. Denials of undeclared tools remain protective boundary behavior; similarly named fields inside model payloads are only data. The permanent checks in [`test_claude_research.py`](../../tests/test_claude_research.py) cover command compilation, terminal/journal propagation, public execution state, and idempotent replay. Installed live tool use remains a separate qualification obligation.

Research uses Claude's verbose JSON output so the native journal retains tool requests and tool results alongside the terminal receipt. Exactly one final native result supplies the domain answer; earlier source/tool turns cannot substitute for completion. Aggregate API usage counters do not establish CLI tool execution: qualify actual research against paired native tool-use and tool-result events.

## Muse response-contract channel

Muse Code exposes prompt-file input and machine-readable JSONL events but no native response-schema flag. Its adapter preserves the original Lattice context as `prompt.txt`, deterministically appends the Draft 2020-12 schema contract in `provider-prompt.txt`, binds both files in `request.json`, and passes only the temporary provider-prompt path to Muse. The final object is accepted only from the single completed terminal event and is then validated by Lattice against the original schema.
