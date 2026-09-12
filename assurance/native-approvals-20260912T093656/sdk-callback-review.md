# Native approval callback evidence

This internal component connects the official SDK permission callback to Lattice's operator ledger. Its source checkpoint does not enable native approval policy. The maintainer must wire the same cancellation event into the guarded transport, run the callback on the provider worker loop, complete SDK finalizers, and inspect accumulated callback failures before reporting success.

## Authority and behavior

The callback never decides an action. It stores the exact native action and context, waits on read snapshots, then consumes the existing authenticated operator decision through the ledger's one-use transaction. An allow returns persisted arguments rather than the caller's mutable dictionary; permission suggestions remain descriptive data and cannot become saved rules. A denial applies to this action without interrupting unrelated work. Native cancellation withdraws its unreleased action; operator cancellation stops the shared process. An error at request, inspection, release, or withdrawal remains independently inspectable even if the SDK encodes it as a control response and continues.

The new [callback cancellation finding](sdk-callback-cancel-finding.json) records an avoidable window during synchronous ledger work. The first candidate checked cancellation before I/O but not after it. Two planted cases signalled cancellation during inspection or release and still received an allow. The repair rechecks after each boundary. Cancellation after inspection withdraws without release; cancellation after a committed release suppresses the response while leaving the one-use record consumed. This is not atomic cancellation against all later effects, and no native execution bypass was observed: the shared transport event remains an independent admission guard.

## Observations

| Evidence | Outcome and scope |
|---|---|
| `sdk-callback-first.xml` | A missing parenthesis prevented collection. No callback behavior was exercised. |
| `sdk-callback-second.xml` | Twelve direct callback/ledger cases passed in 17.04 seconds. |
| `sdk-callback-integration.xml` | Sixty callback/ledger/transport cases passed in 67.49 seconds before the I/O cancellation repair. Three cases route actual pinned SDK control messages through real local pipes, using a synthetic peer that performs no tool action. |
| `sdk-callback-cancel-before.xml` | Both new I/O cancellation cases failed at the intended missing-CancelledError assertion; fifteen other cases were deselected. |
| `sdk-callback-cancel-after.xml` | All seventeen callback cases passed after repair, including exact SDK allow/deny encoding and native control-request cancellation. |
| `sdk-callback-mutable-input.json` | An in-memory mutant returning the caller's changed dictionary was rejected at the persisted-input assertion, observing offset 99 instead of 17. |
| `sdk-callback-lost-signal.json` | An in-memory mutant omitting the failure cancellation signal was rejected at the shared-event assertion. |

Each mutation has an immutable `.input.json` binding and is reproducible with `sdk_callback_mutation.py MODE NEW_OUTPUT`. It changes only an in-memory class method and restores it in `finally`. Pytest's already-imported-plugin rewrite warning is retained separately from the intended assertion rejection. Planted test databases are disposable; source bindings, findings, reports, and first failures remain diagnostic evidence. The separately identified post-mutation suite, `sdk-callback-final.xml`, records **62 passed**, zero failed/skipped/warnings, 64.94 seconds. The callback-local cancellation finding is closed against these checks and the independent re-review; broader native/installed obligations are not.

## Independent source review

The separate read-only `english_transport_review` context identified the I/O cancellation defect and re-reviewed the repaired callback, assertions, ledger, SDK dispatch/cancellation/result encoding, imported peer/transport fixture, and supervisor cancellation seam. It found no remaining consequential source defect within this callback-local scope. The review executed no tests or native calls; parent-executed outcomes above remain separate evidence.

The reviewed source is based on Git revision `63896178453e5b56492b22f4955a7a52b6455e76`:

| Input | SHA-256 |
|---|---|
| `lattice/execution/native_approval.py` | `1ab84ce2405d7e75cd1bc38d38a9ebc1782e4032eb0879928dd934466848660c` |
| `tests/test_native_approval_sdk.py` | `ec95d1379e880a55c556f971a10edb53f17b45aa2e0abf0c8753de3c1eb9ea96` |
| `lattice/native_approvals.py` | `9238c726bad92eb6966c34a470873077f584dfbf01c9f6c2615abda13b6088a0` |
| `tests/test_native_approvals.py` | `6be13baddda4b65e6b13ed0e762d62dedbb596a6f6d00c5255794fc3c16647b1` |
| `lattice/execution/sdk_transport.py` | `c270ad5a08efa83912358319225d864201ed683431b72c143fbd92e84adc24e1` |
| `tests/test_sdk_transport.py` | `b4e03f368a3af71a4fa37f5fde3b036a6db7ddcbab239dbaf066683fd1ae405a` |
| pinned SDK `query.py` | `c24f24ac2cb45a7debd3ba8fb945c65ffca7f1414ee0fb37a49fc500e1afcb8c` |
| pinned SDK `types.py` | `49e6a4ca6155ad9b245133d138516d9269bbb3aad2eb21311d02d77d75bca86a` |

The first callback hash was `74cc4e93ad11a9c1a9ee6eec58aa7b4eaded41b2334500457b04fa29b57e6183`; the earlier integration-test hash was `b4a343da0324af6a1d3415e294413a065af12ab8c85e5f15a70df732f53c4d24`. The pre-repair I/O cancellation run used the final test hash above with that first callback source.

## Remaining integration obligations

Operator decisions are planted beneath the authenticated HTTP boundary in these tests. The peer is synthetic and performs no native tool action. Simultaneous primary/withdrawal failures and adapter termination after all callback finalizers need integrated assertions. Database contention/storage delay is not bounded by the polling interval. Callback success, queue acceptance, native receipt, native action enforcement, same-user credential isolation, and installed-runtime qualification are distinct claims. Production policy admission and English's E1 gate remain open.
