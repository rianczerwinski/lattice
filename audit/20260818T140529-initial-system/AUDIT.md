# Audit — audit:loom-initial-system

Contract: **valid**. Evidentiary assurance: **candidate**. Admissible state: **adversarial-review**.

Canonical bytes: `79576fedf8340c4983f49857a3a44469942c177532b4766aa223a02a79380acc`. Source revision: `7b44a0ffd5e3216a60c13542c373b7be605a91d2`. Surface bytes: `f3f6c4a4cc93769a0f717542f24faaa95b52a123b66e0e2bf2b14b4cbfe4a104`.

## Material findings

- **S1** · `finding:0baef75f-9b6d-50ff-b934-5de166e7765a` · External workflow writes have no durable idempotency boundary · active. Consequence: Canonical quaternary state and Loom history can diverge precisely at the authority boundary the workflow exists to govern.
- **S1** · `finding:5caf7b10-7ed3-5925-a5ff-6320e39c0b81` · Exact-context claims exceed what is validated and shown · active. Consequence: The central promise—knowing exactly what each agent saw—can be confidently but incorrectly believed.
- **S1** · `finding:6f54f5fd-e79e-5a3e-8aa5-a18d6570cd71` · Workflow packs are declarations without an enforcing engine · active. Consequence: The first complete German workflow and any later workflow pack cannot inherit their declared epistemic guarantees from Loom.
- **S1** · `finding:9a3e943d-7991-5bfa-b068-03ee36cc6cd6` · The append-only ledger cannot rebuild its derived state · active. Consequence: A broad storage fault invalidates the durable-history guarantee and leaves no independent canonical recovery path.
- **S1** · `finding:ea5107c2-62fd-5632-b9f3-649eb208fb92` · Subscription-only execution is not enforced by the child environment · active. Consequence: Private prompts may cross an undeclared billing and trust boundary, invalidating the execution contract across providers.
- **S2** · `finding:0de2e74e-3ecf-57f5-b486-d52973f65de5` · Model-controlled HTML is rendered without sanitization · active. Consequence: Untrusted provider output can impersonate the operator surface and distort consequential routing decisions.
- **S2** · `finding:4d96729d-056a-5024-8f78-41258fea9fbc` · The clean wheel omits the cockpit and workflow packs · active. Consequence: The advertised standalone service is not functionally installable from its own package artifact.
- **S2** · `finding:667eb591-f5bc-56d9-88c6-0076904c893b` · Trailheads are roots rather than branch tips · active. Consequence: Ordinary conversational steering branches from the wrong place unless the user manually finds and checks history.
- **S2** · `finding:ac20454d-1f3e-5b13-aa62-0f20a50a2344` · Provider artifacts disappear after successful execution · active. Consequence: Downstream joins and workflow evidence cannot consume or inspect one of the invocation contract’s promised outputs.
- **S2** · `finding:d6bea7db-bdf9-57cf-a2f8-ff9bc827a955` · Ordinary role creation leaves orphan context on failure · active. Consequence: Repeated mistakes pollute the canonical lattice and make failed operations observably non-atomic.
- **S2** · `finding:e808ffcc-d6d8-5391-89f9-53b5422673b9` · Gemini and Grok expose private prompts in process arguments · active. Consequence: The future German native lane violates the plan’s explicit prompt-privacy boundary before qualification.

## Derived depth

| Subject | Plane | Target | Derived |
|---|---:|---|---|
| Subscription execution and operator authority | 2 | standard | none |
| Subscription execution and operator authority | 3 | standard | none |
| Subscription execution and operator authority | 4 | standard | none |
| Subscription execution and operator authority | 5 | standard | none |
| Subscription execution and operator authority | 6 | standard | none |
| Subscription execution and operator authority | 7 | standard | none |
| Subscription execution and operator authority | 8 | standard | none |
| Subscription execution and operator authority | 9 | standard | none |
| Subscription execution and operator authority | 10 | standard | none |
| Subscription execution and operator authority | 11 | standard | none |
| Durable lattice and context truth kernel | 1 | standard | none |
| Durable lattice and context truth kernel | 2 | standard | none |
| Durable lattice and context truth kernel | 3 | standard | none |
| Durable lattice and context truth kernel | 4 | standard | none |
| Durable lattice and context truth kernel | 6 | standard | none |
| Durable lattice and context truth kernel | 7 | standard | none |
| Durable lattice and context truth kernel | 8 | standard | none |
| Durable lattice and context truth kernel | 9 | standard | none |
| Durable lattice and context truth kernel | 10 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 2 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 3 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 4 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 6 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 7 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 8 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 9 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 10 | standard | none |
| Quaternary, Octopus, Slate, and Axial integration | 12 | standard | none |
| Response-first cockpit and clean installation | 3 | standard | none |
| Response-first cockpit and clean installation | 4 | standard | none |
| Response-first cockpit and clean installation | 6 | standard | none |
| Response-first cockpit and clean installation | 8 | standard | none |
| Response-first cockpit and clean installation | 9 | standard | none |
| Response-first cockpit and clean installation | 10 | standard | none |
| Response-first cockpit and clean installation | 11 | standard | none |
| Response-first cockpit and clean installation | 12 | standard | none |

## Assurance blockers

- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 2 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 3 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 4 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 5 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 6 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 7 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 8 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 9 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 10 target standard exceeds derived none
- subject:7b170973-d145-588d-a5fc-6192e21d2592 plane 11 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 1 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 2 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 3 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 4 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 6 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 7 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 8 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 9 target standard exceeds derived none
- subject:7b18532c-d0ac-5fdf-a024-3b46e8e34aa6 plane 10 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 2 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 3 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 4 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 6 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 7 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 8 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 9 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 10 target standard exceeds derived none
- subject:7b872e2e-8428-55f9-9d56-8c069b13f136 plane 12 target standard exceeds derived none
- subject:7eca51c8-648b-5322-9254-502fdb52dc41 plane 3 target standard exceeds derived none
- subject:7eca51c8-648b-5322-9254-502fdb52dc41 plane 4 target standard exceeds derived none
- subject:7eca51c8-648b-5322-9254-502fdb52dc41 plane 6 target standard exceeds derived none
- subject:7eca51c8-648b-5322-9254-502fdb52dc41 plane 8 target standard exceeds derived none
- subject:7eca51c8-648b-5322-9254-502fdb52dc41 plane 9 target standard exceeds derived none
- subject:7eca51c8-648b-5322-9254-502fdb52dc41 plane 10 target standard exceeds derived none
- subject:7eca51c8-648b-5322-9254-502fdb52dc41 plane 11 target standard exceeds derived none
- subject:7eca51c8-648b-5322-9254-502fdb52dc41 plane 12 target standard exceeds derived none
- active material findings remain: ['finding:5caf7b10-7ed3-5925-a5ff-6320e39c0b81', 'finding:ea5107c2-62fd-5632-b9f3-649eb208fb92', 'finding:6f54f5fd-e79e-5a3e-8aa5-a18d6570cd71', 'finding:0baef75f-9b6d-50ff-b934-5de166e7765a', 'finding:9a3e943d-7991-5bfa-b068-03ee36cc6cd6', 'finding:ac20454d-1f3e-5b13-aa62-0f20a50a2344', 'finding:667eb591-f5bc-56d9-88c6-0076904c893b', 'finding:d6bea7db-bdf9-57cf-a2f8-ff9bc827a955', 'finding:4d96729d-056a-5024-8f78-41258fea9fbc', 'finding:0de2e74e-3ecf-57f5-b486-d52973f65de5', 'finding:e808ffcc-d6d8-5391-89f9-53b5422673b9']
- blocking residuals remain: ['residual:06179420-12c4-528b-9294-84dc97d24d96', 'residual:8545c491-1713-51ce-9a79-410249821a32']

## Proof limits and residuals

- residual:06179420-12c4-528b-9294-84dc97d24d96: Five S1 and six S2 findings remain active; remediation and independent mutation rejection are required before closure. (blocks-closure)
- residual:b2d9304d-eaba-55fa-957b-8f3e3d52bf09: The canonical Surface census covers Loom itself; model-runner, Slate, and Axial integration entry paths are explicitly inspected subjects but are not recursively represented in that Surface. (proof-limit)
- residual:8545c491-1713-51ce-9a79-410249821a32: The authoritative German quaternary preparation remains calibrating, so authenticated end-to-end closure cannot yet be claimed. (blocks-closure)

## Scope

Determine whether Loom truthfully supports arbitrary one-edge multi-model conversation, exact observable context, durable execution, and the governed German workflow without losing personal-operational-graph provenance.
