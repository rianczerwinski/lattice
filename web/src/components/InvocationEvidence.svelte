<script lang="ts">
  import type { Invocation } from '../types';
  export let selected: Invocation;
</script>

    <div class="truth-status" class:valid={selected.manifest_verification?.valid}>{selected.manifest_verification?.valid ? 'Manifest reconstructed' : 'Manifest unavailable'}</div>
    <h3>What this invocation saw</h3>
    <div class="segments">{#each selected.manifest.segments as segment}<div class="segment"><i class={`plane ${segment.plane}`}></i><div><strong>{segment.label}</strong><small>{segment.plane} · {segment.status}</small><code>{segment.sha256.slice(0, 12)}</code></div></div>{/each}</div>
    {#if selected.manifest.omitted.length}<h3>Not supplied</h3>{#each selected.manifest.omitted as segment}<div class="omitted">{segment.label} · {segment.disposition}</div>{/each}{/if}
    <details><summary>Exact compiled context</summary><pre>{selected.compiled_prompt}</pre></details>
    <details><summary>Execution route</summary><pre>{JSON.stringify(selected.manifest.execution, null, 2)}</pre></details>
    <details><summary>Response contract</summary><pre>{JSON.stringify(selected.manifest.response_contract, null, 2)}</pre></details>
    <details><summary>Tools & authority</summary><pre>{JSON.stringify(selected.manifest.capabilities, null, 2)}</pre></details>
    <details open={(selected.manifest.provider_boundary.opaque_contexts ?? []).length > 0}><summary>Proof limits & provider-opaque context</summary>{#each selected.manifest.provider_boundary.proof_limits as limit}<p>{limit}</p>{/each}{#each (selected.manifest.provider_boundary.opaque_contexts ?? []) as opaque}<p><strong>Provider-opaque context:</strong> {opaque.statement}</p><pre>{JSON.stringify(opaque, null, 2)}</pre>{/each}</details>
    <details><summary>Attempts ({selected.attempts.length})</summary><pre>{JSON.stringify(selected.attempts, null, 2)}</pre></details>
