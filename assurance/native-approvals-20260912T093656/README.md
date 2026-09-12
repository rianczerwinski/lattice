# Native approval conformance

Bounded native-interface observations for Canon's English completion plan, E1. This directory preserves raw local protocol evidence; it does not certify interactive approvals or enable an execution route.

`control_preflight.py` sends exactly one SDK-shaped `initialize` control request to the installed, unmodified Claude CLI with empty tool and permission lists, safe mode, empty settings sources, strict empty MCP configuration, and session persistence disabled. It sends no user message or model task. The operation is bounded to 30 seconds and 1 MiB of combined output. Its input, executable digest, code revision, command, raw stdout/stderr, and terminal observation are retained before any later conclusion. A successful initialization is only protocol compatibility evidence, not proof that approval, denial, cancellation, or persistence works.

The initial source revision is `bbd9849be4b6074ad274da7ffb7a85a1978bcf4d`. The official Python SDK's [permission configuration](https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/src/claude_agent_sdk/types.py) selects `--permission-prompt-tool stdio`; its [query transport](https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/src/claude_agent_sdk/_internal/query.py) sends an initialization request over newline-delimited JSON. This is a diagnostic use of the observed native channel, not a decision to reimplement the SDK's production protocol.

Retain every file for native-version conformance and failure diagnosis. Never rerun into an occupied observation directory; an interrupted observation must be inspected before any successor is launched. No source credentials are copied or recorded, and no developer API route is configured.

## Observations and limits

- `start.log`, `stdout.log`, `stderr.log`, and `terminal.log` retain the initial no-model exchange. Claude returned a matching successful control response, `current_permission_mode=default`, and `session_state=idle`; stderr was empty and the process exited zero in about half a second. This is not authentication or tool-approval proof.
- `duplex-tests.xml` and `duplex-final-tests.xml` retain the two 91-check source runs around a writer-teardown refinement. `transport-final-tests.xml` retains the subsequent 131-check affected execution/research/verifier suite. These are local process/contract checks, not live provider qualification.
- `duplex_mutation.py` recreates an in-memory premature-EOF defect and uses the permanent real-pipe test's exact-response assertion as oracle. `duplex-mutation.log` and its final successor retain source/test digests and the intended assertion rejection. The temporary fixture directory is disposable because only planted pipe data is generated; the decisive failure and reproduction inputs remain here.
- `pre-cancel-before.xml` records a separate observed gap: cancellation already set before `run_bounded` still reached process spawn. The final test rejects any such spawn before returning `ProcessCancelled`. This repairs the pre-entry condition, not an atomic guarantee that cancellation can undo an action already begun.

## Process ownership evidence

The independent [ownership review](ownership-review.md) identified two inherited failures, retained as [process-group](process-group-finding.json) and [sidecar-cleanup](sidecar-cleanup-finding.json) findings. `ownership-before.xml` contains five intended assertion failures: redirected TERM-resistant descendants continued after cancellation, timeout, or normal leader exit; sidecar read/projection errors left the provider or watchdog alive. The fixtures clean up only their exact, recorded process groups even when assertions fail.

`ownership-after.xml` and `ownership-host-after.xml` preserve the intermediate duplicate-signal failure: a second group kill raised `PermissionError` after successful termination, both inside and outside the outer sandbox. Idempotent stop removes the redundant signal without suppressing a real failed group kill. `ownership-idempotent-after.xml` records 75 passing tests; `ownership-transport-tests.xml` records the expanded 139-test affected suite. `inflight-writer-tests.xml` records all 13 duplex tests after strengthening the in-flight write fixture.

`full-source-tests.xml` contains the earlier 553-test run before the ownership repair. `full-owned-source-input.json` binds the repaired source and final test files; `full-owned-source-tests/` retains the command, live log and terminal status; `full-owned-source-tests.xml` records **561 passed**, zero failed or skipped, and two dependency deprecation warnings in 477.31 seconds. These are local source-suite results, not installed/native approval evidence. The wrapper's `alive` probe can itself be denied by the outer sandbox; its terminal status, log, and harness process result must be inspected before inferring that an operation needs relaunch.

`inflight-eof-mutation.log` and `inflight-input-bound-mutation.log` bind the final source/test bytes and retain rejection of premature stdin closure and input-cap enforcement after delivery. Earlier mutation logs preserve their respective earlier code/test bindings. The input-cap oracle observes the actual write boundary so child scheduling cannot hide delivery of an over-budget frame. The binary burst fixture checks FIFO order, raw non-UTF-8 bytes, and the exact input-cap boundary. The in-flight fixture waits for the child to consume one byte and verifies that the parent write has not returned before cancellation; it does not claim syscall-level scheduling introspection.

The same-group cleanup follows Python's [process-group signal interface](https://docs.python.org/3/library/os.html#os.killpg), independently of the direct child's `wait()` result. It cannot contain a descendant that escapes the group or reverse work already performed. All three findings, including [pre-cancelled launch](pre-cancel-finding.json), retain their broader native/supervisor/installed obligations rather than inheriting closure from this suite.

The input FIFO uses Python's [synchronized queue contract](https://docs.python.org/3/library/queue.html), with a bounded wait so process shutdown does not depend on a caller supplying a sentinel. It does not infer completion from `empty()` or supply its own concurrency primitive.

Production SDK integration, native denial/cancellation/stale-delivery conformance, route isolation, installed qualification, and the complete E1 gate remain open. The exact-action ledger/cockpit checkpoint is described below. No model prompt has been dispatched by these observations.

## Pinned SDK initialization

`sdk_preflight.py` uses the official `ClaudeSDKClient` and its public `Transport` interface; the SDK, not Lattice, handles initialization and permission protocol frames. The existing guarded process runner owns the installed `/opt/homebrew/bin/claude` process. The SDK [documents the transport seam as low-level and version-sensitive](https://code.claude.com/docs/en/agent-sdk/python), so this observation is pinned, not a claim of cross-version compatibility.

The initial isolated development environment used `claude-agent-sdk==0.2.152` from the published source distribution, SHA-256 `0822bbdf700ccdd1acecf8365345a56bde07833afe7f8bd1bee68cba0e2fcf9a`. Its package versions are retained in `sdk-dependencies.json`; the editable Lattice dependency uses the source bindings in the observation. That initial observation changed no bundled CLI, primary application dependency, installed runtime, credential configuration, or global permission setting. The ignored environment is reconstructible from the package versions and retained install commands; its purpose is compatibility diagnosis.

`sdk-install/` and `sdk-project-deps/` preserve successful environment setup. `sdk-setup-failure.log` preserves the first missing-project-dependency failure before any native process or observation home was created. `sdk-init-20260912T102000/` contains the subsequent exact command, executable and process-source hashes, raw input/output, and terminal observation: native initialization succeeded in 0.507 seconds, permission mode was `default`, session state was `idle`, and exit status was zero. It sent **zero model prompts** and requested **zero tools**. This proves only SDK/native initialization for these bindings, not authentication, action approval, sandbox enforcement, or production integration.

The source application subsequently added the same pinned SDK dependency. [The guarded SDK transport record](sdk-transport-review.md) preserves setup, the independently examined transport defects, repairs, source bindings, and native observations. The initialization helper uses that implementation rather than a second diagnostic copy. The current production approval gate remains closed.

## Exact-action ledger and cockpit

The append-only approval events in `lattice/native_approvals.py` reuse Lattice's existing ledger without a schema migration. Protected inspection and decision endpoints bind a one-action allow/deny decision to an exact native callback and supervisor-owned attempt; native callbacks cannot create operator decisions. Cancellation, withdrawal, terminality, and a fixed monotonic lifetime invalidate pending authority. The cockpit shows the action and target, exact arguments and invocation/workspace binding, explicit allow/deny/cancel controls, request failures, and the distinction between a saved decision, adapter release, and actual execution. No native permission rule is saved. These controls remain behind the unqualified production route.

[The independent ledger review](ledger-review.md) preserves reviewed source hashes, successive findings, repairs, inspected boundaries, and proof limits. Both material findings remain contained-not-closed until native and installed obligations are discharged. The following local observations are retained distinctly:

| Evidence | Observation and limit |
|---|---|
| `ledger-first-tests.xml` | Nineteen setup failures caused by a missing explicit direct actor in the fixture; no approval behavior was exercised. |
| `ledger-second-tests.xml`, `ledger-api-tests.xml` | Eighteen passed and one wrong HTTP-status expectation; then the selected API test passed with the actual protected-boundary status, 403. |
| `ledger-review-before.xml`, `ledger-review-after.xml` | Five intended expiry/cancellation assertion failures; the first repair then passed 24 local cases. |
| `ledger-affected-tests.xml` | Nonexistent selector; no selected-test coverage. |
| `ledger-affected-final-tests.xml` | 191 passed before the final monotonic-clock repair; not evidence for the final clock implementation. |
| `ledger-clock-before.xml`, `ledger-monotonic-tests.xml` | Two intended mutable-clock assertion failures, then 29 passing final-module cases. |
| `ledger-ignore-binding-final.json`, `ledger-repeat-release.json` | Final-source in-memory false-binding and duplicate-release mutations rejected at their intended assertions. Earlier binding receipts retain their earlier source identities. |
| `ledger-final-affected-input.json`, `ledger-final-affected-tests/`, `ledger-final-affected-tests.xml` | Final-source affected-suite input binding and incremental log/status: 193 passed, zero failed/skipped, one dependency deprecation warning, 204.63 seconds. This also reruns the unchanged baseline after both in-memory mutations. |
| `ledger-web-unit-tests.json` | All 52 web unit tests passed; no browser/provider integration claim. |
| `ledger-browser-first.json` | Outer sandbox denied the local preview listener with EPERM before browser execution. |
| `ledger-browser-host.json`, `ledger-browser-final.json` | The selected real-browser test passed in both approved host runs, using wholly synthetic API responses. It rejected rendered HTML injection, surfaced an HTTP 503, sent an exact-action denial, and showed an honest adapter-release state. |

The final browser screenshots live under `ledger-browser-final/`. Visual inspection confirmed the exact target and three action controls are above the long argument details; a sticky composer occupies the lower viewport. This is a scoped desktop rendering observation, not a full responsive/accessibility or native-provider qualification. The final Svelte check reported zero errors/warnings and the production build succeeded. The first browser render remains for diagnosis of the control-placement refinement.

Every listed observation has a continuing regression, diagnosis, or source-binding function. Temporary planted databases belong to the test runner and are disposable; retained reports and raw native journals are not. To repeat a mutation, run `ledger_mutation.py` with `ignore-binding` or `repeat-release` and a new output path; it refuses an occupied output and changes only an in-memory module. SDK/native qualification must use a separately bounded observation and may not infer authority from this ledger's green tests.

## SDK callback integration

[The SDK callback record](sdk-callback-review.md) binds the exact-action ledger to the pinned SDK callback and synthetic native control channel. It retains the observed cancellation-during-I/O defect, its independent re-review, permanent failure-sensitive checks, and false mutations. No production adapter or policy admission is enabled by these local results.
