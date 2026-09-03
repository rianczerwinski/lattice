# Lattice Durable Work Control Plane and Primary UX Plan

Status: active implementation plan for advancing Lattice 0.3.1 from its shipped conversation-first foundation into the ordinary owned interface for provider-backed coding and consequential local work.

## Purpose

Make Lattice the user-owned control plane for consequential computational and agentic work: every expensive or long-running operation becomes interruptible, resumable, visibly progressing, and information-preserving; conversations can launch, inspect, steer, stop, resume, and metabolize those operations; and Lattice becomes the ordinary UX for Claude, Codex, and other provider harnesses without prematurely reimplementing the engines underneath them.

The shipped foundation is not hypothetical. Lattice already has direct conversation, exact context manifests, response-first routing, provider-qualified fresh/workspace/native-continuation profiles, an append-only SQLite ledger, content-addressed artifacts, restart recovery, snapshots, and a supervised cockpit. This plan extends that implementation rather than rebuilding it from an abstract workflow design.

Progressive real-work dogfooding begins as soon as the relevant vertical slice works. Provider-native interfaces remain available as break-glass and capability-comparison tools; they cease to be the ordinary UX only after Lattice can carry the work without systematic friction or lost capability. No external workflow platform, developer API billing, or replacement of official subscription-authenticated harnesses is authorized.

## Priority and operating posture

Architecture outranks opportunistic compute. A convenient early workload may exercise the design, but it may not force the durable-run contract, ledger ontology, cockpit, or project boundary into an audio-specific or short-lived shape. When a few additional hours of compute conflict with laying the correct bones, the compute waits.

Implementation nevertheless uses orthogonal work intelligently. A frozen external workload may run while Lattice or another repository changes if its executable, inputs, outputs, and conclusion remain independent of those changes. Every such run is divided into semantic atomic units, commits each completed unit durably, exposes a safe-stop boundary, and can resume without repeating prior units.

The first demanding live consumer is Relata because its prior evaluation lost expensive causal anatomy and because its next external-reference run is independent of the Engine architecture being rebuilt. Relata is a proving ground for the general contract, not the source of its ontology.

## Product outcome

From the user's perspective, consequential work has one stable behavior regardless of repository or domain:

1. A conversation or direct command launches a named durable run.
2. The run immediately has an identity, exact inputs, configuration, code binding, output home, and interruption policy.
3. Lattice shows what is complete, active, remaining, failed, and already learned; elapsed time, throughput, resource use, and an explicitly qualified ETA are visible when available.
4. Stopping, rebooting, losing a provider process, or restarting Lattice does not erase completed units. A run's immutable declaration sets its maximum parallel semantic units; resumption may repeat only the units that were active at interruption and never mixes incompatible revisions.
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

- process launch, liveness, signal handling, and exit classification for trusted foreground work that remains in the guardian-owned process group;
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
- any domain-specific resource or exposure boundary;
- keeping all computation represented as local guardian-owned work in the foreground process group; detached sessions, double-fork daemons, reparented descendants, and independently managed services require a different adapter and are not claimed as controlled by the local executor.

Lattice never infers a project's evidence population by walking its filesystem or archive store. The project supplies an exact, bound unit manifest; Lattice preserves that population as part of run identity, exposes its restrictions, and refuses a stop, resume, retry, or adapter action that would silently add or substitute units. Bytes being present does not mean they are authorized inputs.

For the first Relata consumer, the adapter may expose only the exact 24 already-scored LibriWASN sessions bound by the Foundry result and configuration digest. Every other preregistered development session and every confirmation or sealed session remains unavailable to performance-bearing access. Lattice may display non-performance metadata needed to explain the boundary, but it may not decode protected audio, inspect performance labels, run timing or smoke work on protected sessions, or treat a successful development gate as permission to advance automatically. The UI must distinguish “bytes remain intact” from “evidence remains untouched,” because a read can irreversibly spend the latter without changing the former.

### Provider harnesses own

- their supported tool loops, sandboxes, permissions, authentication, and native session mechanisms;
- provider-specific continuation behavior;
- provider-owned hidden state that Lattice cannot independently observe.

Lattice records and controls the observable envelope but does not claim authority over hidden provider state.

### Resolved execution ownership

The current code and tool boundaries support one default architecture:

| Surface | Continuing role |
|---|---|
| Lattice ledger, blob store, server, and supervisor | Canonical semantic run identity, attempts, units, events, artifact relationships, approvals, context projections, operator control, and current-state views |
| Lattice execution journal/process primitives | Reusable process launch, typed failure, incremental journal, provider-session capture, and lease mechanics; generalized only where their semantics are genuinely provider-neutral |
| Project runner | Declares the semantic unit population, checkpoints, domain progress, validation of reusable completions, evidence classes, aggregation, and conclusions |
| `runjob` | Compatibility wrapper for opaque commands and a simple signal/log/status boundary; it never claims semantic resumability or canonical unit state |
| Former `model-runner` source | Historical execution evidence and rollback material only while ADR 001's installed-runtime, consumer, history-resolution, rollback, and independent-verifier gates remain open; it has no continuing operational role and new work may not depend on it |
| job registry | Machine-wide health and cadence aggregation; it links to Lattice runs but does not duplicate semantic progress or run control |
| Smithers or another compound executor | Optional adapter for a real job whose internal graph needs exposure; never the default ontology or a prerequisite for local durable runs |

The default local durable-run protocol and its canonical projection therefore live in Lattice. A project may host its own worker process and state, but it reports through a small Lattice-owned protocol. The first implementation extends Lattice's existing ledger, supervisor, event stream, and artifact relations rather than growing `runjob` into a second application or installing a separate workflow engine.

The default local adapter is deliberately narrower than an operating-system process sandbox. Its guardian durably identifies and controls the direct trusted worker and the foreground process group inherited by that worker. The project contract forbids moving represented run work into a detached session or reparented daemon; Lattice does not claim to discover, terminate, or recover work that violates that boundary. This keeps ordinary project-worker recovery exact without implying hostile same-user containment.

### Daily-driver quality contract

Replacing provider panels means more than making their commands technically reachable. Ordinary work must feel continuous, fast, and difficult to lose.

The primary surface must provide:

- instant reopening into the active project, latest response, exact scroll position, and draft;
- fast project, conversation, file, symbol, artifact, run, and command search from one command surface;
- keyboard-first send, route, side-quest, return, compare, stop, resume, open-file, open-diff, and focus movement, with visible discoverability rather than memorization as a prerequisite;
- persistent per-project drafts, deliberate prompt history, editable retries, and protection against navigation or refresh loss;
- streaming output that remains readable while tools run, preserves the user's reading position, and exposes completion or failure without stealing focus;
- a quiet activity model: current action, tool progress, approval needed, completion, failure, and unread changes are visible without turning the conversation into a log feed;
- workspace selection, recent work, file and symbol navigation, exact local file links, terminal sessions, command output, patches, diffs, review, and approval handling inside the same work context;
- one-click movement from an answer, code location, diff hunk, terminal result, run cell, artifact, or failure into a new invocation with an inspectable context delta;
- explicit data, empty-success, loading, waiting, interrupted, permission-needed, provider-failed, backend-failed, and stale states at every fetched surface;
- provider session, model, effort, workspace, tools, permissions, sandbox, network, health, and continuation eligibility visible at the moment they matter;
- safe task switching while work continues, with completion notification and no requirement to keep one conversation foregrounded;
- responsive first paint and local navigation even when provider discovery, a secondary drawer, or an unrelated backend is slow;
- storage visibility by durable evidence, report, checkpoint, and disposable bytes, with exact reclamation consequences;
- a break-glass route to the underlying provider harness, terminal, or repository when Lattice itself is the failed component.

The conversation remains the center of gravity. Files, terminals, diffs, runs, manifests, and graphs are contextual instruments around it, not competing full-screen products whose state the user must manually reconcile.

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

When a local worker reports a file locator, Lattice verifies the emitted content identity and streams every non-disposable artifact into Lattice's content-addressed store before admitting the ledger record. Later mutation, deletion, or relocation of the project file therefore cannot weaken evidence or checkpoint recovery. A disposable artifact may retain its verified external file locator only because its declaration includes an explicit regeneration method.

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

The sequence is architecture-first but vertical: each phase extends the same ledger, service, and cockpit toward one real all-day path. Do not complete a generic scheduler in isolation and postpone the ordinary experience until the end.

### Phase 0 — Reconcile the existing execution surfaces

Bind implementation to the resolved ownership table above. Inspect exact source symbols and schemas in Lattice's store, server, supervisor, execution package, the former `model-runner` source pending ADR 001's retirement gate, `runjob`, job registry, workflow packs, and any live compound-executor adapter. Record reusable primitives, concepts that must not be duplicated, schema additions, compatibility adapters, and exact retirement candidates. The former `model-runner` source is evidence for consolidation and rollback, not a lower-level dependency or second execution authority.

Map the current cockpit against the daily-driver quality contract using real visible behavior rather than an implementation inventory. Preserve the successful conversation-first interaction from the completed remediation: direct blank projects, response-first focus, inline provider routing, exact selection actions, context deltas, trailheads, episodes, session visibility, secondary-drawer failure isolation, restart continuity, and the current provider-profile guarantees.

Decompose the current 1,116-line `App.svelte` at functional seams before adding the durable-run and coding surfaces. The goal is not component-count aesthetics; it is to prevent conversation, workspace, run, review, and secondary-drawer state from becoming one unmaintainable failure domain.

**Gate:** every execution surface has one continuing role or exact retirement disposition; the schema and component map identify the minimal extension path; no two components claim canonical run state; and no planned daily-driver behavior lacks an owning service or cockpit surface.

### Phase 1 — Implement the default local durable-run executor

Add the normalized run identity, attempts, atomic state updates, append-only events, signal-aware stopping, resume entry point, resource readings, declaration-bound bounded concurrency, and durable/disposable storage accounting to the selected existing substrate. The default remains one active unit; a project may declare a higher exact limit when its units are independent and its worker can preserve each child result separately.

Define a minimal SDK or protocol through which a project declares units, records unit completion and artifacts, reports progress, and restores a checkpoint. A project must be able to adopt the protocol without importing Lattice's UI or conversational graph internals.

Preserve application-level responsibility: wrapping an opaque command may provide logs and process control, but it cannot advertise semantic resumability unless the application declares checkpoints and unit identities.

**Gate:** a synthetic multi-unit foreground job reaches its declared concurrency limit, is interrupted with several units active, restarts after executor and managed-process loss, preserves all prior completed units, requeues only the interrupted active set, and exposes truthful per-unit status throughout. Detached or reparented work is rejected by the adapter contract rather than counted as guardian-controlled work.

### Phase 2 — Make durable runs first-class Lattice objects

Add normalized run, attempt, unit, checkpoint, progress-event, artifact-classification, and executor-binding records to Lattice's append-only ledger. Project current state from events without overwriting historical attempts.

Expose authenticated endpoints and client methods for submission, idempotent lookup, status, event streaming, stop, resume, artifact listing, artifact retrieval, and evidence consumption. Import existing external runs through adapters without pretending their earlier history contains information that was never recorded.

Relate every run to its initiating invocation, project, concern, and workflow action where applicable. Permit direct runs with no initiating model invocation.

**Gate:** Lattice restarts while a local job continues, reconstructs the job's current state from the executor and ledger, and neither duplicates the run nor loses its conversation and artifact relationships.

### Phase 3 — Build the run cockpit

Extend the response-first cockpit into a unified daily-driver shell rather than adding a detached monitoring dashboard. A run is reachable from its conversation, project, invocation, artifact, workspace, and global activity view. The ordinary view shows status in functional language, current work, completed and remaining counts, latest result, rate, ETA quality, resources, storage classes, and safe available actions.

Provide deeper drawers for unit history, attempts, structured events, logs, exact bindings, artifacts, checkpoints, and proof limits. The foreground must not require understanding workflow-engine terminology.

Allow an operator to open any meaningful artifact or failure and route it into a new Claude, Codex, or multi-provider invocation with an inspectable context manifest. Allow later outputs to become findings or reports linked back to their source evidence.

Implement the first tranche of the daily-driver quality contract here: persistent drafts and view position, project/recent-work switching, command palette and keyboard routes, background activity and completion signals, readable streaming, error-state discrimination, responsive local navigation, and failure isolation between conversation, workspace, run, and secondary drawers.

Integrate the global activity view with the existing job-registry/Panels ecology without creating another machine-wide service registry. Lattice owns semantic progress; the job registry may continue to own machine-level health aggregation.

**Gate:** the operator can answer “what is it doing, how far is it, what has it learned, can I stop it safely, what would resume repeat, and where are its bytes?” without opening a terminal or reading raw JSON; switching away and returning loses neither draft, reading position, activity, nor control.

### Phase 4 — Exercise retention on Relata and another unlike workload

Make the Relata computational runner the first demanding consumer because it has expensive score cells, bounded evidence cohorts, significant diagnostic intermediates, and prior information loss. Import the exact project-supplied 24-session exposed allowlist rather than discovering cells from LibriWASN storage; show the protected remainder as unavailable, and prove that interruption and resumption preserve the original population. Preserve per-cell outputs and the internal hypotheses needed to distinguish detector, embedding, spatial, clustering, and scoring failures rather than retaining only fused labels and aggregate DER.

Use a structurally different second consumer—such as a document-processing, retrieval, or repository-analysis run—to prevent audio-specific semantics from leaking into the core protocol.

Measure storage by class. Delete only declared disposable material after the run proves that the final report, diagnosis, and resumption path remain intact. Do not create a broad retention bureaucracy; use the artifact classes and one inspectable manifest.

**Gate:** interruption and later diagnosis on both consumers require no recomputation of completed work and no recovery from terminal logs; removing the disposable area changes neither conclusion nor resumption capability.

### Phase 5 — Bring coding work into Lattice

Expose the already-qualified provider execution profiles through one conversational coding surface:

- `compiled-fresh` for exact isolated context;
- `workspace-agent` for provider-supported repository tools and authority;
- `native-continuation` for eligible provider sessions;
- portable continuation when native state is unavailable.

Add the practical foreground capabilities required to replace daily VS Code provider panels: workspace and recent-repository selection; fast file, symbol, and text navigation; terminal sessions with preserved command/output boundaries; patches and diffs with file/hunk addressing; staged, unstaged, branch, and last-turn review; approval requests attached to the action that needs them; tool progress; provider-session health; interruption; continuation; and clear renderings of success, empty success, failure, and waiting-for-user states.

Do not turn Lattice into a full text editor before evidence demands it. Opening, inspecting, selecting, commenting on, and routing exact code must be excellent; provider agents and existing editors may continue to perform freeform editing behind the same recorded workspace until an owned editor yields a measured experiential gain. Conversely, do not use that boundary to excuse a crippled cockpit: the user must be able to understand and direct a coding task, inspect every consequential change, run or steer commands, resolve approvals, and finish review without returning to a provider panel.

Project-scoped provider activity, cross-project switching, unread/completion state, resumable live journals, selected-route cancellation, and launch-failure discrimination are part of the current cockpit foundation. Complete the remaining daily-driver quality contract with operating-system notifications, retry and editable continuation, prompt history, stable keyboard focus, large-output virtualization, search across conversations and artifacts, exact file links, copy/export affordances, and recovery surfaces that state what survived and what must be repeated.

Do not reimplement provider tool loops merely to claim independence. Invoke official subscription-authenticated harnesses through qualified adapters and preserve their provider-specific strengths. Lattice owns the UX, context, authority envelope, durable record, and relationships; provider harnesses remain leased engines.

**Gate:** one representative repository task can begin, navigate source, make edits, run commands, show and review diffs, request and resolve an approval, switch to another project while work continues, survive UI and provider interruption, resume, and finish entirely through Lattice with no material loss of speed, capability, comprehension, or provenance.

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

Execute Phase 0 against the current source and then continue directly into the smallest end-to-end Phase 1/2 vertical slice. The ownership default is settled here: Lattice owns the semantic protocol, ledger projection, control API, and cockpit; project runners own domain checkpoints; existing process and provider mechanisms remain lower-level adapters. Stop for user input only if the source reveals a genuinely incompatible constraint that would reverse that architecture, not because multiple physical modules could technically host equivalent code.

Use the Relata external N=1 reference as the first live run only after its runner is frozen, semantically atomic, independently resumable, orthogonal to the changing Engine revision, and fail-closed on the exact already-exposed LibriWASN population. If that bounded adapter is not immediately available, continue the Lattice and Engine architecture rather than distorting either one to start compute early.
