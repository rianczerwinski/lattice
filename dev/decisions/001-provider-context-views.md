# Provider-facing context is a view

## Decision

The canonical context manifest owns selections, ordering, provenance, transformations, omissions, capabilities, and exact request identity. Provider-facing text is a versioned view of that manifest, not the canonical conversational structure. Fan-out requires equal semantic selections, not equal presentation hashes. Each view must reconstruct independently before preview and execution.

Claude receives ordinary attributed documents followed by the current request, using its normal system context. Markdown fences longer than every source backtick run preserve exact UTF-8 text, including literal markup and control characters, without introducing an XML character-domain restriction. Labels are single-line JSON-quoted text. Other provider formats are unchanged. Historical manifests remain immutable and readable under their original format contracts.

## Evidence and alternatives

The retained six-case synthetic packaging diagnostic at `sui/mission/projects/canon/dev/runtime/opus-packaging-20260909T020946` in AXISVI observed refusals with the old wrapper, including without its custom system instructions, and successful structured answers when the same inner task was supplied inline. The failed request had no ancestors. This implicates the wrapper as a package; it does not isolate one phrase or show that edited/multi-parent ancestry is unavailable.

Keeping the wrapper or adding a task prefix did not resolve that observation. Native Claude Code continuation cannot express an arbitrary multi-parent edited history through the supported subscription interface. XML documents were considered; ordinary fenced text preserves the existing arbitrary-UTF-8 content contract without escaping source bodies. This is presentation engineering, not a weakening of provider safeguards or a claim of native role equivalence.

## Consequences and verification

The independent manifest reader retains legacy reconstruction and verifies the new renderer without importing it. Permanent tests plant altered content, provenance, system instructions, byte counts, truncation claims, delimiters, and cross-provider semantic divergence. The installed five-case canary exercises exact task delivery, real parent responses, an edited two-parent join, source attribution, and unresolved conflicting claims. Small live samples establish those capabilities only; they do not estimate reliability or guarantee model obedience. Capabilities and workflow state remain enforced by application machinery, never by the text fence.

The compositor and independent reader jointly own this contract. A new representation requires a new format identity, retained historical readers, regression falsifiers, and installed qualification before operational readiness is claimed.
