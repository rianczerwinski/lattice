# Lattice Web Components

These components carve the conversational cockpit at functional seams. `App.svelte` retains orchestration and shared state; components own coherent rendered surfaces and emit operator intents through explicit callbacks.

- `ProjectRail.svelte` renders project selection, project creation, and governed-workflow launch controls.
- `ResponsePane.svelte` renders the active response, live provider progress, and response-to-context actions.
- `ConversationHistory.svelte` renders the invocation trail and parent-selection controls.
- `ComposerPane.svelte` renders route, authority, context-graft, and send controls while emitting orchestration intents to the application shell.
- `ContextInspector.svelte` renders the exact context, provenance, artifact, module, and reuse surfaces without owning their orchestration.
