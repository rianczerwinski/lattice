"""Additive schema for operator-owned conversation continuity (schema 16)."""

TABLES = (
    'conversations', 'conversation_rounds', 'conversation_executions',
    'conversation_checkpoints', 'conversation_steering', 'conversation_mutations',
)

DDL = """
CREATE TABLE IF NOT EXISTS conversations(
    conversation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    title TEXT NOT NULL,
    owning_task_ref TEXT NOT NULL,
    parent_conversation_id TEXT REFERENCES conversations(conversation_id),
    anchor_json TEXT CHECK(anchor_json IS NULL OR json_valid(anchor_json)),
    revision INTEGER NOT NULL CHECK(revision>=1),
    route_json TEXT NOT NULL CHECK(json_valid(route_json)),
    recovery_policy TEXT NOT NULL CHECK(recovery_policy IN ('auto','native','portable')),
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(project_id,owning_task_ref,conversation_id);
CREATE TABLE IF NOT EXISTS conversation_rounds(
    round_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=1),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
    admission_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','active','needs_attention','completed','withdrawn')),
    accepted_response_id TEXT REFERENCES context_blocks(block_id),
    checkpoint_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id,ordinal)
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS conversation_one_pending ON conversation_rounds(conversation_id)
    WHERE state IN ('queued','active','needs_attention');
CREATE TABLE IF NOT EXISTS conversation_executions(
    execution_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES conversation_rounds(round_id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=1),
    invocation_id TEXT UNIQUE REFERENCES invocations(invocation_id),
    recovery_json TEXT NOT NULL CHECK(json_valid(recovery_json)),
    created_at TEXT NOT NULL,
    UNIQUE(round_id,ordinal)
) STRICT;
CREATE TABLE IF NOT EXISTS conversation_checkpoints(
    checkpoint_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL UNIQUE REFERENCES conversation_rounds(round_id),
    content_json TEXT NOT NULL CHECK(json_valid(content_json)),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    created_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS conversation_steering(
    steering_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    target_round_id TEXT NOT NULL REFERENCES conversation_rounds(round_id),
    ordinal INTEGER NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','dispatched','superseded')),
    resulting_round_id TEXT REFERENCES conversation_rounds(round_id),
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id,ordinal)
) STRICT;
CREATE TABLE IF NOT EXISTS conversation_mutations(
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope,idempotency_key)
) STRICT;
CREATE TRIGGER IF NOT EXISTS conversation_steering_immutable BEFORE UPDATE ON conversation_steering
WHEN NEW.steering_id<>OLD.steering_id OR NEW.conversation_id<>OLD.conversation_id
 OR NEW.target_round_id<>OLD.target_round_id OR NEW.ordinal<>OLD.ordinal OR NEW.content<>OLD.content
 OR NEW.created_at<>OLD.created_at OR OLD.state IN ('dispatched','superseded')
BEGIN SELECT RAISE(ABORT,'steering instructions and terminal dispositions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS conversation_mutation_immutable BEFORE UPDATE ON conversation_mutations
BEGIN SELECT RAISE(ABORT,'conversation mutation receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS conversation_mutation_retained BEFORE DELETE ON conversation_mutations
BEGIN SELECT RAISE(ABORT,'conversation mutation receipts are retained'); END;
CREATE TRIGGER IF NOT EXISTS conversation_execution_immutable BEFORE UPDATE ON conversation_executions
BEGIN SELECT RAISE(ABORT,'conversation executions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS conversation_execution_retained BEFORE DELETE ON conversation_executions
BEGIN SELECT RAISE(ABORT,'conversation executions are retained'); END;
CREATE TRIGGER IF NOT EXISTS conversation_completion_bound BEFORE UPDATE ON conversation_rounds
WHEN NEW.state='completed' AND (NEW.accepted_response_id IS NULL OR NEW.checkpoint_id IS NULL
 OR NOT EXISTS (SELECT 1 FROM conversation_checkpoints c WHERE c.checkpoint_id=NEW.checkpoint_id AND c.round_id=NEW.round_id
   AND json_extract(c.content_json,'$.response_id')=NEW.accepted_response_id
   AND json_extract(c.content_json,'$.request_sha256')=NEW.request_sha256))
BEGIN SELECT RAISE(ABORT,'conversation completion requires its matching checkpoint and response'); END;
CREATE TRIGGER IF NOT EXISTS conversation_request_immutable BEFORE UPDATE ON conversation_rounds
WHEN NEW.round_id<>OLD.round_id OR NEW.conversation_id<>OLD.conversation_id OR NEW.ordinal<>OLD.ordinal
 OR NEW.request_json<>OLD.request_json OR NEW.request_sha256<>OLD.request_sha256 OR NEW.admission_revision<>OLD.admission_revision
 OR OLD.state IN ('completed','withdrawn')
BEGIN SELECT RAISE(ABORT,'conversation requests and terminal rounds are immutable'); END;
CREATE TRIGGER IF NOT EXISTS conversation_checkpoint_immutable BEFORE UPDATE ON conversation_checkpoints
BEGIN SELECT RAISE(ABORT,'conversation checkpoints are immutable'); END;
CREATE TRIGGER IF NOT EXISTS conversation_checkpoint_retained BEFORE DELETE ON conversation_checkpoints
BEGIN SELECT RAISE(ABORT,'conversation checkpoints are retained'); END;
CREATE TRIGGER IF NOT EXISTS conversation_round_retained BEFORE DELETE ON conversation_rounds
BEGIN SELECT RAISE(ABORT,'conversation rounds are retained'); END;
"""
