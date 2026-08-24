# Audit — audit:c062824e-b182-4880-9290-e84bac4cb0f7

Contract: **valid**. Evidentiary assurance: **candidate**. Admissible state: **closure-candidate**.

Canonical bytes: `f96b3cf5b7961112fdc1b82d3b805c76c8bea1b8a3f0381ce63e639cf6ef0bf9`. Source revision: `f4d70ba44777603b8e624c23a7bd882f249b16bd`. Surface bytes: `6c6eb816b6b6502299129c130f5bf204d6a929acc6f9cce9af1e31e01c0b74d4`.

## Material findings

- **S2** · `finding:7ad1b636-46f5-4fe2-b2f3-a9d7cfed305e` · The supervised service does not run the audited conversation-first generation · active. Consequence: Real personal operation would validate and depend on a different object from the one this remediation and audit describe, so real-data-ready closure would be materially false even though data integrity and rollback remain healthy.

## Derived depth

| Subject | Plane | Target | Derived |
|---|---:|---|---|
| Lattice conversation-first and operational-reliability system | 1 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 2 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 3 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 4 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 5 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 6 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 7 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 8 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 9 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 10 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 11 | sanity | sanity |
| Lattice conversation-first and operational-reliability system | 12 | sanity | sanity |

## Assurance blockers

- active material findings remain: ['finding:7ad1b636-46f5-4fe2-b2f3-a9d7cfed305e']
- blocking residuals remain: ['residual:authenticated-current-source', 'residual:browser-current-complete-path', 'residual:independent-reviewer']

## Proof limits and residuals

- assessment:plane-1-discoverability: This sanity-depth discoverability assessment inspected README.md and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated discoverability campaign.
- assessment:plane-1-dead-code: This sanity-depth dead-code assessment inspected README.md and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated dead-code campaign.
- assessment:plane-1-duplication: This sanity-depth duplication assessment inspected README.md and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated duplication campaign.
- assessment:plane-1-file-cohesion: This sanity-depth file-cohesion assessment inspected README.md and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated file-cohesion campaign.
- assessment:plane-1-layering: This sanity-depth layering assessment inspected README.md and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated layering campaign.
- assessment:plane-1-dependency-graph-health: This sanity-depth dependency-graph-health assessment inspected README.md and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated dependency-graph-health campaign.
- assessment:plane-2-abstraction-fit: This sanity-depth abstraction-fit assessment inspected lattice/contracts.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated abstraction-fit campaign.
- assessment:plane-2-config-surface: This sanity-depth config-surface assessment inspected lattice/contracts.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated config-surface campaign.
- assessment:plane-2-missing-types: This sanity-depth missing-types assessment inspected lattice/contracts.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated missing-types campaign.
- assessment:plane-2-extension-points: This sanity-depth extension-points assessment inspected lattice/contracts.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated extension-points campaign.
- assessment:plane-2-error-model: This sanity-depth error-model assessment inspected lattice/contracts.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated error-model campaign.
- assessment:plane-2-state-model: This sanity-depth state-model assessment inspected lattice/contracts.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated state-model campaign.
- assessment:plane-2-testability: This sanity-depth testability assessment inspected lattice/contracts.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated testability campaign.
- assessment:plane-3-mutability-contract: This sanity-depth mutability-contract assessment inspected lattice/server.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated mutability-contract campaign.
- assessment:plane-3-identifier-encoding: This sanity-depth identifier-encoding assessment inspected lattice/server.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated identifier-encoding campaign.
- assessment:plane-3-version-source-of-truth: This sanity-depth version-source-of-truth assessment inspected lattice/server.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated version-source-of-truth campaign.
- assessment:plane-3-serialization-boundary: This sanity-depth serialization-boundary assessment inspected lattice/server.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated serialization-boundary campaign.
- assessment:plane-3-dead-parameters: This sanity-depth dead-parameters assessment inspected lattice/server.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated dead-parameters campaign.
- assessment:plane-3-data-flow-invariants: This sanity-depth data-flow-invariants assessment inspected lattice/server.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated data-flow-invariants campaign.
- assessment:plane-3-compatibility: This sanity-depth compatibility assessment inspected lattice/server.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated compatibility campaign.
- assessment:plane-3-deprecation-discipline: This sanity-depth deprecation-discipline assessment inspected lattice/server.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated deprecation-discipline campaign.
- assessment:plane-4-translation-fidelity: This sanity-depth translation-fidelity assessment inspected lattice/manifest_reader.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated translation-fidelity campaign.
- assessment:plane-4-deviation-soundness: This sanity-depth deviation-soundness assessment inspected lattice/manifest_reader.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated deviation-soundness campaign.
- assessment:plane-4-magic-number: This sanity-depth magic-number assessment inspected lattice/manifest_reader.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated magic-number campaign.
- assessment:plane-4-uncited-heuristic: This sanity-depth uncited-heuristic assessment inspected lattice/manifest_reader.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated uncited-heuristic campaign.
- assessment:plane-4-numerical-stability: This sanity-depth numerical-stability assessment inspected lattice/manifest_reader.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated numerical-stability campaign.
- assessment:plane-4-operator-misuse: This sanity-depth operator-misuse assessment inspected lattice/manifest_reader.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated operator-misuse campaign.
- assessment:plane-4-provenance: This sanity-depth provenance assessment inspected lattice/manifest_reader.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated provenance campaign.
- assessment:plane-5-redundant-computation: This sanity-depth redundant-computation assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated redundant-computation campaign.
- assessment:plane-5-hot-loop-vectorization: This sanity-depth hot-loop-vectorization assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated hot-loop-vectorization campaign.
- assessment:plane-5-inner-loop-allocation: This sanity-depth inner-loop-allocation assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated inner-loop-allocation campaign.
- assessment:plane-5-memory-layout: This sanity-depth memory-layout assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated memory-layout campaign.
- assessment:plane-5-realtime-budget: This sanity-depth realtime-budget assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated realtime-budget campaign.
- assessment:plane-5-resource-amortization: This sanity-depth resource-amortization assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated resource-amortization campaign.
- assessment:plane-6-silent-failure: This sanity-depth silent-failure assessment inspected lattice/snapshot.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated silent-failure campaign.
- assessment:plane-6-assumption-violation: This sanity-depth assumption-violation assessment inspected lattice/snapshot.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated assumption-violation campaign.
- assessment:plane-6-degradation-containment: This sanity-depth degradation-containment assessment inspected lattice/snapshot.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated degradation-containment campaign.
- assessment:plane-6-recovery-after-fault: This sanity-depth recovery-after-fault assessment inspected lattice/snapshot.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated recovery-after-fault campaign.
- assessment:plane-6-input-degradation: This sanity-depth input-degradation assessment inspected lattice/snapshot.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated input-degradation campaign.
- assessment:plane-7-race-freedom: This sanity-depth race-freedom assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated race-freedom campaign.
- assessment:plane-7-deadlock-freedom: This sanity-depth deadlock-freedom assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated deadlock-freedom campaign.
- assessment:plane-7-atomicity: This sanity-depth atomicity assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated atomicity campaign.
- assessment:plane-7-data-integrity: This sanity-depth data-integrity assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated data-integrity campaign.
- assessment:plane-7-idempotency: This sanity-depth idempotency assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated idempotency campaign.
- assessment:plane-7-toctou: This sanity-depth toctou assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated toctou campaign.
- assessment:plane-7-ordering-visibility: This sanity-depth ordering-visibility assessment inspected lattice/store.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated ordering-visibility campaign.
- assessment:plane-8-secrets-in-code: This sanity-depth secrets-in-code assessment inspected lattice/security.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated secrets-in-code campaign.
- assessment:plane-8-untrusted-input-sink: This sanity-depth untrusted-input-sink assessment inspected lattice/security.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated untrusted-input-sink campaign.
- assessment:plane-8-trusted-path-io: This sanity-depth trusted-path-io assessment inspected lattice/security.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated trusted-path-io campaign.
- assessment:plane-8-transport-security: This sanity-depth transport-security assessment inspected lattice/security.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated transport-security campaign.
- assessment:plane-8-supply-chain: This sanity-depth supply-chain assessment inspected lattice/security.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated supply-chain campaign.
- assessment:plane-8-privacy-retention: This sanity-depth privacy-retention assessment inspected lattice/security.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated privacy-retention campaign.
- assessment:plane-8-license-redistribution: This sanity-depth license-redistribution assessment inspected lattice/security.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated license-redistribution campaign.
- assessment:plane-9-runtime-logging: This sanity-depth runtime-logging assessment inspected lattice/diagnostics.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated runtime-logging campaign.
- assessment:plane-9-metrics-health: This sanity-depth metrics-health assessment inspected lattice/diagnostics.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated metrics-health campaign.
- assessment:plane-9-trace-correlation: This sanity-depth trace-correlation assessment inspected lattice/diagnostics.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated trace-correlation campaign.
- assessment:plane-9-error-surface: This sanity-depth error-surface assessment inspected lattice/diagnostics.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated error-surface campaign.
- assessment:plane-9-debuggability: This sanity-depth debuggability assessment inspected lattice/diagnostics.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated debuggability campaign.
- assessment:plane-10-docstring-contract: This sanity-depth docstring-contract assessment inspected lattice/cli.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated docstring-contract campaign.
- assessment:plane-10-error-actionability: This sanity-depth error-actionability assessment inspected lattice/cli.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated error-actionability campaign.
- assessment:plane-10-naming-consistency: This sanity-depth naming-consistency assessment inspected lattice/cli.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated naming-consistency campaign.
- assessment:plane-10-breaking-change-surface: This sanity-depth breaking-change-surface assessment inspected lattice/cli.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated breaking-change-surface campaign.
- assessment:plane-10-help-behavior-match: This sanity-depth help-behavior-match assessment inspected lattice/cli.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated help-behavior-match campaign.
- assessment:plane-11-metadata: This sanity-depth metadata assessment inspected ops/install.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated metadata campaign.
- assessment:plane-11-dependency-spec: This sanity-depth dependency-spec assessment inspected ops/install.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated dependency-spec campaign.
- assessment:plane-11-clean-build: This sanity-depth clean-build assessment inspected ops/install.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated clean-build campaign.
- assessment:plane-11-asset-inclusion: This sanity-depth asset-inclusion assessment inspected ops/install.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated asset-inclusion campaign.
- assessment:plane-11-clean-install: This sanity-depth clean-install assessment inspected ops/install.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated clean-install campaign.
- assessment:plane-11-path-resolution: This sanity-depth path-resolution assessment inspected ops/install.py and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated path-resolution campaign.
- assessment:plane-12-audience-framing: This sanity-depth audience-framing assessment inspected web/src/App.svelte and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated audience-framing campaign.
- assessment:plane-12-first-run: This sanity-depth first-run assessment inspected web/src/App.svelte and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated first-run campaign.
- assessment:plane-12-positioning: This sanity-depth positioning assessment inspected web/src/App.svelte and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated positioning campaign.
- assessment:plane-12-doc-coverage: This sanity-depth doc-coverage assessment inspected web/src/App.svelte and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated doc-coverage campaign.
- assessment:plane-12-accessibility: This sanity-depth accessibility assessment inspected web/src/App.svelte and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated accessibility campaign.
- assessment:plane-12-community-infra: This sanity-depth community-infra assessment inspected web/src/App.svelte and its named symbols; it did not traverse every one of the 105 frozen census entries or run a dedicated community-infra campaign.
- residual:authenticated-current-source: No successful authenticated Claude/Codex acceptance report binds the exact audited source revision after three bounded attempts. (blocks-closure)
- residual:browser-current-complete-path: The complete mocked browser path passed before secondary-failure isolation and the current negative mutation passed afterward, but the corrected exact-current full path was not rerun after its fixture defect. (blocks-closure)
- residual:independent-reviewer: This audit used validator, independent reader, and negative-control code paths but was produced in the same implementation session; no separate human or agent reviewer has verified the immutable revision. (blocks-closure)
- residual:outport-go-suite: The host lacks the Go toolchain, so Outport's own repository suite was not rerun; Lattice's consumption and disagreement contracts passed independently. (proof-limit)

## Scope

Determine whether the conversation-first and operational-reliability implementation at the frozen revision warrants real-data-ready operation without losing exact context, graph history, authority, or recoverability.
