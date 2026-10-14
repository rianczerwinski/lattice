"""Conversation persistence over existing invocation, artifact, and event authority.

All mutations use the store's IMMEDIATE transaction and change ledger. A round
outlives individual provider attempts; an immutable checkpoint admits completion.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any
from .canonical import canonical_bytes, opaque_id, sha256_bytes, stamp
from .conversation_contracts import ConversationCreate, ConversationSend, ConversationSteer, ConversationAdopt, ConversationMutation
from .store import LatticeConflict, LatticeStore


def encode(value: Any) -> str:
    return canonical_bytes(value).decode('utf-8')


def replay(conn, scope: str, key: str, request: dict) -> dict | None:
    row = conn.execute('SELECT * FROM conversation_mutations WHERE scope=? AND idempotency_key=?', (scope, key)).fetchone()
    if row is None:
        return None
    if row['request_sha256'] != sha256_bytes(canonical_bytes(request)):
        raise LatticeConflict('conversation idempotency key was reused for different bytes')
    return json.loads(row['receipt_json'])


def receipt(conn, scope: str, key: str, request: dict, result: dict) -> dict:
    result = {**result, 'event_cursor':conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]}
    conn.execute('INSERT INTO conversation_mutations VALUES(?,?,?,?,?)',
                 (scope, key, sha256_bytes(canonical_bytes(request)), encode(result), stamp()))
    return result


def current(store, conn, conversation_id, expected_revision=None):
    value = store._row(conn.execute('SELECT * FROM conversations WHERE conversation_id=?', (conversation_id,)).fetchone(), conversation_id)
    if expected_revision is not None and value['revision'] != expected_revision:
        raise LatticeConflict('conversation revision changed; inspect the current conversation')
    return value


def bump(store, conn, conversation, kind, payload):
    conn.execute('UPDATE conversations SET revision=revision+1 WHERE conversation_id=?', (conversation['conversation_id'],))
    store._event(conn, project_id=conversation['project_id'], event_type='conversation.'+kind,
                 subject_type='conversation', subject_id=conversation['conversation_id'], payload=payload)


def pending(conn, conversation_id):
    return conn.execute("SELECT * FROM conversation_rounds WHERE conversation_id=? AND state IN ('queued','active','needs_attention')", (conversation_id,)).fetchone()


def admitted_corrections(conn, round_id: str, request: dict) -> list[dict]:
    """Return corrections that supplement, rather than constitute, the request.

    Next-round steering already forms that round's immutable prompt. Steering
    admitted during recovery remains additional operator input for that round.
    """
    original = set(request.get('steering_ids', []))
    return [dict(row) for row in conn.execute("SELECT steering_id,ordinal,content FROM conversation_steering WHERE resulting_round_id=? AND state='dispatched' ORDER BY ordinal", (round_id,)) if row['steering_id'] not in original]


def insert_round(conn, conversation, request):
    round_id = opaque_id('round')
    ordinal = conn.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM conversation_rounds WHERE conversation_id=?', (conversation['conversation_id'],)).fetchone()[0]
    conn.execute("INSERT INTO conversation_rounds VALUES(?,?,?,?,?,?,'queued',NULL,NULL,?)",
                 (round_id, conversation['conversation_id'], ordinal, encode(request), sha256_bytes(canonical_bytes(request)), conversation['revision'], stamp()))
    return round_id


class ConversationStore:
    def __init__(self, store: LatticeStore):
        self.store = store

    def create(self, value: ConversationCreate) -> dict:
        request = value.model_dump(mode='json')
        with self.store.transaction() as conn:
            old = replay(conn, 'conversation-create', value.idempotency_key, request)
            if old is not None:
                return old
            self.store._row(conn.execute('SELECT project_id FROM projects WHERE project_id=?', (value.project_id,)).fetchone(), value.project_id)
            if value.parent_conversation_id:
                parent = current(self.store, conn, value.parent_conversation_id)
                if parent['project_id'] != value.project_id:
                    raise LatticeConflict('parent conversation belongs to another project')
            if (value.anchor_type is None) != (value.anchor_id is None):
                raise LatticeConflict('conversation anchor requires both type and identity')
            anchor = None
            if value.anchor_type:
                anchor = {'type': value.anchor_type, 'id': value.anchor_id,
                          'revision': self.store._object_revision(conn, value.project_id, value.anchor_type, value.anchor_id)}
            if value.parent_conversation_id and anchor is None:
                anchor = {'type':'conversation','id':value.parent_conversation_id,'revision':parent['revision']}
            identity = opaque_id('conversation')
            conn.execute('INSERT INTO conversations VALUES(?,?,?,?,?,?,1,?,?,?)',
                         (identity, value.project_id, value.title, value.owning_task_ref, value.parent_conversation_id,
                          encode(anchor) if anchor else None, encode(value.route.model_dump(mode='json')), value.recovery_policy, stamp()))
            self.store._event(conn, project_id=value.project_id, event_type='conversation.created', subject_type='conversation', subject_id=identity, payload=request)
            return receipt(conn, 'conversation-create', value.idempotency_key, request, {'conversation_id': identity, 'revision': 1})

    def get(self, conversation_id: str, *, after: int = 0, limit: int = 100) -> dict:
        if after < 0 or not 1 <= limit <= 500:
            raise LatticeConflict('invalid conversation pagination')
        with self.store._connect() as conn:
            value = current(self.store, conn, conversation_id)
            value['route'] = json.loads(value.pop('route_json'))
            value['anchor'] = json.loads(value.pop('anchor_json') or 'null')
            rows = [dict(r) for r in conn.execute('SELECT * FROM conversation_rounds WHERE conversation_id=? AND ordinal>? ORDER BY ordinal LIMIT ?', (conversation_id, after, limit+1))]
            value['has_more'] = len(rows)>limit
            value['rounds'] = rows[:limit]
            for r in value['rounds']:
                r['request'] = json.loads(r.pop('request_json'))
                r['operator_steering'] = admitted_corrections(conn, r['round_id'], r['request'])
                r['executions'] = [dict(e) for e in conn.execute('SELECT * FROM conversation_executions WHERE round_id=? ORDER BY ordinal', (r['round_id'],))]
                for e in r['executions']:
                    e['recovery'] = json.loads(e.pop('recovery_json'))
                    e['attempts'] = [dict(a) for a in conn.execute('SELECT attempt_id,state,failure_kind,error_text FROM attempts WHERE invocation_id=? ORDER BY attempt_number',(e['invocation_id'],))]
                if r['accepted_response_id']:
                    block = conn.execute('SELECT content_sha256 FROM context_blocks WHERE block_id=?', (r['accepted_response_id'],)).fetchone()
                    r['response_sha256'] = block[0]
                    r['response'] = self.store.blobs.get(block[0]).decode('utf-8')
            live = pending(conn, conversation_id)
            value['pending_round_id'] = live['round_id'] if live else None
            value['pending'] = {key:live[key] for key in ('round_id','ordinal','state')} if live else None
            if live:
                value['pending']['attempt_ids'] = [r[0] for r in conn.execute('SELECT a.attempt_id FROM attempts a JOIN conversation_executions e USING(invocation_id) WHERE e.round_id=?', (live['round_id'],))]
            value['steering'] = [dict(r) for r in conn.execute("SELECT * FROM conversation_steering WHERE conversation_id=? AND state='queued' ORDER BY ordinal", (conversation_id,))]
            last = conn.execute('SELECT c.* FROM conversation_checkpoints c JOIN conversation_rounds r USING(round_id) WHERE r.conversation_id=? ORDER BY r.ordinal DESC LIMIT 1', (conversation_id,)).fetchone()
            value['checkpoint'] = dict(last) if last else None
            if value['checkpoint']:
                value['checkpoint']['content'] = json.loads(value['checkpoint'].pop('content_json'))
            value['event_cursor'] = conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            return value

    def observations(self, conversation_id: str, *, after: int = 0, limit: int = 100) -> dict:
        if after < 0 or not 1 <= limit <= 500:
            raise LatticeConflict('invalid observation pagination')
        with self.store._connect() as conn:
            current(self.store,conn,conversation_id)
            events = [dict(row) for row in conn.execute("""SELECT e.seq,e.event_type,e.subject_id,e.payload_json FROM events e
                WHERE e.seq>? AND (e.subject_id=? OR e.subject_id IN (
                    SELECT a.attempt_id FROM attempts a JOIN conversation_executions x USING(invocation_id)
                    JOIN conversation_rounds r USING(round_id) WHERE r.conversation_id=?))
                ORDER BY e.seq LIMIT ?""", (after,conversation_id,conversation_id,limit+1))]
        for event in events:
            event['payload']=json.loads(event.pop('payload_json'))
        return {'items':events[:limit],'has_more':len(events)>limit,'cursor':events[min(len(events),limit)-1]['seq'] if events else after}

    def list(self, project_id: str, owning_task_ref: str | None = None, *, after: str = '', limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 500:
            raise LatticeConflict('invalid conversation page limit')
        with self.store._connect() as conn:
            return [dict(r) for r in conn.execute('SELECT conversation_id,title,owning_task_ref,parent_conversation_id,revision FROM conversations WHERE project_id=? AND (? IS NULL OR owning_task_ref=?) AND conversation_id>? ORDER BY conversation_id LIMIT ?', (project_id, owning_task_ref, owning_task_ref, after, limit))]

    def send(self, conversation_id: str, value: ConversationSend) -> dict:
        request = value.model_dump(mode='json')
        with self.store.transaction() as conn:
            old = replay(conn, conversation_id, value.idempotency_key, {'action':'send', **request})
            if old is not None:
                return old
            conversation = current(self.store, conn, conversation_id, value.expected_revision)
            if pending(conn, conversation_id):
                raise LatticeConflict('conversation already has an unfinished round; steer or continue it')
            if len(set(value.context_block_ids)) != len(value.context_block_ids):
                raise LatticeConflict('context block identities must be unique')
            for block in value.context_block_ids:
                self.store._object_revision(conn, conversation['project_id'], 'context-block', block)
            round_id = insert_round(conn, conversation, request)
            bump(self.store, conn, conversation, 'request-admitted', {'round_id':round_id})
            return receipt(conn, conversation_id, value.idempotency_key, {'action':'send', **request}, {'conversation_id':conversation_id,'round_id':round_id,'revision':conversation['revision']+1})

    def steer(self, conversation_id: str, value: ConversationSteer) -> dict:
        request = {'action':'steer', **value.model_dump(mode='json')}
        with self.store.transaction() as conn:
            old = replay(conn, conversation_id, value.idempotency_key, request)
            if old is not None:
                return old
            conversation = current(self.store, conn, conversation_id, value.expected_revision)
            target = pending(conn, conversation_id)
            if target is None:
                raise LatticeConflict('no unfinished round to steer; send a new request')
            identity = opaque_id('steering')
            ordinal = conn.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM conversation_steering WHERE conversation_id=?', (conversation_id,)).fetchone()[0]
            conn.execute("INSERT INTO conversation_steering VALUES(?,?,?,?,?,'queued',NULL,?)", (identity, conversation_id, target['round_id'], ordinal, value.content, stamp()))
            attempt_id = interrupt_target(self.store, conn, target) if value.interrupt else None
            bump(self.store, conn, conversation, 'steering-queued', {'steering_id':identity,'round_id':target['round_id'],'interrupt':value.interrupt})
            return receipt(conn, conversation_id, value.idempotency_key, request, {'conversation_id':conversation_id,'round_id':target['round_id'],'steering_id':identity,'revision':conversation['revision']+1,'state':'queued','attempt_id':attempt_id})

    def bind_execution(self, round_id: str, invocation_id: str, recovery: dict) -> dict:
        with self.store.transaction() as conn:
            return bind_in_transaction(self.store, conn, round_id=round_id, invocation_id=invocation_id, recovery=recovery)

    def interrupt(self, conversation_id: str, value: ConversationMutation) -> dict:
        request = {'action':'interrupt', **value.model_dump(mode='json')}
        with self.store.transaction() as conn:
            old = replay(conn,conversation_id,value.idempotency_key,request)
            if old is not None:
                return old
            conversation=current(self.store,conn,conversation_id,value.expected_revision)
            r=pending(conn,conversation_id)
            if r is None:
                raise LatticeConflict('no unfinished request to interrupt')
            target=interrupt_target(self.store,conn,r)
            bump(self.store,conn,conversation,'interrupt-requested',{'round_id':r['round_id'],'attempt_id':target})
            return receipt(conn,conversation_id,value.idempotency_key,request,{'conversation_id':conversation_id,'round_id':r['round_id'],'attempt_id':target,'revision':conversation['revision']+1})

    def withdraw(self, conversation_id: str, value: ConversationMutation) -> dict:
        request = {'action': 'withdraw', **value.model_dump(mode='json')}
        with self.store.transaction() as conn:
            old = replay(conn, conversation_id, value.idempotency_key, request)
            if old is not None:
                return old
            conversation = current(self.store, conn, conversation_id, value.expected_revision)
            r = pending(conn, conversation_id)
            if r is None or r['state'] == 'active':
                raise LatticeConflict('interrupt active work before withdrawing its request')
            conn.execute("UPDATE conversation_rounds SET state='withdrawn' WHERE round_id=?", (r['round_id'],))
            conn.execute("UPDATE conversation_steering SET state='superseded' WHERE conversation_id=? AND state='queued'", (conversation_id,))
            bump(self.store, conn, conversation, 'request-withdrawn', {'round_id': r['round_id']})
            return receipt(conn, conversation_id, value.idempotency_key, request, {'conversation_id': conversation_id, 'round_id': r['round_id'], 'revision': conversation['revision']+1})

    def adopt(self, conversation_id: str, value: ConversationAdopt) -> dict:
        request = {'action': 'adopt', **value.model_dump(mode='json')}
        with self.store.transaction() as conn:
            old = replay(conn, conversation_id, value.idempotency_key, request)
            if old is not None:
                return old
            conversation = current(self.store, conn, conversation_id, value.expected_revision)
            if pending(conn, conversation_id):
                raise LatticeConflict('finish or withdraw the pending request before adopting history')
            if len(set(value.invocation_ids)) != len(value.invocation_ids):
                raise LatticeConflict('adopted invocation identities must be unique')
            round_ids = []
            for invocation_id in value.invocation_ids:
                inv = self.store._row(conn.execute('SELECT i.*,b.content_sha256 AS prompt_sha256 FROM invocations i JOIN drafts d USING(draft_id) JOIN context_blocks b ON b.block_id=d.prompt_block_id WHERE i.invocation_id=?', (invocation_id,)).fetchone(), invocation_id)
                if inv['project_id'] != conversation['project_id'] or inv['state'] not in ('succeeded', 'imported') or not inv['output_block_id']:
                    raise LatticeConflict('adoption requires a completed project-local invocation with an output')
                if conn.execute('SELECT 1 FROM conversation_executions WHERE invocation_id=?', (invocation_id,)).fetchone():
                    raise LatticeConflict('invocation already belongs to a conversation; reference that conversation instead')
                material = {'prompt': self.store.blobs.get(inv['prompt_sha256']).decode('utf-8'), 'adopted_invocation_id': invocation_id, 'original_manifest_sha256': inv['manifest_sha256'], 'original_route': json.loads(inv['route_json']), 'context_block_ids': []}
                round_id = insert_round(conn, conversation, material)
                conn.execute("UPDATE conversation_rounds SET state='active' WHERE round_id=?", (round_id,))
                conn.execute('INSERT INTO conversation_executions VALUES(?,?,1,?,?,?)', (opaque_id('conversation_execution'), round_id, invocation_id, encode({'mode':'adopted','native_eligible':False}), stamp()))
                finish_in_transaction(self.store, conn, invocation_id, response_id=inv['output_block_id'])
                round_ids.append(round_id)
                conversation = current(self.store, conn, conversation_id)
            return receipt(conn, conversation_id, value.idempotency_key, request, {'conversation_id':conversation_id,'round_ids':round_ids,'revision':conversation['revision']})


def interrupt_target(store,conn,r):
    if r['state']=='needs_attention':
        return None
    execution=conn.execute('SELECT invocation_id FROM conversation_executions WHERE round_id=? ORDER BY ordinal DESC LIMIT 1',(r['round_id'],)).fetchone()
    attempt=conn.execute("SELECT attempt_id FROM attempts WHERE invocation_id=? AND state IN ('leased','running') ORDER BY rowid DESC LIMIT 1",(execution['invocation_id'],)).fetchone() if execution else None
    if attempt:
        return attempt['attempt_id']
    # Admission can be interrupted before any provider process exists. The state
    # change prevents startup dispatch from spending the request after the stop.
    conn.execute("UPDATE conversation_rounds SET state='needs_attention' WHERE round_id=?",(r['round_id'],))
    if execution:
        conn.execute("UPDATE invocations SET state='interrupted',terminal_kind='operator_interruption',completed_at=? WHERE invocation_id=? AND state='sealed'",(stamp(),execution['invocation_id']))
    return None


def bind_in_transaction(store, conn, *, round_id: str, invocation_id: str, recovery: dict, expected_revision: int | None = None, mutation: dict | None = None):
    r = store._row(conn.execute('SELECT * FROM conversation_rounds WHERE round_id=?', (round_id,)).fetchone(), round_id)
    conversation = current(store, conn, r['conversation_id'])
    previous = conn.execute('SELECT * FROM conversation_executions WHERE invocation_id=?', (invocation_id,)).fetchone()
    if previous:
        if previous['round_id'] != round_id or previous['recovery_json'] != encode(recovery):
            raise LatticeConflict('invocation already bound to a different round or recovery')
        return dict(previous)
    if expected_revision is not None and conversation['revision'] != expected_revision:
        raise LatticeConflict('conversation revision changed before execution admission')
    allowed = ('queued', 'needs_attention') if recovery.get('continuing') else ('queued',)
    if r['state'] not in allowed:
        raise LatticeConflict('round must be admitted before binding an execution')
    inv = store._row(conn.execute('SELECT project_id,state FROM invocations WHERE invocation_id=?', (invocation_id,)).fetchone(), invocation_id)
    if inv['project_id'] != conversation['project_id'] or inv['state'] != 'sealed':
        raise LatticeConflict('conversation execution requires a sealed project-local invocation')
    identity = opaque_id('conversation_execution')
    ordinal = conn.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM conversation_executions WHERE round_id=?', (round_id,)).fetchone()[0]
    conn.execute('INSERT INTO conversation_executions VALUES(?,?,?,?,?,?)', (identity, round_id, ordinal, invocation_id, encode(recovery), stamp()))
    conn.execute("UPDATE conversation_rounds SET state='active' WHERE round_id=?", (round_id,))
    if recovery.get('route_changed'):
        conn.execute('UPDATE conversations SET route_json=? WHERE conversation_id=?', (encode(recovery['route']), r['conversation_id']))
    conn.execute("UPDATE conversation_steering SET state='dispatched' WHERE resulting_round_id=? AND state='queued'", (round_id,))
    for instruction in recovery.get('steering', []):
        changed = conn.execute("UPDATE conversation_steering SET state='dispatched',resulting_round_id=? WHERE steering_id=? AND conversation_id=? AND state='queued' AND resulting_round_id IS NULL", (round_id,instruction['steering_id'],r['conversation_id'])).rowcount
        if changed != 1:
            raise LatticeConflict('selected steering changed before execution admission')
    bump(store, conn, conversation, 'execution-bound', {'round_id':round_id,'invocation_id':invocation_id})
    result = {'execution_id':identity,'conversation_id':r['conversation_id'],'round_id':round_id,'invocation_id':invocation_id,'revision':conversation['revision']+1,'event_cursor':conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]}
    if mutation is not None:
        receipt(conn, r['conversation_id'], mutation['idempotency_key'], mutation, result)
    return result



def finish_in_transaction(store, conn, invocation_id: str, *, response_id: str | None):
    """Called inside invocation terminalization, never as a later best-effort save."""
    r = conn.execute('SELECT r.* FROM conversation_rounds r JOIN conversation_executions e USING(round_id) WHERE e.invocation_id=?', (invocation_id,)).fetchone()
    if r is None:
        return
    if r['state'] != 'active':
        raise LatticeConflict('conversation terminalization requires its active round')
    conversation = current(store, conn, r['conversation_id'])
    if response_id is None:
        conn.execute("UPDATE conversation_rounds SET state='needs_attention' WHERE round_id=?", (r['round_id'],))
        bump(store, conn, conversation, 'needs-attention', {'round_id':r['round_id'],'invocation_id':invocation_id})
        return
    inv = conn.execute('SELECT manifest_sha256,route_json FROM invocations WHERE invocation_id=?', (invocation_id,)).fetchone()
    response = conn.execute('SELECT content_sha256 FROM context_blocks WHERE block_id=?', (response_id,)).fetchone()
    previous = conn.execute('SELECT c.checkpoint_id,c.content_sha256 FROM conversation_checkpoints c JOIN conversation_rounds r USING(round_id) WHERE r.conversation_id=? ORDER BY r.ordinal DESC LIMIT 1', (r['conversation_id'],)).fetchone()
    artifacts = [dict(a) for a in conn.execute("SELECT artifact_id,content_sha256 FROM artifacts WHERE json_extract(generated_json,'$.producing_invocation_id')=? ORDER BY artifact_id", (invocation_id,))]
    attempt = conn.execute('SELECT attempt_id,runtime_receipt_json FROM attempts WHERE invocation_id=? ORDER BY rowid DESC LIMIT 1', (invocation_id,)).fetchone()
    content = {'conversation_id':r['conversation_id'],'round_id':r['round_id'],'ordinal':r['ordinal'],'request_sha256':r['request_sha256'],'invocation_id':invocation_id,'manifest_sha256':inv['manifest_sha256'],'route':json.loads(inv['route_json']),'response_id':response_id,'response_sha256':response[0],'artifacts':artifacts,'attempt_id':attempt['attempt_id'] if attempt else None,'provider_receipt':json.loads(attempt['runtime_receipt_json'] or 'null') if attempt else None,'previous':dict(previous) if previous else None,'event_cursor':conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]}
    identity = opaque_id('checkpoint')
    conn.execute('INSERT INTO conversation_checkpoints VALUES(?,?,?,?,?)', (identity,r['round_id'],encode(content),sha256_bytes(canonical_bytes(content)),stamp()))
    conn.execute("UPDATE conversation_rounds SET state='completed',accepted_response_id=?,checkpoint_id=? WHERE round_id=?", (response_id,identity,r['round_id']))
    queued = [dict(s) for s in conn.execute("SELECT * FROM conversation_steering WHERE conversation_id=? AND state='queued' AND resulting_round_id IS NULL ORDER BY ordinal", (r['conversation_id'],))]
    if queued:
        prior_request = json.loads(r['request_json'])
        request = {**{k:v for k,v in prior_request.items() if k not in ('expected_revision','idempotency_key','steering_ids')},'prompt':'\n\n'.join(s['content'] for s in queued),'steering_ids':[s['steering_id'] for s in queued]}
        next_id = insert_round(conn, conversation, request)
        for s in queued:
            conn.execute("UPDATE conversation_steering SET resulting_round_id=? WHERE steering_id=?", (next_id,s['steering_id']))
    bump(store, conn, conversation, 'round-completed', {'round_id':r['round_id'],'checkpoint_id':identity,'response_id':response_id})
