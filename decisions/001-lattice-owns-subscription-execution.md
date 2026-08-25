# ADR 001: Lattice owns subscription-model execution

**Status:** Accepted

## Decision

Lattice is the sole active operational authority for subscription-authenticated model execution. The provider-neutral process, checkpoint, journal, retry, redaction, receipt, and recovery kernel lives inside `lattice.execution`; Claude, Codex, Antigravity, and Grok are immutable provider-profile adapters behind that boundary.

Every external caller submits an `ExecutionSubmission/1.0` identified by project, operation, provider profile, prompt/context references, output contract, and idempotency key. A singleton call is one Lattice invocation node automatically. Related calls may be connected as a batch or workflow without exposing draft graph ceremony to ordinary callers. Identical retries reuse the existing invocation; changed bytes under the same operation identity fail.

Domain projects own prompts, research semantics, source processing, validation, selection policy, and domain closure. They may store Lattice identities, hashes, and their own dispositions, but they do not launch provider processes, duplicate execution journals, or reinterpret transport failures locally. Historical project journals remain in place and enter Lattice through immutable byte censuses rather than rewritten imports.

Official consumer-subscription CLI surfaces are the only provider routes. Google execution uses Antigravity through `agy`; no developer API or obsolete Gemini CLI fallback exists. Unsupported or unqualified providers remain unavailable.

## Consequences

Provider behavior, credential handling, retries, and receipts can no longer diverge silently between projects. Canon and field-survey gain the same recoverable execution identity without moving their epistemic policies into Lattice. Public installation no longer depends on an AXISVI sibling package. A provider-profile, adapter, package-byte, or binary change invalidates the relevant runtime evidence and requires qualification rather than inheriting an earlier claim.

The former `axis/tools/model-runner` package may be removed only after the installed Lattice runtime, all active consumers, historical evidence resolution, rollback rehearsal, and the independent consolidation verifier pass together.

## Verification boundary

Source tests cannot close this decision. `ops/verify_execution_consolidation.py` independently reads the installed runtime manifest, provider and execution base tables, content-addressed blobs, raw runtime receipts, consumer source census, historical journal files, and exact credential canaries. Only that evidence may emit `lattice_execution_ready`.
