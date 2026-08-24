import { describe, expect, it } from 'vitest';
import { buildContextModuleDefinition } from './contextModules';
import type { ContextModuleCatalogue } from './types';

const catalogue: ContextModuleCatalogue = {
  contract_version: 'LatticeContextModuleCatalogue/1.0',
  modes: ['manual', 'project_default', 'workflow_required'],
  sources: [
    { source_kind: 'file', required_capability: 'read-local-file', configuration_fields: { path: 'required' }, output: 'exact-utf8-text' },
    { source_kind: 'http_readonly', required_capability: 'read-http', configuration_fields: { url: 'required', allowed_origins: 'required' }, output: 'exact-utf8-text' },
  ],
};

describe('context module definitions', () => {
  it('binds a registered source to its exact capability and origin', () => {
    expect(buildContextModuleDefinition(catalogue, { moduleId: 'weather.home', mode: 'project_default', sourceKind: 'http_readonly', source: 'https://weather.example/current' })).toEqual({
      module_id: 'weather.home',
      version: 1,
      mode: 'project_default',
      source_kind: 'http_readonly',
      capabilities: ['read-http'],
      configuration: { url: 'https://weather.example/current', allowed_origins: ['https://weather.example'] },
    });
  });

  it('rejects unregistered and credential-bearing source mutations', () => {
    expect(() => buildContextModuleDefinition(catalogue, { moduleId: 'bad', mode: 'manual', sourceKind: 'surface', source: '/tmp/SURFACE.json' })).toThrow('not registered');
    expect(() => buildContextModuleDefinition(catalogue, { moduleId: 'bad', mode: 'manual', sourceKind: 'http_readonly', source: 'https://user:secret@weather.example/current' })).toThrow('uncredentialed');
  });
});
