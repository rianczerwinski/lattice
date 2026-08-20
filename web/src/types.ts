export type Project = {
  project_id: string;
  title: string;
  purpose: string;
  sessions?: RoleSession[];
  roles?: Role[];
  invocations?: InvocationSummary[];
  trailheads?: InvocationSummary[];
  workflow_pack?: string;
  workflow?: WorkflowInstance | null;
};

export type WorkflowTrigger = { trigger_id: string; trigger_name: string; required_action: string; state: string; result_ref?: string; error_text?: string };
export type WorkflowInstance = { pack_id: string; state: string; revision: number; updated_at: string; automatic_grants: string[]; triggers: WorkflowTrigger[]; blackboard?: { block_id: string; content: string; source_ref?: string; content_sha256: string } };
export type RoleRoutePolicy = { kind: 'operator-selectable' | 'model' | 'non-model'; provider?: string; harness?: string; allowed_models?: string[] };
export type Role = { role_id: string; name: string; instructions_block_id: string; workflow_role_key?: string; authority_limits: string[]; route_policy: RoleRoutePolicy };
export type RoleSession = { role_session_id: string; role_id: string; label: string };
export type Route = { provider_code: string; model_code: string; official_cli: string; installed: boolean; proof_limits: string[] };
export type InvocationSummary = { invocation_id: string; state: string; role_session_id: string; output_block_id?: string; created_at: string };
export type ManifestSegment = { block_id: string; plane: string; label: string; disposition: string; sha256: string; status: string; semantic_type: string };
export type Manifest = {
  invocation_id: string;
  manifest_sha256: string;
  segments: ManifestSegment[];
  omitted: ManifestSegment[];
  execution: Record<string, unknown>;
  capabilities: Record<string, unknown>;
  provider_boundary: { proof_limits: string[] };
  compilation: { byte_count: number; compiled_prompt_sha256: string };
};
export type InvocationArtifact = { artifact_id: string; content_sha256: string; generated_json: string; artifact_kind: string };
export type Invocation = InvocationSummary & { manifest: Manifest; manifest_verification: { valid: boolean }; compiled_prompt: string; output_block?: { content: string }; attempts: Array<Record<string, unknown>>; artifacts: InvocationArtifact[]; edges: Array<Record<string, unknown>> };
