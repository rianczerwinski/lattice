# Lattice — Ontology

surface-id: surface:de409e10-50d6-5eaf-973f-e0e044b4dc42 · source-revision: 28974dac742eb01b1b1e5ec34432d6de86d2a70c · generated: 20260920T193257
view-artifact: artifact:786d31d1-7768-5274-8a0e-20a470404bf1 · role: derived-view · parent-artifact: artifact:de409e10-50d6-5eaf-973f-e0e044b4dc42
source-surface: SURFACE.json · parent-sha256: ee6f12ae29c4efa34e45d84ce2c061164bc03d3b0532c7bff01681f5c1c169ca · renderer: surface-renderer/2.1.0

## 1 · Durable lattice kernel — Direct and role-bound actors, atomic invocation batches, exact content-addressed context, overlapping episodes, graph operations, and independent manifest reconstruction. [1✓]
- ✓ **shipped** — Lattice natively authors direct or role-bound conversational graph edges, atomically seals route-specific batches, preserves exact context and overlapping episodes, relates durable runs to invocations, and independently reconstructs provider-facing manifests. _(sources: lattice/store.py)_

## 2 · Execution and authority boundary — Outport-addressed loopback authority, snapshot recovery, and leased subscription-CLI execution across Claude, Codex, Antigravity, Grok, and Muse with explicit profile qualification and durable failure state. [1✓]
- ✓ **partial** — Outport authority, launchd supervision, leased attempts, typed failure, Claude/Codex/Antigravity/Grok/Muse fresh execution, guarded native continuation, cancellation, durable-run recovery, snapshots, and fail-closed provider attestation execute; current full authenticated-profile and restored-continuation qualification remains unclosed. _(sources: lattice/supervisor.py, file:/Users/rian/Library/Application Support/Lattice/runtime/versions/0.3.1-26ae0704b88a/runtime.json, file:/Users/rian/Library/Application Support/Lattice/acceptance/execution-closure/20260908T033700-muse-doctor.json, file:/Users/rian/Library/Application Support/Lattice/model-runs/20260908T033635-muse-9f01769d/continuation.json)_

## 3 · Evidence and workflow integrations — Octopus exact-source import, governed context modules, workflow packs, live Slate concern slices, and Axial projections. [1✓]
- ✓ **partial** — Octopus exact-source import, declarative governed workflows, context modules, live concern-backed Slate slices, and Axial projection execute; complete authenticated German native-role acceptance remains unproven. _(sources: lattice/octopus_import.py, tests/test_workflows.py)_

## 4 · Response-first cockpit — The response-first Svelte conversation surface, exact selection actions, inline routing, progressive graph authorship, and isolated secondary drawers. [1✓]
- ✓ **shipped** — The same-origin cockpit supports direct and dual routing, side quests, exact grafts, compare, return, cross-feed, joins, dormant trailheads, latest-wins project navigation, virtualized long responses, visible context deltas, and typed failure surfaces. _(sources: web/src/App.svelte)_

## 5 · Operations and assurance — Runtime installation, qualification evidence, and the verification suite that keep Lattice honest. [4✓ 2◐]
- ✓ **partial** — Lattice 0.3.1 runs from the source- and byte-attested revision 26ae0704b88a under launchd supervision; the current doctor passes runtime, provider, state, Outport, and backup checks, the installed Muse canary succeeds, and formal execution-consolidation closure remains unqualified. _(sources: tests/test_execution_consolidation_verifier.py, file:/Users/rian/Library/Application Support/Lattice/runtime/versions/0.3.1-26ae0704b88a/runtime.json, file:/Users/rian/Library/Application Support/Lattice/backups/20260908T034618-af03b8e7-f92/snapshot.json, file:/Users/rian/Library/Application Support/Lattice/acceptance/execution-closure/20260908T033700-muse-doctor.json)_
- ◐ **open** — Execution consolidation awaits an independently passing current-runtime bundle covering browser, authenticated providers, restart, separate-root restore, all registered consumers, historical imports, negative controls, and final finding closure. _(sources: assurance/execution-consolidation/findings.json, file:/Users/rian/Library/Application Support/Lattice/acceptance/execution-closure/20260908T033700-muse-doctor.json)_
- ◐ **open** — The durable-work program is extending Lattice from conversational provider execution into the ordinary control plane for resumable local jobs, incremental evidence, resource-aware progress, and real-work dogfooding. _(sources: DURABLE-WORK-AND-PRIMARY-UX-PLAN.md, lattice/supervisor.py)_
- 5.1 · Runtime operations — Verified installation, launchd supervision, upgrades, backups, and operator qualification commands. [1✓]
  - ✓ **partial** — Runtime install, supervision, and context-qualification operator commands exist in ops/. _(sources: ops/qualify_context.py)_
- 5.2 · Qualification corpus — Finding-to-proof ledgers, frozen baselines, and timestamped audit engagements. [1✓]
  - ✓ **partial** — Execution-consolidation and later native-approval qualification receipts are materialized under assurance/. _(sources: audit/20260821T223204-pagani-remediation/audit.validation.json)_
- 5.3 · Verification suite — Truth-typed kernel, API, supervisor, workflow, and mutation checks. [1✓]
  - ✓ **partial** — A verification suite covers native approvals, context rendering, process ownership, and SDK transport. _(sources: tests/test_native_approvals.py)_

## Derived views

### is
- 1 Durable lattice kernel — **shipped**: Lattice natively authors direct or role-bound conversational graph edges, atomically seals route-specific batches, preserves exact context and overlapping episodes, relates durable runs to invocations, and independently reconstructs provider-facing manifests.
- 2 Execution and authority boundary — **partial**: Outport authority, launchd supervision, leased attempts, typed failure, Claude/Codex/Antigravity/Grok/Muse fresh execution, guarded native continuation, cancellation, durable-run recovery, snapshots, and fail-closed provider attestation execute; current full authenticated-profile and restored-continuation qualification remains unclosed.
- 3 Evidence and workflow integrations — **partial**: Octopus exact-source import, declarative governed workflows, context modules, live concern-backed Slate slices, and Axial projection execute; complete authenticated German native-role acceptance remains unproven.
- 4 Response-first cockpit — **shipped**: The same-origin cockpit supports direct and dual routing, side quests, exact grafts, compare, return, cross-feed, joins, dormant trailheads, latest-wins project navigation, virtualized long responses, visible context deltas, and typed failure surfaces.
- 5 Operations and assurance — **partial**: Lattice 0.3.1 runs from the source- and byte-attested revision 26ae0704b88a under launchd supervision; the current doctor passes runtime, provider, state, Outport, and backup checks, the installed Muse canary succeeds, and formal execution-consolidation closure remains unqualified.
- 5.1 Runtime operations — **partial**: Runtime install, supervision, and context-qualification operator commands exist in ops/.
- 5.2 Qualification corpus — **partial**: Execution-consolidation and later native-approval qualification receipts are materialized under assurance/.
- 5.3 Verification suite — **partial**: A verification suite covers native approvals, context rendering, process ownership, and SDK transport.

### doing
- 5 Operations and assurance — **open**: Execution consolidation awaits an independently passing current-runtime bundle covering browser, authenticated providers, restart, separate-root restore, all registered consumers, historical imports, negative controls, and final finding closure.
- 5 Operations and assurance — **open**: The durable-work program is extending Lattice from conversational provider execution into the ordinary control plane for resumable local jobs, incremental evidence, resource-aware progress, and real-work dogfooding.

### could
- None.

### constraints
- None.

## Residuals

### Modularize
- **excluded_decorations** — `{"distributed_as": "Workframe policy, product contract, or active-plan state distributed over substantive modules.", "evidence": ["Workframe policy, product contract, or active-plan state distributed over substantive modules."], "item_id": "item:20cc4b37-8c07-5063-afff-b3ab3031c1f7"}`
- **excluded_decorations** — `{"distributed_as": "Packaging or program ledger distributed onto operations.", "evidence": ["Packaging or program ledger distributed onto operations."], "item_id": "item:99f3ddf9-77a1-5a6e-aea8-f15dad9078bd"}`
- **excluded_decorations** — `{"distributed_as": "Workframe policy, product contract, or active-plan state distributed over substantive modules.", "evidence": ["Workframe policy, product contract, or active-plan state distributed over substantive modules."], "item_id": "item:ac819482-0272-54e2-b539-2c26679a3926"}`
- **excluded_decorations** — `{"distributed_as": "Packaging or program ledger distributed onto operations.", "evidence": ["Packaging or program ledger distributed onto operations."], "item_id": "item:579124fa-97bf-5efe-b2b7-f467502c8b22"}`
- **excluded_decorations** — `{"distributed_as": "Workframe policy, product contract, or active-plan state distributed over substantive modules.", "evidence": ["Workframe policy, product contract, or active-plan state distributed over substantive modules."], "item_id": "item:987075b1-a3a6-58b2-9fec-872d881e8424"}`
- **excluded_decorations** — `{"distributed_as": "Maker scratch under dev/ does not constitute a capability subsystem.", "evidence": ["Maker scratch under dev/ does not constitute a capability subsystem."], "item_id": "item:a31dea94-d04d-5ec6-b16e-9be3a0f5daa1"}`
- **excluded_decorations** — `{"distributed_as": "Maker scratch under dev/ does not constitute a capability subsystem.", "evidence": ["Maker scratch under dev/ does not constitute a capability subsystem."], "item_id": "item:a315cdf4-1be3-5295-b1c7-e722617c1947"}`
- **excluded_decorations** — `{"distributed_as": "Maker scratch under dev/ does not constitute a capability subsystem.", "evidence": ["Maker scratch under dev/ does not constitute a capability subsystem."], "item_id": "item:4119ca5c-a2c4-52d0-bbfd-8b6e4e83b1d5"}`
- **excluded_decorations** — `{"distributed_as": "Packaging or program ledger distributed onto operations.", "evidence": ["Packaging or program ledger distributed onto operations."], "item_id": "item:bfbb5da3-11ce-5364-8466-70eea4556e29"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the assurance capability boundary.", "evidence": ["Primary source for the assurance capability boundary."], "item_id": "item:ed83afee-c94a-5588-a774-2f6b4ca14249"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the assurance capability boundary.", "evidence": ["Primary source for the assurance capability boundary."], "item_id": "item:55c69407-f462-5c3c-b06f-84f23272f5b4"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the assurance capability boundary.", "evidence": ["Primary source for the assurance capability boundary."], "item_id": "item:dd419047-4a7b-56f6-a904-8957cb720509"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the assurance capability boundary.", "evidence": ["Primary source for the assurance capability boundary."], "item_id": "item:2ecfc994-868b-54b7-9791-d2b9faf113fb"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the assurance capability boundary.", "evidence": ["Primary source for the assurance capability boundary."], "item_id": "item:e17834d5-a38e-5644-974b-df9c0aaf859a"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the assurance capability boundary.", "evidence": ["Primary source for the assurance capability boundary."], "item_id": "item:8a1124a7-655e-500f-a577-05b8dcd873cf"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the operations and assurance capability boundary.", "evidence": ["Primary source for the operations and assurance capability boundary."], "item_id": "item:ce710c82-aa0d-53b5-9412-ad3a90aca6b6"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the operations and assurance capability boundary.", "evidence": ["Primary source for the operations and assurance capability boundary."], "item_id": "item:fe1a9c46-a41b-4d5f-a0be-af078df2b9e2"}`
- **excluded_decorations** — `{"distributed_as": "Immutable parent generation for the Surface assurance lineage.", "evidence": ["Immutable parent generation for the Surface assurance lineage."], "item_id": "item:7adaf76f-883e-4460-b5f6-0352f83abee4"}`
- **excluded_decorations** — `{"distributed_as": "Immutable parent generation for the Surface assurance lineage.", "evidence": ["Immutable parent generation for the Surface assurance lineage."], "item_id": "item:8b4e5b56-9153-4a63-b286-91dab5ab2833"}`
- **excluded_decorations** — `{"distributed_as": "Primary source for the assurance capability boundary.", "evidence": ["Primary source for the assurance capability boundary."], "item_id": "item:327bcb35-d3eb-5906-a962-b858c5229a19"}`
- **excluded_decorations** — `{"distributed_as": "Packaging or program ledger distributed onto operations.", "evidence": ["Packaging or program ledger distributed onto operations."], "item_id": "item:91ab609d-be36-5d49-b80c-c1ee31f92be5"}`
- **judgment_calls** — `{"alternative": "Home server.py under the interface surface.", "decision": "Keep API hosting with execution rather than cockpit.", "node_ids": ["node:1bed9604-7f0c-589c-a954-1508e9e796f2", "node:d8ccdbad-93a7-58ca-90ef-9064091f3a7c"], "reason": "Its trust, lease, and workflow command boundary dominates its static-hosting role."}`
- **judgment_calls** — `{"alternative": "Keep tests, audits, assurance receipts, and ops scripts in one leaf.", "decision": "Descend Operations and assurance into runtime operations, qualification corpus, and verification suite.", "node_ids": ["node:cdfeb109-7a08-55d3-b0fc-3a93472c6b05", "node:3cca2ddd-43c9-51b2-82b6-251f3a7541ac", "node:f1a80ab5-baba-5e93-ba14-889cf57b2f37", "node:56ac65a9-61eb-5343-8704-df2fdeb5f234"], "reason": "The parent leaf mixed four positive kinds and then absorbed a 130-file native-approvals qualification engagement."}`
- **semi_lattice_signals** — `{"item_ids": ["item:f1534b56-d009-59f3-b06a-0a51b13d8e4f"], "node_ids": ["node:1bed9604-7f0c-589c-a954-1508e9e796f2", "node:0123f24e-b8ff-544d-959a-444272f7d0ff", "node:d8ccdbad-93a7-58ca-90ef-9064091f3a7c"], "reason": "The composition root legitimately connects API authority, workflows, and UI hosting while retaining one primary home."}`

### Surface
- **staleness** — `{"reason": "The earlier whole-system audit remains historical assurance at its frozen pre-conversation-first revision.", "source_id": "source:f333a39f-3890-565c-82cb-93805cfab6c6"}`
- **staleness** — `{"reason": "The authenticated acceptance report proves the earlier conversation-first runtime, not the current installed runtime or current execution-consolidation closure.", "source_id": "source:a67ac2c4-e97d-40a8-9d10-18b0fcce5869"}`
