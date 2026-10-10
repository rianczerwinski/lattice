# Recoverable-conversation execution status

## Current boundary

Packet A baseline and independent storage expectations are retained. Packet B storage foundation is implemented: additive schema 16, immutable rounds/checkpoints, idempotent admission, transactional completion, explicit adoption, and pending steering. Packet C streaming and observation admission are in progress; D–F remain pending. Packet exit gates are not yet closed: process-kill admission/dispatch tests and continuation through round twenty require the execution service. No runtime installation, authenticated qualification, or pilot has been performed.

## Fixed implementation choices

Thin conversations reference existing invocations and artifacts; episodes remain overlapping concern views. Requests persist before dispatch. Completion checkpoints and accepted responses commit together. Native provider state is optional for portable recovery. Native qualification never follows from a transcript alone.

## Independent expectations

Ten completed rounds retain identical response hashes and dispatch counts after interruption of round eleven and continuation through twenty. Conflicting idempotency bytes and concurrent heads are rejected. Failed attempts preserve requests, responses, pending steering, and exposed evidence. Missing observation bytes, wrong native heads, unknown mutating effects, authority expansion, silent history omission, stale runtime receipts, and model substitution must fail the respective gate. A replacement operator knows only the owning-task reference.

## Evidence

Storage foundation committed as `c09501013fa332112ebece88da19b9da26ff4792`; exact-path commit verified on `main`.

- `storage-tests.xml`: 8 passed; command `.venv/bin/python -m pytest -q tests/test_recoverable_conversations.py --junitxml=assurance/recoverable-conversations/storage-tests.xml`. Direct SQLite/hash oracles cover ten completed rounds plus an interrupted eleventh, concurrent replay, transactional completion rollback, steering, immutability, adoption and reconstructed schema-15 migration. This is not yet the real copied-store upgrade qualification.
- `storage-boundary-tests.xml`: 146 passed; command `.venv/bin/python -m pytest -q tests/test_kernel.py tests/test_reliability.py tests/test_supervisor.py --junitxml=assurance/recoverable-conversations/storage-boundary-tests.xml`. Executed against storage foundation before observation supervisor changes.
- `streaming-tests.xml`: 53 passed; command `.venv/bin/python -m pytest -q tests/test_claude_research.py tests/test_sdk_runner.py --junitxml=assurance/recoverable-conversations/streaming-tests.xml`. Claude stream-json/verbose retains noninteractive authority and strict terminal validation. Live route qualification remains open.

- `observation-tests.xml`: 18 passed; command `.venv/bin/python -m pytest -q tests/test_observation_recovery.py tests/test_supervisor.py --junitxml=assurance/recoverable-conversations/observation-tests.xml`. Includes split UTF-8/JSON, replay conflicts/gaps, persisted-but-unindexed reconciliation, source/session distinction and existing supervisor behavior. Real daemon-kill and separate-root restore remain unqualified.

- `exposed-stream-tests.xml`: 71 passed after suffix-indexing optimization; command `.venv/bin/python -m pytest -q tests/test_observation_recovery.py tests/test_claude_research.py tests/test_sdk_runner.py tests/test_supervisor.py --junitxml=assurance/recoverable-conversations/exposed-stream-tests.xml`.

## Resume

Read this record, the governing plan and current code. Inspect Git changes without discarding unrelated work. Current entry point: conversation dispatch/recovery manifests and operators. Raw observation bytes now persist with channel offsets; normalized observations reference exact source bytes. Supervisor records the immutable journal binding before launch and reconciles unfinished journals on restart. Indexing reads only the unindexed suffix; actual process-kill and separate-root restore qualification remain open. Conversation atomic batch binding and virtual recovery context are being implemented in `operations.py`, `store.py`, and `conversations.py`; those edits are not yet qualified. Commit usable packets; append evidence and the next exact boundary here. Do not mark completion until installed Claude/Fable 5.1 and Codex recovery, separate-root restore, browser/CLI acceptance, adversarial checks, and the real Relata pilot pass.
