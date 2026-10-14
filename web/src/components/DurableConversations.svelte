<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { api, post } from '../api';
  import { renderMarkdown } from '../renderMarkdown';

  export let projectId: string;
  export let getRoute: () => Record<string, unknown>;
  export let onSelection: (selected: boolean) => void;
  export let getContextBlockIds: () => string[] = () => [];
  type Child = { conversation_id: string; title: string; owning_task_ref: string; revision: number; parent_conversation_id: string | null };
  type Attempt = { attempt_id: string; state: string; failure_kind: string | null; error_text: string | null };
  type Round = { round_id: string; ordinal: number; state: string; request: { prompt: string }; response?: string; operator_steering?: Array<{steering_id:string;content:string}>; executions: Array<{ invocation_id: string; attempts: Attempt[] }> };
  type Conversation = Child & { rounds: Round[]; has_more: boolean; pending: { round_id: string; state: string; ordinal: number; attempt_ids?: string[] } | null; event_cursor: number; steering: Array<{ steering_id: string; content: string }>; checkpoint: { checkpoint_id: string; content: { artifacts: Array<{ artifact_id: string }> } } | null; route: { model: string } };
  type Effect = { attempt_id: string; tool_id: string | null; request: { name?: string; [key:string]:unknown } };
  type Disposition = { attempt_id:string; tool_id:string|null; disposition:string; evidence:string };
  type ContextChoice = { omitted_round_ids:string[]; omitted_observation_ids:string[]; omitted_artifact_ids:string[]; context_block_ids:string[]|null; reason:string };
  type RecoveryOptions = { context_selection?:ContextChoice; route?:Record<string,unknown> };
  type Confirmation = { code:string; detail:Record<string,unknown>; reason_sha256:string };
  type Preview = { revision: number; preview_sha256: string; mode: string; maximum_remaining_loss: string; blockers: Array<{ code: string; detail: string; effects?: Effect[] }>; context_selection?:ContextChoice|null; required_confirmations?:Confirmation[]; available_context?:{rounds:Array<{round_id:string;ordinal:number}>;observations:Array<{observation_id:string;attempt_id:string;channel:string}>;artifacts:Array<{artifact_id:string}>;blocks?:Array<{block_id:string;label:string;plane:string}>} };
  type ViewState = { selected: string; drafts: Record<string,string>; positions: Record<string,number>; seen: Record<string,number>; pages?: Record<string,number>; requests?: Record<string,{signature:string;request:Record<string,unknown>}>; recovery?:Record<string,RecoveryOptions>; childOwner?:string; childPages?:string[] };
  let view: ViewState = { selected:'',drafts:{},positions:{},seen:{} };
  let children: Child[] = [];
  let conversation: Conversation | null = null;
  let preview: Preview | null = null;
  let error = '';
  let persistenceError = '';
  let liveError = '';
  let loading = true;
  let busy = false;
  let title = '';
  let owner = `project:${projectId}`;
  let ownerFilter = '';
  let draft = '';
  let activity = '';
  let activityEvents = new Map<number,string>();
  let dispositions: Record<string,Disposition> = {};
  let recoveryOptions: RecoveryOptions = {};
  let confirmations: Record<string,boolean> = {};
  function contextChoice(options=recoveryOptions, recovered=preview): ContextChoice { return options.context_selection ?? recovered?.context_selection ?? {omitted_round_ids:[],omitted_observation_ids:[],omitted_artifact_ids:[],context_block_ids:null,reason:''}; }
  function saveRecovery(options: RecoveryOptions) { recoveryOptions=options; confirmations={}; view.recovery={...view.recovery,[view.selected]:options};save(); }
  function chooseContext(field:'omitted_round_ids'|'omitted_observation_ids'|'omitted_artifact_ids', id:string, included:boolean) {
    const choice=contextChoice(); const values=choice[field].filter(value=>value!==id);
    if (!included) values.push(id);
    saveRecovery({...recoveryOptions,context_selection:{...choice,[field]:values}});
  }
  function chooseBlock(id:string,included:boolean) {
    const choice=contextChoice(); const blocks=choice.context_block_ids ?? preview?.available_context?.blocks?.map(b=>b.block_id) ?? [];
    const values=blocks.filter(value=>value!==id); if (included) values.push(id);
    saveRecovery({...recoveryOptions,context_selection:{...choice,context_block_ids:values}});
  }
  function confirmationLabel(reason: Confirmation) {
    if (reason.code==='context.operator-selection') return 'Use this explicit context selection; retain omitted material in saved history.';
    if (reason.code==='route.operator-change') return 'Use the selected route as this conversation’s default.';
    return reason.code.replaceAll('.',' ').replaceAll('-',' ');
  }
  function confirmed(reason: Confirmation, approvals=confirmations, requests=view.requests, recovered=preview) {
    if (approvals[reason.reason_sha256]) return true;
    const retained=requests?.[`${view.selected}:continue`]?.request;
    return Boolean(retained && retained.expected_preview_sha256===recovered?.preview_sha256 && Array.isArray(retained.confirmed_reason_sha256s) && retained.confirmed_reason_sha256s.includes(reason.reason_sha256));
  }
  async function reviewRecovery() {
    const id=view.selected; busy=true;
    try { const result=await post<Preview>(`${path(id)}/recovery-preview`,{...recoveryOptions,effect_dispositions:selectedDispositions()}); if (id===view.selected) {preview=result;error='';} }
    catch (cause:unknown) {error=failure(cause);}
    finally {busy=false;}
  }
  function chooseRoute() { try {saveRecovery({...recoveryOptions,route:getRoute()});} catch (cause:unknown) {error=failure(cause);} }
  const effectKey = (effect: Effect) => JSON.stringify([effect.attempt_id,effect.tool_id]);
  function disposition(effect: Effect, values: Record<string,Disposition>) { return values[effectKey(effect)] ?? {attempt_id:effect.attempt_id,tool_id:effect.tool_id,disposition:'',evidence:''}; }
  function setDisposition(effect: Effect, field: 'disposition'|'evidence', value: string) { dispositions={...dispositions,[effectKey(effect)]:{...disposition(effect, dispositions),[field]:value}}; }
  function selectedDispositions() { return Object.values(dispositions).filter(d=>d.disposition && d.evidence.trim()); }
  function admitActivity(seq:number, text:string) {
    activityEvents.set(seq,text);
    const ordered=[...activityEvents].sort((a,b)=>a[0]-b[0]);
    let bytes=0;
    for (let i=ordered.length-1;i>=0;i--) { bytes+=ordered[i][1].length; if (bytes>24000 && i<ordered.length-1) activityEvents.delete(ordered[i][0]); }
    activity=[...activityEvents].sort((a,b)=>a[0]-b[0]).map(e=>e[1]).join('').slice(-24000);
  }
  let reading: HTMLElement;
  let stream: EventSource | null = null;
  let destroyed = false;
  let refreshTask: Promise<void> | null = null;
  let refreshAgain = false;
  const key = `lattice.conversations.v1:${projectId}`;
  const path = (id: string) => `/api/v1/conversations/${encodeURIComponent(id)}`;
  function failure(cause: unknown) { return cause instanceof Error ? cause.message : String(cause); }
  function save() {
    try { localStorage.setItem(key,JSON.stringify(view)); persistenceError=''; }
    catch (cause: unknown) { persistenceError=`Draft and reading position could not be saved: ${failure(cause)}`; }
  }
  function saveDraft() { view.drafts[view.selected]=draft; save(); }
  function savePosition() { if (reading && view.selected) { view.positions[view.selected]=reading.scrollTop; save(); } }
  function childScope() { return JSON.stringify([view.childOwner ?? '',view.childPages?.at(-1) ?? '']); }
  function filterChildren() {view.childOwner=ownerFilter.trim();view.childPages=[];children=[];loading=true;save();void refresh();}
  function pageChildren(next:boolean) {
    const pages=[...(view.childPages ?? [])];
    if (next && children.length) pages.push(children.at(-1)!.conversation_id); else pages.pop();
    view.childPages=pages;children=[];loading=true;save();void refresh();
  }
  async function refresh() {
    if (refreshTask) { refreshAgain=true; return refreshTask; }
    refreshTask=(async () => {
      do {
        refreshAgain=false;
        try {
          const scope=childScope();
          const query=new URLSearchParams({project_id:projectId,limit:'500',after:view.childPages?.at(-1) ?? ''});
          if (view.childOwner) query.set('owning_task_ref',view.childOwner);
          const values=await api<Child[]>(`/api/v1/conversations?${query}`);
          if (destroyed) return;
          if (scope!==childScope()) continue;
          children=values;
          const selected=view.selected;
          if (selected) {
            const data=await api<Conversation>(`${path(selected)}?after=${view.pages?.[selected] ?? 0}`);
            if (destroyed || selected!==view.selected) continue;
            conversation=data;
            view.seen[selected]=data.revision; save();
            if (data.pending?.state==='needs_attention') {
              const recovered=await post<Preview>(`${path(selected)}/recovery-preview`,recoveryOptions);
              if (destroyed || selected!==view.selected) continue;
              preview=recovered;
            }
            else preview=null;
          }
          error='';
        } catch (cause: unknown) { error=`Conversation service unavailable: ${failure(cause)}`; }
        finally { loading=false; }
      } while (refreshAgain && !destroyed);
    })();
    try { await refreshTask; } finally { refreshTask=null; }
  }
  async function select(id: string) {
    savePosition();
    view.selected=id; draft=view.drafts[id] ?? ''; recoveryOptions=view.recovery?.[id] ?? {}; confirmations={}; conversation=null; preview=null; activity=''; activityEvents.clear(); dispositions={}; save(); onSelection(Boolean(id));
    const url=new URL(window.location.href); url.searchParams.set('project',projectId);
    if (id) url.searchParams.set('conversation',id); else url.searchParams.delete('conversation');
    window.history.replaceState(null,'',url);
    await refresh(); await tick();
    if (destroyed || id!==view.selected) return;
    if (reading) reading.scrollTop=view.positions[id] ?? 0;
    const restored = conversation as Conversation | null;
    if (id && restored?.pending) {
      const attemptIds = new Set(restored.pending.attempt_ids ?? restored.rounds.filter(r=>r.round_id===restored.pending?.round_id).flatMap(r=>r.executions.flatMap(e=>e.attempts.map(a=>a.attempt_id))));
      let cursor=0;
      try {
        let more=true;
        while (more && view.selected===id && !destroyed) {
          const page=await api<{items:Array<{seq:number;subject_id:string;event_type:string;payload:{preview?:string}}>;cursor:number;has_more:boolean}>(`${path(id)}/observations?after=${cursor}&limit=500`);
          if (destroyed || id!==view.selected) return;
          for (const event of page.items) if (attemptIds.has(event.subject_id) && event.event_type==='attempt.chunk') admitActivity(event.seq,event.payload.preview ?? '');
          cursor=page.cursor;more=page.has_more;
        }
      } catch (cause: unknown) { error=`Retained activity unavailable: ${failure(cause)}`; }
    }
  }
  async function pageRounds(after: number) {
    const id=view.selected;
    view.pages={...view.pages,[id]:after};view.positions[id]=0;save();
    await refresh(); if (reading && id===view.selected) reading.scrollTop=0;
  }
  async function create() {
    busy=true;
    try {
      const payload={project_id:projectId,title:title.trim(),owning_task_ref:owner.trim(),parent_conversation_id:view.selected || null,route:getRoute()};
      const signature=JSON.stringify(payload);
      const retained=view.requests?.create;
      const request=retained?.signature===signature ? retained.request : {...payload,idempotency_key:crypto.randomUUID()};
      view.requests={...view.requests,create:{signature,request}}; save();
      const result=await post<{conversation_id:string}>('/api/v1/conversations',request);
      delete view.requests.create; save();
      title=''; await select(result.conversation_id);
    } catch (cause: unknown) { error=failure(cause); }
    finally { busy=false; }
  }
  async function mutate(action: string, redirect=false) {
    if (!conversation) return;
    busy=true;
    const id=conversation.conversation_id;
    const submittedDraft=draft;
    try {
      const requestKey=`${id}:${action}`;
      const signature=JSON.stringify({action,redirect,text:action==='send' || action==='steer' ? draft : '',effects:action==='continue' ? selectedDispositions() : [],recovery:action==='continue' ? recoveryOptions : {}});
      const retained=view.requests?.[requestKey];
      const retry=retained?.signature===signature;
      let request: Record<string,unknown>=retry ? retained.request : {expected_revision:conversation.revision,idempotency_key:crypto.randomUUID()};
      if (action==='send') request.prompt=draft;
      if (action==='steer') { request.content=draft; request.interrupt=redirect; }
      if (action==='continue' && !retry) {
        const effect_dispositions=selectedDispositions();
        const current=await post<Preview>(`${path(id)}/recovery-preview`,{...recoveryOptions,effect_dispositions});
        if (id===view.selected) preview=current;
        if (current.blockers.length) return;
        if (current.required_confirmations?.some(reason=>!confirmed(reason))) return;
        request={...request,...recoveryOptions,expected_revision:current.revision,expected_preview_sha256:current.preview_sha256,effect_dispositions,confirmed_reason_sha256s:current.required_confirmations?.map(r=>r.reason_sha256) ?? []};
      }
      view.requests={...view.requests,[requestKey]:{signature,request}}; save();
      await post(`${path(id)}/${action}`,request);
      delete view.requests[requestKey]; save();
      if (action==='send' || action==='steer') {
        if (view.drafts[id]===submittedDraft) view.drafts[id]='';
        if (view.selected===id && draft===submittedDraft) draft='';
        save();
      }
      await refresh();
    } catch (cause: unknown) { error=failure(cause); }
    finally { busy=false; }
  }
  onMount(() => {
    try {
      const raw=localStorage.getItem(key);
      if (raw) {
        const saved=JSON.parse(raw) as Partial<ViewState>;
        if (typeof saved.selected!=='string' || !saved.drafts || !saved.positions || !saved.seen) throw new Error('Unsupported saved conversation view');
        view=saved as ViewState;
      }
    } catch (cause: unknown) { persistenceError=`Saved conversation view unavailable: ${failure(cause)}`; }
    const creation=view.requests?.create?.request;
    ownerFilter=view.childOwner ?? '';
    if (creation && typeof creation.title==='string' && typeof creation.owning_task_ref==='string') { title=creation.title; owner=creation.owning_task_ref; }
    const linked=new URLSearchParams(window.location.search).get('conversation');
    if (linked) view.selected=linked;
    void select(view.selected);
    stream=new EventSource(`/api/v1/projects/${encodeURIComponent(projectId)}/events/stream?after=0`);
    const receive=(raw: Event) => {
      const message=raw as MessageEvent<string>;
      try {
        const event=JSON.parse(message.data);
        if (event.event_type?.startsWith('conversation.')) void refresh();
        if (event.event_type==='attempt.chunk' && (conversation?.pending?.attempt_ids?.includes(event.subject_id) || conversation?.rounds.some(r=>r.round_id===conversation?.pending?.round_id && r.executions.some(e=>e.invocation_id===event.payload.invocation_id)))) {
          admitActivity(event.seq,event.payload.preview ?? '');
        }
      } catch (cause: unknown) { error=`Conversation event could not be read: ${failure(cause)}`; }
    };
    for (const name of ['conversation.created','conversation.request-admitted','conversation.execution-bound','conversation.round-completed','conversation.needs-attention','conversation.steering-queued','conversation.interrupt-requested','conversation.dispatch-blocked','conversation.dispatch-failed','conversation.request-withdrawn','attempt.chunk']) stream.addEventListener(name,receive);
    stream.onerror=()=> { liveError='Live conversation connection interrupted; retained history remains available. Reconnecting…'; };
    stream.onopen=()=> { liveError=''; void refresh(); };
  });
  onDestroy(() => { destroyed=true; savePosition(); stream?.close(); });
</script>

<section class="durable-conversations" aria-label="Child conversations">
  <div class="section-label">Child conversations</div>
  {#if error}<p role="alert" class="surface-failure">{error} <button on:click={refresh}>Refresh</button></p>{/if}
  {#if liveError}<p role="status" class="surface-failure">{liveError}</p>{/if}
  {#if persistenceError}<p role="alert" class="surface-failure">{persistenceError}</p>{/if}
  <form on:submit|preventDefault={filterChildren}><input aria-label="Filter children by owning task" placeholder="All owning tasks" bind:value={ownerFilter} /><button>Filter children</button></form>
  {#if loading}<p class="muted">Loading saved conversations…</p>{:else if !error && !children.length}<p class="muted">{view.childPages?.length ? 'No further conversations for this selection.' : view.childOwner ? 'No child conversations match this task.' : 'No child conversations in this project.'}</p>{/if}
  <nav aria-label="Saved child conversations">
    {#each children as child}
      <button class:selected={view.selected===child.conversation_id} on:click={()=>select(child.conversation_id)}>
        {child.title} <small>{child.owning_task_ref}</small>{#if child.revision>(view.seen[child.conversation_id] ?? 0)} <span aria-label="Unread activity">●</span>{/if}
      </button>
    {/each}
    {#if view.selected}<button on:click={()=>select('')}>Invocation workspace</button>{/if}
  </nav>
  {#if view.childPages?.length}<button disabled={loading} on:click={()=>pageChildren(false)}>Previous conversations</button>{/if}
  {#if children.length===500}<button disabled={loading} on:click={()=>pageChildren(true)}>More conversations</button>{/if}
  <form on:submit|preventDefault={create} class="new-child">
    <input aria-label="Child conversation title" placeholder="New child conversation" bind:value={title} required />
    <input aria-label="Owning task reference" bind:value={owner} required />
    <button disabled={busy || !title.trim() || !owner.trim()}>Create with current route</button>
  </form>
  {#if conversation}
    <header><h2>{conversation.title}</h2><span>{conversation.route.model} · {conversation.pending?.state ?? 'Ready'}</span> <a href={`?project=${encodeURIComponent(projectId)}&conversation=${encodeURIComponent(conversation.conversation_id)}`}>Conversation link</a></header>
    <div class="rounds" bind:this={reading} on:scroll={savePosition} role="region" aria-label="Saved conversation rounds">
      {#each conversation.rounds as round}
        <article><small>Round {round.ordinal} · {round.state}</small><h3>You</h3><div class="operator-text">{round.request.prompt}</div>
          {#each round.operator_steering ?? [] as instruction}<p class="operator-text">Admitted correction: {instruction.content}</p>{/each}
          {#if round.response !== undefined}<h3>Model</h3><div class="markdown">{@html renderMarkdown(round.response)}</div>{/if}
          {#each round.executions as execution}{#each execution.attempts as attempt}{#if attempt.failure_kind}<p class="surface-failure">{attempt.failure_kind}: {attempt.error_text ?? 'This attempt ended before completion.'}</p>{/if}{/each}{/each}
        </article>
      {/each}
      {#if (view.pages?.[view.selected] ?? 0)>0}<button on:click={()=>pageRounds(Math.max(0,(view.pages?.[view.selected] ?? 0)-100))}>Earlier rounds</button>{/if}
      {#if conversation.has_more}<button on:click={()=>pageRounds(conversation?.rounds.at(-1)?.ordinal ?? 0)}>Later rounds</button>{/if}
    </div>
    {#if activity}<details open={conversation.pending?.state==='active'}><summary>Current partial activity</summary><pre>{activity}</pre></details>{/if}
    {#each conversation.steering as instruction}<p class="queued">Queued steering: {instruction.content}</p>{/each}
    {#if conversation.checkpoint}<p class="muted">Last checkpoint: <code>{conversation.checkpoint.checkpoint_id}</code></p>{#each conversation.checkpoint.content.artifacts as artifact}<a href={`/api/v1/artifacts/${encodeURIComponent(artifact.artifact_id)}`}>Retained artifact {artifact.artifact_id.slice(-8)}</a>{/each}{/if}
    {#if preview}<p>Continuation: <strong>{preview.mode}</strong>. {preview.maximum_remaining_loss}</p>
      {#if preview.available_context}<details><summary>Recovery context and route</summary><p>Include exact retained material below. Exclusions change model input and preserve the saved records.</p>
        {#each preview.available_context.rounds as round}<label><input type="checkbox" checked={!contextChoice(recoveryOptions,preview).omitted_round_ids.includes(round.round_id)} on:change={e=>chooseContext('omitted_round_ids',round.round_id,e.currentTarget.checked)} /> Include completed round {round.ordinal}</label>{/each}
        {#each preview.available_context.observations as item}<label><input type="checkbox" checked={!contextChoice(recoveryOptions,preview).omitted_observation_ids.includes(item.observation_id)} on:change={e=>chooseContext('omitted_observation_ids',item.observation_id,e.currentTarget.checked)} /> Include {item.channel} from attempt {item.attempt_id.slice(-8)}</label>{/each}
        {#each preview.available_context.artifacts as item}<label><input type="checkbox" checked={!contextChoice(recoveryOptions,preview).omitted_artifact_ids.includes(item.artifact_id)} on:change={e=>chooseContext('omitted_artifact_ids',item.artifact_id,e.currentTarget.checked)} /> Include retained artifact {item.artifact_id.slice(-8)}</label>{/each}
        {#each preview.available_context.blocks ?? [] as item}<label><input type="checkbox" checked={contextChoice(recoveryOptions,preview).context_block_ids===null || contextChoice(recoveryOptions,preview).context_block_ids?.includes(item.block_id)} on:change={e=>chooseBlock(item.block_id,e.currentTarget.checked)} /> Include {item.label} ({item.plane})</label>{/each}
        <button disabled={busy} on:click={()=>saveRecovery({...recoveryOptions,context_selection:{...contextChoice(recoveryOptions,preview),context_block_ids:getContextBlockIds()}})}>Replace with workspace context selection ({getContextBlockIds().length} blocks)</button>
        {#if recoveryOptions.context_selection}<label>Reason for context selection<textarea rows="2" value={contextChoice(recoveryOptions,preview).reason} on:input={e=>saveRecovery({...recoveryOptions,context_selection:{...contextChoice(recoveryOptions,preview),reason:e.currentTarget.value}})}></textarea></label>{/if}
        <button disabled={busy} on:click={chooseRoute}>Use current workspace route</button>
        {#if recoveryOptions.route}<p>Selected route: {String(recoveryOptions.route.model)}. This becomes the conversation default after confirmed admission.</p>{/if}
        <button disabled={busy || Boolean(recoveryOptions.context_selection && !recoveryOptions.context_selection.reason.trim())} on:click={reviewRecovery}>Review recovery changes</button>
      </details>{/if}
      {#each preview.required_confirmations ?? [] as reason}<label><input type="checkbox" checked={confirmed(reason,confirmations,view.requests,preview)} on:change={e=>confirmations={...confirmations,[reason.reason_sha256]:e.currentTarget.checked}} /> {confirmationLabel(reason)}</label>{/each}
      {#each preview.blockers as blocker}<p class="surface-failure">{blocker.detail}</p>
      {#each blocker.effects ?? [] as effect}<fieldset><legend>Unconfirmed action: {effect.request.name ?? 'Unknown tool'}</legend>
        <pre>{JSON.stringify(effect.request,null,2)}</pre>
        <label>Outcome <select value={disposition(effect, dispositions).disposition} on:change={e=>setDisposition(effect,'disposition',e.currentTarget.value)}><option value="">Inspect before choosing</option><option value="confirmed_applied">Confirmed completed — do not repeat</option><option value="confirmed_not_applied">Confirmed not performed — may perform</option><option value="do_not_repeat">Outcome remains unknown — do not repeat</option></select></label>
        <label>Evidence or instruction <textarea rows="2" value={disposition(effect, dispositions).evidence} on:input={e=>setDisposition(effect,'evidence',e.currentTarget.value)}></textarea></label>
      </fieldset>{/each}
    {/each}{/if}
    <label for="durable-request">{conversation.pending ? 'Steer this conversation' : 'Next request'}</label>
    <textarea id="durable-request" bind:value={draft} on:input={saveDraft} rows="5" placeholder="Your next request or correction"></textarea>
    <div class="actions">
      {#if !conversation.pending}<button disabled={busy || !draft.trim()} on:click={()=>mutate('send')}>Send</button>{:else}
        <button disabled={busy || !draft.trim()} on:click={()=>mutate('steer')}>Steer next round</button>
        {#if conversation.pending.state==='active' || conversation.pending.state==='queued'}<button disabled={busy} on:click={()=>mutate('interrupt')}>Interrupt</button><button disabled={busy || !draft.trim()} on:click={()=>mutate('steer',true)}>Interrupt and redirect</button>{/if}
        {#if conversation.pending.state==='needs_attention'}<button disabled={busy || !preview || Boolean(recoveryOptions.context_selection && !recoveryOptions.context_selection.reason.trim()) || Boolean(preview?.required_confirmations?.some(reason=>!confirmed(reason,confirmations,view.requests,preview))) || Boolean(preview.blockers.some(b=>b.code!=='uncertain_mutating_effect' || b.effects?.some(e=>!disposition(e, dispositions).disposition || !disposition(e, dispositions).evidence.trim())))} on:click={()=>mutate('continue')}>Continue</button>{/if}
      {/if}
    </div>
  {/if}
</section>

<style>
  .durable-conversations { border-bottom:1px solid var(--border,#30353e); padding:1rem 0; }
  nav,.actions,.new-child { display:flex; flex-wrap:wrap; gap:.5rem; margin:.7rem 0; }
  nav button { text-align:left; } nav small { display:block; opacity:.65; } nav button.selected { outline:1px solid currentColor; }
  .new-child input { min-width:10rem; flex:1; } header { display:flex; gap:1rem; align-items:baseline; flex-wrap:wrap; }
  .rounds { max-height:60vh; overflow:auto; } article { border-bottom:1px solid var(--border,#30353e); padding:1rem 0; }
  .operator-text { white-space:pre-wrap; } textarea { width:100%; box-sizing:border-box; margin:.5rem 0; }
  pre { white-space:pre-wrap; max-height:16rem; overflow:auto; } .queued { white-space:pre-wrap; }
</style>
