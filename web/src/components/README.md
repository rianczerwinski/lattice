# Lattice Web Components

These components carve the conversational cockpit at functional seams. `App.svelte` retains orchestration and shared state; components own coherent rendered surfaces and emit operator intents through explicit callbacks.

- `ProjectRail.svelte` renders project selection, project-local provider activity, project creation, and governed-workflow launch controls.
- `ResponsePane.svelte` renders the selected invocation, its own recoverable provider activity, and response-to-context actions without borrowing another project's progress state.
- `NativeApprovals.svelte` shows exact-action permission requests, invocation/workspace bindings, one-action allow/deny controls, route cancellation, and saved decision/delivery distinctions without persisting native permission rules.
- `ConversationHistory.svelte` renders the invocation trail and parent-selection controls.
- `ComposerPane.svelte` renders route, authority, context-graft, and send controls while emitting orchestration intents to the application shell.
- `ContextInspector.svelte` renders the exact context, provenance, artifact, module, and reuse surfaces without owning their orchestration.
- `WorkflowControl.svelte` renders governed state, blackboards, transitions, grants, and command review as one coherent control surface.
- `RunSurface.svelte` keeps semantic work beside its initiating conversation, exposing progress, safe interruption, retained evidence, exact bindings, and the condition preventing progress.
- `WorkspaceSurface.svelte` gives the conversation an exact read-only repository instrument: bounded file/path/symbol search, line-addressed file views, current Git state, diff review, and exact-file context capture. Provider harnesses and durable runs remain the editing and command-execution authorities.
- `CommandPalette.svelte` provides one keyboard-first route across projects, conversation search, active runs, providers, and common cockpit actions without taking navigation ownership away from `App.svelte`.
- `ResponsePane.svelte` also exposes retry/edit, copy, export, and exact file-link navigation around the selected response.
