# Audit — audit:loom-closure-system

Contract: **valid**. Evidentiary assurance: **closed**. Admissible state: **closed-audited**.

Canonical bytes: `500f8e3f502ea90c0d85150b93fd4595f590af863572eb86a3fb1134741b387e`. Source revision: `31b3388579ce5d5e870c7bc58a13165578e6800f`. Surface bytes: `a13b327b2ddce05ad89869eff28094a9019ecc5da60639b481177c456061f9b4`.

## Material findings

- **S1** · `finding:0baef75f-9b6d-50ff-b934-5de166e7765a` · External workflow writes have no durable idempotency boundary · verified. Consequence: Canonical quaternary state and Loom history can diverge precisely at the authority boundary the workflow exists to govern.
- **S1** · `finding:18396742-d087-5a8f-b47d-58dc64cb5743` · Workflow role authority could be bypassed after draft creation · verified. Consequence: A workflow role could be impersonated after the cockpit had shown the correct boundary.
- **S1** · `finding:5caf7b10-7ed3-5925-a5ff-6320e39c0b81` · Exact-context claims exceed what is validated and shown · verified. Consequence: The central promise—knowing exactly what each agent saw—can be confidently but incorrectly believed.
- **S1** · `finding:6f54f5fd-e79e-5a3e-8aa5-a18d6570cd71` · Workflow packs are declarations without an enforcing engine · verified. Consequence: The first complete German workflow and any later workflow pack cannot inherit their declared epistemic guarantees from Loom.
- **S1** · `finding:9a3e943d-7991-5bfa-b068-03ee36cc6cd6` · The append-only ledger cannot rebuild its derived state · verified. Consequence: A broad storage fault invalidates the durable-history guarantee and leaves no independent canonical recovery path.
- **S1** · `finding:ea5107c2-62fd-5632-b9f3-649eb208fb92` · Subscription-only execution is not enforced by the child environment · verified. Consequence: Private prompts may cross an undeclared billing and trust boundary, invalidating the execution contract across providers.
- **S2** · `finding:0de2e74e-3ecf-57f5-b486-d52973f65de5` · Model-controlled HTML is rendered without sanitization · verified. Consequence: Untrusted provider output can impersonate the operator surface and distort consequential routing decisions.
- **S2** · `finding:4d96729d-056a-5024-8f78-41258fea9fbc` · The clean wheel omits the cockpit and workflow packs · verified. Consequence: The advertised standalone service is not functionally installable from its own package artifact.
- **S2** · `finding:6020b738-1812-58d0-8395-648d47935442` · Committed workflow transitions could return ambiguous trigger failures · verified. Consequence: The operator could retry the old transition, encounter a revision conflict, and lose the governed automatic action.
- **S2** · `finding:667eb591-f5bc-56d9-88c6-0076904c893b` · Trailheads are roots rather than branch tips · verified. Consequence: Ordinary conversational steering branches from the wrong place unless the user manually finds and checks history.
- **S2** · `finding:ac20454d-1f3e-5b13-aa62-0f20a50a2344` · Provider artifacts disappear after successful execution · verified. Consequence: Downstream joins and workflow evidence cannot consume or inspect one of the invocation contract’s promised outputs.
- **S2** · `finding:b4d5bbc8-21bc-575c-b5a9-b481c1a6870e` · Route discovery blocked cockpit boot on sequential CLI probes · verified. Consequence: The habitual interface could spend many seconds showing only a loading state despite the ledger being healthy.
- **S2** · `finding:b8db6320-4c88-5484-bbd3-f34d826d37e8` · Artifact inspection exposed metadata without exact content · verified. Consequence: A consequential output could exist in the lattice without a usable human inspection path.
- **S2** · `finding:d6bea7db-bdf9-57cf-a2f8-ff9bc827a955` · Ordinary role creation leaves orphan context on failure · verified. Consequence: Repeated mistakes pollute the canonical lattice and make failed operations observably non-atomic.
- **S2** · `finding:e808ffcc-d6d8-5391-89f9-53b5422673b9` · Gemini and Grok expose private prompts in process arguments · verified. Consequence: The future German native lane violates the plan’s explicit prompt-privacy boundary before qualification.

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

- residual:6552b952-f8a3-57c0-850a-0c0fda336dc0: The Loom Surface census covers the Loom workframe; model-runner, Slate, and Axial integration files are separately hash-inspected by this audit rather than recursively represented in that Surface. (proof-limit)
- residual:e9302de6-3c20-5556-b69d-38c401d2f83b: The German native-role authenticated acceptance remains unavailable because no qualified official Gemini subscription automation surface is present; the governed workflow machinery is verified with fakes and Codex, but the full German workflow is not claimed complete. (proof-limit)
- residual:4f63d11b-4ea5-5d14-9312-64c6356a449e: Official provider CLIs do not expose every provider-owned system instruction or server-side transformation; manifests name this opacity and claim only locally observable inputs and influences. (proof-limit)

## Scope

Determine whether Loom truthfully supports arbitrary one-edge multi-model conversation, exact observable context, durable execution, and the governed German workflow without losing personal-operational-graph provenance.
