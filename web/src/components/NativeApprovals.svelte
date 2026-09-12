<script lang="ts">
  import type { NativeApproval } from '../types';

  export let requests: NativeApproval[];
  export let workspace: string = '';
  export let busy: string[] = [];
  export let errors: Record<string, string> = {};
  export let onDecide: (request: NativeApproval, decision: 'allow' | 'deny') => void | Promise<void>;
  export let onCancel: (attemptId: string) => void | Promise<void>;
  export let onRefresh: (invocationId: string) => void | Promise<void>;

  function actionTarget(request: NativeApproval): string {
    const input = request.binding.action.tool_input;
    return ['file_path', 'path', 'url', 'command', 'query'].map((key) => input[key]).find((value): value is string => typeof value === 'string') ?? 'Inspect the exact arguments below.';
  }
</script>

{#if requests.length}
  <section class="native-approvals" aria-label="Native tool permissions">
    {#each requests as request (request.request_id)}
      <article class:pending={request.state === 'awaiting-operator'}>
        <strong>{request.binding.action.tool_name} · {request.state === 'awaiting-operator' ? 'Needs your permission' : request.state}</strong>
        <pre class="target">{actionTarget(request)}</pre>
        {#if workspace}<p>Workspace: <code>{workspace}</code></p>{/if}
        {#if typeof request.binding.action.context.description === 'string'}<p>Provider description: {request.binding.action.context.description}</p>{/if}
        <p>Applies only to this action in invocation <code>{request.binding.invocation_id.slice(-8)}</code>. No permission rules will be saved.</p>
        {#if request.state === 'awaiting-operator'}
          <div class="actions">
            <button disabled={busy.includes(request.request_id)} on:click={() => onDecide(request, 'allow')}>Allow this action</button>
            <button disabled={busy.includes(request.request_id)} on:click={() => onDecide(request, 'deny')}>Deny this action</button>
            <button class="quiet" on:click={() => onCancel(request.binding.attempt_id)}>Cancel this route</button>
          </div>
        {:else if request.state === 'decided'}
          <p>{request.decision?.decision === 'allow' ? 'Allowed' : 'Denied'}; waiting for the native callback to consume your decision.</p>
        {:else if request.state === 'released-to-adapter'}
          <p>{request.decision?.decision === 'allow' ? 'Allowed' : 'Denied'}; released to the adapter. This record does not confirm tool execution.</p>
        {:else}
          <p>{request.withdrawal?.reason ?? request.inactive_reason ?? 'This request is no longer actionable.'}</p>
        {/if}
        {#if errors[request.request_id]}<p class="failure" role="alert">Decision not confirmed: {errors[request.request_id]} Refresh to inspect the saved decision before retrying.</p><button class="quiet" on:click={() => onRefresh(request.binding.invocation_id)}>Refresh saved decision</button>{/if}
        <details><summary>Exact arguments and binding</summary><pre>{JSON.stringify(request.binding.action.tool_input, null, 2)}</pre><p>Native tool call: <code>{request.binding.action.tool_use_id}</code></p><p>Request: <code>{request.request_sha256}</code></p><p>Manifest: <code>{request.binding.manifest_sha256}</code></p></details>
      </article>
    {/each}
  </section>
{/if}

<style>
  .native-approvals { display: grid; gap: 0.75rem; margin: 1rem 0; }
  article { padding: 1rem; border: 1px solid #4a4a4a; border-radius: 0.7rem; }
  article.pending { border-color: #b09a65; }
  p { overflow-wrap: anywhere; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 24rem; overflow: auto; }
  .target { font-size: 0.95rem; line-height: 1.45; }
  details { margin-top: 0.8rem; }
  .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem; }
  .failure { color: #e9a9a2; }
</style>
