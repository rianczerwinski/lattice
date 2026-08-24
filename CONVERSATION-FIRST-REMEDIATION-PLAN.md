# Lattice Conversation-First and Operational-Reliability Remediation

## Summary

Execute two sequential workstreams inside the existing Lattice and AXISVI repositories:

1. Invert Lattice from role/workflow-first configuration into freestyle conversational graph authorship.
2. Harden the resulting system into an Outport-addressed, launchd-supervised local service suitable for deliberate personal-data testing.

The first execution action writes this plan verbatim to `/Users/rian/AXISVI/axis/chariots/lattice/CONVERSATION-FIRST-REMEDIATION-PLAN.md` and links it from the Lattice README. Lattice and AXISVI remain their existing independent repositories; no new repositories or submodules are created.

Real personal data is not imported automatically. Closure means the system has survived synthetic, sanitized, authenticated-provider, restart, and restore testing and is ready for the user to begin real-data operation.

## Implementation sequence

### 0. Freeze the baseline and assurance ledger

- Record the current Lattice, AXISVI, model-runner, Outport declaration, and workflow-pack revisions.
- Preserve existing manual role-bound invocations, Octopus imports, Slate slices, Axial projections, workflow projects, and historical manifest readers with characterization fixtures.
- Convert the six redteam findings above and every panel discrimination into a finding → implementation → oracle → false-mutation ledger.
- Do not regenerate Surface or claim closure until the final adversarial pass.
- Keep existing real runtime data untouched; run migrations first against exact copies.

**Gate:** the current database rebuilds and all existing supported flows remain reproducible from a clean checkout and copied data store.

### 1. Make direct conversation a native kernel mode

Introduce:

```text
ActorBindingV1 =
  { kind: "direct" }
  | { kind: "role-session", role_session_id: string }
```

- New manual projects contain no implicit role or role session.
- Direct invocations inherit only explicitly selected parents, project defaults, context modules, and the current prompt.
- Role instructions and role defaults enter context only under a role-session binding.
- Workflow-pack invocations remain role-bound.
- Conversation continuity derives from selected ancestry; provider-native session identity remains execution metadata rather than conversational identity.
- Add a restartable SQLite migration that permits null role-session references only when `actor_kind="direct"`. Rebuild affected tables in one exclusive transaction, recreate indexes/triggers, run row-count, foreign-key, manifest-byte, append-chain, and replay checks before commit.
- Preserve existing manifest bytes. New invocations use `LatticeContextManifest/4.0`; readers retain explicit v2/v3 compatibility.
- Keep the existing bootstrap endpoint for compatibility, mark it role-oriented, and move the cockpit to ordinary project creation.

**Gate:** direct Claude/Codex drafts seal without role records; legacy and workflow invocations retain their original manifests and behavior after migration and restart.

### 2. Add stateless preflight and atomic invocation batches

Introduce:

```text
InvocationSpecV1
InvocationBatchPreviewV1
InvocationBatchCreateV1
InvocationBatchReceiptV1
ContextAssemblyV1
```

- A preview accepts intended parents, selections, prompt, actor binding, and route without persisting keystroke drafts.
- A batch contains one or more route-specific invocation specifications plus one idempotency key.
- Fan-out shares a content-bound `ContextAssemblyV1`; every route still receives its own manifest, capability record, proof limits, and compiled bytes.
- Batch sealing is all-or-none. Provider execution starts only after every invocation is durably sealed.
- A provider failure after sealing creates a truthful partial batch result; successful siblings remain usable.
- Existing draft/preview/seal endpoints remain for workflows, imports, templates, and compatibility.
- UI verbs compile into the primitive:
  - send: one specification;
  - side quest: one specification tagged to a child episode;
  - fan-out: several sibling specifications;
  - fan-in/join: one specification with several selected parents;
  - reciprocal cross-feed: two child specifications created after both source outputs exist;
  - return: one invocation selecting exact side-quest contributions.
- A stale preview rejects the batch without creating drafts, invocations, attempts, or events.

**Gate:** fork, fan-out, reciprocal cross-feed, join, retry, and duplicate submission preserve atomic sealing and idempotent execution through restart.

### 3. Make context sculpting exact but lightweight

- Add derived text blocks using validated UTF-8 byte ranges, source block identity, source hash, excerpt hash, and transformation receipt.
- Grafting is an exact selection of a whole block, excerpt, or artifact into another invocation; it never copies an opaque transcript.
- Muting and restoring alter only the draft selection. They never change the source block’s global status.
- Preserve fixed instruction/knowledge/state/ancestry/prompt plane precedence; permit ordering only inside the operator-controlled knowledge plane.
- Add project-level context defaults and a registered context-module catalogue with three modes: manual, removable project default, and workflow required.
- Every module execution emits exact source, version, transformation, sensitivity, authority, and failure receipts.
- Do not add automatic summarization or compaction. Oversized context remains a visible typed failure until a separately provenance-bound summarizer is designed.
- Compute deterministic confirmation reasons:
  - expanded tools, permissions, workspace, network, host, or write authority;
  - automatic omission or transformation not initiated by the operator;
  - changed execution profile that weakens a previously qualified isolation guarantee.
- A deliberately selected provider chip is already consent to that provider; route changes do not create redundant confirmation dialogs.

**Gate:** exact excerpt grafts reconstruct independently, muted material remains available for restoration, and planted hidden omission or authority expansion prevents one-action execution.

### 4. Rebuild the cockpit around the active conversation

Replace the monolithic cockpit with:

```text
ConversationSurface
├── ResponsePane
├── Composer
├── ContextTray
├── TrailheadShelf
├── EpisodeBreadcrumb
└── SessionStrip

Secondary drawers
├── WorkflowControl
├── RoleManagement
├── GraphNeighborhood
└── ManifestInspector
```

- Open directly into the most recent response and composer.
- Show Claude, Codex, and “both” as inline route chips; expose advanced model/effort settings without requiring them.
- Ordinary Send performs preview validation, sealing, and execution in one action unless deterministic confirmation reasons exist.
- Selecting response text, a block, or an artifact exposes Side quest, Ask Claude, Ask Codex, Ask both, Bring here, and Compare.
- Add a paginated trailhead endpoint; the foreground may recommend three, but every dormant branch remains discoverable and resumable.
- Keep context delta continuously visible in the tray as added, removed, transformed, omitted, unchanged, or provider-opaque.
- Move workflow blackboards, transitions, triggers, automation grants, roles, raw graph data, and reusable-path extraction into secondary drawers.
- Distinguish loading, empty success, partial history, provider failure, backend failure, and completed output at every fetched surface.

**Gate:** a browser operator completes the complete freestyle lattice without opening a workflow, role, graph, or manifest panel.

### 5. Add overlapping episodes and expose existing Slate semantics

- Model an episode as a saved concern-backed slice, not an exclusive container. One invocation may participate in multiple episodes.
- A side quest creates a child episode anchored to an exact invocation, block, excerpt, or artifact.
- Returning work creates a new invocation in the parent episode; it does not mutate the earlier parent or collapse the child.
- Provide invocation → episode → project → Slate-topic navigation.
- Reuse the existing `concerns` edges and live slice API rather than building a second Slate integration.
- Add cockpit actions for tagging selected invocations or blocks to existing Slate identities and creating saved topic slices.
- Preserve Slate Mirror’s populated, partial, empty, and unavailable distinctions.
- Keep Axial’s projection bounded to enduring identities and relationships; temporal execution remains canonical in Lattice.

**Gate:** a live Slate card updates from returned side-quest work without transcript copying, and every rendered claim resolves back to exact Lattice objects.

### 6. Qualify genuine harness sessions and coding authority

Extend model-runner with independently qualified execution profiles:

```text
compiled-fresh
workspace-agent
native-continuation
```

- `compiled-fresh` retains the current exact-context, no-tools correctness baseline.
- `workspace-agent` admits only provider-specific workspace, tools, permissions, sandbox, network, and approval settings that the adapter can enforce and record.
- `native-continuation` requires an observed provider session ID, matching provider/model/workspace/authority, explicit parent selection, and a qualified resume/fork command.
- Claude qualification uses its official session, resume/fork, tool, directory, and permission surfaces.
- Codex qualification uses its official app-server/session, queue, fork, workspace, sandbox, and approval surfaces rather than terminal-screen scraping.
- Portable ancestry continuation remains available when native continuation is unavailable or ineligible.
- Provider-owned system context, memory, hooks, plugins, MCP, and other ambient influences remain explicit proof limits.
- Lattice’s session strip displays the effective profile, provider session, workspace, tools, authority, health, and continuation eligibility.
- Antigravity becomes the held-out adapter: it may join only after independently satisfying the same profile contracts.

**Gate:** real Claude and Codex runs prove fresh execution, workspace execution, native continuation, deliberate fork, portable fallback, permission rejection, and provider crash recovery. Unsupported profiles remain visibly unavailable.

### 7. Requalify workflows without letting them shape ordinary use

- Keep Quaternary and Platform Foundry role-bound and governed.
- Confirm workflow triggers cannot consume direct invocations where a named role is required.
- Preserve blackboard authority, evidence freshness, blinded challenge, write confirmation, and workflow generation binding.
- Extract reusable paths only from explicitly selected invocations; evidence inputs remain parameters.
- Permit “Pin as role” to promote a demonstrated conversational function without changing its prior history.
- Run all workflow and import fixtures against the role-optional manifest/store generation.

**Gate:** blank projects contain no workflow concepts, while both workflow packs retain their prior authority and closure guarantees.

### 8. Make Lattice a recoverable Outport-addressed service

- Keep Outport as the sole service identity, allocation, hostname, URL, discovery, and port-process authority.
- Do not recreate a `portctl` allocator or CLI.
- Add a Lattice CLI with `serve`, `open`, `status`, `doctor`, `backup`, and `restore --to <new-directory>`.
- In AXISVI, `lattice serve` resolves the current `lattice` allocation from `outport status --json`; standalone installations may receive an explicit port.
- Convert the AXISVI Lattice runner from detached command execution to a user launchd job with `RunAtLoad`, `KeepAlive`, bounded restart throttling, and logs outside the repository.
- Change the installed runtime to a versioned, pinned environment rather than an editable dependency on the development checkout. Stage upgrades beside the active version, verify them, atomically switch, and retain the prior runtime.
- Move canonical runtime state to `~/Library/Application Support/Lattice/`. Copy any legacy repository-local state, verify SQLite, append-chain, blob, journal, and manifest equivalence, then cut over while retaining the legacy source until post-cutover verification proves retirement safe.
- Create SQLite-consistent snapshots plus a content-addressed blob inventory before every migration and on the configured backup schedule.
- `lattice doctor` checks Outport resolution, service ownership, database integrity, append-chain integrity, blob reachability, migration generation, runtime version, adapter qualification, backup freshness, and recovery readability.
- Keep the Python daemon for this real-data-readiness milestone. A Rust-kernel migration is triggered only by a verified failure that process isolation, packaging, transactions, or supervision cannot bound; it is not performed speculatively.

**Gate:** login/reboot starts Lattice automatically at its current Outport identity; UI, provider, daemon, and upgrade interruptions recover independently; a snapshot restores into a separate directory with byte-identical manifests and projections.

## Test and adversarial system

- Mark every test by truth kind: oracle, property, regression, contract, smoke, fuzz, concurrency, performance, browser acceptance, or authenticated acceptance.
- Use hand-authored manifest, batch, episode, and Slate fixtures as independent oracles; never derive expected values with the compositor or store under test.
- Preserve the independent manifest reader as a separate implementation boundary.
- Use fake Claude/Codex executables for deterministic routine integration and Playwright for the actual conversational cockpit.
- Run authenticated subscription acceptance only after synthetic suites pass.
- Use sanitized Octopus conversations and shape-preserving workflow evidence before any personal corpus is admitted.
- Permanently reject these mutations:
  - direct invocation secretly requires or injects a role;
  - one fan-out child receives different semantic context;
  - route-specific capabilities are omitted from a fan-out manifest;
  - cross-feed consumes a nonterminal source;
  - preview changes after confirmation;
  - muted context changes global block status;
  - excerpt offsets or source hashes drift;
  - automatic omission is hidden;
  - authority expands without confirmation;
  - provider continuation crosses model, workspace, or permission boundaries;
  - a dormant trailhead becomes unreachable after pagination;
  - an episode copies or exclusively owns an invocation;
  - Slate copies transcript bytes;
  - workflow role gates accept direct invocations;
  - duplicate batch submission creates duplicate invocations;
  - migration or restart loses sealed history;
  - restore reports success with a missing blob;
  - Outport and the running service disagree about Lattice’s port.

## Real-data-ready closure

The overnight execution target is reached only when this path succeeds:

```text
Outport resolves Lattice
→ launchd starts the service
→ open a blank project
→ send directly to Claude
→ create a Codex side quest
→ fan out one prompt to both
→ graft exact excerpts
→ reciprocal cross-feed
→ join selected contributions
→ inspect exact context and proof limits
→ resume a dormant branch
→ tag the episode into a live Slate slice
→ extract the useful path
→ restart UI, provider, and daemon
→ restore a verified backup into a separate root
→ continue without lost or invented history
```

Closure additionally requires all six redteam findings and every panel discrimination to have performed remediation, an independent verifier, and a rejected false mutation; the existing manual/workflow/import guarantees remain green; authenticated Claude and Codex acceptance passes; and no in-scope S0–S2 finding remains.

## Defaults

- One integrated program with sequential product and reliability workstreams.
- Existing repositories remain independent nested repositories; changes commit in their owning repository without parent-pointer updates.
- Outport is authoritative for service identity and allocation.
- Fresh compiled execution is always available; native continuation is provider-qualified, never assumed.
- Roles are optional for direct conversation and mandatory only where workflows declare them.
- UI gestures compile into generic invocation batches rather than becoming kernel state machines.
- Existing context-delta, Slate-slice, Axial-projection, workflow, ledger, and manifest machinery is reused.
- No developer API credentials, usage-billed fallback, arbitrary generated code, hidden authority expansion, automatic real-data import, remote push, or Rust rewrite is authorized by this plan.
