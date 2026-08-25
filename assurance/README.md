# assurance/

Canonical frozen inputs and finding-to-proof obligations for the Lattice remediation program.

```text
├── baseline.json    Pre-implementation revisions, source hashes, runtime versions, and initial checks
├── findings.json    Permanent finding → implementation → oracle → false-mutation closure ledger
└── history/         Immutable prior ledger generations
```

## Active work

[`../CONVERSATION-FIRST-REMEDIATION-PLAN.md`](../CONVERSATION-FIRST-REMEDIATION-PLAN.md) defines the program; `findings.json` is its canonical closure ledger. All 24 discriminated findings are closed against installed runtime `0.2.0-cb5e27b25e30-eeef1ba475f6`, authenticated acceptance artifact `artifact:f6d95938-5c4b-47b0-b811-bb7eba4b555d`, and post-acceptance recovery artifact `artifact:bb8eeb55-ed7e-42c0-9f64-3b463ac90345`. Provider-owned opacity and the unavailable Gemini qualification remain explicit proof limits rather than silently counted as proved.
