"""Versioned requests for durable human- or model-operated conversations."""
from typing import Literal
from pydantic import Field
from .contracts import StrictModel, ProviderRoute, OutputContractV2


class ConversationCreate(StrictModel):
    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    owning_task_ref: str = Field(min_length=1, max_length=1000)
    parent_conversation_id: str | None = None
    anchor_type: Literal['invocation', 'context-block', 'artifact'] | None = None
    anchor_id: str | None = None
    route: ProviderRoute
    recovery_policy: Literal['auto', 'native', 'portable'] = 'auto'
    idempotency_key: str = Field(min_length=8, max_length=300)


class ConversationMutation(StrictModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=300)


class ConversationSend(ConversationMutation):
    prompt: str = Field(min_length=1, max_length=1_000_000)
    context_block_ids: list[str] = Field(default_factory=list, max_length=1000)
    output_contract: OutputContractV2 = Field(default_factory=OutputContractV2)


class ConversationSteer(ConversationMutation):
    content: str = Field(min_length=1, max_length=1_000_000)
    interrupt: bool = False


class EffectDisposition(StrictModel):
    attempt_id: str
    tool_id: str | None
    disposition: Literal['confirmed_applied', 'confirmed_not_applied', 'do_not_repeat']
    evidence: str = Field(min_length=1, max_length=8000)


class RecoveryContextSelection(StrictModel):
    """Explicit exclusions preserve retained records and newly completed work."""
    omitted_round_ids: list[str] = Field(default_factory=list, max_length=10000)
    omitted_observation_ids: list[str] = Field(default_factory=list, max_length=10000)
    omitted_artifact_ids: list[str] = Field(default_factory=list, max_length=10000)
    context_block_ids: list[str] | None = Field(default=None, max_length=1000)
    reason: str = Field(min_length=1, max_length=8000)


class ConversationContinue(ConversationMutation):
    expected_preview_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    policy: Literal['auto', 'native', 'portable'] | None = None
    include_steering: bool = True
    effect_dispositions: list[EffectDisposition] = Field(default_factory=list, max_length=1000)
    context_selection: RecoveryContextSelection | None = None
    route: ProviderRoute | None = None
    confirmed_reason_sha256s: list[str] = Field(default_factory=list, max_length=1000)


class ConversationAdopt(ConversationMutation):
    invocation_ids: list[str] = Field(min_length=1, max_length=1000)
