# Lattice — Surface

surface-id: surface:66dc92cf-0852-4a62-ad82-cc382ab87884 · source-revision: 38a8d053336c2c3c516876ad7c1cdd1f714c1d6a · generated: 20260824T182341
view-artifact: artifact:48fd2afa-e538-5ae6-a18b-0649621179d3 · role: derived-view · parent-artifact: artifact:66dc92cf-0852-4a62-ad82-cc382ab87884
source-surface: SURFACE.json · parent-sha256: 3730deca1677ab74ad7dc1354635d5958a57e65849dc83fe1d299c82b834a692 · renderer: surface-renderer/2.0.1

## 1 · Durable lattice kernel — Direct and role-bound actors, atomic invocation batches, exact content-addressed context, overlapping episodes, graph operations, and independent manifest reconstruction. [1✓]
- ✓ **shipped** — Lattice natively authors direct or role-bound conversational graph edges, atomically seals route-specific fan-out batches, preserves exact excerpts and overlapping episodes, and independently reconstructs both full provenance and the provider-safe conversation bytes. _(sources: lattice/store.py)_

## 2 · Execution and authority boundary — Outport-addressed loopback authority, snapshot recovery, and leased subscription-CLI execution with explicit profile qualification and durable failure state. [1✓]
- ✓ **shipped** — Outport authority, leased attempts, typed failure, fresh and workspace execution, guarded native continuation, snapshots, restore, and provider-drift diagnosis execute; semantic Claude and Codex fork, resume, fallback, failure recovery, and restart continuation passed. _(sources: lattice/supervisor.py, file:/Users/rian/Library/Application Support/Lattice/acceptance/reports/20260824T181329-e1b954b1-a7b.json, file:/Users/rian/Library/Application Support/Lattice/runtime/versions/0.2.0-95a67a0486c1-3888c1e62ca3/runtime.json, file:/Users/rian/Library/Application Support/Lattice/backups/20260824T181355-43d1db6a-700/snapshot.json)_

## 3 · Evidence and workflow integrations — Octopus exact-source import, governed context modules, workflow packs, live Slate concern slices, and Axial projections. [1✓]
- ✓ **partial** — Octopus exact-source import, declarative governed workflows, context modules, live concern-backed Slate slices, and Axial projection execute; complete authenticated German native-role acceptance remains unproven. _(sources: lattice/octopus_import.py, tests/test_workflows.py)_

## 4 · Response-first cockpit — The response-first Svelte conversation surface, exact selection actions, inline routing, progressive graph authorship, and isolated secondary drawers. [1✓]
- ✓ **shipped** — The same-origin cockpit supports direct Claude, Codex, or dual routing, side quests, exact grafts, compare, return, cross-feed, joins, dormant trailheads, continuously visible context deltas, and failure-isolated secondary drawers. _(sources: web/src/App.svelte)_

## 5 · Operations and assurance — Pinned installation, launchd supervision, backup and restore, dependency locks, finding ledger, historical audits, and truth-typed regression suites. [1✓]
- ✓ **shipped** — Lattice 0.2.0 runs from a source- and byte-attested pinned environment under launchd, with verified backup and restore, exact-current browser and semantic authenticated provider acceptance, and all 20 remediation findings closed. _(sources: tests/test_reliability.py, assurance/findings.json, file:/Users/rian/Library/Application Support/Lattice/acceptance/reports/20260824T181329-e1b954b1-a7b.json, file:/Users/rian/Library/Application Support/Lattice/runtime/versions/0.2.0-95a67a0486c1-3888c1e62ca3/runtime.json, file:/Users/rian/Library/Application Support/Lattice/backups/20260824T181355-43d1db6a-700/snapshot.json)_

## Derived views

### is
- 1 Durable lattice kernel — **shipped**: Lattice natively authors direct or role-bound conversational graph edges, atomically seals route-specific fan-out batches, preserves exact excerpts and overlapping episodes, and independently reconstructs both full provenance and the provider-safe conversation bytes.
- 2 Execution and authority boundary — **shipped**: Outport authority, leased attempts, typed failure, fresh and workspace execution, guarded native continuation, snapshots, restore, and provider-drift diagnosis execute; semantic Claude and Codex fork, resume, fallback, failure recovery, and restart continuation passed.
- 3 Evidence and workflow integrations — **partial**: Octopus exact-source import, declarative governed workflows, context modules, live concern-backed Slate slices, and Axial projection execute; complete authenticated German native-role acceptance remains unproven.
- 4 Response-first cockpit — **shipped**: The same-origin cockpit supports direct Claude, Codex, or dual routing, side quests, exact grafts, compare, return, cross-feed, joins, dormant trailheads, continuously visible context deltas, and failure-isolated secondary drawers.
- 5 Operations and assurance — **shipped**: Lattice 0.2.0 runs from a source- and byte-attested pinned environment under launchd, with verified backup and restore, exact-current browser and semantic authenticated provider acceptance, and all 20 remediation findings closed.

### doing
- None.

### could
- None.

### constraints
- None.

## Residuals

### Modularize
- **excluded_decorations** — `{"distributed_as": "Local-state and build-residue policy distributed across every module.", "evidence": [".gitignore"], "item_id": "item:20cc4b37-8c07-5063-afff-b3ab3031c1f7"}`
- **excluded_decorations** — `{"distributed_as": "Active remediation state distributed through module overlays and the finding ledger.", "evidence": ["CONVERSATION-FIRST-REMEDIATION-PLAN.md"], "item_id": "item:ac819482-0272-54e2-b539-2c26679a3926"}`
- **excluded_decorations** — `{"distributed_as": "Canonical product and architecture contract distributed across every module.", "evidence": ["README.md"], "item_id": "item:987075b1-a3a6-58b2-9fec-872d881e8424"}`
- **judgment_calls** — `{"alternative": "Home server.py under the interface surface.", "decision": "Keep API hosting with execution rather than cockpit.", "node_ids": ["node:1bed9604-7f0c-589c-a954-1508e9e796f2", "node:d8ccdbad-93a7-58ca-90ef-9064091f3a7c"], "reason": "Its trust, lease, and workflow command boundary dominates its static-hosting role."}`
- **semi_lattice_signals** — `{"item_ids": ["item:f1534b56-d009-59f3-b06a-0a51b13d8e4f"], "node_ids": ["node:1bed9604-7f0c-589c-a954-1508e9e796f2", "node:0123f24e-b8ff-544d-959a-444272f7d0ff", "node:d8ccdbad-93a7-58ca-90ef-9064091f3a7c"], "reason": "The composition root legitimately connects API authority, workflows, and UI hosting while retaining one primary home."}`

### Surface
- **staleness** — `{"reason": "The earlier whole-system audit remains historical assurance at its frozen pre-conversation-first revision; a new immutable-revision audit is published separately from this Surface generation.", "source_id": "source:f333a39f-3890-565c-82cb-93805cfab6c6"}`
