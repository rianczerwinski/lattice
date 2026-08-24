import type { ContextModuleCatalogue, ContextModuleSourceKind } from './types';

export function buildContextModuleDefinition(
  catalogue: ContextModuleCatalogue,
  input: { moduleId: string; mode: 'manual' | 'project_default'; sourceKind: ContextModuleSourceKind; source: string },
): Record<string, unknown> {
  const adapter = catalogue.sources.find((item) => item.source_kind === input.sourceKind);
  if (!adapter) throw new Error(`Context source ${input.sourceKind} is not registered.`);
  if (!input.moduleId.trim() || !input.source.trim()) throw new Error('Context module identity and source are required.');
  let configuration: Record<string, unknown>;
  if (input.sourceKind === 'file' || input.sourceKind === 'surface') configuration = { path: input.source.trim() };
  else if (input.sourceKind === 'octopus') configuration = { conversation_id: input.source.trim(), representation: 'transcript.txt' };
  else {
    const url = new URL(input.source.trim());
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('HTTP context requires an uncredentialed http or https URL.');
    configuration = { url: url.toString(), allowed_origins: [url.origin] };
  }
  return {
    module_id: input.moduleId.trim(),
    version: 1,
    mode: input.mode,
    source_kind: input.sourceKind,
    capabilities: [adapter.required_capability],
    configuration,
  };
}
