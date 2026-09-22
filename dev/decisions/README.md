# Lattice architectural decisions

The [repository README](../../README.md) defines the current product and architecture contract. These records preserve consequential choices and their reasons; a recorded status does not independently prove owner approval, implementation completeness, or installed qualification.

| Decision | Recorded status | Original collection |
|---|---|---|
| [001 — Lattice owns subscription execution](001-lattice-owns-subscription-execution.md) | Accepted; no adoption date recorded | Root `decisions/` |
| [002 — Stable slice successors](002-stable-slice-successors.md) | Accepted; no adoption date recorded | Root `decisions/` |
| [001 — Provider-facing context is a view](001-provider-context-views.md) | No adoption date or status recorded | `dev/decisions/` |

The two historical `001` labels identify different decisions. Cite the full title and file link, not a bare number; filenames and original numbering are preserved. New records use a number above the highest existing number in this collection.

Execution authority, stable-identity transfer, and provider-facing representation remain distinct concerns. Their stated verification boundaries identify the evidence needed for qualification; they do not renew older runtime receipts when source files change. Historical manifests and qualification results remain bound to their recorded revisions.
