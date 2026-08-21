"""Versioned contracts at Loom's operator, storage, and provider boundaries."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    provider: Literal["anthropic", "openai", "google", "xai"]
    model: str = Field(min_length=1)
    harness: Literal["claude", "codex", "gemini", "grok"]
    effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] = "high"
    session_policy: Literal["fresh", "visible_native_continuation"] = "fresh"
    tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    workspace: str | None = None
    network_policy: Literal["disabled", "provider_managed", "declared_allowlist"] = "disabled"

    @model_validator(mode="after")
    def provider_matches_official_harness(self) -> "ProviderRoute":
        expected = {"anthropic": "claude", "openai": "codex", "google": "gemini", "xai": "grok"}
        if expected[self.provider] != self.harness:
            raise ValueError(f"{self.provider} is qualified only through {expected[self.provider]}")
        return self


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
    derived_from_block_ids: list[str] = Field(default_factory=list)
    reason: str | None = None

    @field_validator("label")
    @classmethod
    def label_is_single_line_and_unambiguous(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value) or "<<<" in value:
            raise ValueError("context labels cannot contain controls or Loom framing tokens")
        return value


class DraftCreate(StrictModel):
    project_id: str
    role_session_id: str
    prompt: str = Field(min_length=1, max_length=1_000_000)
    route: ProviderRoute
    selections: list[ContextSelection] = Field(default_factory=list)
    parent_invocation_ids: list[str] = Field(default_factory=list, max_length=32)
    relation: EdgeRelation = EdgeRelation.responds_to
    context_mode: Literal["standard", "explicit"] = "standard"
    response_schema: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "additionalProperties": False,
            "required": ["response", "artifacts"],
            "properties": {
                "response": {"type": "string"},
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "media_type", "content"],
                        "properties": {
                            "name": {"type": "string"},
                            "media_type": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
            },
        }
    )

    @field_validator("parent_invocation_ids")
    @classmethod
    def parents_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("parent invocation IDs must be unique")
        return value

    @model_validator(mode="after")
    def explicit_context_is_complete(self) -> "DraftCreate":
        if self.context_mode == "explicit":
            prompts = [
                item
                for item in self.selections
                if item.plane == "current_prompt"
                and item.disposition in {BlockDisposition.selected, BlockDisposition.summarized}
            ]
            if len(prompts) != 1:
                raise ValueError("explicit context requires exactly one selected current-prompt block")
        return self


class DraftPatch(StrictModel):
    expected_revision: int = Field(ge=1)
    prompt: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    route: ProviderRoute | None = None
    selections: list[ContextSelection] | None = None
    parent_invocation_ids: list[str] | None = Field(default=None, max_length=32)


class ExecuteRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=200)


class TagRequest(StrictModel):
    concern_ref: str = Field(min_length=1, max_length=500)
    label: str = Field(min_length=1, max_length=300)


class SliceCreate(StrictModel):
    project_id: str
    scope: Literal["project", "global"] = "project"
    name: str = Field(min_length=1, max_length=300)
    query: dict[str, Any]
    external_ref: str | None = None


class ImportConversationRequest(StrictModel):
    project_id: str
    conversation_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)


class ContextModuleMode(StrEnum):
    manual = "manual"
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
    harness: Literal["claude", "codex", "gemini", "grok"] | None = None
    allowed_models: list[str] = Field(default_factory=list)
    authority: str = Field(min_length=1)
    instructions: str | None = Field(default=None, min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def execution_boundary_is_explicit(self) -> "WorkflowRoleDefinition":
        expected = {"anthropic": "claude", "openai": "codex", "google": "gemini", "xai": "grok"}
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


class WorkflowTransitionDefinition(StrictModel):
    from_state: str = Field(alias="from", min_length=1)
    to_state: str = Field(alias="to", min_length=1)
    trigger: str | None = None
    requires_completed_trigger: str | None = None


class WorkflowPackDefinition(StrictModel):
    pack_id: str = Field(pattern=r"^[a-z][a-z0-9.-]*$")
    version: int = Field(ge=1)
    title: str
    initial_state: str = Field(min_length=1)
    roles: list[WorkflowRoleDefinition]
    context_modules: list[ContextModuleDefinition] = Field(default_factory=list)
    allowed_transitions: list[WorkflowTransitionDefinition] = Field(default_factory=list)
    triggers: list[WorkflowTriggerDefinition] = Field(default_factory=list)
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
        known_triggers = set(trigger_names)
        bound_triggers = [transition.trigger for transition in self.allowed_transitions if transition.trigger]
        if set(bound_triggers) != known_triggers or len(bound_triggers) != len(set(bound_triggers)):
            raise ValueError("every workflow trigger must be bound to exactly one transition")
        for transition in self.allowed_transitions:
            if transition.requires_completed_trigger and transition.requires_completed_trigger not in known_triggers:
                raise ValueError("required workflow trigger is not declared")
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
