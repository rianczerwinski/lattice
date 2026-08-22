# Audit — audit:loom-closure-system

Contract: **valid**. Evidentiary assurance: **closed**. Admissible state: **closed-audited**.

Canonical bytes: `78e11f89f86c81ea07fd102edbb526ace18c1b9352fe395e4ecaf2f36915f72b`. Source revision: `f3a12b6bc3983ce026e3618c5c9053fd59a6ebe6`. Surface bytes: `d50e9f51aa28d31168f7371653e3c2a35c07f1a72ccf05ff96a36a1e6b6dec3d`.

## Material findings

- **S1** · `finding:0baef75f-9b6d-50ff-b934-5de166e7765a` · External workflow writes have no durable idempotency boundary · verified. Consequence: Canonical quaternary state and Loom history can diverge precisely at the authority boundary the workflow exists to govern.
- **S1** · `finding:18396742-d087-5a8f-b47d-58dc64cb5743` · Workflow role authority could be bypassed after draft creation · verified. Consequence: A workflow role could be impersonated after the cockpit had shown the correct boundary.
- **S1** · `finding:2847cb6d-2641-5343-a07b-9e041fca3b5b` · Context and result aggregation can exhaust memory before admission · verified. Consequence: A local invocation could terminate the supervisor or starve unrelated projects instead of producing a typed bounded failure.
- **S1** · `finding:460a7741-b034-5fe5-9636-a95f204eb8e6` · Provider output is journaled before aggregate admission · verified. Consequence: Downstream observers could consume bytes that the immutable result later claimed were never admitted.
- **S1** · `finding:5caf7b10-7ed3-5925-a5ff-6320e39c0b81` · Exact-context claims exceed what is validated and shown · verified. Consequence: The central promise—knowing exactly what each agent saw—can be confidently but incorrectly believed.
- **S1** · `finding:6f4216ba-bbec-525f-9b78-b6d097b85b37` · Structured provider output is only partially validated · verified. Consequence: Model prose could cross a deterministic workflow authority boundary as if it satisfied the declared contract.
- **S1** · `finding:6f54f5fd-e79e-5a3e-8aa5-a18d6570cd71` · Workflow packs are declarations without an enforcing engine · verified. Consequence: The first complete German workflow and any later workflow pack cannot inherit their declared epistemic guarantees from Loom.
- **S1** · `finding:9a3e943d-7991-5bfa-b068-03ee36cc6cd6` · The append-only ledger cannot rebuild its derived state · verified. Consequence: A broad storage fault invalidates the durable-history guarantee and leaves no independent canonical recovery path.
- **S1** · `finding:bf16b55f-a7fb-5ee1-8872-29bcef26ee99` · Transient context previews masquerade as durable manifests · verified. Consequence: The operator could approve one world while a different world became the permanent invocation.
- **S1** · `finding:d0f25a02-af37-5a00-b0c5-9ec0072af8a5` · Append-only events and current views can diverge silently · verified. Consequence: Restart or partial persistence could produce plausible current state that no canonical history justified.
- **S1** · `finding:ea5107c2-62fd-5632-b9f3-649eb208fb92` · Subscription-only execution is not enforced by the child environment · verified. Consequence: Private prompts may cross an undeclared billing and trust boundary, invalidating the execution contract across providers.
- **S1** · `finding:ef9f595b-6433-55c3-a747-c12967effb4f` · Quaternary state receipts omit authoritative scope and bytes · verified. Consequence: Two materially different research worlds could present the same context receipt to a model or closure challenger.
- **S1** · `finding:fa396166-7dd1-5dfb-b90e-b386a8ea8563` · Axial launders unverified Loom source claims · verified. Consequence: Forged or superseded provenance could become an enduring personal-operational-graph fact.
- **S2** · `finding:0de2e74e-3ecf-57f5-b486-d52973f65de5` · Model-controlled HTML is rendered without sanitization · verified. Consequence: Untrusted provider output can impersonate the operator surface and distort consequential routing decisions.
- **S2** · `finding:2ec73f85-c095-5c9b-8632-82d1f683f28d` · Workflow extraction trusts its own producer and emits nonexecutable paths · verified. Consequence: A reusable path could look proven while omitting the context or trigger dependency that made it work.
- **S2** · `finding:429c7fb0-a2c2-5dec-a1dc-6702427b5934` · The cockpit hides consequential context deltas · verified. Consequence: The user could route an edge without noticing that a contradiction, source, or authority influence had changed.
- **S2** · `finding:453ce215-69d8-5a54-bfd2-9a3c5b1e3d5b` · Slate filesystem discovery is unbounded and nondeterministic · verified. Consequence: The same Slate request could hang, overread, or return different cards across runs.
- **S2** · `finding:4d96729d-056a-5024-8f78-41258fea9fbc` · The clean wheel omits the cockpit and workflow packs · verified. Consequence: The advertised standalone service is not functionally installable from its own package artifact.
- **S2** · `finding:4f1d2c27-f681-59e3-99fa-f209d4769e59` · Canonical JSON validation recurses without structural limits · verified. Consequence: Hostile model or import data could crash the local service at the identity boundary.
- **S2** · `finding:53d8a819-a01d-595e-90af-5ffcd2da1a07` · Project summaries leak provenance and leave stale Axial concerns · verified. Consequence: The enduring graph could accumulate false project membership and privacy-inappropriate conversation detail.
- **S2** · `finding:55c8f361-0df4-54ec-a8e0-67d05ee5cbb9` · Blob staging failures leave unowned temporary bytes · verified. Consequence: Private context could accumulate outside the canonical blob index and storage accounting.
- **S2** · `finding:6020b738-1812-58d0-8395-648d47935442` · Committed workflow transitions could return ambiguous trigger failures · verified. Consequence: The operator could retry the old transition, encounter a revision conflict, and lose the governed automatic action.
- **S2** · `finding:667eb591-f5bc-56d9-88c6-0076904c893b` · Trailheads are roots rather than branch tips · verified. Consequence: Ordinary conversational steering branches from the wrong place unless the user manually finds and checks history.
- **S2** · `finding:7a20e77d-151f-50b8-b828-99e4c78882cf` · Workflow templates omit dependency closure · verified. Consequence: Reusing the path would silently change its epistemic world or fail after project data moved.
- **S2** · `finding:80d8b15c-2da4-5cf4-af58-b88a9e2fa014` · Slices can be unbounded while claiming completion · verified. Consequence: A large project could freeze the cockpit and silently omit relevant history.
- **S2** · `finding:89fe0605-b299-5327-b549-bbe9ae44f889` · Mutable source files can violate content-addressed identity · verified. Consequence: Later context reconstruction could retrieve different bytes under an apparently exact identity.
- **S2** · `finding:91737305-2c6b-5a48-ab38-765b05251003` · Loom can emit unbounded Axial projections · verified. Consequence: One project could overload Axial or use volume to hide malformed lineage.
- **S2** · `finding:9e07b658-3c9d-5b3a-bdb7-5c2eae45af4f` · Slate slices collapse identity, safety, and evidence states · verified. Consequence: A missing Loom service or hostile transcript could masquerade as an empty trusted topic card.
- **S2** · `finding:a4f1c31a-f95b-5518-8bf7-dd6767e635f4` · Graph neighborhoods explode while hiding truncation · verified. Consequence: Inspection of a dense lattice could exhaust memory or omit the exact branch that changes a routing decision.
- **S2** · `finding:ac20454d-1f3e-5b13-aa62-0f20a50a2344` · Provider artifacts disappear after successful execution · verified. Consequence: Downstream joins and workflow evidence cannot consume or inspect one of the invocation contract’s promised outputs.
- **S2** · `finding:b4d5bbc8-21bc-575c-b5a9-b481c1a6870e` · Route discovery blocked cockpit boot on sequential CLI probes · verified. Consequence: The habitual interface could spend many seconds showing only a loading state despite the ledger being healthy.
- **S2** · `finding:b8db6320-4c88-5484-bbd3-f34d826d37e8` · Artifact inspection exposed metadata without exact content · verified. Consequence: A consequential output could exist in the lattice without a usable human inspection path.
- **S2** · `finding:c9a11a21-f3c3-5cc6-b136-0a5c35a757eb` · Startup integrity digests are materialized and key-order dependent · verified. Consequence: A healthy large ledger could fail to start, while equivalent rows could hash differently across schema adapters.
- **S2** · `finding:d1eb6fe3-8a2e-5edf-b299-c1ae04bb1874` · Tagging and extraction retries create duplicate semantic objects · verified. Consequence: Ordinary retries would fragment topic slices and reusable workflow lineage.
- **S2** · `finding:d6bea7db-bdf9-57cf-a2f8-ff9bc827a955` · Ordinary role creation leaves orphan context on failure · verified. Consequence: Repeated mistakes pollute the canonical lattice and make failed operations observably non-atomic.
- **S2** · `finding:e42cf5a4-05c1-5cb3-8fd8-8c0bc3344182` · Project history and workflow templates are operationally undiscoverable · verified. Consequence: Older trailheads and proven paths could exist but be functionally unreachable.
- **S2** · `finding:e808ffcc-d6d8-5391-89f9-53b5422673b9` · Gemini and Grok expose private prompts in process arguments · verified. Consequence: The future German native lane violates the plan’s explicit prompt-privacy boundary before qualification.
- **S2** · `finding:e9ec5896-00ba-5303-ae0e-7d5c4ed8e785` · Workflow-pack parsing is unbounded · verified. Consequence: A local or imported pack could starve the service during discovery or conceal an unusable workflow behind a parse failure.
- **S2** · `finding:ede0d7bd-d6ec-5ff6-8537-a8b835079b24` · Blob rebuild duplicates immutable content in memory · verified. Consequence: Recovery from a large corpus could exceed memory precisely when durability repair was needed.

## Derived depth

| Subject | Plane | Target | Derived |
|---|---:|---|---|
| Subscription execution and operator authority | 2 | standard | standard |
| Subscription execution and operator authority | 3 | standard | standard |
| Subscription execution and operator authority | 4 | standard | standard |
| Subscription execution and operator authority | 5 | standard | standard |
| Subscription execution and operator authority | 6 | standard | standard |
| Subscription execution and operator authority | 7 | standard | standard |
| Subscription execution and operator authority | 8 | standard | standard |
| Subscription execution and operator authority | 9 | standard | standard |
| Subscription execution and operator authority | 10 | standard | standard |
| Subscription execution and operator authority | 11 | standard | standard |
| Durable lattice and context truth kernel | 1 | standard | standard |
| Durable lattice and context truth kernel | 2 | standard | standard |
| Durable lattice and context truth kernel | 3 | standard | standard |
| Durable lattice and context truth kernel | 4 | standard | standard |
| Durable lattice and context truth kernel | 6 | standard | standard |
| Durable lattice and context truth kernel | 7 | standard | standard |
| Durable lattice and context truth kernel | 8 | standard | standard |
| Durable lattice and context truth kernel | 9 | standard | standard |
| Durable lattice and context truth kernel | 10 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 2 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 3 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 4 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 6 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 7 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 8 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 9 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 10 | standard | standard |
| Quaternary, Octopus, Slate, and Axial integration | 12 | standard | standard |
| Response-first cockpit and clean installation | 3 | standard | standard |
| Response-first cockpit and clean installation | 4 | standard | standard |
| Response-first cockpit and clean installation | 6 | standard | standard |
| Response-first cockpit and clean installation | 8 | standard | standard |
| Response-first cockpit and clean installation | 9 | standard | standard |
| Response-first cockpit and clean installation | 10 | standard | standard |
| Response-first cockpit and clean installation | 11 | standard | standard |
| Response-first cockpit and clean installation | 12 | standard | standard |

## Assurance blockers

- None.

## Proof limits and residuals

- residual:6552b952-f8a3-57c0-850a-0c0fda336dc0: The Loom Surface census covers the Loom workframe; model-runner, Slate, and Axial integration files are separately full-file hash-inspected by this audit rather than recursively represented in that Surface. (proof-limit)
- residual:e9302de6-3c20-5556-b69d-38c401d2f83b: The complete authenticated German native-role acceptance remains unavailable because Gemini CLI 0.45.0 rejects the consumer tier and cannot satisfy Loom's exact-context isolation contract; workflow machinery is verified, but full German closure is not claimed. (proof-limit)
- residual:4f63d11b-4ea5-5d14-9312-64c6356a449e: Official provider CLIs do not expose every provider-owned system instruction or server-side transformation; manifests name this opacity and claim only locally observable inputs and influences. (proof-limit)
- residual:f29679ec-7a38-549a-8ea5-b03e9fd7495e: The rendered acceptance used seeded local results and did not repeat authenticated Claude or Codex spend on the exact frozen revision; prior authenticated local journals remain historical evidence rather than exact-revision acceptance. (proof-limit)

## Scope

Determine whether Loom truthfully supports arbitrary one-edge multi-model conversation, exact observable context, durable execution, and the governed German workflow without losing personal-operational-graph provenance.
