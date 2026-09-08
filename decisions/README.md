# Lattice decisions

This directory records durable architecture decisions whose consequences outlive an implementation session. The canonical product contract remains [`../README.md`](../README.md); these records preserve the reasons for consequential boundaries.

- [`001-lattice-owns-subscription-execution.md`](001-lattice-owns-subscription-execution.md) — Lattice is the sole operational authority for subscription-model execution.
- [`002-stable-slice-successors.md`](002-stable-slice-successors.md) — Stable external slice identities transfer atomically between governed project generations without rewriting their predecessors.
