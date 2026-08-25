"""Versioned contracts at Lattice's operator, storage, and provider boundaries."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EdgeRelation(StrEnum):
    inherited_context_from = "inherited_context_from"
    consumed_artifact = "consumed_artifact"
    derived_from = "derived_from"
    responds_to = "responds_to"
    challenges = "challenges"
    supersedes = "supersedes"
    validates = "validates"
    triggered_by = "triggered_by"
    concerns = "concerns"
    produced = "produced"


class BlockDisposition(StrEnum):
    selected = "selected"
    summarized = "summarized"
    excluded_superseded = "excluded_superseded"
    omitted = "omitted"
    unavailable = "unavailable"
    provider_opaque = "provider_opaque"


class BlockStatus(StrEnum):
    active = "active"
    superseded = "superseded"
    disputed = "disputed"
    rejected = "rejected"
    unknown = "unknown"


class DraftState(StrEnum):
    editable = "editable"
    sealed = "sealed"
    abandoned = "abandoned"


class InvocationState(StrEnum):
    sealed = "sealed"
    leased = "leased"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    interrupted = "interrupted"
    imported = "imported"


class AttemptState(StrEnum):
    leased = "leased"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    interrupted = "interrupted"
    orphaned = "orphaned"


class ProviderRoute(StrictModel):
    route_kind: Literal["execution", "historical-import"] = "execution"
    provider: Literal["anthropic", "openai", "google", "xai"]
    model: str = Field(min_length=1)
    harness: Literal["claude", "codex", "antigravity", "grok"]
    provider_profile_id: str | None = Field(default=None, pattern=r"^provider-profile:[0-9a-f]{64}$")
    effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] = "high"
    execution_profile: Literal["compiled-fresh", "research-fresh", "workspace-agent", "native-continuation"] = "compiled-fresh"
    session_policy: Literal["fresh", "visible_native_continuation"] = "fresh"
    continuation_mode: Literal["resume", "fork"] | None = None
    native_parent_invocation_id: str | None = None
    native_session_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    workspace: str | None = None
    sandbox: Literal["read-only", "workspace-write", "provider-permissions"] = "read-only"
    approval_policy: Literal["never", "on-request"] = "never"
    network_policy: Literal["disabled", "provider_managed", "declared_allowlist"] = "disabled"

    @model_validator(mode="after")
    def provider_matches_official_harness(self) -> "ProviderRoute":
        expected = {"anthropic": "claude", "openai": "codex", "google": "antigravity", "xai": "grok"}
        if expected[self.provider] != self.harness:
            raise ValueError(f"{self.provider} is qualified only through {expected[self.provider]}")
        if self.route_kind == "historical-import":
            if self.provider_profile_id is not None:
                raise ValueError("historical imports cannot claim a current executable provider profile")
            if not self.model.startswith("historical-"):
                raise ValueError("historical imports must name their unverified historical model explicitly")
            if self.execution_profile != "compiled-fresh" or self.session_policy != "fresh" or self.continuation_mode is not None or self.native_parent_invocation_id is not None or self.native_session_id is not None or self.tools or self.permissions or self.workspace is not None or self.sandbox != "read-only" or self.approval_policy != "never" or self.network_policy != "disabled":
                raise ValueError("historical imports cannot carry executable authority")
            return self
        from .execution import DEFAULT_ADAPTERS

        base = next((profile for profile in DEFAULT_ADAPTERS if profile.provider_code == self.provider and profile.harness == self.harness), None)
        if base is None:
            raise ValueError(f"{self.provider}/{self.harness} has no immutable provider profile")
        try:
            profile = base.select_model(self.model)
        except RuntimeError as error:
            raise ValueError(str(error)) from error
        if self.provider_profile_id is None:
            self.provider_profile_id = profile.profile_id
        elif self.provider_profile_id != profile.profile_id:
            raise ValueError("provider profile identity disagrees with provider, harness, and model")
        native = self.execution_profile == "native-continuation"
        if native:
            if self.session_policy != "visible_native_continuation":
                raise ValueError("native continuation requires visible_native_continuation session policy")
            if self.continuation_mode is None or self.native_parent_invocation_id is None:
                raise ValueError("native continuation requires an explicit parent and resume or fork mode")
        elif self.session_policy != "fresh" or self.continuation_mode is not None or self.native_parent_invocation_id is not None or self.native_session_id is not None:
            raise ValueError("only native continuation may carry provider-session ancestry")
        return self


def execution_policy(route: dict[str, Any]) -> dict[str, Any]:
    """Project a stored Lattice route into the enforceable execution-policy surface."""

    execution_profile = route.get("execution_profile", "native-continuation" if route.get("session_policy") == "visible_native_continuation" else "compiled-fresh")
    return {
        "execution_profile": execution_profile,
        "session_policy": route.get("session_policy", "visible_native_continuation" if execution_profile == "native-continuation" else "fresh"),
        "continuation_mode": route.get("continuation_mode"),
        "native_session_id": route.get("native_session_id"),
        "tools": route.get("tools", []),
        "permissions": route.get("permissions", []),
        "workspace": route.get("workspace"),
        "sandbox": route.get("sandbox", "read-only"),
        "approval_policy": route.get("approval_policy", "never"),
        "network_policy": route.get("network_policy", "disabled"),
    }


class DirectActorBinding(StrictModel):
    kind: Literal["direct"] = "direct"


class RoleSessionActorBinding(StrictModel):
    kind: Literal["role-session"] = "role-session"
    role_session_id: str = Field(min_length=1)


ActorBindingV1 = Annotated[DirectActorBinding | RoleSessionActorBinding, Field(discriminator="kind")]


class ProjectCreate(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    purpose: str = Field(default="", max_length=20_000)
    workflow_pack: str | None = None


class RoleCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=100_000)
    incentives: list[str] = Field(default_factory=list)
    authority_limits: list[str] = Field(default_factory=list)


class RoleSessionCreate(StrictModel):
    project_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=200)


class RolePinCreate(RoleCreate):
    """Create a future role from an exact completed conversational demonstration."""

    session_label: str = Field(min_length=1, max_length=200)


class ContextBlockCreate(StrictModel):
    project_id: str
    semantic_type: str = Field(min_length=1, max_length=120)
    content: str
    status: BlockStatus = BlockStatus.active
    producing_invocation_id: str | None = None
    source_ref: str | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authority: str = "operator"
    evidence_level: str = "assertion"
    sensitivity: Literal["private", "shareable", "public"] = "private"
    transformation: dict[str, Any] | None = None

    @model_validator(mode="after")
    def exact_external_sources_are_bound(self) -> "ContextBlockCreate":
        if (self.source_ref is None) != (self.source_sha256 is None):
            raise ValueError("external context sources require both ref and exact SHA-256")
        return self


class ContextSelection(StrictModel):
    block_id: str
    disposition: BlockDisposition = BlockDisposition.selected
    plane: Literal["instruction", "knowledge", "state", "ancestry", "current_prompt"]
    label: str = Field(min_length=1, max_length=200)
    derived_from_block_ids: list[str] = Field(default_factory=list, max_length=1000)
    reason: str | None = None

    @field_validator("label")
    @classmethod
    def label_is_single_line_and_unambiguous(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value) or "<<<" in value:
            raise ValueError("context labels cannot contain controls or Lattice framing tokens")
        return value

    @field_validator("derived_from_block_ids")
    @classmethod
    def derivation_sources_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("derived context source IDs must be unique")
        return value


def standard_response_contract() -> dict[str, Any]:
    """Return the only response envelope the current persistence layer can preserve losslessly."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["response", "artifacts"],
        "properties": {
            "response": {"type": "string", "maxLength": 67_108_864},
            "artifacts": {
                "type": "array",
                "maxItems": 256,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "media_type", "content"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 300},
                        "media_type": {"type": "string", "minLength": 1, "maxLength": 200},
                        "content": {"type": "string", "maxLength": 67_108_864},
                    },
                },
            },
        },
    }


class OutputContractV2(StrictModel):
    """Lossless result shape requested by a caller.

    Providers still receive a strict JSON Schema in every mode. Text and
    Markdown are wrapped only at the provider boundary, then persisted as
    their exact UTF-8 content and a content-addressed artifact.
    """

    contract_version: Literal["OutputContract/2.0"] = "OutputContract/2.0"
    mode: Literal["conversation", "json", "text", "markdown"] = "conversation"
    json_schema: dict[str, Any] | None = None
    media_type: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def mode_has_exact_payload_contract(self) -> "OutputContractV2":
        if self.mode == "json":
            if self.json_schema is None:
                raise ValueError("JSON output requires an arbitrary strict JSON Schema")
            if self.media_type not in {None, "application/json"}:
                raise ValueError("JSON output media type is application/json")
            self.media_type = "application/json"
        elif self.json_schema is not None:
            raise ValueError(f"{self.mode} output cannot carry a JSON payload schema")
        elif self.mode == "markdown":
            if self.media_type not in {None, "text/markdown"}:
                raise ValueError("Markdown output media type is text/markdown")
            self.media_type = "text/markdown"
        elif self.mode == "text":
            if self.media_type not in {None, "text/plain"}:
                raise ValueError("text output media type is text/plain")
            self.media_type = "text/plain"
        elif self.media_type is not None:
            raise ValueError("conversation output uses its response-and-artifacts envelope")
        return self

    def provider_schema(self) -> dict[str, Any]:
        if self.mode == "conversation":
            return standard_response_contract()
        if self.mode == "json":
            assert self.json_schema is not None
            return {
                "type": "object",
                "additionalProperties": False,
                "required": ["payload"],
                "properties": {"payload": self.json_schema},
            }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["content"],
            "properties": {"content": {"type": "string", "maxLength": 67_108_864}},
        }

    @classmethod
    def from_stored(cls, value: dict[str, Any]) -> "OutputContractV2":
        if value == standard_response_contract():
            return cls()
        return cls.model_validate(value)


def validate_stored_output_contract(value: dict[str, Any]) -> dict[str, Any]:
    """Accept the exact legacy envelope or a versioned OutputContract/2.0."""

    try:
        OutputContractV2.from_stored(value)
    except ValidationError as error:
        raise ValueError("output must use the lossless response-and-artifacts envelope or OutputContract/2.0") from error
    return value


class DraftCreate(StrictModel):
    project_id: str
    actor_binding: ActorBindingV1 | None = None
    role_session_id: str | None = None
    # Context-only invocations and exact historical imports can legitimately carry
    # an empty visible prompt. Presence is represented by the block, not its length.
    prompt: str = Field(max_length=1_000_000)
    route: ProviderRoute
    selections: list[ContextSelection] = Field(default_factory=list, max_length=1000)
    parent_invocation_ids: list[str] = Field(default_factory=list, max_length=32)
    relation: EdgeRelation = EdgeRelation.responds_to
    context_mode: Literal["standard", "explicit"] = "standard"
    response_schema: dict[str, Any] = Field(default_factory=standard_response_contract)

    @field_validator("parent_invocation_ids")
    @classmethod
    def parents_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("parent invocation IDs must be unique")
        return value

    @model_validator(mode="after")
    def explicit_context_is_complete(self) -> "DraftCreate":
        if self.actor_binding is None:
            if self.role_session_id is None:
                raise ValueError("drafts require an explicit direct or role-session actor binding")
            self.actor_binding = RoleSessionActorBinding(role_session_id=self.role_session_id)
        elif isinstance(self.actor_binding, DirectActorBinding):
            if self.role_session_id is not None:
                raise ValueError("direct actor bindings cannot carry a role session")
        else:
            if self.role_session_id is not None and self.role_session_id != self.actor_binding.role_session_id:
                raise ValueError("role session compatibility field disagrees with actor binding")
            self.role_session_id = self.actor_binding.role_session_id
        if self.context_mode == "explicit":
            prompts = [item for item in self.selections if item.plane == "current_prompt"]
            if len(prompts) != 1 or prompts[0].disposition != BlockDisposition.selected:
                raise ValueError("explicit context requires exactly one directly selected current-prompt block")
        return self

    @field_validator("response_schema")
    @classmethod
    def response_contract_is_losslessly_supported(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_stored_output_contract(value)


class DraftPatch(StrictModel):
    expected_revision: int = Field(ge=1)
    prompt: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    route: ProviderRoute | None = None
    selections: list[ContextSelection] | None = Field(default=None, max_length=1000)
    parent_invocation_ids: list[str] | None = Field(default=None, max_length=32)

    @field_validator("parent_invocation_ids")
    @classmethod
    def parents_are_unique_when_replaced(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("parent invocation IDs must be unique")
        return value


class ContextAssemblyV1(StrictModel):
    """The route-neutral semantic context shared by one or more invocations."""

    project_id: str = Field(min_length=1)
    actor_binding: ActorBindingV1 = Field(default_factory=DirectActorBinding)
    prompt: str = Field(max_length=1_000_000)
    selections: list[ContextSelection] = Field(default_factory=list, max_length=1000)
    parent_invocation_ids: list[str] = Field(default_factory=list, max_length=32)
    relation: EdgeRelation = EdgeRelation.responds_to
    context_mode: Literal["standard", "explicit"] = "standard"
    response_schema: dict[str, Any] = Field(default_factory=standard_response_contract)

    @field_validator("parent_invocation_ids")
    @classmethod
    def assembly_parents_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("parent invocation IDs must be unique")
        return value

    @model_validator(mode="after")
    def assembly_context_is_complete(self) -> "ContextAssemblyV1":
        if self.context_mode == "explicit":
            prompts = [item for item in self.selections if item.plane == "current_prompt"]
            if len(prompts) != 1 or prompts[0].disposition != BlockDisposition.selected:
                raise ValueError("explicit context requires exactly one directly selected current-prompt block")
        validate_stored_output_contract(self.response_schema)
        return self


class ExecutionSubmissionV1(StrictModel):
    """Atomic direct-call contract shared by the service, CLI, and clients."""

    contract_version: Literal["ExecutionSubmission/1.0"] = "ExecutionSubmission/1.0"
    external_project_key: str = Field(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
    operation_key: str = Field(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
    actor: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    workflow_role: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    prompt: str = Field(max_length=1_000_000)
    context_block_ids: list[str] = Field(default_factory=list, max_length=1000)
    parent_invocation_ids: list[str] = Field(default_factory=list, max_length=32)
    route: ProviderRoute
    output_contract: OutputContractV2 = Field(default_factory=OutputContractV2)
    idempotency_key: str = Field(min_length=8, max_length=300)

    @model_validator(mode="after")
    def identities_and_references_are_unique(self) -> "ExecutionSubmissionV1":
        if len(self.context_block_ids) != len(set(self.context_block_ids)):
            raise ValueError("submission context block IDs must be unique")
        if len(self.parent_invocation_ids) != len(set(self.parent_invocation_ids)):
            raise ValueError("submission parent invocation IDs must be unique")
        return self


class ExecutionResultV1(StrictModel):
    contract_version: Literal["ExecutionResult/1.0"] = "ExecutionResult/1.0"
    external_project_key: str
    operation_key: str
    project_id: str
    invocation_id: str
    attempt_id: str | None = None
    state: InvocationState
    terminal_kind: str | None = None
    typed_failure: str | None = None
    output_block_id: str | None = None
    output_sha256: str | None = None
    output_artifact_ids: list[str] = Field(default_factory=list)
    manifest_sha256: str
    journal_receipt: dict[str, Any] | None = None
    provider_attestation: dict[str, Any]


class HistoricalExecutionFileV1(StrictModel):
    relative_path: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00-\x1f\x7f]+$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)

    @field_validator("relative_path")
    @classmethod
    def path_is_relative_and_contained(cls, value: str) -> str:
        if value.startswith(("/", "\\")) or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("historical execution file paths must be relative and contained")
        return value


class HistoricalExecutionImportV1(StrictModel):
    contract_version: Literal["HistoricalExecutionImport/1.0"] = "HistoricalExecutionImport/1.0"
    external_project_key: str = Field(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
    operation_key: str = Field(min_length=1, max_length=300, pattern=r"^[^\x00-\x1f\x7f]+$")
    source_ref: str = Field(min_length=1, max_length=4000, pattern=r"^[^\x00-\x1f\x7f]+$")
    source_census_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[HistoricalExecutionFileV1] = Field(min_length=1, max_length=10000)
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    original_invocation_id: str | None = Field(default=None, max_length=300)
    terminal_state: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def census_is_unique(self) -> "HistoricalExecutionImportV1":
        paths = [item.relative_path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("historical execution file paths must be unique")
        canonical_files = [item.model_dump(mode="json") for item in sorted(self.files, key=lambda item: item.relative_path)]
        encoded = json.dumps(canonical_files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.source_census_sha256:
            raise ValueError("historical execution census hash does not bind its exact file inventory")
        return self


class HistoricalExecutionImportResultV1(StrictModel):
    contract_version: Literal["HistoricalExecutionImportResult/1.0"] = "HistoricalExecutionImportResult/1.0"
    import_id: str
    external_project_key: str
    project_id: str
    operation_key: str
    source_ref: str
    source_census_sha256: str
    imported_at: str


class InvocationSpecV1(StrictModel):
    spec_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$")
    assembly: ContextAssemblyV1
    route: ProviderRoute
    concern_refs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("concern_refs")
    @classmethod
    def concerns_are_unique_and_printable(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("invocation concern identities must be unique")
        if any(not item or len(item) > 500 or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in value):
            raise ValueError("invocation concern identities must be printable")
        return value


class InvocationBatchPreviewV1(StrictModel):
    batch_id: str = Field(pattern=r"^batch_[0-9a-f]{32}$")
    specifications: list[InvocationSpecV1] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def batch_is_one_project_with_unique_members(self) -> "InvocationBatchPreviewV1":
        spec_ids = [item.spec_id for item in self.specifications]
        if len(spec_ids) != len(set(spec_ids)):
            raise ValueError("batch specification IDs must be unique")
        if len({item.assembly.project_id for item in self.specifications}) != 1:
            raise ValueError("one invocation batch cannot cross projects")
        return self


class InvocationBatchCreateV1(InvocationBatchPreviewV1):
    idempotency_key: str = Field(min_length=8, max_length=200)
    expected_preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_reason_sha256s: list[str] = Field(default_factory=list, max_length=100, min_length=0)

    @field_validator("confirmed_reason_sha256s")
    @classmethod
    def confirmations_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("confirmation reason identities must be unique")
        return value


class InvocationBatchReceiptV1(StrictModel):
    batch_id: str
    project_id: str
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    invocation_ids: list[str] = Field(min_length=1, max_length=16)
    manifest_sha256s: list[str] = Field(min_length=1, max_length=16)
    assembly_sha256s: list[str] = Field(min_length=1, max_length=16)
    generated_object: dict[str, Any]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DerivedTextBlockCreate(StrictModel):
    project_id: str
    source_block_id: str = Field(min_length=1)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(gt=0)
    semantic_type: str = Field(default="exact-excerpt", min_length=1, max_length=120)
    sensitivity: Literal["private", "shareable", "public"] | None = None

    @model_validator(mode="after")
    def excerpt_range_is_forward(self) -> "DerivedTextBlockCreate":
        if self.end_byte <= self.start_byte:
            raise ValueError("excerpt end byte must follow its start byte")
        return self


class EpisodeCreate(StrictModel):
    project_id: str
    name: str = Field(min_length=1, max_length=300)
    parent_episode_id: str | None = None
    anchor_type: Literal["invocation", "context-block", "artifact"]
    anchor_id: str = Field(min_length=1)


class EpisodeMemberCreate(StrictModel):
    object_type: Literal["invocation", "context-block", "artifact"]
    object_id: str = Field(min_length=1)


class ExecuteRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=200)


class TagRequest(StrictModel):
    concern_ref: str = Field(min_length=1, max_length=500, pattern=r"^[^\x00-\x1f\x7f]+$")
    label: str = Field(min_length=1, max_length=300)


class SliceQuery(StrictModel):
    concern_ref: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"^[^\x00-\x1f\x7f]+$")
    object_types: list[Literal["invocation", "context-block", "artifact"]] = Field(default_factory=lambda: ["invocation"], min_length=1)

    @field_validator("object_types")
    @classmethod
    def object_types_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("slice object types must be unique")
        return value


class SliceCreate(StrictModel):
    project_id: str
    scope: Literal["project", "global"] = "project"
    name: str = Field(min_length=1, max_length=300)
    query: SliceQuery
    external_ref: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"^[^\x00-\x1f\x7f]+$")

    @model_validator(mode="after")
    def external_identities_are_global(self) -> "SliceCreate":
        if self.external_ref is not None and self.scope != "global":
            raise ValueError("externally addressable Lattice slices must be global")
        if self.external_ref is not None and self.query.concern_ref != self.external_ref:
            raise ValueError("external slice identity must equal its concern query")
        if self.scope == "global" and self.query.concern_ref is None:
            raise ValueError("global Lattice slices require an exact concern query")
        return self


class ImportConversationRequest(StrictModel):
    project_id: str
    conversation_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)


class ContextModuleMode(StrEnum):
    manual = "manual"
    project_default = "project_default"
    # Read compatibility for workflow packs and databases authored before
    # project-scoped defaults became available.
    role_default = "role_default"
    workflow_required = "workflow_required"


class ContextModuleDefinition(StrictModel):
    module_id: str = Field(pattern=r"^[a-z][a-z0-9.-]*$")
    version: int = Field(ge=1)
    mode: ContextModuleMode
    capabilities: list[str] = Field(default_factory=list)
    source_kind: Literal["file", "octopus", "surface", "http_readonly"]
    configuration: dict[str, Any]


class WorkflowRoleDefinition(StrictModel):
    role_id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=120)
    provider: Literal["anthropic", "openai", "google", "xai", "local", "operator"]
    harness: Literal["claude", "codex", "antigravity", "grok"] | None = None
    allowed_models: list[str] = Field(default_factory=list)
    authority: str = Field(min_length=1)
    instructions: str | None = Field(default=None, min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def execution_boundary_is_explicit(self) -> "WorkflowRoleDefinition":
        expected = {"anthropic": "claude", "openai": "codex", "google": "antigravity", "xai": "grok"}
        if self.provider in expected and self.harness != expected[self.provider]:
            raise ValueError(f"{self.provider} workflow roles require the {expected[self.provider]} harness")
        if self.provider in {"local", "operator"} and self.harness is not None:
            raise ValueError("local and operator workflow roles cannot declare a model harness")
        return self


class FreezePayloadFieldAction(StrictModel):
    type: Literal["freeze_payload_field"]
    field: str = Field(min_length=1)


class InvokeRoleAction(StrictModel):
    type: Literal["invoke_role"]
    role_id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    prompt: str = Field(min_length=1, max_length=100_000)
    exclude_payload_block_field: str | None = None


class WorkflowTriggerDefinition(StrictModel):
    trigger: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    required_action: str = Field(min_length=1)
    automatic: bool
    action: FreezePayloadFieldAction | InvokeRoleAction = Field(discriminator="type")


class WorkflowEvidenceRequirement(StrictModel):
    issuer_kind: Literal["workflow-engine", "workflow-adapter", "invocation", "context-module", "importer", "system"]
    semantic_type: str | None = Field(default=None, min_length=1, max_length=120)
    authority: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_level: str | None = Field(default=None, min_length=1, max_length=200)
    status: BlockStatus | None = None
    provenance: Literal["current_workflow_blackboard"] | None = None
    minimum_count: int = Field(default=1, ge=1, le=1000)
    after_current_state_entry: bool = False

    @model_validator(mode="after")
    def requirement_has_a_truth_predicate(self) -> "WorkflowEvidenceRequirement":
        if self.semantic_type is None and self.authority is None and self.evidence_level is None and self.status is None and self.provenance is None and not self.after_current_state_entry:
            raise ValueError("workflow evidence requirements must constrain observable block truth")
        return self


class WorkflowTerminalOutputRequirement(StrictModel):
    workflow_role_key: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    invocation_states: list[Literal["succeeded", "imported"]] = Field(default_factory=lambda: ["succeeded"], min_length=1)
    minimum_count: int = Field(default=1, ge=1, le=1000)
    after_current_state_entry: bool = True

    @field_validator("invocation_states")
    @classmethod
    def states_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("workflow terminal output states must be unique")
        return value


class WorkflowTransitionDefinition(StrictModel):
    from_state: str = Field(alias="from", min_length=1)
    to_state: str = Field(alias="to", min_length=1)
    trigger: str | None = None
    requires_completed_trigger: str | None = None
    minimum_evidence_blocks: int = Field(default=0, ge=0, le=1000)
    maximum_evidence_blocks: int | None = Field(default=None, ge=0, le=1000)
    minimum_terminal_invocation_outputs: int = Field(default=0, ge=0, le=1000)
    terminal_outputs_after_current_state_entry: bool = False
    terminal_output_requirements: list[WorkflowTerminalOutputRequirement] = Field(default_factory=list)
    evidence_requirements: list[WorkflowEvidenceRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def terminal_outputs_are_part_of_evidence(self) -> "WorkflowTransitionDefinition":
        if self.maximum_evidence_blocks is not None and self.maximum_evidence_blocks < self.minimum_evidence_blocks:
            raise ValueError("maximum workflow evidence cannot be below minimum evidence")
        if self.minimum_terminal_invocation_outputs > self.minimum_evidence_blocks:
            raise ValueError("terminal invocation evidence cannot exceed total evidence")
        if self.evidence_requirements and self.minimum_evidence_blocks < max(item.minimum_count for item in self.evidence_requirements):
            raise ValueError("typed evidence requirements cannot exceed total evidence")
        if self.terminal_output_requirements and self.minimum_evidence_blocks < max(item.minimum_count for item in self.terminal_output_requirements):
            raise ValueError("typed terminal output requirements cannot exceed total evidence")
        return self


class WorkflowCommandArgument(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=120)
    value_type: Literal["string", "integer", "json", "file"] = "string"
    required: bool = True
    default: Any | None = None
    minimum: int | None = None
    maximum: int | None = None
    max_bytes: int | None = Field(default=None, ge=1, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def default_and_bounds_match_declared_type(self) -> "WorkflowCommandArgument":
        if self.value_type != "integer" and (self.minimum is not None or self.maximum is not None):
            raise ValueError("workflow argument bounds apply only to integers")
        if self.value_type != "file" and self.max_bytes is not None:
            raise ValueError("workflow argument max_bytes applies only to files")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("workflow argument minimum exceeds maximum")
        if self.default is not None:
            if self.value_type in {"string", "file"} and not isinstance(self.default, str):
                raise ValueError("workflow string argument has a non-string default")
            if self.value_type == "integer" and (not isinstance(self.default, int) or isinstance(self.default, bool)):
                raise ValueError("workflow integer argument has a non-integer default")
            if self.value_type == "integer" and self.minimum is not None and self.default < self.minimum:
                raise ValueError("workflow integer default is below minimum")
            if self.value_type == "integer" and self.maximum is not None and self.default > self.maximum:
                raise ValueError("workflow integer default exceeds maximum")
        return self


class WorkflowCommandDefinition(StrictModel):
    operation: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    authority: Literal["read", "write", "provider_transmission"]
    arguments: list[WorkflowCommandArgument] = Field(default_factory=list)

    @model_validator(mode="after")
    def argument_keys_are_unique(self) -> "WorkflowCommandDefinition":
        keys = [item.key for item in self.arguments]
        if len(keys) != len(set(keys)):
            raise ValueError("workflow command argument keys must be unique")
        return self


class WorkflowSelectorField(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=120)
    value_type: Literal["string", "integer"] = "string"
    required: bool = True
    default: str | int | None = None

    @model_validator(mode="after")
    def default_matches_declared_type(self) -> "WorkflowSelectorField":
        if self.default is not None and self.value_type == "string" and not isinstance(self.default, str):
            raise ValueError("workflow string selector has a non-string default")
        if self.default is not None and self.value_type == "integer" and (not isinstance(self.default, int) or isinstance(self.default, bool)):
            raise ValueError("workflow integer selector has a non-integer default")
        return self


class WorkflowPackDefinition(StrictModel):
    pack_id: str = Field(pattern=r"^[a-z][a-z0-9.-]*$")
    version: int = Field(ge=1)
    title: str
    initial_state: str = Field(min_length=1)
    roles: list[WorkflowRoleDefinition]
    context_modules: list[ContextModuleDefinition] = Field(default_factory=list)
    project_selector: list[WorkflowSelectorField] = Field(default_factory=list)
    allowed_transitions: list[WorkflowTransitionDefinition] = Field(default_factory=list)
    triggers: list[WorkflowTriggerDefinition] = Field(default_factory=list)
    commands: list[WorkflowCommandDefinition] = Field(default_factory=list)
    closure_contract: dict[str, Any]

    @model_validator(mode="after")
    def workflow_graph_is_unambiguous(self) -> "WorkflowPackDefinition":
        pairs: set[tuple[str, str]] = set()
        for transition in self.allowed_transitions:
            pair = (transition.from_state, transition.to_state)
            if pair in pairs:
                raise ValueError("workflow transitions must be unique")
            pairs.add(pair)
        role_ids = [role.role_id for role in self.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("workflow role IDs must be unique")
        trigger_names = [trigger.trigger for trigger in self.triggers]
        if len(trigger_names) != len(set(trigger_names)):
            raise ValueError("workflow trigger names must be unique")
        command_names = [command.operation for command in self.commands]
        if len(command_names) != len(set(command_names)):
            raise ValueError("workflow command operations must be unique")
        selector_keys = [field.key for field in self.project_selector]
        if len(selector_keys) != len(set(selector_keys)):
            raise ValueError("workflow project selector keys must be unique")
        known_triggers = set(trigger_names)
        bound_triggers = [transition.trigger for transition in self.allowed_transitions if transition.trigger]
        if set(bound_triggers) != known_triggers or len(bound_triggers) != len(set(bound_triggers)):
            raise ValueError("every workflow trigger must be bound to exactly one transition")
        trigger_sources = {transition.trigger: transition for transition in self.allowed_transitions if transition.trigger}
        for transition in self.allowed_transitions:
            if transition.requires_completed_trigger and transition.requires_completed_trigger not in known_triggers:
                raise ValueError("required workflow trigger is not declared")
            if transition.requires_completed_trigger and trigger_sources[transition.requires_completed_trigger].to_state != transition.from_state:
                raise ValueError("required workflow triggers must be produced by the transition that enters the gated state")
        for trigger in self.triggers:
            if isinstance(trigger.action, InvokeRoleAction) and trigger.action.role_id not in set(role_ids):
                raise ValueError("workflow trigger references an undeclared role")
        adjacency: dict[str, set[str]] = {}
        states = {self.initial_state}
        for transition in self.allowed_transitions:
            adjacency.setdefault(transition.from_state, set()).add(transition.to_state)
            states.update((transition.from_state, transition.to_state))
        reachable = {self.initial_state}
        frontier = [self.initial_state]
        while frontier:
            current = frontier.pop()
            for target in adjacency.get(current, set()) - reachable:
                reachable.add(target)
                frontier.append(target)
        if reachable != states:
            raise ValueError("every workflow state must be reachable from initial_state")
        return self


class WorkflowTransitionRequest(StrictModel):
    project_id: str
    expected_revision: int = Field(ge=1)
    from_state: str = Field(min_length=1)
    to_state: str = Field(min_length=1)
    evidence_block_ids: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowAutomationGrantRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    enabled: bool
    pack_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
