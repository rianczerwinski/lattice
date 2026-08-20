# Loom — Surface

surface-id: surface:a286cf0c-70e5-5fef-9570-a095f6ba9e25 · source-revision: 2495f35b0c83080b975180a1145aa4bd10367e0d · generated: 20260820T014500
view-artifact: artifact:0b5bc52f-b27d-5145-9c8c-4c6cc4febc08 · role: derived-view · parent-artifact: artifact:a286cf0c-70e5-5fef-9570-a095f6ba9e25
source-surface: SURFACE.json · parent-sha256: 5be0be7d3b1fdf150c74cab83d1993acc6aebb9c21fe6e2a201fd364735481d0 · renderer: surface-renderer/2.0.1

## 1 · Durable lattice kernel — Versioned contracts, append-only events, content-addressed context, graph operations, and independent manifest reconstruction. [1✓]
- ✓ **shipped** — Loom persists an append-only invocation lattice whose current database, joins, context, and lineage can be reconstructed exactly from the ledger and content-addressed blobs. _(sources: loom/store.py)_

## 2 · Execution and authority boundary — Loopback API authority and leased subscription-CLI execution with durable failure state. [1✓]
- ✓ **shipped** — Claude and Codex execution is isolated, leased, journaled, cancellable, restart-aware, artifact-preserving, and bound to observable CLI/runtime identity with explicit provider proof limits. _(sources: loom/supervisor.py)_

## 3 · Evidence and workflow integrations — Octopus exact-source import, governed context modules, workflow packs, and the quaternary boundary. [1✓]
- ✓ **partial** — Octopus import, governed context modules, executable quaternary transitions, Slate/Axial projections, and read-only Platform Foundry reuse work; German native-role qualification remains externally blocked. _(sources: loom/octopus_import.py, tests/test_workflows.py)_

## 4 · Response-first cockpit — The Svelte conversational surface and its compiled same-origin assets. [1✓]
- ✓ **shipped** — The response-first cockpit operates arbitrary Claude/Codex routing, forks and joins, exact preflight and retrospective context inspection, sanitized responses, artifacts, attempts, local graph views, and workflow blackboards. _(sources: web/src/App.svelte)_

## 5 · Packaging and assurance — Install metadata, dependency locks, and truth-typed regression, property, contract, and oracle tests. [1✓]
- ✓ **partial** — Truth-typed tests, clean source and wheel builds, mutation controls, and a complete initial audit-remediation ledger exist; the new frozen-revision closure audit remains to be issued. _(sources: tests/test_kernel.py)_

## Derived views

### is
- 1 Durable lattice kernel — **shipped**: Loom persists an append-only invocation lattice whose current database, joins, context, and lineage can be reconstructed exactly from the ledger and content-addressed blobs.
- 2 Execution and authority boundary — **shipped**: Claude and Codex execution is isolated, leased, journaled, cancellable, restart-aware, artifact-preserving, and bound to observable CLI/runtime identity with explicit provider proof limits.
- 3 Evidence and workflow integrations — **partial**: Octopus import, governed context modules, executable quaternary transitions, Slate/Axial projections, and read-only Platform Foundry reuse work; German native-role qualification remains externally blocked.
- 4 Response-first cockpit — **shipped**: The response-first cockpit operates arbitrary Claude/Codex routing, forks and joins, exact preflight and retrospective context inspection, sanitized responses, artifacts, attempts, local graph views, and workflow blackboards.
- 5 Packaging and assurance — **partial**: Truth-typed tests, clean source and wheel builds, mutation controls, and a complete initial audit-remediation ledger exist; the new frozen-revision closure audit remains to be issued.

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
