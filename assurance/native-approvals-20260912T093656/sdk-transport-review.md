# Guarded SDK transport evidence

This record supports a maintainer deciding whether the internal SDK transport checkpoint is usable. It does not authorize an interactive execution route. The official SDK handles permission protocol frames; Lattice retains its process ownership, byte bounds, raw journals, and independent terminal-result check.

## Dependency and native executable

The application pins `claude-agent-sdk==0.2.152`. Its source distribution is SHA-256 `0822bbdf700ccdd1acecf8365345a56bde07833afe7f8bd1bee68cba0e2fcf9a`, 344710 bytes. The [uv no-binary-package setting](https://docs.astral.sh/uv/reference/settings/#no-binary-package) selects the Python source distribution instead of the SDK's platform wheel containing another native CLI. Inspection of the installed `_bundled/` directory found only `.gitignore`, not a CLI executable. `/opt/homebrew/bin/claude` remains the existing 2.1.241 executable. Neither the supervised Lattice runtime nor global credentials/settings were changed.

Setup observations, in order: `uv lock --offline` was first denied access to the global cache by the outer sandbox; the approved host retry lacked cached package metadata. Approved `uv lock` resolved the dependency closure and preserved every existing locked package version. The isolated diagnostic environment lacked pytest, so that attempted test run exercised no behavior. `uv sync --frozen --offline` lacked the cryptography wheel; approved `uv sync --frozen --inexact` then built the SDK source package and synchronized the application environment while preserving unrelated installed packages. These are retained summaries of tool observations, not raw install-log attestations. The resulting exact dependency authority is `uv.lock`.

## Defects and retained observations

- [Input journal failure](sdk-input-journal-finding.json): `sdk-transport-first.xml` records ten passes and the intended cancellation assertion failure when opening the input journal failed. Moving open inside the failure boundary produced eleven passes in `sdk-transport-repaired.xml`.
- [Reader shutdown](sdk-reader-shutdown-finding.json): the real zero-model observation in `sdk-guarded-init-20260912T220800/` initialized successfully but ended with `ProcessCancelled`, so the whole probe failed. `sdk-reader-shutdown-before.xml` reproduced the exact ordering: pinned SDK `Query._close_impl` cancels its reader before `transport.close`. Reader cancellation must not become an operator cancellation signal.
- [Input entry bound](sdk-input-entries-finding.json): `sdk-empty-writes-before.xml` records the intended failure from one hundred empty queue entries consuming zero admitted bytes. Empty writes are now no-ops. The finite queue also prevents small-frame entry overhead from scaling to the cumulative byte allowance; one slot remains available for EOF.

`sdk-lifecycle-repaired.xml` contains thirteen passing local cases. `sdk-backpressure-tests.xml` contains sixteen passing cases, including a controlled stalled consumer released by space, EOF, or cancellation. The synthetic SDK peer checks exact allow/deny response encoding and absence of saved permission rules; it executes no tool. `sdk-unbounded-queue-mutation.json` and its input record bind an in-memory removal of the queue bound and the permanent test's intended assertion rejection. Its pytest plugin-rewrite warning is distinct from that rejection. `sdk_queue_mutation.py` reproduces it only into a new evidence path; planted temporary journals are disposable, while the decisive input and result are retained.

`sdk-guarded-init-20260912T221732/` repeats the fixed, bounded zero-model initialization through the repaired transport: permission mode `default`, session state `idle`, zero tools requested, process exit zero, no process error, 1.580 seconds elapsed. Raw journals, exact command, executable digest, helper/transport/process hashes, and lock hash are retained. This is native initialization and graceful-disconnect evidence only—not authentication, action denial, isolation, or recovery after uncertain delivery.

The repaired observation's `stdout.log` includes personal account metadata. It remains in its original local observation home but is excluded from Git by an exact-path rule; its SHA-256 is `3fdc2496792acd94228d791cc1fddadfa4fcc3e978310e98b14904bb96dcba57`. The private response is needed to reconstruct the full native exchange. Versioned command, terminal, source, and process records remain usable without publishing that account data. No scanner, source implementation, or failure receipt is hidden by this privacy boundary.

## Source bindings and broader check

Candidate base revision: `8c038625822c469a1c3f6455010dc45e12582428`.

| Input | SHA-256 |
|---|---|
| `lattice/execution/sdk_transport.py` | `c270ad5a08efa83912358319225d864201ed683431b72c143fbd92e84adc24e1` |
| `tests/test_sdk_transport.py` | `b4e03f368a3af71a4fa37f5fde3b036a6db7ddcbab239dbaf066683fd1ae405a` |
| `lattice/execution/process.py` | `8bf945ef44c0a9503815f073b535ec2851b7d6cbc09822d47124bcbaeb730f82` |
| `pyproject.toml` | `dd2ef2014f8143a79f94e28a7ab9c9cc91c3e041fd67541f544085947deb8f0c` |
| `uv.lock` | `8a837d04089c26e36c572656e428a1c3b8e81de0cb565aaa906945f4251eaf63` |

The first transport candidate was `c97d0173210f1509eddacc6e18ece8222960088e7c2c50628ad3fb67a57a38cb`; the journal-open repair was `e67008e1355037d01cf7c0536e991215e68fe731bbb794d8a01d20a2cdcf6456`. The first independent review examined that latter source, with test hash `b8a52d95394cf99edf8329eba578e29850d0aa081bac817db7aa7670b9b43e10`, and identified the reader and empty-write defects. The earlier eleven-case test hash was `a8d6e4be8d6c16abc693d5dfb5a5681e12f262dff1ac62ae453c83a2cba5180b`.

`sdk-full-source-input.json` freezes the broader source-suite invocation under this dependency lock. `sdk-full-source-tests/` retains the command, incremental log and terminal status; `sdk-full-source-tests.xml` records **606 passed**, zero failed/skipped, one dependency deprecation warning, 531.38 seconds. The three transport-local findings are closed against their failure-sensitive checks, this unchanged-source full run, and the independent re-review below. This does not close the native approval or installed-route obligations. The prior ledger suite predates this dependency addition and cannot substitute for it.

## Independent source re-review

The separate read-only `english_transport_review` context inspected the final source and tests at the hashes above and found both reader-shutdown and empty-entry defects repaired, with no new consequential lifecycle finding. Its traversal covered the complete transport and test module, process admission/drain/writer/termination paths at lines 154–369, the pinned SDK Transport contract, and `Query._close_impl` ordering at lines 908–965. The SDK transport interface hash was `30b34480e397aef06f43f53277d2f5c4661d41eb7a339b506528e2550a32ef01`; the SDK query implementation was `c24f24ac2cb45a7debd3ba8fb945c65ffca7f1414ee0fb37a49fc500e1afcb8c`.

The reviewer verified the EOF reservation under the actual single-event-loop producer contract: writers serialize, the capacity wait yields before a final readiness check, and no await separates successful journal append from enqueue. Only the process consumer removes entries. `end_input` closes admission and can enqueue its reserved sentinel while a writer waits. The journal-open/write failure boundary signals cancellation; interrupted `close` also signals in `finally`. The reviewer executed no tests or native calls. Test and CLI outcomes above were observed separately by the parent; source review is not a substitute for those results.

## Proof limits

`stdin.accepted.log` alone proves neither acceptance nor delivery: a partial write/fsync failure can leave bytes that were never enqueued. A successful `write` return establishes whole-message queue acceptance only. Callers must inspect transport failure and process exit even after SDK disconnect returns. Bounds cover admitted payload and queue entries, not caller-created pending tasks, temporary encoding/parsing copies, or total resident memory. This implementation requires asyncio. Slow-storage event-loop responsiveness, raw asyncio interruption during close, simultaneous waiting writers, partial input-journal writes/fsync errors, spawn failure, nonzero native exit, and native action isolation require their own evidence before broader claims. The source approval gate is still closed; SDK callback integration and native/installed journeys remain E1 work.
