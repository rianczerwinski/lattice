<script lang="ts">
  import type { Project, WorkflowPack } from '../types';
  import type { ProjectActivityBadge } from '../invocationActivity';
  import { selectorReady, selectorValue } from '../workflowPresentation';

  export let projects: Project[];
  export let project: Project | null;
  export let title: string;
  export let purpose: string;
  export let working: boolean;
  export let workflowCatalogueError: string;
  export let workflowPacks: WorkflowPack[];
  export let workflowPackFailures: Array<{ pack_id: string; detail: string }>;
  export let workflowSelectorValues: Record<string, Record<string, string>>;
  export let activityBadges: Record<string, ProjectActivityBadge>;
  export let onOpenProject: (projectId: string) => void | Promise<void>;
  export let onCreateProject: () => void | Promise<void>;
  export let onSetWorkflowSelectorValue: (packId: string, key: string, value: string) => void;
  export let onCreateWorkflowProject: (pack: WorkflowPack) => void | Promise<void>;
</script>

<aside class="projects">
  <div class="section-label">Projects</div>
  {#each projects as item}
    <button class:active={project?.project_id === item.project_id} on:click={() => onOpenProject(item.project_id)}>
      <span>{item.title}{#if activityBadges[item.project_id]?.attention}<b class="project-activity failed">needs attention</b>{:else if activityBadges[item.project_id]?.active}<b class="project-activity">{activityBadges[item.project_id].active} working</b>{:else if activityBadges[item.project_id]?.unread}<b class:failed={activityBadges[item.project_id].failed} class="project-activity">updated</b>{/if}</span><small>{item.purpose || 'Open lattice'}</small>
    </button>
  {/each}
  <div class="new-project">
    <input bind:value={title} placeholder="New project" />
    <textarea bind:value={purpose} rows="2" placeholder="What is this for?"></textarea>
    <button class="quiet" disabled={working || !title.trim()} on:click={onCreateProject}>Create</button>
  </div>
  <details class="secondary-control workflow-catalog">
    <summary>Governed workflows</summary>
    {#if workflowCatalogueError}<p class="surface-failure">Workflow catalogue unavailable: {workflowCatalogueError}</p>{/if}
    {#each workflowPacks.filter((pack) => pack.available) as pack}
      <div class="workflow-launch">
        <strong>{pack.definition.title}</strong>
        {#each pack.definition.project_selector as field}
          <label>{field.label}<input type={field.value_type === 'integer' ? 'number' : 'text'} value={selectorValue(pack, field, workflowSelectorValues)} on:input={(event) => onSetWorkflowSelectorValue(pack.definition.pack_id, field.key, event.currentTarget.value)} /></label>
        {/each}
        <button class="quiet" disabled={working || !selectorReady(pack, workflowSelectorValues)} on:click={() => onCreateWorkflowProject(pack)}>Open authoritative live view</button>
      </div>
    {/each}
    {#each workflowPackFailures as failure}
      <div class="workflow-launch workflow-unavailable"><strong>{failure.pack_id}</strong><small>Unavailable · {failure.detail}</small></div>
    {/each}
  </details>
</aside>
