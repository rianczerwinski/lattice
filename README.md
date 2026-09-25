# Lattice

Lattice is a local-first conversational lattice harness: a user progressively authors a graph of model invocations, one consequential edge at a time, while retaining exact control over what each invocation receives, which model performs it, what tools it may use, and how its output becomes available to later work.

The graph is not primarily a workflow drawn in advance. It is the durable, inspectable record of thought and action as it develops. A successful path may later be extracted into a reusable workflow, but automation is downstream of understanding the path.

This document is the canonical product and architecture contract for Lattice. The current software version is `0.3.1`; publishing the repository does not imply a 1.0 stability claim. Lattice is an independent repository developed in place inside AXISVI, which supplies personal integrations without owning or pinning Lattice's source revision. The implementation is a FastAPI service, Svelte cockpit, append-only SQLite ledger, content-addressed blob store, provider-neutral subscription execution kernel, Claude/Codex/Antigravity/Grok/Muse adapters, Octopus importer, first-party workflow-pack boundary, and live Slate/Axial projections. The multilingual quaternary-literature program is its first governed workflow.

## Run it

Install or upgrade the supervised runtime from a clean committed Lattice revision:

```bash
./.venv/bin/python ops/install.py
lattice status
lattice open
lattice doctor
```

Submit an ordinary model call as a one-node graph through the same boundary used by compound workflows:

```bash
lattice invoke --request execution-submission.json --wait
lattice result <external-project-key> <operation-key>
```

Python consumers use `lattice.execution.LatticeClient`; local services may submit the same `ExecutionSubmission/1.0` contract to the authenticated execution endpoint. Reusing an external operation key with identical bytes returns the existing invocation, while different bytes fail rather than purchasing a second or ambiguous execution.

Outport owns Lattice's `lattice.axisvi.test` identity and current port allocation; launchd keeps the installed, version-pinned runtime alive across crashes and login. `lattice` works from any shell directory. Canonical private state and verified backups live under `~/Library/Application Support/Lattice/`; logs live under `~/Library/Logs/Lattice/`. The former repository-local store is not active; it remains byte-preserved only as the pre-cutover recovery source. The source layout is:

```text
lattice/          backend contracts, ledger, compositor, execution, imports, workflow adapters
tests/            truth-typed kernel, API, supervisor, workflow, and mutation checks
web/              response-first Svelte cockpit and production build
workflow_packs/   bundled definitions owned by Lattice itself; project packs load from explicit external roots
assurance/        frozen remediation baseline and permanent finding-to-proof ledger
dev/decisions/    durable architecture decisions and their verification boundaries
ops/              verified runtime installation, launchd supervision, upgrades, backups, and recovery
```

Set `LATTICE_WORKFLOW_PACK_ROOTS` to an `os.pathsep`-separated list of additional pack roots in a standalone installation. Inside AXISVI, the existing Canon adapter adds `sui/mission/projects/canon/src/canon/discovery/reference_cascade/workflows/` automatically. A duplicate `pack_id` across roots is a startup error, so an external project can never be silently shadowed by a bundled definition.

Lattice `0.3.1` separates source assurance from installed-runtime qualification. Exact test counts belong to immutable qualification receipts rather than this living document. The execution-consolidation verifier independently reads base SQLite rows, blob bytes, runtime attestations, consumer source, provider receipts, and historical journal censuses; source tests alone cannot declare the installed execution boundary ready. Operational qualification and its exact current evidence live under [`assurance/execution-consolidation/`](assurance/execution-consolidation/).

The implemented durable-run control plane and repository cockpit, together with the remaining work required before Lattice can replace provider-native coding surfaces as the ordinary UX, are specified in [`DURABLE-WORK-AND-PRIMARY-UX-PLAN.md`](DURABLE-WORK-AND-PRIMARY-UX-PLAN.md).

Provider invocations remain attached to their owning project after launch. The cockpit reconstructs active provider journals from durable attempt cursors, keeps background projects independently navigable, scopes progress and cancellation to the selected invocation rather than its batch siblings, distinguishes unresolved launch failures from running work, and preserves each project's unread position across reloads. A slower overlapping status response cannot replace a newer project projection.

The local durable-run adapter executes trusted project workers under a foreground process-group contract. The worker and every process it owns must remain in the guardian's process group until the direct worker exits; `setsid`, double-fork daemonization, reparenting run work, and delegation to an unbound background service are outside this adapter's lifecycle and recovery claims. This is an ownership boundary, not a hostile same-user process sandbox. A project that needs an independently managed service must expose that service through its own durable adapter rather than representing it as guardian-owned local work. At worker-event admission, Lattice streams every file-backed durable evidence, durable report, and checkpoint into its verified content-addressed store; only explicitly disposable artifacts with a regeneration method retain external file locators.

## Telos

Lattice should make the following interaction ordinary:

> Send this intent to the coverage orchestrator. Give its proposed search region and the relevant German evidence to the native-language partner. Execute the exact strings the native partner returns. Feed the results back to both roles through different, inspectable views. Fork a challenger when the orchestrator begins considering closure. Preserve the whole path, then expose it as the German quaternary slice of Canon.

The user experiences a live conversation with routing powers, not a workflow IDE. The system underneath may be an orchestrator, but the foreground interaction is a friendly, slow-motion macro-Lattice in which the next edge is visible and steerable.

The essential promise is not merely that a response can be traced to a prior response. It is that an invocation can be answered in full:

- What exact world was this agent shown?
- Which earlier material was included, transformed, summarized, superseded, or deliberately omitted?
- Which rules, files, tools, permissions, memories, retrievals, and live modules were present?
- Which provider, model, harness, and runtime executed it?
- Which evidence and actions did it produce?
- Which later invocations consumed which parts of that output?

Where a provider or harness injects context that Lattice cannot observe, the answer must explicitly name that proof limit. Lattice must never claim omniscience about hidden provider state.

## Product identity

Lattice occupies a different center of gravity from both chat applications and workflow runners.

| System shape | Primary object | Human role | Graph behavior |
|---|---|---|---|
| Ordinary chat | One linear transcript | Supplies the next message | Branching is absent or copies a thread opaquely |
| Visual prompt workflow | A graph specified before execution | Programs the graph | Nodes execute a substantially predefined plan |
| Durable agent runner | A job and its recoverable task graph | Launches or supervises work | The graph exists to control execution |
| Lattice | An evolving conversational lattice | Authors and routes the next invocation | The graph is progressively discovered, then may become a workflow |

Lattice is therefore not reducible to “multi-model chat,” “agent orchestration,” “a graph viewer,” or “prompt chaining.” Its distinctive combination is:

- conversational graph authorship one edge at a time;
- arbitrary provider and model selection at each invocation;
- real multi-parent context composition rather than tree-only ancestry;
- inspectable and editable context before execution;
- immutable context and execution lineage afterward;
- first-class human steering without requiring graph-programming ceremony;
- extraction of a successful emergent path into a reusable workflow;
- tagged project and topic slices through one provenance lattice rather than copied conversation silos.

## Architectural boundaries

Lattice owns the canonical conversational graph, context-manifest contract, routing semantics, role sessions, workflow-pack contract and loader, and operator experience. A domain project owns the pack that defines its own semantics. Lattice composes these systems without collapsing their truth conditions.

| Neighbor | What it owns | Lattice relation |
|---|---|---|
| `lattice.execution` | Durable provider-neutral invocation, raw journals, checkpoints, retries, redaction, provider attestation, and subscription-authenticated CLI adapters | Internal execution boundary owned by Lattice; domain projects reach it only through Lattice submissions and retain their own semantic policy |
| Former `axis/tools/model-runner` source | Retired from the live tree after its active mechanics, tests, and consumer authority were absorbed by `lattice.execution` | Git history and Lattice's legacy checkpoint reader preserve historical recovery; no source, package, or runtime authority remains |
| Smithers ontology and upstream clone | Durable job-scale workflows, scheduling, attempts, recovery, approvals, rewind, fork, and replay | Prior art and optional adapter for compound jobs; a Smithers task imports as an expandable execution subgraph rather than defining Lattice’s canonical node scale |
| [Octopus](https://github.com/rianczerwinski/AXISVI/tree/main/axis/soma/octopus) | Captured source events, raw provenance, retrieval, and cross-platform sensory continuity | Supplies and receives evidence-bearing artifacts and interaction events |
| [Axial](https://github.com/rianczerwinski/AXISVI/tree/main/sui/console/axial) | Enduring semantic entities such as projects, people, concepts, and tasks | Supplies world-model context and receives simplified analytical projections; it is not the authoritative temporal execution ledger |
| [Slate](https://github.com/rianczerwinski/AXISVI/tree/main/sui/console/slate) | Frictionless capture and browsable topic/project surfaces | Exposes tagged slices through Lattice’s lattice without copying conversations |
| [Surface](https://github.com/rianczerwinski/AXISVI/tree/main/axis/soma/habits/surface) | Evidence-bound statements of what a workframe is, is doing, could become, and cannot presently do | Supplies project state as a versioned context module and receives provenance for material changes |
| [Panels](https://github.com/rianczerwinski/AXISVI/tree/main/axis/panel) | Persistent host for project-specific control surfaces | Natural home for the Lattice cockpit and project-grown interfaces |
| [Quaternary reference cascade](https://github.com/rianczerwinski/AXISVI/tree/main/sui/mission/projects/canon/src/canon/discovery/reference_cascade) | Domain protocol, source/search ledgers, corpus evidence, executable workflow pack, and closure claims | First externally owned workflow pack and end-to-end qualification case |

Lattice does not fork Smithers by default, does not absorb the quaternary database, and does not make its execution kernel responsible for domain routing. These are seams, not unfinished mergers.

## Canonical ontology

### Invocation node

The main visible node is one harness-level agent invocation. It may contain many internal model turns and tool calls, but it represents one coherent engagement with one compiled context and one terminal disposition.

```text
Invocation
├── direct or role-session actor binding
├── provider, model, harness, and effort
├── exact observable context manifest
├── prompt and expected response contract
├── internal execution trace
│   ├── model turns
│   ├── tool calls
│   ├── source receipts
│   └── produced artifacts
├── final response or typed failure
└── hashes, attempts, timing, and lineage
```

Internal tool calls normally remain nested so the lattice stays readable. A tool result or artifact becomes an explicit node when another invocation directly consumes it, when it carries independent authority, or when its lifecycle must survive the invocation that produced it.

### Edge

An edge records a typed relationship, not a decorative line. The minimum relation vocabulary is:

| Relation | Meaning |
|---|---|
| `inherited_context_from` | The child received a declared projection of an earlier invocation |
| `consumed_artifact` | The child received a specific file, receipt, result set, or other artifact |
| `derived_from` | A block, summary, decision, or artifact was transformed from named sources |
| `responds_to` | The invocation directly addresses a human or agent message |
| `challenges` | The invocation was created to attack a claim or proposed closure |
| `supersedes` | A later assertion replaces an earlier current assertion without erasing history |
| `validates` | An independent check supports a named claim under stated limits |
| `triggered_by` | A recorded event caused the invocation or tool action to become eligible |
| `concerns` | The node or artifact participates in a project, topic, person, or Slate slice |
| `produced` | The invocation created the linked artifact or assertion |

Every edge identifies the exact source and target revisions. A join is not a cache merge or a vague “use both branches”; it is a new invocation whose manifest names the selected contributions from several parents.

### Role and role session

A role is an optional durable function with incentives, authority limits, and a view contract. Ordinary conversation is direct: it inherits only selected ancestry, project defaults, explicit context modules, and the current prompt. Governed workflows bind named roles when their semantics actually require them. A role session is the ordered spine of invocations that perform such a function for a particular project or run.

Role identity and model identity are orthogonal. Using the same model for two roles does not make the roles share cognition, and using two providers does not create useful independence unless their prompts, evidence, and incentives differ. Conversely, a persistent role may change model only through a recorded route amendment.

The role session provides continuity without requiring every invocation to inherit the full transcript. It holds an addressable history, a stable instruction prefix, and a current role view derived from canonical state.

#### Operator-gated runner amendments

[`lattice/route_amendments.py`](lattice/route_amendments.py) implements runner substitution for role sessions. A request binds the terminal source invocation and manifest, the previous and proposed provider/harness/model routes, one or more exact dispatch-scope sets, a reason, and separate active failure and task-qualification evidence blocks with their hashes and status revisions. The failed invocation must carry one of those exact immutable scopes. The request appends `role-route.requested` and leaves that session `awaiting-operator`. Requesting a switch never approves it or launches the reserve. New dispatch is blocked transactionally, including retries and invocations sealed before the pause; another role session can continue independently. An active attempt must finish or be cancelled before the request can be accepted.

The separate decision appends `role-route.decided`: `approve` selects the proposed runner after rechecking evidence freshness; `resume-current` explicitly retains the old runner. Both require an operator note and preserve all previous invocation bytes. Duplicate identical delivery is idempotent. Every scoped batch invocation carries its canonical exact `route_scope_refs` set inside the hashed immutable manifest; append-only graph edges are a navigational projection, never the authorization source. The amended runner is mandatory for a matching scope, forbidden outside the approved scopes, and leaves nonmatching work on the workflow's ordinary role route. The recorded runner and scopes govern until another amendment; they do not widen tool permissions, certify domain competence, or rewrite the workflow's default role binding. Domain workflows own failure classification, task/lane qualification, and when to request another amendment or evidence a return. Direct one-node executions are not role sessions and do not inherit this gate.

The authenticated API exposes `GET`/`POST /api/v1/role-sessions/{session_id}/route-amendments` and a separate `POST /api/v1/role-sessions/{session_id}/route-amendments/{amendment_id}/decision`. The native CLI exposes the same boundary:

```bash
lattice route-amendment SESSION_ID
lattice route-amendment SESSION_ID --request route-amendment.json
lattice route-amendment SESSION_ID --amendment-id AMENDMENT_ID --decision approve --note "Reviewed failure and qualification for the affected task."
```

The request JSON follows `RouteAmendmentRequest` in [`lattice/contracts.py`](lattice/contracts.py): `source_invocation_id`, `proposed_route`, `reason`, `failure_evidence_block_id`, `qualification_evidence_block_id`, and canonically ordered `route_scope_sets`. API/CLI decisions use Lattice's existing operator credential boundary; that credential authenticates operator authority, not whether a human physically clicked. Autonomous consumers must not submit the decision without the operator's explicit instruction. The CLI/API history is the inspection surface; a dedicated cockpit approval control is not implemented. Source regression and falsifier checks live in [`tests/test_route_amendments.py`](tests/test_route_amendments.py); installing a committed source revision remains a separate runtime step.

### Context block

A context block is the smallest reusable, content-addressed unit that may enter an invocation. Examples include a response excerpt, source receipt, active constraint, project Surface claim, validated native lexeme, search-result batch, status projection, file, memory, tool schema, or live module output.

Every block records:

- immutable bytes or an immutable locator and content hash;
- semantic type;
- provenance and producing node;
- current status such as active, superseded, disputed, rejected, or unknown;
- authority and evidence level;
- transformation lineage if excerpted, normalized, translated, summarized, or generated;
- scope, freshness dependencies, and sensitivity policy.

A summary is never allowed to erase its source. It is a smaller context block with explicit derivation, not a replacement for raw evidence.

### Context manifest

The context manifest is the load-bearing artifact of Lattice. It is an immutable, ordered account of the observable inputs to one invocation.

At minimum it records:

| Plane | Required record |
|---|---|
| Ancestry | Selected parent invocations and exact projected portions |
| Instructions | System, developer, role, project, workflow-pack, and user instructions with versions and hashes |
| Knowledge | Files, evidence, retrieval results, memory, summaries, live modules, and manual selections |
| State | Active decisions, supersessions, uncertainties, unresolved questions, and current run revision |
| Capabilities | Tool definitions, MCP surfaces, skills, permissions, workspace, network policy, and side-effect authority |
| Execution | Provider, model, harness, effort, sampling parameters where exposed, response contract, and expected artifacts |
| Compilation | Ordered normalized messages or CLI input, transformations, omissions, token estimates, truncation, and compaction |
| Provider boundary | Observable final payload or command, plus any provider- or harness-owned context Lattice cannot inspect |
| Result | Raw journal locators, output hash, tool trace, artifacts, failure classification, and attempt lineage |

The manifest must distinguish “not selected,” “selected but summarized,” “excluded because superseded,” “unavailable,” and “silently unknown.” These states are not interchangeable. An agent given a polished but distorted slice of history can produce a coherent answer to the wrong world; preventing that is a correctness requirement, not merely a transparency feature.

Claude uses `lattice-context-documents-v11`: ordinary attributed reference documents followed by the current request. A lone current request is exactly its original UTF-8 text. Other selections retain their exact content inside collision-safe Markdown fences, with their label, plane, status, and selected/summary distinction visible; derived selections are explicitly marked. The manifest retains the full source, edit, omission, and ancestry receipts. Claude's normal system context is not replaced. Other providers retain their v10 presentation, and historical manifests retain their original bytes and system-instruction channels. The independent reader reconstructs both generations without calling the producer's renderer.

Shared fan-out compares ordered semantic selections and provenance, not provider-specific presentation bytes. Each presentation is independently reconstructed before a preview is returned and again before execution. This follows the separation between stored context and per-call model input described in [LangChain's context-engineering guidance](https://docs.langchain.com/oss/python/langchain/context-engineering); attributed documents and a task at the end follow [Anthropic's long-context guidance](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices). Fences preserve arbitrary UTF-8 source text without XML escaping or truncation. They do not guarantee that a model obeys the intended boundary. Portable ancestry is observable prior text, not native assistant-role history, hidden reasoning, or a merged provider session.

The execution kernel can still journal and hash an explicitly declared official system-prompt file for historical requests and direct kernel callers. Muse lacks a native response-schema flag, so its adapter deterministically appends the schema contract to a separate provider prompt file and journals both byte streams. Native continuation records its selected provider session as opaque context: Lattice can prove which session it resumed or forked, but not the exact hidden history or compaction state supplied by the provider. Provider-owned instructions and admin policy remain proof limits. [The architectural decision](dev/decisions/001-provider-context-views.md) defines this boundary. [`ops/qualify_context.py`](ops/qualify_context.py) provides bounded installed-Claude qualification; its live results are capability evidence for the frozen cases and runtime, not a general reliability estimate.

The [installed context qualification](assurance/context-qualification-20260909T182639.json) is partial: runtime `0.3.1-a47e5f2a20a6` accepted the original standalone eligibility fixture and both synthetic parent-generation requests. The edited join was refused with `reasoning_extraction`; two bounded XML variants were also refused, including descriptive source labels. Provider trials stopped after three consecutive join refusals, and the conflicting-reference case remains unrun. The local representation change is installed, but edited-join qualification is blocked. This does not prove that all multi-parent context is unsupported, and it does not qualify the English production queue.

### Artifact

An artifact is a durable object whose meaning survives its generating invocation: a source receipt, file, structured response, query plan, profile, evidence set, decision, validation report, or exported workflow. It has content identity, provenance, schema identity where applicable, and lifecycle status.

### Trigger

A trigger is a declarative condition over canonical state that makes a new invocation or action eligible. Triggers may respond to a human decision, a failed canary, a newly observed native heading, source contradiction, unresolved coverage region, stale dependency, or proposed stopping claim. A trigger creates a visible planned action; it never secretly sends a model call.

### Slice

A slice is a saved query over the lattice, not a copy of its contents. A Slate topic such as `Canon > Quaternary > German` can therefore expose every relevant human message, model invocation, query, source receipt, result, decision, and residual while preserving each object’s single canonical identity. When a new governed project succeeds a historical project at the same stable Slate address, Lattice transfers that external identity in the same transaction as project creation, preserves the predecessor slice by its internal identity and concern query, and records the exact supersession relation; it never upgrades the predecessor in place or lets an ordinary create overwrite the address.

### Workflow pack

A workflow pack supplies domain semantics without changing the Lattice engine. It declares roles, view projections, artifact schemas, allowed transitions, evidence triggers, tool adapters, assurance rules, and closure language. The project whose meaning it encodes owns its editable pack; Lattice loads explicitly configured roots, refuses duplicate identities rather than applying precedence, and binds every installed generation to the pack's canonical hash. Canon's quaternary workflow is the first external pack; Platform Foundry is presently bundled because its adapter and definition are part of this repository's qualification surface.

## Execution grammar

The core user-facing operations are intentionally small:

```text
preview(batch_id, invocation_specs) -> exact context + confirmation reasons
invoke(actor_binding, prompt, context_selection, route) -> invocation
fan_out(context_assembly, routes) -> atomic invocation batch
fork(invocation, actor_binding, route, context_overrides) -> invocation
join(invocations, actor_binding, route, context_selection) -> invocation
inspect(invocation) -> context_manifest + trace
diff(invocation_a, invocation_b) -> context_and_execution_delta
replay(invocation, explicit_overrides) -> new_invocation
tag(object, slice) -> relation
extract(path_or_subgraph) -> workflow_template
```

`replay` never overwrites the original and never implies deterministic output. `fork` creates a new descendant with explicit inheritance. `join` compiles a fresh context from named blocks; it does not concatenate whole transcripts or pretend that incompatible branches jointly occurred. `extract` captures the proven dependency and trigger structure of a path while leaving project-specific evidence as parameters.

Send, side quest, fan-out, cross-feed, and return are cockpit gestures over the same batch primitive rather than separate kernel state machines. The ordinary interaction is:

1. The user selects a route and writes the next conversational edge.
2. Lattice previews exact context without persisting keystroke drafts.
3. If no deterministic authority or context-loss reason requires confirmation, Lattice atomically seals the invocation batch and starts execution in the same action.
4. Each selected runtime executes through its own leased, journaled action and route-specific manifest.
5. Lattice persists the response, trace, artifacts, and edges before projecting the new state.
6. The next frontier is derived from the updated state.

Automation may execute several already-authorized edges, but it must remain possible to see the currently executing action, its context delta, and why it became eligible.

Each edge also chooses one of three explicit harness profiles. `compiled-fresh` is the correctness baseline: exact compiled context, no tools, no workspace, and no persistent provider session. `workspace-agent` starts a recorded Claude or Codex session with the workspace, tools, sandbox, permissions, approval policy, and network boundary visible in the composer. `native-continuation` resumes or forks one explicitly selected provider-backed parent only after Lattice resolves its observed session receipt and proves that provider, model, workspace, and authority are unchanged. Portable graph ancestry remains available regardless of native-session eligibility.

Interactive `on-request` execution admits only Claude workspace sessions with the Bash tool and no pre-granted permission rules. Its source implementation connects exact-action cockpit decisions to the official Claude SDK through Lattice's guarded process runner, with one-use grants, joined response handlers, and independently retained failure causes. [Native approval evidence](assurance/native-approvals-20260912T093656/README.md) distinguishes source checks, synthetic control peers, native action probes, and installed-service qualification. Admission of that narrow route does not itself qualify its installed behavior or the overall execution boundary.

## The canonical blackboard and role views

The lattice must not coordinate agents through one ever-growing communal Markdown document. It also must not create one editable “truth” document per agent. Both shapes produce drift, duplication, and accidental authority.

Canonical state is a structured, append-only blackboard with derived current views. It includes:

- project purpose and governing constraints;
- active and superseded decisions;
- role definitions and session histories;
- validated evidence and unresolved claims;
- coverage or task maps;
- planned, leased, completed, failed, and interrupted actions;
- immutable exchanges and produced artifacts;
- challenge results and closure evidence;
- dependency hashes and freshness state.

Each role receives the smallest faithful projection needed for its function:

| Role view | Includes | Deliberately excludes |
|---|---|---|
| Orchestrator | Scope, coverage, yields, active hypotheses, decisions, unresolved territory, available actions | Raw repetition and irrelevant provider logs |
| Native-language partner | Native lexicon, source-supported genre evidence, representative raw titles and headings, previous native queries and yields, unexplored patterns | Corpus-level administrative noise and the evaluator’s sealed answers |
| Deterministic searcher | Exact query bytes, source binding, route, cursor, pagination contract, lease, and write destination | Strategic reasoning and authority to rewrite the query |
| Countersearcher | Coverage state, executed queries, yields, unresolved evidence, and source capabilities | The incumbent orchestrator’s persuasive stopping rationale and preferred conclusion |
| Verifier | Base records, raw receipts, frozen contracts, and expected invariants | Producer-owned summaries and producer classifications as authority |
| Human peephole | Recent runner/native exchange, accepted or rejected contribution, current action, and linked broader state | The full operational ledger unless explicitly expanded |

The blackboard is the continuity mechanism. Persistent model conversations are useful for fluency and cache locality, but no hidden model state may become the only copy of a decision, term, result, or plan.

## Context and KV-cache economics

A naïve conversational lattice would be economically pathological: every node would receive the entire graph, branch joins would repeatedly reprocess huge histories, and each role would accumulate irrelevant context. Lattice instead uses semantic locality: every invocation sees the smallest faithful world required for its function.

The normal topology is a small number of persistent, cache-friendly role spines connected by compact delta packets:

```text
Coverage orchestrator  O0 ── O1 ── O2 ── O3
                            │  ▲      │  ▲
                    intent  │  │      │  │ evidence delta
                            ▼  │      ▼  │
Native partner         N0 ── N1 ── N2 ── N3
                                 │
                           exact query actions
                                 ▼
                    deterministic search/tools
```

This changes the economics in five ways:

- Stable instructions, schemas, project state, and shared ancestry form the prefix; volatile branch evidence and the current prompt come later.
- Large evidence lives in content-addressed blocks and is retrieved or excerpted by identity instead of copied into every role history.
- Cross-role messages are compact artifacts with lineage, not full transcript relays.
- Branches are created only when evidence or the human demands a genuinely different context or incentive structure.
- Joins compile the minimum necessary contributions from each branch instead of inheriting every branch wholesale.

KV caches still cannot be merged. Once token prefixes diverge, later states are not composable; providers cannot share cache state; consumer subscription CLIs may not expose caching at all. Lattice therefore treats cache reuse as an opportunistic performance benefit, never a correctness dependency.

Node identity is valuable precisely because it does not require KV merging. It makes complete-history inheritance unnecessary. A node can name a prior artifact, decision, or evidence block without replaying the entire path that produced it, while a user can still expand that lineage when needed.

Context order should normally be:

```text
stable provider/harness prefix
stable Lattice and workflow-pack rules
stable role contract and tool schemas
versioned project state
selected shared ancestry
branch-specific evidence delta
current prompt
```

Changing an early prefix has a broad performance cost and must create a visible dependency revision. Summaries and embeddings may reduce prompt size, but neither is allowed to become an unaudited substitute for source state.

## The multilingual quaternary workflow

The quaternary workflow is the first real proof that Lattice can conduct adaptive research without either freezing inquiry in advance or wandering invisibly.

Its epistemic structure has three layers:

1. A fixed recall floor declares the functional forms, mission regions, language lanes, source ecologies, and assurance rules that every run must cover.
2. An adaptive loop learns native bibliographic language and new search regions from retrieved evidence through append-only amendments.
3. An adversarial closure layer challenges the strongest remaining stopping claim and preserves the proof limit.

This is neither a static list of translated queries nor an unconstrained agent improvisation. The invariant search floor is known ahead of time; the actual search language and productive native routes are learned while searching; every adaptation remains inspectable.

### Current role binding

| Function | Current route | Authority |
|---|---|---|
| Coverage orchestrator | GPT-5.6 Sol | Maintains mission scope, coverage model, search hypotheses, action economics, and candidate closure; cannot execute searches or verify its own closure |
| Native bibliographic partner | Gemini 3.7 for German | Proposes native genres, terms, headings, source semantics, literal query strings, and interpretation of native results; cannot validate its own terms or write searches directly |
| Searcher | Deterministic source adapters | Executes exact frozen query bytes, pagination, receipts, and result preservation; has no discretion to paraphrase intent |
| Terminology surfacer | Deterministic extraction and local embeddings, followed by native interpretation | Detects recurring title constructions, headings, series, abbreviations, and neighboring vocabulary; cannot admit works or silently add semantic terms |
| Countersearcher | Claude Opus 5 in a fresh, deliberately opposed context | Receives only the compiled countersearch packet, searches for materially different routes and evidence against closure, and cannot close the run |
| Verifier | Independent deterministic verifier plus sealed oracles | Recomputes contracts and closure evidence from base records; does not reuse producer classifications as authority |

The model names are configuration, not engine branches. Another language may select different routes after equivalent qualification. “No Fable” is a current workflow constraint: Fable is not a participant in this model set.

### Native-language loop

The native partner participates on both sides of retrieval. This native sandwich is essential because query formulation and result interpretation expose different parts of the bibliographic culture.

```text
orchestrator defines a semantic search intent
    ↓
native partner proposes exact native-language strings, controlled headings, source-specific expressions, and alternatives
    ↓
validator checks source-backed language and typed-token constraints
    ↓
accepted queries become append-only, leased manifest amendments
    ↓
deterministic searcher executes the exact bytes and preserves every page and result
    ↓
terminology surfacer identifies recurring native constructions without judging membership
    ↓
native partner interprets those patterns and proposes new native regions or repairs
    ↓
orchestrator updates the coverage map, yields, hypotheses, and next intent
    ↺
```

The orchestrator does not need to know in advance every query it will want. It must know what function or coverage region it is trying to probe. The native partner converts that intent into culturally and source-appropriate search language. Results then teach both roles what the initial ontology missed.

The sequence may include a short native poll before the next search: “Given the regions sampled, the exact queries and yields, and these newly observed headings, which materially different native naming regions remain?” This is not a generic brainstorm. Every proposal must point to a coverage gap, source behavior, or observed bibliographic signal.

### Static language state and dynamic consultation

The validated language profile is not a giant lookup table that attempts to pre-enumerate the language. It is the accumulated memory and constraint layer of the dynamic loop.

Dynamic consultation discovers candidate terms, constructions, headings, false friends, script/orthography lanes, source syntax, and query strategies. The static profile remembers which exact bytes were validated, where they came from, what they mean functionally, which sources and jurisdictions they apply to, and which failures must not recur.

Both layers are necessary:

- Without the dynamic partner, the search is trapped inside an English-shaped ontology and cannot learn from the native bibliography it encounters.
- Without validated persistent state, the same terms are repeatedly reinvented, translations drift between turns, hallucinated forms can enter searches, source-specific syntax is lost, and later verification cannot reconstruct what the queries meant.

The profile therefore grows through source-backed amendments. A term proposed by a model remains a proposal until supported by a language-tagged authority, repeated native source use, or another declared validation route. Exact query strings and every disposition remain in the exchange ledger.

Script checks are only consistency checks. German is not validated because a string uses Latin characters, just as Korean is not validated merely because it contains Hangul. Language, bibliographic function, locale, and source semantics are separate claims.

### Result-derived terminology surfacing

“Result-pattern extraction” means deriving new search clues from the retrieved bibliographic culture, not asking a model to declare what the works are.

The deterministic layer groups and ranks recurring features such as:

- title phrases and title-order constructions;
- controlled subject or genre headings;
- classification neighborhoods;
- series, publishers, review venues, and institutional lineages;
- abbreviations and historical orthographies;
- co-occurring form and population terms;
- terminology that appears preferentially in positive-suspicion results;
- unexpected neighboring genres or source-specific field values.

Local embeddings may propose semantic neighbors. They may not merge identities, reject works, validate language, or promote a term into the query compiler. The native partner interprets ambiguous patterns; source evidence validates useful proposals; the orchestrator decides whether the resulting search region is materially new.

Query expansion must occur as separate, attributable search arms rather than an indiscriminate OR-expression. This prevents a productive new term from being hidden inside an enormous noisy result set and makes query drift measurable.

### Search execution

The searcher is deliberately non-linguistic. It receives:

- exact UTF-8 query bytes;
- source-profile and route versions;
- field/operator semantics;
- expected pagination or partition behavior;
- a positive sentinel or other access proof;
- a lease and write destination;
- coverage eligibility and preparation/run identity.

It persists raw response receipts before parsing, records every result regardless of screening outcome, and cannot replace the query with a paraphrase. Authentication, route failure, zero results, and source semantics remain distinct states.

When a source or provider is blocked by a login, membership action, CAPTCHA, or another simple human-resolvable boundary, the workflow makes one normal attempt and one declared alternate attempt, then emits a concise handoff naming the blocked action and the human-facing page. It sidelines that lane while independent work continues. It must not spend an unbounded research run engineering around a click the user can complete directly.

### Closure challenge

An orchestrator’s inclination to say “continued searching has low ROI,” “this region seems exhausted,” “no new patterns remain,” or “the language may be complete” is not closure evidence. It is a trigger for opposition.

The countersearcher receives one bounded projection, `CountersearchPacket/1.0`, compiled from the executed-arm ledger, yields, coverage map, unresolved regions, source capabilities, validated native forms, and deterministic result patterns. It does not receive the transcript, raw result corpus, closure candidate, or incumbent orchestrator’s persuasive rationale. The transition can declare both a minimum and maximum evidence-block count; Canon sets both to one so the runtime rejects any extra context. From that packet, the countersearcher must propose the strongest materially different search routes available, including neglected native genres, alternate controlled headings, source jurisdictions, title constructions, classification neighborhoods, or query decompositions.

Those proposals pass the same language validation and deterministic execution path as ordinary queries. If they reveal a new plausible work, a productive native naming family, or a previously unsearched region, the stopping claim is falsified and the main loop resumes. If the strongest distinct routes produce no such novelty, closure becomes better grounded.

The challenger’s usefulness comes from different context and incentives, not merely from adding another model. A second model given the same summary and asked to agree is not independent. The current Sol-versus-Opus split supplies provider-family diversity, while packet blindness and opposed authority supply the load-bearing independence; provider diversity by itself proves nothing.

No token budget, call count, wall-clock duration, or conversational estimate may serve as the stopping rule. These are operating constraints, not evidence about a literature. The run closes only at the claim level authorized by the quaternary protocol. It may establish that the declared multilingual search process was executed, preserved, challenged, and independently verified. It cannot prove that no unknown publication exists.

### Quaternary state projections

The shared blackboard should generate these views rather than hand-maintain them:

| View | Core contents |
|---|---|
| Coverage overview | Forms, mission regions, language lanes, source ecologies, sampled regions, yields, gaps, and challenge state |
| Orchestrator packet | Current coverage delta, recent yield, hypotheses, unresolved signals, and eligible next actions |
| Native packet | Validated lexicon, representative raw records, recent query/yield pairs, newly surfaced constructions, and unexplored native regions |
| Search action | Exact query, source, route, cursor/partition plan, sentinel, lease, and result destination |
| Peephole | Last runner/native exchange, accepted or rejected contribution, current action, and broader-action link |
| Human status | What phase is actually complete, current counts by evidence level, active blocker if any, and the explicit proof limit |

The current `gatherctl peek` concept is the right scale for the peephole. It should become a Lattice projection, not a second operational ledger.

## Generic research grammar

The quaternary loop is not an isolated curiosity. It instantiates a general pattern:

```text
hypothesis about a search or design space
    ↓
domain-native formulation
    ↓
bounded action through a deterministic or governed tool
    ↓
evidence and newly observed structure
    ↓
revised hypothesis and coverage state
    ↓
discriminating action or adversarial countersearch
    ↓
bounded, evidence-typed closure
```

Platform Foundry has the same shape: a platform hypothesis becomes a source-native probe, which produces evidence, which revises the hypothesis, which selects a discriminating probe, which supports a verified specification. Field-survey research likewise routes domain generation through source grounding, cross-model criticism, and promoted outputs.

Lattice should therefore generalize the mechanics, not the domain semantics. The engine owns roles, nodes, edges, manifests, artifacts, triggers, routing, provenance, leases, recovery, views, and the pack-loading contract. Project-owned workflow packs define what counts as evidence, which roles exist, which actions are permitted, and what closure means.

## Operator experience

The graph must remain available without becoming the homepage-sized hairball that the user must comprehend before acting.

The primary surface is response-first and project-local:

- The current answer, artifact, or project state is foregrounded.
- Selecting a claim or paragraph illuminates the causal subgraph that produced it.
- Selecting an invocation reveals its exact context manifest, tool trace, output, and proof limits.
- A context tray shows what the next invocation will receive and how it differs from its nearest relevant predecessor.
- The user normally maintains two or three live trailheads; dormant alternatives recede into provenance until reopened.
- Branch comparison emphasizes differences in context, evidence, and conclusions rather than merely displaying two long responses side by side.
- Project Panels provide habitual workspaces; Slate topics provide cross-project tagged slices; the total lattice remains underneath both.

The user should not need to create schemas, name every edge, or draw a graph before speaking. The interface infers a proposed edge and context selection, makes consequential choices visible, and accepts correction before execution. Routine provenance is automatic; authority-changing or meaning-changing choices remain legible.

A blank project opens directly into the composer and can speak to any qualified model without creating a role or workflow. Existing conversations, files, decisions, people, and model work may then be recovered or grafted into inspectable branches without becoming a prerequisite for ordinary use.

## Failure model and non-negotiable invariants

| Failure | Consequence | Invariant |
|---|---|---|
| An invocation receives omitted or superseded constraints without disclosure | A fluent answer is produced against the wrong reality | Every included, transformed, excluded, and unknown context state is manifest-addressable |
| A branch join silently treats incompatible claims as jointly true | False consensus enters later reasoning | Joins select typed contributions and preserve contradiction/status |
| A model response directly becomes validated state or an external action | Hallucination or prompt drift changes the run | Proposals cross an explicit validator/authority boundary before activation |
| A searcher paraphrases a native query | Coverage evidence no longer corresponds to the proposed search | Exact compiled bytes are frozen and receipt-bound |
| A low-signal result disappears | The system creates an unrecoverable false negative | Every returned result remains in the ledger |
| A producer verifies its own completion view | Circular assurance can certify missing work | Closure is independently recomputed from base artifacts and receipts |
| “Low ROI” directly closes a run | Search ends at the orchestrator’s blind spot | Every material stopping claim triggers countersearch |
| Full histories are copied into every node | Context and KV costs explode; relevant evidence is diluted | Role views and content-addressed blocks enforce semantic locality |
| Hidden provider context is represented as exact | The context inspector lies at its most consequential boundary | Opaque provider and harness additions are named proof limits |
| Model/provider identity is mistaken for role independence | Debate theater replaces real opposition | Independence is evaluated through evidence, context, incentives, and authority |
| Conversation state exists only inside a provider session | A crash, model switch, or expiry destroys project truth | Canonical blackboard and journals remain external to every provider |
| A graph viewer becomes the only way to navigate | Realistic scale becomes an unusable hairball | Local project views, trailheads, search, and slices dominate the UX |

## Build architecture

The logical architecture should be implemented in five separable layers.

### 1. Provenance and state kernel

An append-only event ledger stores nodes, typed edges, role sessions, context blocks, manifests, artifacts, attempts, leases, triggers, decisions, and slice relations. Current state is derived. Large immutable bytes may live in a content-addressed blob store so long as the ledger binds their identity and lifecycle.

SQLite plus content-addressed local blobs is the natural first physical implementation because the system is local-first, transactional, and initially single-user. The logical contracts must not depend on SQLite-specific query behavior so that later scale or synchronization work can change the store without changing Lattice’s meaning.

### 2. Context compositor

The compositor resolves manual selections, role views, project state, retrieval, files, live modules, tools, and ancestry into one immutable manifest. Compilation has two products:

- a provider-neutral semantic manifest that remains canonical;
- a provider/harness-specific request representation and declared opacity boundary.

The compositor is independently testable. Provider adapters may not silently append project knowledge, rewrite selected evidence, or mutate prior manifests.

### 3. Execution adapters

Simple invocations use the internal `lattice.execution` kernel for durable subscription-authenticated execution. A singleton submission is automatically one invocation node; related submissions may form a batch or a governed workflow without creating a second execution path. Compound agent jobs may delegate to Smithers through an adapter. Deterministic tools such as catalogue search execute through workflow-specific adapters. Every path returns the same Lattice-level invocation, trace, and artifact contracts.

The runtime must support interruption, typed failures, lease recovery, idempotent request reuse, append-only retries, and restart without provider-specific state becoming canonical.

### 4. Routing and workflow packs

The routing engine evaluates explicit triggers against canonical state and produces a frontier of eligible actions. It does not decide domain truth. Workflow packs define roles, view compilers, source/tool bindings, proposal validation, triggers, and closure contracts.

The routing engine supports both modes:

- manual Lattice mode, where the user chooses or edits one proposed edge;
- governed workflow mode, where previously authorized actions can advance automatically until a real decision, authority boundary, or blocker occurs.

### 5. Cockpit and projections

The cockpit exposes the response-first conversation, graph neighborhood, context tray, manifest inspector, branch comparison, live action, and trailheads. The same underlying lattice supplies Slate slices, Panel views, status documents, and exported workflow templates.

No projection becomes a competing source of truth. A hand-edited Markdown status or copied conversation cannot outrank the ledger objects from which it was rendered.

## Build sequence

The implementation should proceed through one real vertical slice rather than building a universal graph framework in isolation.

### Phase A — Contracts and truthful storage

Implement stable IDs and schemas for invocation nodes, typed edges, context blocks, context manifests, artifacts, role sessions, attempts, triggers, and slices. Add append-only storage, content hashes, exact revision binding, and an independent context-manifest reader. Prove restart and replay lineage before adding a UI.

Closure evidence:

- A two-parent join can be reconstructed from base records with every included and omitted contribution identified.
- Altering any prompt, block, transformation, tool definition, provider setting, or output invalidates the manifest hash.
- A provider-opaque instruction is displayed as unknown rather than synthesized.
- A deliberately omitted governing constraint is detected by a manifest oracle.

### Phase B — One-edge manual Lattice

Connect the internal execution kernel, create persistent role sessions, implement `invoke`, `fork`, `join`, `inspect`, `diff`, and `replay`, and expose a minimal conversational surface plus context tray. Use ordinary singleton submissions before integrating compound Smithers jobs.

Closure evidence:

- A user can route `P1` to one model, `P2` to another, fork `P3` to both, cross-feed each response, and inspect exactly what each invocation observably received.
- Restart preserves the graph and permits a new descendant without copying the whole transcript.
- Two branches with different context cannot be displayed as identical inheritance.
- Replay creates new lineage and never mutates the prior invocation.

### Phase C — Quaternary workflow pack

Bind Sol as the coverage orchestrator, Gemini 3.7 as the German native role, Claude Opus 5 as the blinded countersearcher, attach deterministic source adapters, import the quaternary ledger as domain state, and implement the native sandwich, terminology surfacing, append-only adaptive queries, `peek`, and countersearch trigger.

German is the canary because it exercises genuine native-language separation without introducing script segmentation as the first confound. The canary must coexist with historical English and Korean state without rewriting it.

Closure evidence:

- The orchestrator can express intent without authoring the final German query.
- The native role emits exact German queries with source-backed terminology and every exchange is inspectable.
- The searcher executes unchanged bytes and preserves raw receipts and every result.
- Retrieved German patterns can produce a validated profile amendment and a materially new query arm.
- A stopping inclination automatically produces a blinded challenge action.
- A challenger discovery reopens the loop; an unproductive challenge records bounded closure evidence rather than absolute exhaustion.
- English and Korean hashes remain unchanged.

### Phase D — Cockpit, trailheads, and slices

Build the response-first Lattice view, local graph neighborhood, claim-to-lineage highlighting, context diff, branch comparison, and two-or-three-trailhead discipline. Bind `Canon > Quaternary > German` as a live Slate slice and expose the project through a Panel.

Closure evidence:

- The full graph remains navigable without being required for routine action.
- A claim in a rendered answer can return to its invocation, context, evidence, and source receipts.
- A Slate slice changes when a relation changes but never duplicates or forks the underlying object.
- Dormant branches remain recoverable without cluttering the active surface.

### Phase E — Workflow extraction and generalization

Extract the proven quaternary path into a parameterized workflow template. Exercise the same engine with one structurally different workflow such as Platform Foundry before promoting any quaternary-specific mechanism into the core.

Closure evidence:

- Domain-specific roles, evidence rules, and closure language remain in workflow packs.
- The core contains no German, bibliography, or Canon-specific branches.
- The extracted workflow can reproduce the same action and provenance topology from a fresh language profile while accepting evidence-triggered amendments.
- The second workflow reuses the kernel without accepting quaternary semantics it does not need.

The closure sequence for every phase is `implement → test → adversarial mutation → remediate → repeat → close`. Producer tests alone are candidate evidence, not closure.

## Required adversarial cases

The permanent assurance suite must reject at least these mutations:

- remove an inherited constraint while leaving the rendered response unchanged;
- mark a summarized block as verbatim;
- detach evidence from the claim that consumed it;
- join two contradictory branches without preserving the contradiction;
- mutate a context block after an invocation completes;
- reuse a stale role view after its dependency changes;
- inject an unrecorded file, skill, rule, tool, or MCP surface;
- claim exact provider context when provider-owned instructions are opaque;
- execute an unleased model or search action;
- overwrite an accepted response with a retry;
- treat an authentication or rate-limit failure as semantic model failure;
- let a model proposal enter validated language state without evidence;
- alter native query bytes between acceptance and execution;
- accept zero results after a broken source route;
- omit a returned result because screening labeled it low signal;
- let an orchestrator close its own run without countersearch and independent verification;
- give the countersearcher the incumbent’s persuasive stopping narrative;
- treat two identical role prompts as independent debate merely because model names differ;
- reissue the full lattice to a role when its declared view is sufficient;
- let one project or language revision stale an unrelated role session;
- import a Smithers task as an opaque final answer when a consumed internal artifact needs explicit lineage;
- copy a Lattice conversation into Slate instead of storing a slice relation;
- render a generated status that disagrees with the base ledger revision.

## Prior art and what Lattice takes from it

No single precedent supplies the complete object. The build should compose established mechanisms rather than confuse component novelty with system novelty.

| Prior art | Useful mechanism | Lattice extension |
|---|---|---|
| [gptel](https://gptel.org/manual.html) | Editable conversational branches, per-heading model/system configuration, inspectable requests | Multi-parent lattice, durable manifests, project slices, and governed execution |
| [Grafychat](https://www.grafychat.com/d/docs/canvas-and-nodes) and Obsidian canvas chat tools | Visual model-selectable conversation nodes | Exact context truth, genuine joins, and external project continuity |
| [Loom](https://github.com/socketteer/loom) | Branching generation topology | Conversational and agent-invocation ontology rather than alternative story completions |
| [ChainForge](https://www.chainforge.ai/) and [PromptChainer](https://arxiv.org/abs/2203.06566) | Visual multi-model dataflow and intermediate routing | Progressively authored rather than substantially preprogrammed graphs |
| [Langfuse](https://langfuse.com/docs) and [Phoenix](https://arize.com/docs/phoenix/tracing/llm-traces) | Prompt, tool, parameter, and trace observability | Context authorship before execution and durable conversational continuation afterward |
| Smithers | Dynamic durable workflows, attempts, scheduling, agent substrate, recovery, rewind, and replay | A live-in conversational center of gravity and finer invocation/context ontology |
| [MIRACLE](https://terpconnect.umd.edu/~oard/pdf/talip03he.pdf) | Interactive translingual search with user participation in translation and retrieval | Persistent native-agent co-investigation with exact query and evidence lineage |
| [Bates’s berrypicking model](https://pages.gseis.ucla.edu/faculty/bates/berrypicking.html) | Search needs and queries evolve as results reveal the territory | Machine-governed adaptive manifests and explicit coverage state |
| [Session context in cross-language information retrieval](https://aclanthology.org/L16-1064/) | Earlier interaction improves later cross-language query interpretation | Persistent role spines and result-to-native feedback |
| [Rewrite–Retrieve–Read](https://aclanthology.org/2023.emnlp-main.322/) | Separate query formulation from retrieval and downstream reading | Native query composer, deterministic retriever, and evidence-bound update loop |
| [TREC Total Recall](https://pages.nist.gov/trec-browser/trec25/recall/proceedings/) | Iterative high-recall retrieval guided by reviewed evidence | Recall-first discovery with explicit challenger and bounded claim language |
| [Query-expansion failure analysis](https://aclanthology.org/2024.findings-eacl.134/) | Expansion can introduce drift and noise | Separate attributable query arms rather than undifferentiated expansion |
| [Multi-agent debate evaluation](https://arxiv.org/abs/2502.08788) | More agents do not reliably improve answers without useful diversity | Roles differ in evidence, incentives, context, and authority; headcount is not assurance |
| Blackboard architectures | Specialists coordinate through shared external state | One canonical structured blackboard with role-specific projections |

The novelty claim that survives is the integrated object: a local-first, cross-provider, progressively authored conversational lattice with exact context sculpture, durable project continuity, evidence-triggered role routing, and tagged project/topic embodiment.

## Settled design decisions

- Lattice is a custom harness and product, not a feature hidden inside the quaternary runner.
- The canonical graph is invocation-scale; tool loops are nested and expandable.
- The graph is authored conversationally and may later yield workflows.
- Context manifests are first-class immutable artifacts.
- Canonical state lives outside every model and provider session.
- Roles receive derived views rather than the full graph.
- Role identity is separate from model identity.
- The quaternary workflow uses Sol for coverage orchestration, Gemini 3.7 through Antigravity for the German native role, and Opus 5 for blinded countersearch; Fable is absent.
- Native consultation occurs before and after retrieval.
- Searching is deterministic and executes exact accepted bytes.
- Validated static language state complements rather than replaces dynamic native consultation.
- Result-derived terminology may propose new search space but cannot adjudicate works.
- A stopping inclination triggers blinded countersearch and cannot directly close the run.
- Smithers remains an optional job-scale substrate through an adapter; `lattice.execution` is the sole direct subscription-model invocation kernel.
- Slate topics are tagged slices, not copied conversations.
- KV reuse is optimized but never relied upon for correctness.

## Open engineering decisions

These remain real implementation choices and should not be decided invisibly by the first builder:

### Event and graph storage boundary

The natural first implementation is SQLite for transactional metadata plus content-addressed local blobs for large immutable payloads. Before freezing it, measure the expected Octopus ingestion volume, graph-neighborhood query shape, and synchronization requirements. The logical contract must remain stable whichever physical store wins.

### Smithers adapter depth

The minimum adapter may import only job-level inputs, outputs, attempts, and artifacts. A deeper adapter may project internal Smithers task graphs into expandable Lattice subgraphs. Choose the deeper route only when a real Lattice consumer needs to address an internal artifact or branch; otherwise preserve the clean job boundary.

### Provider-native session reuse

Native sessions may improve continuity and latency, while portable fresh calls improve reproducibility and context control. The manifest must make the choice visible. Correctness must survive a fresh-session replay even when native continuation is preferred operationally.

### Context-module authority

Live modules such as weather, calendar, project status, or retrieval can be manually selected, role-defaulted, or mandatory under a workflow pack. The operator experience should distinguish these modes clearly; the system must not quietly convert a convenience module into an unoverrideable global truth source.

### Human approval granularity

Manual one-edge-at-a-time steering and governed automatic advancement must share one ledger. The remaining product decision is which trigger/action classes may run automatically in each project. This is an authority preference, not merely an implementation detail, and belongs in visible project policy.

## Definition of the first complete system

Lattice’s first complete system is not “a graph renders” and not “several models exchanged messages.” It is the following observable end state:

1. A user opens the German quaternary project and sees its actual current state, live trailheads, recent model exchange, coverage gaps, and proof limits.
2. The user or orchestrator proposes a semantic search region; the native role returns exact German query alternatives; the accepted query is visibly validated and frozen.
3. A deterministic source adapter executes it and every page and result enters the canonical evidence ledger.
4. New native constructions become inspectable proposals, are interpreted by the native role, and may create a new append-only search arm.
5. Every Sol and Gemini invocation can be opened to reveal its observable context, selected ancestry, tools, output, and opaque provider boundary.
6. The system survives interruption and resumes without trusting hidden provider memory.
7. A proposed stopping point automatically invokes a contextually independent challenger; the result either resumes search or strengthens bounded closure.
8. The complete path is visible as the `Canon > Quaternary > German` slice without duplicating its nodes.
9. The proven path can be extracted into the next-language workflow while preserving room for native evidence to alter its search space.
10. Independent mutation tests demonstrate that omitted context, dropped results, unvalidated language, query alteration, stale state, and self-certified closure cannot pass as success.

At that point Lattice is no longer a diagrammatic idea. It is the reusable cognition-and-action substrate that the quaternary program, Platform Foundry, field surveys, and later project-specific Panels can inhabit.

## Design sources

- [Quaternary corpus protocol](https://github.com/rianczerwinski/AXISVI/blob/main/sui/mission/projects/canon/src/canon/discovery/reference_cascade/PROTOCOL.md)
- [Execution submission and result contracts](lattice/contracts.py)
- [Octopus evidence-bound Platform Foundry decision](https://github.com/rianczerwinski/AXISVI/blob/main/axis/soma/octopus/decisions/061-evidence-bound-platform-foundry.md)
