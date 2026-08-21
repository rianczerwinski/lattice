# Loom — Surface

surface-id: surface:a13913a0-618e-59d4-a800-81458d10aecd · source-revision: b7c1af32042bae234f1eb56332d658feaca0741c · generated: 20260821T160714
view-artifact: artifact:bb125555-a377-5322-836f-76f8c1961416 · role: derived-view · parent-artifact: artifact:a13913a0-618e-59d4-a800-81458d10aecd
source-surface: SURFACE.json · parent-sha256: 046ac286fff0b002691853ddf1f1c555191e9c82572766be371db4d7dbf5866b · renderer: surface-renderer/2.0.1

## 1 · Durable lattice kernel — Versioned contracts, append-only events, content-addressed context, graph operations, and independent manifest reconstruction. [1✓]
- ✓ **partial** — Loom persists an append-only invocation lattice, but cross-project topic slices, replay fidelity, semantic diffing, and extracted-artifact durability are presently partial. _(sources: loom/store.py)_

## 2 · Execution and authority boundary — Loopback API authority and leased subscription-CLI execution with durable failure state. [1✓]
- ✓ **partial** — Claude and Codex invocations are leased and journaled, but the observable execution contract is partial because declared session, workspace, tool, permission, and network policies are not yet executed faithfully. _(sources: loom/supervisor.py)_

## 3 · Evidence and workflow integrations — Octopus exact-source import, governed context modules, workflow packs, and the quaternary boundary. [1✓]
- ✓ **partial** — Octopus import, governed workflow packs, Slate/Axial projection, and Platform Foundry inspection exist; import idempotency and the complete German quaternary workflow remain partial. _(sources: loom/octopus_import.py, tests/test_workflows.py)_

## 4 · Response-first cockpit — The Svelte conversational surface and its compiled same-origin assets. [1✓]
- ✓ **partial** — The cockpit supports manual Claude/Codex forks, joins, routing, and inspection, while governed workflow operation and runtime-policy truth remain partial. _(sources: web/src/App.svelte)_

## 5 · Packaging and assurance — Install metadata, dependency locks, and truth-typed regression, property, contract, and oracle tests. [1✓]
- ✓ **partial** — Build and test machinery exists, but the prior closure audit is not independent evidence of completion and material redteam findings remain active. _(sources: tests/test_kernel.py)_

## Derived views

### is
- 1 Durable lattice kernel — **partial**: Loom persists an append-only invocation lattice, but cross-project topic slices, replay fidelity, semantic diffing, and extracted-artifact durability are presently partial.
- 2 Execution and authority boundary — **partial**: Claude and Codex invocations are leased and journaled, but the observable execution contract is partial because declared session, workspace, tool, permission, and network policies are not yet executed faithfully.
- 3 Evidence and workflow integrations — **partial**: Octopus import, governed workflow packs, Slate/Axial projection, and Platform Foundry inspection exist; import idempotency and the complete German quaternary workflow remain partial.
- 4 Response-first cockpit — **partial**: The cockpit supports manual Claude/Codex forks, joins, routing, and inspection, while governed workflow operation and runtime-policy truth remain partial.
- 5 Packaging and assurance — **partial**: Build and test machinery exists, but the prior closure audit is not independent evidence of completion and material redteam findings remain active.

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
