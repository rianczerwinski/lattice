# assurance/

Canonical frozen inputs and finding-to-proof obligations for the Lattice remediation program.

```text
├── baseline.json    Pre-implementation revisions, source hashes, runtime versions, and initial checks
└── findings.json    Permanent finding → implementation → oracle → false-mutation closure ledger
```

## Active work

[`../CONVERSATION-FIRST-REMEDIATION-PLAN.md`](../CONVERSATION-FIRST-REMEDIATION-PLAN.md) defines the program; `findings.json` is its canonical closure ledger. All 19 discriminated findings are closed against the installed `0.2.0` acceptance generation, with provider-opacity and unavailable Gemini qualification retained as explicit proof limits rather than silently counted as proved.
