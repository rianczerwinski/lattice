<script lang="ts">
  import { selectorSummary } from '../workflowPresentation';
  import type { Role, WorkflowCommand, WorkflowInstance, WorkflowPack, WorkflowTransition } from '../types';

  type CommandReview = {
    review_sha256: string;
    prepared_arguments_sha256: string;
    state_basis: { blackboard_block_id: string; blackboard_sha256: string; adapter_state_sha256: string } | null;
    file_receipts: Array<{ argument: string; source_ref: string; content_sha256: string; byte_count: number }>;
  };

  export let workflow: WorkflowInstance;
  export let roles: Role[];
  export let activeWorkflowPack: WorkflowPack | undefined;
  export let workflowPackCurrent: boolean;
  export let nextWorkflowTransitions: WorkflowTransition[];
  export let working: boolean;
  export let blackboardContents: Record<string, string>;
  export let blackboardLoading: string;
  export let armedAutomation: string;
  export let armedTransition: string;
  export let armedCommand: string;
  export let transitionValues: Record<string, string>;
  export let commandReviews: Record<string, CommandReview>;
  export let onCreateCurrentWorkflowProject: (pack: WorkflowPack) => void | Promise<void>;
  export let onRevealBlackboard: (blockId: string) => void | Promise<void>;
  export let hasAutomationGrant: (trigger: string) => boolean;
  export let onSetAutomationGrant: (trigger: string, enabled: boolean) => void | Promise<void>;
  export let onRetryTrigger: (triggerId: string) => void | Promise<void>;
  export let onRefreshWorkflow: () => void | Promise<void>;
  export let onSetTransitionValue: (key: string, value: string) => void;
  export let onAdvanceWorkflow: (transition: WorkflowTransition) => void | Promise<void>;
  export let commandValue: (command: WorkflowCommand, key: string, fallback: unknown) => string;
  export let onSetCommandValue: (operation: string, key: string, value: string) => void;
  export let onRunWorkflowCommand: (command: WorkflowCommand) => void | Promise<void>;
</script>

<details class="workflow-control">
  <summary>Workflow control · {workflow.pack_id} · {workflow.state}</summary>
  <section class="workflow-strip">
    <div>
      <span>{workflow.pack_id}</span>
      <strong>{workflow.state}</strong>
      {#each selectorSummary(activeWorkflowPack, workflow.selector) as item}<small class="workflow-selector">{item}</small>{/each}
      <small>revision {workflow.revision} · {workflowPackCurrent ? `authoritative state remains in ${workflow.pack_id === 'quaternary' ? 'the quaternary system' : 'Octopus'}` : 'pack generation changed; this history is preserved but cannot be reinterpreted'}</small>
      {#if !workflowPackCurrent && activeWorkflowPack}<button class="quiet" disabled={working} on:click={() => onCreateCurrentWorkflowProject(activeWorkflowPack)}>Open current generation</button>{/if}
    </div>
    <details>
      <summary>Blackboard & roles</summary>
      {#if workflow.blackboard}
        {@const blackboard = workflow.blackboard}
        <div class="blackboard-receipt"><small>{blackboard.byte_count} bytes · {blackboard.content_sha256.slice(0, 12)} · {blackboard.source_ref ?? 'local source'}</small></div>
        {#if blackboardContents[blackboard.block_id] !== undefined}<pre>{blackboardContents[blackboard.block_id]}</pre>{:else}<button class="quiet" disabled={blackboardLoading === blackboard.block_id} on:click={() => onRevealBlackboard(blackboard.block_id)}>{blackboardLoading === blackboard.block_id ? 'Loading exact snapshot…' : 'Reveal exact snapshot'}</button>{/if}
      {:else}<p class="muted">No authoritative blackboard snapshot is available.</p>{/if}
      <div class="role-list">{#each roles as role}<span>{role.name}</span>{/each}</div>
      {#if workflowPackCurrent && activeWorkflowPack?.definition.triggers.some((trigger) => trigger.automatic)}
        <h3>Automatic advancement</h3>
        {#each activeWorkflowPack.definition.triggers.filter((trigger) => trigger.automatic) as trigger}
          <div class="automation-grant">
            <div><strong>{trigger.trigger}</strong><small>{hasAutomationGrant(trigger.trigger) ? 'Granted for this exact pack generation' : 'Manual activation only'}</small></div>
            <button class:armed={armedAutomation === `${trigger.trigger}:${!hasAutomationGrant(trigger.trigger)}`} class="quiet" disabled={working} on:click={() => onSetAutomationGrant(trigger.trigger, !hasAutomationGrant(trigger.trigger))}>{armedAutomation === `${trigger.trigger}:${!hasAutomationGrant(trigger.trigger)}` ? (hasAutomationGrant(trigger.trigger) ? 'Confirm revoke' : 'Confirm grant') : (hasAutomationGrant(trigger.trigger) ? 'Revoke' : 'Enable')}</button>
          </div>
        {/each}
      {/if}
      {#if workflow.triggers.length}<h3>Governed actions</h3>{#each workflow.triggers as trigger}<div class="trigger"><div><strong>{trigger.trigger_name}</strong><small>{trigger.state} · {trigger.required_action}</small>{#if trigger.error_text}<small>{trigger.error_text}</small>{/if}</div>{#if ['pending', 'execution_failed'].includes(trigger.state)}<button class="quiet" disabled={working} on:click={() => onRetryTrigger(trigger.trigger_id)}>{trigger.state === 'execution_failed' ? 'Retry execution' : (trigger.error_text ? 'Retry planning' : 'Activate once')}</button>{/if}</div>{/each}{/if}
      <button class="quiet" disabled={working || !workflowPackCurrent} on:click={onRefreshWorkflow}>Refresh authoritative state</button>
      {#if workflowPackCurrent && nextWorkflowTransitions.length}
        <h3>Next governed edge</h3>
        {#each nextWorkflowTransitions as transition}
          {@const transitionKey = `${transition.from}:${transition.to}`}
          {@const transitionTrigger = activeWorkflowPack?.definition.triggers.find((item) => item.trigger === transition.trigger)}
          <div class="workflow-transition">
            <strong>{transition.from} → {transition.to}</strong>
            {#if transitionTrigger?.action.type === 'freeze_payload_field'}<label>Exact {transitionTrigger.action.field}<textarea rows="3" value={transitionValues[transitionKey] ?? ''} on:input={(event) => onSetTransitionValue(transitionKey, event.currentTarget.value)}></textarea></label>{/if}
            {#if transitionTrigger?.action.type === 'invoke_role'}<small>The inspected response is excluded as the proposal; checked responses plus the live blackboard become challenger evidence.</small>{:else}<small>The live blackboard and checked response outputs become the evidence receipt.</small>{/if}
            {#if transition.evidence_requirements.some((requirement) => requirement.after_current_state_entry)}<small>Freshness gate: refresh or produce this evidence after entering the current state; older receipts cannot advance it.</small>{/if}
            {#each transition.terminal_output_requirements as requirement}<small>Role gate: {requirement.minimum_count} fresh {requirement.workflow_role_key} output ({requirement.invocation_states.join(' or ')}).</small>{/each}
            <button class:armed={armedTransition === transitionKey} class="quiet" disabled={working} on:click={() => onAdvanceWorkflow(transition)}>{armedTransition === transitionKey ? `Confirm ${transition.to}` : `Review ${transition.to}`}</button>
          </div>
        {/each}
      {/if}
      {#if workflowPackCurrent && activeWorkflowPack?.definition.commands.length}
        <h3>Control plane</h3>
        {#each activeWorkflowPack.definition.commands as command}
          {@const review = commandReviews[command.operation]}
          <details class="workflow-command">
            <summary>{command.label} · {command.authority}</summary>
            <p>{command.description}</p>
            {#each command.arguments as argument}
              <label>{argument.label}<input type={argument.value_type === 'integer' ? 'number' : 'text'} min={argument.minimum} max={argument.maximum} value={commandValue(command, argument.key, argument.default)} placeholder={argument.required ? 'Required' : 'Optional'} on:input={(event) => onSetCommandValue(command.operation, argument.key, event.currentTarget.value)} /></label>
            {/each}
            {#if command.authority !== 'read'}<p class="authority-warning">{command.authority === 'provider_transmission' ? 'This sends the entered material to a subscription model provider.' : 'This crosses the authoritative workflow write boundary.'}</p>{/if}
            {#if review}
              <div class="command-review"><small>Exact review {review.review_sha256.slice(0, 12)} · arguments {review.prepared_arguments_sha256.slice(0, 12)}</small>{#if review.state_basis}<small>Authoritative state {review.state_basis.adapter_state_sha256.slice(0, 12)} · snapshot {review.state_basis.blackboard_block_id.slice(-8)}</small>{/if}{#each review.file_receipts as receipt}<small>{receipt.argument} · {receipt.byte_count} bytes · {receipt.content_sha256.slice(0, 12)}</small>{/each}</div>
            {/if}
            <button class:armed={armedCommand === command.operation} class="quiet" disabled={working} on:click={() => onRunWorkflowCommand(command)}>{command.authority !== 'read' && armedCommand !== command.operation ? `Review ${command.label}` : `Run ${command.label}`}</button>
          </details>
        {/each}
      {/if}
    </details>
  </section>
</details>
