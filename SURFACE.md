# Loom — Surface

surface-id: surface:7060869c-9dc2-5a54-bdc3-7d21ffe7f7f1 · source-revision: bc4133e10cac48816159c542fb0c1f1e9924e454 · generated: 20260821T223000
view-artifact: artifact:f79f9277-04de-5cc1-8e7e-0fea6eaef7f3 · role: derived-view · parent-artifact: artifact:7060869c-9dc2-5a54-bdc3-7d21ffe7f7f1
source-surface: SURFACE.json · parent-sha256: d50e9f51aa28d31168f7371653e3c2a35c07f1a72ccf05ff96a36a1e6b6dec3d · renderer: surface-renderer/2.0.1

## 1 · Durable lattice kernel — Versioned contracts, append-only events, content-addressed context, graph operations, and independent manifest reconstruction. [1✓]
- ✓ **shipped** — Loom durably stores a hash-chained append-only lattice, seals immutable context manifests with compare-and-swap, reconstructs locally observable context independently, and exposes bounded replay, diff, slice, and neighborhood operations. _(sources: loom/store.py)_

## 2 · Execution and authority boundary — Loopback API authority and leased subscription-CLI execution with durable failure state. [1✓]
- ✓ **shipped** — Qualified subscription CLIs execute through leased, restart-safe attempts with bounded journaled I/O, authority-bound manifests, typed failures, and explicit provider-owned opacity. _(sources: loom/supervisor.py)_

## 3 · Evidence and workflow integrations — Octopus exact-source import, governed context modules, workflow packs, and the quaternary boundary. [1✓]
- ✓ **partial** — Octopus exact-source import, declarative governed workflows, quaternary and Platform Foundry live projections, Slate slices, and Axial projection execute; complete authenticated German native-role acceptance remains unproven. _(sources: loom/octopus_import.py, tests/test_workflows.py)_

## 4 · Response-first cockpit — The Svelte conversational surface and its compiled same-origin assets. [1✓]
- ✓ **shipped** — The same-origin cockpit supports manual one-edge routing, fork and join trailheads, exact preflight and manifest views, governed live views, reusable path discovery, and explicit partial, empty, unavailable, and failure states. _(sources: web/src/App.svelte)_

## 5 · Packaging and assurance — Install metadata, dependency locks, and truth-typed regression, property, contract, and oracle tests. [1✓]
- ✓ **partial** — The installable wheel includes the cockpit and workflow packs, and the full local assurance suites pass; a fresh immutable post-remediation audit is still being issued. _(sources: tests/test_kernel.py)_

## Derived views

### is
- 1 Durable lattice kernel — **shipped**: Loom durably stores a hash-chained append-only lattice, seals immutable context manifests with compare-and-swap, reconstructs locally observable context independently, and exposes bounded replay, diff, slice, and neighborhood operations.
- 2 Execution and authority boundary — **shipped**: Qualified subscription CLIs execute through leased, restart-safe attempts with bounded journaled I/O, authority-bound manifests, typed failures, and explicit provider-owned opacity.
- 3 Evidence and workflow integrations — **partial**: Octopus exact-source import, declarative governed workflows, quaternary and Platform Foundry live projections, Slate slices, and Axial projection execute; complete authenticated German native-role acceptance remains unproven.
- 4 Response-first cockpit — **shipped**: The same-origin cockpit supports manual one-edge routing, fork and join trailheads, exact preflight and manifest views, governed live views, reusable path discovery, and explicit partial, empty, unavailable, and failure states.
- 5 Packaging and assurance — **partial**: The installable wheel includes the cockpit and workflow packs, and the full local assurance suites pass; a fresh immutable post-remediation audit is still being issued.

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
