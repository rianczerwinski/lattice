export type Project = {
  project_id: string;
  title: string;
  purpose: string;
  sessions?: RoleSession[];
  roles?: Role[];
  invocations?: InvocationSummary[];
  trailheads?: InvocationSummary[];
};

export type Role = { role_id: string; name: string; instructions_block_id: string };
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
export type Invocation = InvocationSummary & { manifest: Manifest; manifest_verification: { valid: boolean }; compiled_prompt: string; output_block?: { content: string }; attempts: Array<Record<string, unknown>>; artifacts: Array<Record<string, unknown>>; edges: Array<Record<string, unknown>> };
