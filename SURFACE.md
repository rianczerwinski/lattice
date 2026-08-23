# Lattice — Surface

surface-id: surface:01d9da84-55f0-4df1-8282-9bc3f19b4b18 · source-revision: 736506e91b5e16ebe8f7572a46cfad736405a192 · generated: 20260823T173404
view-artifact: artifact:e2bdbbdb-3e18-5888-a867-15bddb18cb7c · role: derived-view · parent-artifact: artifact:01d9da84-55f0-4df1-8282-9bc3f19b4b18
source-surface: SURFACE.json · parent-sha256: 798f35ecf4b8ad551440f4ef10b1a58b25e30773be23dad2916acc3e2dcb50f9 · renderer: surface-renderer/2.0.1

## 1 · Durable lattice kernel — Versioned contracts, append-only events, content-addressed context, graph operations, and independent manifest reconstruction. [1✓]
- ✓ **shipped** — Lattice durably stores a hash-chained append-only lattice, seals immutable context manifests with compare-and-swap, reconstructs locally observable context independently, and exposes bounded replay, diff, slice, and neighborhood operations. _(sources: lattice/store.py)_

## 2 · Execution and authority boundary — Loopback API authority and leased subscription-CLI execution with durable failure state. [1✓]
- ✓ **shipped** — Qualified subscription CLIs execute through leased, restart-safe attempts with bounded journaled I/O, authority-bound manifests, typed failures, and explicit provider-owned opacity. _(sources: lattice/supervisor.py)_

## 3 · Evidence and workflow integrations — Octopus exact-source import, governed context modules, workflow packs, and the quaternary boundary. [1✓]
- ✓ **partial** — Octopus exact-source import, declarative governed workflows, quaternary and Platform Foundry live projections, Slate slices, and Axial projection execute; complete authenticated German native-role acceptance remains unproven. _(sources: lattice/octopus_import.py, tests/test_workflows.py)_

## 4 · Response-first cockpit — The Svelte conversational surface and its compiled same-origin assets. [1✓]
- ✓ **shipped** — The same-origin cockpit supports manual one-edge routing, fork and join trailheads, exact preflight and manifest views, governed live views, reusable path discovery, and explicit partial, empty, unavailable, and failure states. _(sources: web/src/App.svelte)_

## 5 · Packaging and assurance — Install metadata, dependency locks, and truth-typed regression, property, contract, and oracle tests. [1✓]
- ✓ **partial** — The 0.1.0 wheel includes the Lattice package, cockpit, and workflow packs, with truth-typed Python and web assurance; the frozen whole-system audit predates the rename and therefore remains historical rather than current-revision assurance. _(sources: tests/test_kernel.py, audit/20260821T223204-pagani-remediation/audit.validation.json)_

## Derived views

### is
- 1 Durable lattice kernel — **shipped**: Lattice durably stores a hash-chained append-only lattice, seals immutable context manifests with compare-and-swap, reconstructs locally observable context independently, and exposes bounded replay, diff, slice, and neighborhood operations.
- 2 Execution and authority boundary — **shipped**: Qualified subscription CLIs execute through leased, restart-safe attempts with bounded journaled I/O, authority-bound manifests, typed failures, and explicit provider-owned opacity.
- 3 Evidence and workflow integrations — **partial**: Octopus exact-source import, declarative governed workflows, quaternary and Platform Foundry live projections, Slate slices, and Axial projection execute; complete authenticated German native-role acceptance remains unproven.
- 4 Response-first cockpit — **shipped**: The same-origin cockpit supports manual one-edge routing, fork and join trailheads, exact preflight and manifest views, governed live views, reusable path discovery, and explicit partial, empty, unavailable, and failure states.
- 5 Packaging and assurance — **partial**: The 0.1.0 wheel includes the Lattice package, cockpit, and workflow packs, with truth-typed Python and web assurance; the frozen whole-system audit predates the rename and therefore remains historical rather than current-revision assurance.

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
- **staleness** — `{"reason": "The whole-system audit is bound to the pre-rename source revision; current rename assurance comes from the tracked regression suite, build checks, and package inspection rather than a replacement architectural audit.", "source_id": "source:f333a39f-3890-565c-82cb-93805cfab6c6"}`
