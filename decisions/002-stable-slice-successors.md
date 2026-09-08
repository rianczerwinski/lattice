# ADR 002: Stable slice identities transfer to explicit successors

**Status:** Accepted

## Decision

A stable external slice identity names the current governed project surface for that concern, while each slice and project keeps its own immutable internal identity. When a new workflow project succeeds the project that currently owns a stable external slice identity, the creation request must name the exact predecessor project. Lattice creates the successor, releases the predecessor slice's external alias, binds the alias to the successor slice, and records a revisioned `supersedes` edge plus events on both sides in one transaction.

The predecessor project, slice, query, context, invocations, and events remain intact and directly readable by their internal identities. Releasing the external alias increments the predecessor slice revision but does not rewrite its concern query, so its historical projection remains meaningful. A generic project creation that collides with an existing external identity fails; a request naming the wrong predecessor fails without partial creation.

## Consequences

A Slate card can keep one durable address while successive workflow-pack generations remain honestly distinct. Historical evidence cannot silently acquire a newer pack hash, selector, role set, or workflow state, and the current address cannot split across two projects. The stable slice continues to project all objects carrying the enduring concern relation, including objects produced by predecessor projects, while direct internal slice references preserve generation-specific navigation.

## Verification boundary

Store-level oracle tests plant a predecessor and successor with distinct authoritative blackboards, require the stable address to resolve only to the successor, require both direct and stable projections to preserve both generations' concern-bearing objects, and inspect the exact supersession edge and alias-release event. Negative controls substitute ordinary creation and a false predecessor identity; both must fail while leaving the predecessor address and project census unchanged.
