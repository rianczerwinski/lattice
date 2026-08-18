# Loom — Surface

surface-id: surface:9a2a02eb-827a-5f93-ab1a-f68b44d9f1f9 · source-revision: f297b55e3a7a648c07d5609faf9859a61947e584 · generated: 20260818T150000
view-artifact: artifact:a4ff5070-50a8-5d02-8bbe-933b3924645a · role: derived-view · parent-artifact: artifact:9a2a02eb-827a-5f93-ab1a-f68b44d9f1f9
source-surface: SURFACE.json · parent-sha256: f3f6c4a4cc93769a0f717542f24faaa95b52a123b66e0e2bf2b14b4cbfe4a104 · renderer: surface-renderer/2.0.1

## 1 · Durable lattice kernel — Versioned contracts, append-only events, content-addressed context, graph operations, and independent manifest reconstruction. [1✓]
- ✓ **shipped** — Loom persists an append-only invocation lattice with content-addressed context and independently reconstructable sealed manifests. _(sources: loom/store.py)_

## 2 · Execution and authority boundary — Loopback API authority and leased subscription-CLI execution with durable failure state. [1✓]
- ✓ **partial** — Claude and Codex execution is leased, journaled, restart-aware, and terminalized, while complete runtime influence and artifact inspection remain incomplete. _(sources: loom/supervisor.py)_

## 3 · Evidence and workflow integrations — Octopus exact-source import, governed context modules, workflow packs, and the quaternary boundary. [1✓]
- ✓ **partial** — Octopus import, governed context modules, workflow bootstrap, and quaternary state inspection exist; the German workflow remains calibrating rather than closed. _(sources: loom/octopus_import.py)_

## 4 · Response-first cockpit — The Svelte conversational surface and its compiled same-origin assets. [1✓]
- ✓ **partial** — The response-first cockpit operates manual multi-parent routing and preflight, but its exact-context presentation remains incomplete. _(sources: web/src/App.svelte)_

## 5 · Packaging and assurance — Install metadata, dependency locks, and truth-typed regression, property, contract, and oracle tests. [1✓]
- ✓ **partial** — Truth-typed automated tests and clean build metadata exist; the independent whole-system audit is not part of the frozen source baseline. _(sources: tests/test_kernel.py)_

## Derived views

### is
- 1 Durable lattice kernel — **shipped**: Loom persists an append-only invocation lattice with content-addressed context and independently reconstructable sealed manifests.
- 2 Execution and authority boundary — **partial**: Claude and Codex execution is leased, journaled, restart-aware, and terminalized, while complete runtime influence and artifact inspection remain incomplete.
- 3 Evidence and workflow integrations — **partial**: Octopus import, governed context modules, workflow bootstrap, and quaternary state inspection exist; the German workflow remains calibrating rather than closed.
- 4 Response-first cockpit — **partial**: The response-first cockpit operates manual multi-parent routing and preflight, but its exact-context presentation remains incomplete.
- 5 Packaging and assurance — **partial**: Truth-typed automated tests and clean build metadata exist; the independent whole-system audit is not part of the frozen source baseline.

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
