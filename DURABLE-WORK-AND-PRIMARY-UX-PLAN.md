# Lattice Durable Work Control Plane and Primary UX Plan

## Purpose

Make Lattice the user-owned control plane for consequential computational and agentic work: every expensive or long-running operation becomes interruptible, resumable, visibly progressing, and information-preserving; conversations can launch, inspect, steer, stop, resume, and metabolize those operations; and Lattice progressively becomes the ordinary UX for Claude, Codex, and other provider harnesses without prematurely reimplementing the engines underneath them.

This is an execution plan for later work. It does not authorize an immediate hard cutover from provider-native interfaces, replacement of official subscription-authenticated harnesses, or installation of an external workflow platform.

## Product outcome

From the user's perspective, consequential work has one stable behavior regardless of repository or domain:

1. A conversation or direct command launches a named durable run.
2. The run immediately has an identity, exact inputs, configuration, code binding, output home, and interruption policy.
3. Lattice shows what is complete, active, remaining, failed, and already learned; elapsed time, throughput, resource use, and an explicitly qualified ETA are visible when available.
4. Stopping, rebooting, losing a provider process, or restarting Lattice does not erase completed units. Resumption repeats at most the active atomic unit and never mixes incompatible revisions.
5. Meaningful intermediate and final outputs survive incrementally. The final report points into the evidence that supports it rather than replacing that evidence.
6. Recomputable caches and duplicate operational material live in a separately declared disposable area. They are never confused with evidence and can be removed without destroying the ability to diagnose, resume, or substantiate the run.
7. Any result, failure, artifact, or intermediate hypothesis can become context for a later Lattice invocation without copying an opaque transcript or losing its provenance.
8. Provider-specific harnesses become replaceable execution engines behind a Lattice-owned experience. Their hidden context and other proof limits remain visible rather than being falsely claimed away.

The target interaction is:

```text
launch a Relata evaluation from the current conversation
    ↓
see the exact revision, cohort, work units, completed count, rate, ETA, CPU, memory, and disk
    ↓
leave or stop the run without losing admitted cells
    ↓
resume it from the last durable checkpoint
    ↓
open a surprising per-cell result and route it to Claude and Codex through different context views
    ↓
retain their analysis, the original evidence, and the relationship between them
    ↓
derive a final report without deleting the evidence that makes the report intelligible
```

## Strategy kernel

**Diagnosis:** Lattice already preserves conversational and provider-execution lineage, while job-scale local computation remains fragmented across project runners, `runjob`, and domain-specific state. Provider-native coding interfaces also remain the practical foreground UX, so Lattice cannot yet dogfood its full product thesis across ordinary work. The result is a split control plane: cognition lives in one place, execution state in several others, and valuable intermediate information can disappear between them.

**Guiding policy:** Lattice becomes the canonical user-facing and relational control plane while execution remains layered. A small provider-neutral durable-run protocol normalizes identity, lifecycle, progress, artifacts, and control. Executors and projects retain the implementation knowledge that only they possess. Official Claude, Codex, Antigravity, and later provider harnesses remain replaceable backends until owning their functions would produce a clear net benefit.

**Coherent action:** Establish the durable-run protocol; adapt the existing local execution mechanisms; project runs into Lattice's ledger and cockpit; make artifact retention semantically explicit; exercise the system first on real long-running work; then progressively move ordinary coding activity into Lattice, using native interfaces only where a measured capability gap remains.

## Architectural boundary

Lattice must not become a generic scheduler whose primary object is a predefined task graph. Its primary object remains the progressively authored conversational lattice. A durable run is an action-bearing object related to that lattice, and its internal work graph is expandable when useful.

### Lattice owns

- the operator experience for launching, viewing, steering, stopping, and resuming runs;
- the canonical relationship between intentions, conversations, invocations, runs, work units, artifacts, findings, and later consumers;
- the normalized durable-run contract and adapter boundary;
- authority and approval state;
- exact context projections constructed from run evidence;
- human-readable progress and resource presentation;
- lifecycle policy for durable and disposable artifacts;
- provider and executor proof limits;
- cross-provider routing and comparison;
- the dogfooding record and native-UX fallback inventory.

### The durable-run executor owns

- process launch, liveness, signal handling, and exit classification;
- atomic persistence of current run state;
- append-only progress and lifecycle events;
- recovery after process or machine interruption;
- resource readings and bounded concurrency;
- invoking the project's resume entry point;
- truthful distinction among running, idle, waiting, stopped, failed, and complete.

### The project owns

- the semantic definition of an atomic work unit;
- dependency order among units;
- checkpoint contents and the procedure for resuming domain work;
- whether a completed unit is still valid under the bound code, configuration, and inputs;
- which intermediate outputs are meaningful evidence;
- domain-specific aggregation and conclusions;
- any domain-specific resource or exposure boundary.

### Provider harnesses own

- their supported tool loops, sandboxes, permissions, authentication, and native session mechanisms;
- provider-specific continuation behavior;
- provider-owned hidden state that Lattice cannot independently observe.

Lattice records and controls the observable envelope but does not claim authority over hidden provider state.

## Durable-run logical contract

The logical contract must remain stable even if its physical implementation evolves. A run contains:

### Immutable identity

- globally unique run identity;
- external project and operation keys for idempotent submission;
- initiating Lattice invocation or direct operator action;
- exact executable, code revision, configuration, input identities, environment profile, and executor identity;
- declared atomic-unit namespace and resume strategy;
- creation time and authority envelope.

Submitting the same external operation key with identical bound bytes returns the existing run. Reusing it with different bytes fails visibly.

### Lifecycle

The normalized lifecycle is:

```text
declared → admitted → running ↔ waiting → stopping → stopped
                                    ↘ failed
                                    ↘ completed
```

`resume` creates a new attempt associated with the same run identity and prior durable state. It does not overwrite previous attempts or pretend an interrupted process never existed.

### Atomic work units

Every unit has a stable semantic key, input binding, status, attempt history, outputs, timestamps, and failure information. Completion becomes durable before the scheduler advances. A safe interruption can invalidate only the active uncommitted unit. Resumption must not repeat completed units unless their binding changed or the operator explicitly requests a fresh attempt.

The protocol does not dictate whether a unit is a LibriWASN score cell, document, query page, build target, agent step, or training checkpoint.

### Progress

A run's status projection must answer:

- what is complete, active, eligible, remaining, waiting, refused, and failed;
- the current unit and latest durable checkpoint;
- the latest meaningful result;
- elapsed time and throughput;
- ETA with its method and uncertainty, or an explicit reason no ETA is defensible;
- CPU, memory, disk, and other resources declared relevant by the executor;
- durable and disposable storage totals;
- the most recent event and the age of the last progress signal;
- whether interruption is presently safe and what would be repeated;
- the exact condition preventing further progress when waiting or blocked.

Progress is structured state written incrementally. Human-readable logs supplement it; they are not the sole status mechanism.

### Evidence and artifact classes

Every produced artifact is classified at creation:

- `durable_evidence`: expensive to reproduce, diagnostically meaningful, surprising, used by a conclusion, or independently useful later;
- `durable_report`: a derived human or machine synthesis pointing to its evidence;
- `checkpoint`: the minimum state required for correct resumption;
- `disposable`: a recomputable cache, duplicate encoding, extraction copy, preview, or transient implementation byproduct;
- `sensitive`: an orthogonal handling marker that may apply to any class.

Unclassified outputs default to durable. A successful run does not retroactively convert its intermediates into disposable material.

Disposable material must be physically or logically isolated. Removal requires that downstream use is complete and loss cannot weaken resumption, diagnosis, provenance, or the final claim. The durable record retains the removed artifact's identity, reason for disposability, byte size, regeneration method, and removal event.

### Evidence relationships

The ledger must express at least:

```text
invocation initiated run
run contains unit
unit generated artifact
artifact supports finding
report derived from artifacts
invocation consumed artifact or finding
attempt resumed checkpoint
artifact superseded artifact
artifact has disposable representation
```

Large immutable bytes may remain in content-addressed storage; the Lattice ledger owns their meaning and relationships, not necessarily every byte's physical location.

## External systems and dependency policy

Do not install Temporal, Nextflow, MLflow, LangGraph, or OpenLineage as part of the first implementation. Their mechanisms are design inputs:

- Nextflow contributes content-bound work-unit reuse and automatic preservation of resumability.
- Temporal contributes append-only recoverable execution history.
- MLflow contributes the separation of searchable run metadata from bulk artifacts.
- LangGraph contributes incremental checkpoints and preservation of successful sibling work.
- OpenLineage and W3C PROV contribute simple run/input/output provenance relations.

The first implementation should metabolize these mechanisms into the existing Lattice ledger, blob store, execution kernel, and AXISVI local-run tooling rather than introducing a second canonical ontology.

Reconsider an external engine only at an observable threshold:

- adopt or adapt Nextflow when multiple real projects require large dataflow DAGs, distributed executors, and cross-machine content-addressed task reuse;
- adopt or adapt Temporal when long-lived distributed workflows, external side effects, and multi-service recovery exceed the local executor's natural bounds;
- adopt or adapt MLflow when experiment comparison, model registries, and shared multi-user artifact stores become recurring requirements;
- adopt OpenLineage interchange when runs must exchange provenance with external organizations or platforms;
- use LangGraph inside a particular agent workflow when its graph semantics fit that workflow, without making it Lattice's canonical state.

## Implementation sequence

### Phase 0 — Reconcile the existing execution surfaces

Inspect the actual source boundaries among Lattice's internal `lattice.execution`, AXISVI `model-runner`, `runjob`, the job registry, workflow packs, and any surviving Smithers implementation. Produce a concise ownership map identifying reusable code, duplicate concepts, and missing behavior. Do not create a new runner until this map demonstrates what cannot be extended cleanly.

Decide the physical home of the default local durable-run executor from evidence. The natural default is an evolution of the smallest existing provider-neutral local mechanism, exposed through a stable adapter; Lattice remains authoritative for the relational and operator surfaces regardless of where the process code lives.

**Gate:** every existing execution surface has one named continuing role, merge target, adapter role, or retirement disposition; no two components silently claim canonical run state.

### Phase 1 — Implement the default local durable-run executor

Add the normalized run identity, attempts, atomic state updates, append-only events, signal-aware stopping, resume entry point, resource readings, and durable/disposable storage accounting to the selected existing substrate.

Define a minimal SDK or protocol through which a project declares units, records unit completion and artifacts, reports progress, and restores a checkpoint. A project must be able to adopt the protocol without importing Lattice's UI or conversational graph internals.

Preserve application-level responsibility: wrapping an opaque command may provide logs and process control, but it cannot advertise semantic resumability unless the application declares checkpoints and unit identities.

**Gate:** a synthetic multi-unit job can be interrupted during a unit, restarted after executor and machine-process loss, preserve all prior completed units, repeat no more than the active unit, and expose truthful status throughout.

### Phase 2 — Make durable runs first-class Lattice objects

Add normalized run, attempt, unit, checkpoint, progress-event, artifact-classification, and executor-binding records to Lattice's append-only ledger. Project current state from events without overwriting historical attempts.

Expose authenticated endpoints and client methods for submission, idempotent lookup, status, event streaming, stop, resume, artifact listing, artifact retrieval, and evidence consumption. Import existing external runs through adapters without pretending their earlier history contains information that was never recorded.

Relate every run to its initiating invocation, project, concern, and workflow action where applicable. Permit direct runs with no initiating model invocation.

**Gate:** Lattice restarts while a local job continues, reconstructs the job's current state from the executor and ledger, and neither duplicates the run nor loses its conversation and artifact relationships.

### Phase 3 — Build the run cockpit

Add a response-first run surface reachable from a conversation, project, invocation, artifact, and global activity view. The ordinary view shows status in functional language, current work, completed and remaining counts, latest result, rate, ETA quality, resources, storage classes, and safe available actions.

Provide deeper drawers for unit history, attempts, structured events, logs, exact bindings, artifacts, checkpoints, and proof limits. The foreground must not require understanding workflow-engine terminology.

Allow an operator to open any meaningful artifact or failure and route it into a new Claude, Codex, or multi-provider invocation with an inspectable context manifest. Allow later outputs to become findings or reports linked back to their source evidence.

Integrate the global activity view with the existing job-registry/Panels ecology without creating another machine-wide service registry. Lattice owns semantic progress; the job registry may continue to own machine-level health aggregation.

**Gate:** the operator can answer “what is it doing, how far is it, what has it learned, can I stop it safely, what would resume repeat, and where are its bytes?” without opening a terminal or reading raw JSON.

### Phase 4 — Exercise retention on Relata and another unlike workload

Make the Relata computational runner the first demanding consumer because it has expensive score cells, bounded evidence cohorts, significant diagnostic intermediates, and prior information loss. Preserve per-cell outputs and the internal hypotheses needed to distinguish detector, embedding, spatial, clustering, and scoring failures rather than retaining only fused labels and aggregate DER.

Use a structurally different second consumer—such as a document-processing, retrieval, or repository-analysis run—to prevent audio-specific semantics from leaking into the core protocol.

Measure storage by class. Delete only declared disposable material after the run proves that the final report, diagnosis, and resumption path remain intact. Do not create a broad retention bureaucracy; use the artifact classes and one inspectable manifest.

**Gate:** interruption and later diagnosis on both consumers require no recomputation of completed work and no recovery from terminal logs; removing the disposable area changes neither conclusion nor resumption capability.

### Phase 5 — Bring coding work into Lattice

Expose the already-qualified provider execution profiles through one conversational coding surface:

- `compiled-fresh` for exact isolated context;
- `workspace-agent` for provider-supported repository tools and authority;
- `native-continuation` for eligible provider sessions;
- portable continuation when native state is unavailable.

Add the practical foreground capabilities required to replace daily VS Code provider panels: workspace selection, file and symbol navigation, terminal sessions, patches and diffs, review, approvals, tool progress, provider-session health, interruption, continuation, and clear renderings of success, empty success, failure, and waiting-for-user states.

Do not reimplement provider tool loops merely to claim independence. Invoke official subscription-authenticated harnesses through qualified adapters and preserve their provider-specific strengths. Lattice owns the UX, context, authority envelope, durable record, and relationships; provider harnesses remain leased engines.

**Gate:** one representative repository task can begin, make edits, run commands, show diffs, request an approval, survive UI and provider interruption, resume, and finish entirely through Lattice with no loss of capability or provenance.

### Phase 6 — Progressive dogfooding and fallback metabolism

Move actual work into Lattice in increasing order of UX sensitivity:

1. long-running local computation and model jobs;
2. research and multi-provider analysis;
3. bounded repository changes;
4. debugging and iterative test loops;
5. ordinary day-long mixed work.

Every fallback to a native Codex, Claude Code, or Antigravity interface records the exact reason as one of:

- missing Lattice capability;
- materially slower or higher-friction interaction;
- provider-only feature not exposed through the adapter;
- Lattice defect or outage;
- intentional break-glass diagnosis;
- user preference not yet represented by the product.

The fallback record is product evidence, not a compliance failure. Repeated reasons become prioritized work; one-off provider specializations may remain deliberate escape hatches.

Track functional outcomes rather than vanity usage: interruption recovery, repeated compute avoided, valuable artifacts retained, time to understand run state, time from intent to first action, time to review a change, number and cause of native-interface fallbacks, and tasks completed without leaving Lattice.

**Gate:** representative long-running, research, and coding workflows complete repeatedly through Lattice; remaining fallbacks are individually understood and either deliberately retained or attached to concrete capability work.

### Phase 7 — Earn primary-UX status

Make Lattice the default entry point only when dogfooding demonstrates that ordinary work is at least as fast, powerful, and legible there as in the provider-native interfaces. Change habits and launch surfaces after capability parity, not before it.

Retain native provider interfaces as break-glass tools for provider diagnosis, new-feature qualification, and recovery from a Lattice-specific failure. Do not uninstall or forbid them. “Abandoning” them means they cease to own the ordinary experience and canonical work history, not that their diagnostic value is denied.

Document which provider capabilities remain opaque or unavailable. Full control means control of the observable work envelope; it never licenses a false claim that Lattice controls provider-owned hidden instructions or infrastructure.

**Gate:** Lattice can be used as the ordinary all-day UX without a systematic speed, capability, or comprehension penalty, and a Lattice outage does not prevent access to the underlying provider or repository needed to repair it.

## Focused verification

Verification should protect the few load-bearing invariants rather than generate a large generic suite.

- **Resumption oracle:** a deterministic independent worker writes known per-unit results, is killed during a unit, then resumes; prior units remain byte-identical and only the interrupted unit may repeat.
- **Binding contract:** changing code, configuration, inputs, or unit identity cannot silently reuse incompatible completion state.
- **Status property:** projected counts and state reconstruct exactly from base events regardless of event insertion order permitted by the protocol; completed plus active plus remaining plus failed/refused accounts for the declared population.
- **Retention contract:** evidence and checkpoints cannot be deleted through the disposable cleanup path; an unclassified output defaults to durable.
- **Disposable negative control:** removing the isolated disposable tree leaves resumption, evidence traversal, and the final report valid.
- **Lattice restart regression:** restarting the UI and service during an external run preserves control, progress, relationships, and artifact access.
- **Provider-boundary contract:** a provider-native continuation cannot cross incompatible workspace, model, permission, or authority bindings; portable continuation remains available.
- **UX acceptance:** a human can determine progress, safety of interruption, latest result, storage disposition, and next action from the ordinary run surface without reading implementation terms.

No independent conclusion bureaucracy, broad mutation campaign, or duplicate custody system is required. Each check exists because its failure would destroy work, falsify status, lose evidence, or undermine the primary UX.

## Failure modes to resist

- Treating live logs as structured progress.
- Treating process survival as application-level resumability.
- Saving only the final aggregate and discarding the hypotheses needed to explain it.
- Preserving every duplicated byte while still losing semantic relationships.
- Calling data disposable only after an inconvenient result appears.
- Letting Lattice become a predefined workflow IDE rather than a conversational control plane.
- Reimplementing provider harness capabilities before exposing them through adapters.
- Forcing an early hard cutover that makes Lattice defects obstruct the work required to repair Lattice.
- Making native-interface use invisible, thereby losing the highest-value dogfooding evidence.
- Claiming complete provider-context control where an official harness retains hidden state.
- Building an elaborate universal scheduler before two unlike real consumers establish the common abstraction.

## Completion condition

This program is complete when:

- consequential local work launched through Lattice is durably identified, incrementally preserved, externally visible, safely interruptible, and correctly resumable;
- reports remain connected to the meaningful evidence and intermediates that support them;
- disposable storage can be reclaimed without weakening diagnosis, provenance, or recovery;
- two unlike real project runners use the same thin protocol without importing each other's semantics;
- Claude, Codex, and other qualified harnesses operate behind one Lattice-owned coding and conversational experience;
- Lattice has become the ordinary UX through demonstrated parity rather than mandate;
- native provider interfaces remain available only for understood specializations and break-glass recovery;
- the system's observable authority and provider proof limits are accurate.

The resulting product is not an imitation of Codex, Claude Code, Temporal, Nextflow, or MLflow. It is a user-owned conversational cockpit in which cognition, action, progress, evidence, interruption, recovery, and later reuse form one continuous durable object.

## First action when execution resumes

Read the current Lattice, `runjob`, model execution, job-registry, and Smithers-adjacent source rather than relying on this plan's description of them. Produce the Phase 0 ownership map and identify the smallest existing substrate that can implement the durable-run executor without duplicating Lattice's ledger or domain runners. Then present any genuine physical-ownership decision before writing implementation code.
