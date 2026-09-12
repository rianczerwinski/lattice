# Independent native approval ledger review

Reader: a Lattice maintainer deciding whether this source checkpoint is usable and what remains before enabling native approvals. This is a parent-retained synthesis of the separate read-only `english_transport_review` context, not a test receipt or a whole-system closure verdict. The reviewer inspected source and assertions without executing tests or evaluating the parent's execution reports.

## Findings and repaired boundary

The initial candidate allowed a late heartbeat to revive an expired request and allowed persistence/read failures to prevent owned cancellation signals. These material failures remain identified in [the expiry finding](approval-expiry-finding.json) and [the cancellation finding](approval-cancel-persistence-finding.json). The first review also identified an incorrect test expectation of HTTP 401; the actual protected mutation boundary returns 403.

The second review supported overdue-heartbeat rejection, finally-based cancellation, sibling shutdown, and coherent invocation snapshots. It retained one expiry defect: a read-only expired projection could become actionable again after the wall clock moved backward, without any persistent closure event. Expired withdrawals were also vulnerable to that reconstruction. The final source replaces this authority clock with a fixed monotonic deadline, bounded by the existing attempt lease duration. The configured default is 900 seconds and the supervisor already bounds the complete invocation by that duration; this is not an indefinite operator wait. Duplicate native callbacks return the original deadline rather than renewing it.

The final independent assessment found no remaining consequential source defect within this internal checkpoint's fixed-lifetime, supervisor-owned boundary:

- Request, decision, and release bind the exact action, invocation, attempt, supervisor owner, lease, manifest, and deadline. Immediate transactions serialize release against withdrawal and closure.
- A repeated operator decision observes the immutable prior result; it does not mint a second grant. A repeated callback release fails.
- Native suggestions confer no authority. The decision schema forbids edited inputs and permission-rule additions.
- Cancellation signals survive errors saving closure or reading the attempt. Shutdown signals captured siblings and joins tasks before surfacing closure errors.
- Startup reconciles previous supervisor owners before the server lifespan yields. Invocation and approval projections share a read snapshot.
- The production `on-request` route remains rejected by `validate_execution_policy`.

## Reviewed bindings

Base Git revision: `ba457d91dc2fea0a24f6143c479ad582d443de94`. The final review examined these candidate bytes:

| Path | SHA-256 |
|---|---|
| `lattice/native_approvals.py` | `9238c726bad92eb6966c34a470873077f584dfbf01c9f6c2615abda13b6088a0` |
| `tests/test_native_approvals.py` | `6be13baddda4b65e6b13ed0e762d62dedbb596a6f6d00c5255794fc3c16647b1` |
| `lattice/store.py` | `70d906437a7b32fccf764582567e79e81c4bcc3e16f274c90e43e94b63cbf94a` |
| `lattice/supervisor.py` | `669170d47515f7ab8988ca0ef6cf1ba37c0c8fc3abda9b82d20c026ae86758de` |
| `lattice/contracts.py` | `3f4262262e3fbae64bf7713717677dfe63182d81171b65b402c643d241848fae` |
| `lattice/server.py` | `c0167291c1965c1aeb9be710451a34d9c5b967ddd16185fd0c92b8f23365e3dc` |
| `lattice/execution/adapters.py` | `27a14c37ca070be1362c6ddaec10dd49aefd693aa23fab90a40d0f42e70728d7` |

Traversal: all approval symbols at lines 37–267; approval fixtures/assertions at lines 22–421; store transaction boundary 1182–1192, invocation snapshot 3401–3413, heartbeat/finalization 3740–3774, orphan reconciliation 3789–3794; supervisor startup/shutdown/cancellation/closure 75–174; strict-model and approval contracts; server startup ordering and approval endpoints.

The first reviewed approval module was `5e4059eeefe15f12dafa2f9e7565edc7423452ba6f9fc8a712b6de2d99db9b2b`; its tests were `c9c2ce75e6655de611583b382c278866ef93687d1a7d4b90a33123853b4cae4e`, store `79e9cd5908d17bac426d6247508a7ea073c98d9339db568d84d6af5bbc97eb92`, supervisor `16f74926e5e3513264b1090c61bf089dc5c878b798460d0e9080078cfc3e3639`. The second review retained that approval-module hash and examined test hash `c938e6bae405d3e5234606ee98047fce7093956fa444b1ca09b7521326978ecd` with the final store/supervisor hashes above. Contracts and server were unchanged across these reviews.

## Potency and proof limits

`ledger-review-before.xml` retains five intended assertion failures for late-heartbeat resurrection and lost cancellation. `ledger-clock-before.xml` retains two intended failures for the mutable-clock lifetime. `ledger-monotonic-tests.xml` records 29 passing final-module cases. `ledger-ignore-binding-final.json` and `ledger-repeat-release.json` record in-memory mutations rejected at the intended `LatticeConflict` assertion, not setup or import failure. Each mutation has an immutable input record binding source/test bytes. They demonstrate these fault detections, not recall on an unseen defect population.

The monotonic-clock choice follows Python's [non-adjustable elapsed-time clock](https://docs.python.org/3/library/time.html#time.monotonic), also used for [asyncio deadlines](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.time). Its reference is meaningful only within the reviewed runtime/boot boundary. Device suspend/resume behavior is not qualified here. Race tests permit either legitimate winner but do not force both orderings in one run.

`released-to-adapter` is a one-use claim, not provider acknowledgement or execution proof. A stolen same-user bearer credential remains indistinguishable from the operator at the HTTP boundary; route isolation must address that independently. SDK integration, native allow/deny/cancel behavior, recovery at delivery, installed-runtime evidence, and the full E1 gate remain open. The production route is not enabled by this checkpoint.

## Cockpit source binding

The parent inspected these final cockpit bytes and the retained pending/denied screenshots. This is a post-run source binding, not the independent source review above or a pre-run browser attestation. The browser used synthetic API responses; its checked assertions and observed render do not qualify the native route.

| Path | SHA-256 |
|---|---|
| `web/src/App.svelte` | `e30bb2d05257d18612ec6f94231d749c090bb73cb09b30f1831a8b4b0dab4c63` |
| `web/src/types.ts` | `4a536f9eda6651307f661e57aa2f2f7cce512e2eb0ce0361f86aa8e8b7bc6804` |
| `web/src/components/NativeApprovals.svelte` | `b8317712a2b25044d20502627891a8eb1978f2bf405fe81d0cd1a086b360bcaf` |
| `web/e2e/conversation.spec.ts` | `c872e12852ed1dc7f8d2e695037be49c75cf5a2fa45df7c26d3758699ffdbff0` |
| `web/dist/index.html` | `d43720dce728522e59d70dd24b37b95f195c5279213b7c54fb30adbf9a7de9e1` |
| `web/dist/assets/index-DPQA-L7q.js` | `c4e4dee3e29fdf49ff88cd18820fbfa19922bff49aa78bb83cb998e5a4ffeaad` |
| `web/dist/assets/index-dHMMTIRN.css` | `12072eefe74725a184203145c4beb29b83271c3f246032650208d63b2e9f0509` |
