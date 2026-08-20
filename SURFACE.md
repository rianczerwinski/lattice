# Loom — Surface

surface-id: surface:5c9ba5a1-d4db-5e1e-bb30-c68e14386429 · source-revision: 144baaa53725adae972303096cb6122f44a14340 · generated: 20260820T115100
view-artifact: artifact:b183ab88-5abf-5817-b75f-bd541fa1c1d7 · role: derived-view · parent-artifact: artifact:5c9ba5a1-d4db-5e1e-bb30-c68e14386429
source-surface: SURFACE.json · parent-sha256: 6d4078bc51e41466e4042c6daf19eae3fa6bc8c1a726a3f2fd139ad4bff53c94 · renderer: surface-renderer/2.0.1

## 1 · Durable lattice kernel — Versioned contracts, append-only events, content-addressed context, graph operations, and independent manifest reconstruction. [1✓]
- ✓ **shipped** — Loom persists an append-only invocation lattice whose current database, joins, context, and lineage can be reconstructed exactly from the ledger and content-addressed blobs. _(sources: loom/store.py)_

## 2 · Execution and authority boundary — Loopback API authority and leased subscription-CLI execution with durable failure state. [1✓]
- ✓ **shipped** — Claude and Codex execution is isolated, leased, journaled, cancellable, restart-aware, artifact-preserving, and bound to observable CLI/runtime identity with explicit provider proof limits. _(sources: loom/supervisor.py)_

## 3 · Evidence and workflow integrations — Octopus exact-source import, governed context modules, workflow packs, and the quaternary boundary. [1✓]
- ✓ **partial** — Octopus import, governed modules, typed route-bound workflow packs, generic trigger actions, Slate/Axial projection, and read-only Platform Foundry reuse work; German Gemini qualification remains externally blocked. _(sources: loom/octopus_import.py, tests/test_workflows.py)_

## 4 · Response-first cockpit — The Svelte conversational surface and its compiled same-origin assets. [1✓]
- ✓ **shipped** — The cockpit operates arbitrary Claude/Codex routing while enforcing workflow role-provider boundaries, showing unavailable routes honestly, and exposing exact preflight, retrospective context, artifacts, attempts, graph neighborhoods, and authoritative workflow state. _(sources: web/src/App.svelte)_

## 5 · Packaging and assurance — Install metadata, dependency locks, and truth-typed regression, property, contract, and oracle tests. [1✓]
- ✓ **partial** — Truth-typed tests, clean source and web builds, authenticated manual routing, mutation controls, and an initial audit-remediation ledger exist; a new frozen-revision closure audit remains outstanding. _(sources: tests/test_kernel.py)_

## Derived views

### is
- 1 Durable lattice kernel — **shipped**: Loom persists an append-only invocation lattice whose current database, joins, context, and lineage can be reconstructed exactly from the ledger and content-addressed blobs.
- 2 Execution and authority boundary — **shipped**: Claude and Codex execution is isolated, leased, journaled, cancellable, restart-aware, artifact-preserving, and bound to observable CLI/runtime identity with explicit provider proof limits.
- 3 Evidence and workflow integrations — **partial**: Octopus import, governed modules, typed route-bound workflow packs, generic trigger actions, Slate/Axial projection, and read-only Platform Foundry reuse work; German Gemini qualification remains externally blocked.
- 4 Response-first cockpit — **shipped**: The cockpit operates arbitrary Claude/Codex routing while enforcing workflow role-provider boundaries, showing unavailable routes honestly, and exposing exact preflight, retrospective context, artifacts, attempts, graph neighborhoods, and authoritative workflow state.
- 5 Packaging and assurance — **partial**: Truth-typed tests, clean source and web builds, authenticated manual routing, mutation controls, and an initial audit-remediation ledger exist; a new frozen-revision closure audit remains outstanding.

### doing
- None.

### could
- None.

### constraints
- None.

## Residuals

### Modularize
- **excluded_decorations** — `{"distributed_as": "README supplies product contract and .gitignore supplies local-state boundaries.", "evidence": [".gitignore"], "item_id": "item:20cc4b37-8c07-5063-afff-b3ab3031c1f7"}`
- **excluded_decorations** — `{"distributed_as": "README supplies product contract and .gitignore supplies local-state boundaries.", "evidence": ["README.md"], "item_id": "item:987075b1-a3a6-58b2-9fec-872d881e8424"}`
- **judgment_calls** — `{"alternative": "Home server.py under the interface surface.", "decision": "Keep API hosting with execution rather than cockpit.", "node_ids": ["node:1bed9604-7f0c-589c-a954-1508e9e796f2", "node:d8ccdbad-93a7-58ca-90ef-9064091f3a7c"], "reason": "Its trust, lease, and workflow command boundary dominates its static-hosting role."}`
- **semi_lattice_signals** — `{"item_ids": ["item:f1534b56-d009-59f3-b06a-0a51b13d8e4f"], "node_ids": ["node:1bed9604-7f0c-589c-a954-1508e9e796f2", "node:0123f24e-b8ff-544d-959a-444272f7d0ff", "node:d8ccdbad-93a7-58ca-90ef-9064091f3a7c"], "reason": "The composition root legitimately connects API authority, workflows, and UI hosting while retaining one primary home."}`

### Surface
- None recorded.
