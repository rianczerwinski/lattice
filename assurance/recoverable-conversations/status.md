# Recoverable-conversation execution status

## Current boundary

Packet A baseline and independent storage expectations are retained. Packet B storage and Packet C incremental streaming foundations are committed. Exact portable continuation, native-boundary eligibility, atomic execution admission and Packet D API/client/CLI/cockpit controls are implemented in the working tree and undergoing shared-boundary verification. No installed-runtime upgrade, authenticated recovery qualification or Fable 5.1 pilot has been performed. Native tests use explicit synthetic qualification fixtures and do not qualify real provider routes.

## Fixed implementation choices

Thin conversations reference existing invocations and artifacts; episodes remain overlapping concern views. Requests persist before dispatch. Completion checkpoints and accepted responses commit together. Native provider state is optional for portable recovery. Native qualification never follows from a transcript alone.

## Independent expectations

Ten completed rounds retain identical response hashes and dispatch counts after interruption of round eleven and continuation through twenty. Conflicting idempotency bytes and concurrent heads are rejected. Failed attempts preserve requests, responses, pending steering, and exposed evidence. Missing observation bytes, wrong native heads, unknown mutating effects, authority expansion, silent history omission, stale runtime receipts, and model substitution must fail the respective gate. A replacement operator knows only the owning-task reference.

## Evidence

Exposed stream admission committed as `f885acab6e660f2d129108e91181b857258000d3`; exact-path commit verified on `main`.

Storage foundation committed as `c09501013fa332112ebece88da19b9da26ff4792`; exact-path commit verified on `main`.

- `storage-tests.xml`: 8 passed; command `.venv/bin/python -m pytest -q tests/test_recoverable_conversations.py --junitxml=assurance/recoverable-conversations/storage-tests.xml`. Direct SQLite/hash oracles cover ten completed rounds plus an interrupted eleventh, concurrent replay, transactional completion rollback, steering, immutability, adoption and reconstructed schema-15 migration. This is not yet the real copied-store upgrade qualification.
- `storage-boundary-tests.xml`: 146 passed; command `.venv/bin/python -m pytest -q tests/test_kernel.py tests/test_reliability.py tests/test_supervisor.py --junitxml=assurance/recoverable-conversations/storage-boundary-tests.xml`. Executed against storage foundation before observation supervisor changes.
- `streaming-tests.xml`: 53 passed; command `.venv/bin/python -m pytest -q tests/test_claude_research.py tests/test_sdk_runner.py --junitxml=assurance/recoverable-conversations/streaming-tests.xml`. Claude stream-json/verbose retains noninteractive authority and strict terminal validation. Live route qualification remains open.

- `observation-tests.xml`: 18 passed; command `.venv/bin/python -m pytest -q tests/test_observation_recovery.py tests/test_supervisor.py --junitxml=assurance/recoverable-conversations/observation-tests.xml`. Includes split UTF-8/JSON, replay conflicts/gaps, persisted-but-unindexed reconciliation, source/session distinction and existing supervisor behavior. Real daemon-kill and separate-root restore remain unqualified.

- `exposed-stream-tests.xml`: 71 passed after suffix-indexing optimization; command `.venv/bin/python -m pytest -q tests/test_observation_recovery.py tests/test_claude_research.py tests/test_sdk_runner.py tests/test_supervisor.py --junitxml=assurance/recoverable-conversations/exposed-stream-tests.xml`.

- `recovery-tests.xml`: 34 passed, one shared regression fixture failed because a new optional keyword was passed to its narrow monkeypatch. The adapter call was repaired; `recovery-repair-tests.xml` then passed 9 checks. The 34 passes include twenty-round continuity with an independent dispatch log and unchanged first-ten hashes, exact 501-observation recovery, stale preview rejection, unknown-effect refusal and atomic batch rollback.
- `portable-restore-tests.xml`: 1 passed; a snapshot restored to another root preserves exact state and seals portable continuation with an empty provider home. This synthetic test is not installed-runtime restore qualification.

## Additional source evidence

- `conversation-source-tests.xml` and `operator-recovery-tests.xml`: each reports 18 passing API/storage/continuation/native fixture checks. The latter predates the additional dispatch-rejection and native-bypass fixes.
- `effect-bypass-tests.xml`: missing observation-index recovery passes; the new low-level native bypass fixture initially stopped at an earlier missing-output compositor guard. `effect-bypass-repair-tests.xml` uses explicit context to reach the intended dispatch guard and passes.
- `dispatch-rejection-tests.xml`: pre-attempt dispatch refusal leaves the request in `needs_attention`, retains a failed invocation and permits a new preview; one pass.
- Frontend `npm --prefix web run check`: zero errors/warnings. `npm --prefix web run test`: 52 checks in 17 files pass. `npm --prefix web run build`: built successfully. These are source checks, not installed-runtime evidence.
- Playwright: the four existing journeys passed. The child journey initially failed on a wrong test selector; after repair `npm --prefix web run test:browser -- --grep 'durable child'` passed saved draft, reading-position restoration, steering, interruption and continuation. The complete five-journey browser suite passed after the client idempotency change (22.9 seconds).
- `crash-shared-boundary-tests.xml`: 154 passed in 239.23 seconds, produced by `.venv/bin/python -m pytest -q tests/test_conversation_crash_boundaries.py tests/test_supervisor.py tests/test_kernel.py tests/test_reliability.py --junitxml=assurance/recoverable-conversations/crash-shared-boundary-tests.xml`. The eight new cases SIGKILL a spawned process before/after admission, sealing, completion and observation commit; inspect base rows/blobs independently; replay lost acknowledgement; reconstruct unindexed admitted bytes. The observation-loss mutation was rejected at the intended base-record assertion (`0 != 1`), with the SIGKILL boundary reached; the patch and failing JUnit output are retained as `observation-loss-mutation.patch` and `observation-loss-mutant.xml`.

## Remaining obligations and entry point

The shared-boundary and browser checks passed; the latest focused continuation/operator suite is running. Commit usable continuation/operator changes with an exact allowlist; exclude inherited Surface history and native-approval scratch evidence. Expand native qualification from receipt declarations to independently checked, runtime-bound evidence before admitting any real native recovery. Complete actual CLI operator journeys, lost-ack/browser failure checks, process-owned execution/daemon restart/sibling cancellation, copied canonical-store migration and negative-control potency checks. Route context-limit behavior and uncertain-effect handling require adversarial review. Then build/install from committed source, qualify exact authenticated Fable 5.1 and Codex routes including interruption and separate-root restore, perform the Relata pilot, reconcile Surface and close only against independent evidence. Claude remains 2.1.241; the latest netgate check permits transfer on unmetered Wi-Fi. No provider update has run.
