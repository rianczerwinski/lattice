"""Independent persistence oracles for first-class conversation boundaries."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import time

import pytest
from lattice import LatticeStore
from lattice.contracts import AttemptState, DraftCreate, DirectActorBinding
from lattice.conversation_contracts import ConversationCreate, ConversationSend, ConversationSteer
from lattice.conversations import ConversationStore
from lattice.store import LatticeConflict


def create(env):
    cs = ConversationStore(env['store'])
    value = ConversationCreate(project_id=env['project']['project_id'], title='Persistent child', owning_task_ref='task:parent', route=env['route'], idempotency_key='create-child')
    return cs, cs.create(value)['conversation_id']


def admit(env, cs, identity, ordinal):
    return cs.send(identity, ConversationSend(prompt=f'request {ordinal}', expected_revision=cs.get(identity)['revision'], idempotency_key=f'send-{ordinal:04}'))


def execute(env, cs, round_id, prompt):
    draft = env['store'].create_draft(DraftCreate(project_id=env['project']['project_id'], actor_binding=DirectActorBinding(), prompt=prompt, route=env['route']))
    inv = env['lattice'].seal(draft['draft_id'])
    cs.bind_execution(round_id, inv['invocation_id'], {'mode':'fresh'})
    attempt = env['store'].create_attempt(inv['invocation_id'], 'execute-'+round_id, 'test-boot', int(time.time())+100)
    env['store'].mark_attempt_running(attempt['attempt_id'])
    return attempt


def success(store, attempt, text):
    return store.finish_successful_attempt(attempt['attempt_id'], response=text, artifacts=[], journal_path='test-oracle', runtime_receipt={'oracle':'hand-authored'})


@pytest.mark.oracle
def test_completed_rounds_survive_failed_eleventh_and_new_operator(lattice_env):
    env=lattice_env; cs, identity=create(env)
    expected={}
    for i in range(1,11):
        r=admit(env,cs,identity,i)
        a=execute(env,cs,r['round_id'],f'request {i}')
        success(env['store'],a,f'response {i}')
        expected[i]=hashlib.sha256(f'response {i}'.encode()).hexdigest()
    r=admit(env,cs,identity,11)
    a=execute(env,cs,r['round_id'],'request 11')
    env['store'].finish_attempt(a['attempt_id'],state=AttemptState.interrupted,failure_kind='operator_interruption')
    replacement=ConversationStore(LatticeStore(env['config']))
    found=replacement.list(env['project']['project_id'],'task:parent')
    assert [v['conversation_id'] for v in found]==[identity]
    history=replacement.get(identity)
    assert len(history['rounds'])==11
    assert history['rounds'][-1]['state']=='needs_attention'
    assert {r['ordinal']:r['response_sha256'] for r in history['rounds'][:-1]}==expected
    with env['store']._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM conversation_checkpoints').fetchone()[0]==10
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==11
    with pytest.raises(LatticeConflict,match='conversation continuation'):
        env['store'].create_attempt(a['invocation_id'],'unsafe-retry','test-boot',int(time.time())+100)


@pytest.mark.concurrency
def test_idempotent_replay_precedes_revision_check_and_conflicting_heads_fail(lattice_env):
    cs,identity=create(lattice_env)
    request=ConversationSend(prompt='one',expected_revision=1,idempotency_key='one-request')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:cs.send(identity,request),range(2)))
    assert results[0]==results[1]
    assert len(cs.get(identity)['rounds'])==1
    with pytest.raises(LatticeConflict,match='different bytes'):
        cs.send(identity,request.model_copy(update={'prompt':'different'}))
    with pytest.raises(LatticeConflict,match='revision changed'):
        cs.send(identity,request.model_copy(update={'idempotency_key':'another-request'}))


@pytest.mark.oracle
def test_success_response_and_checkpoint_rollback_together(lattice_env,monkeypatch):
    env=lattice_env;cs,identity=create(env)
    r=admit(env,cs,identity,1);a=execute(env,cs,r['round_id'],'one')
    import lattice.conversations as implementation
    original=implementation.finish_in_transaction
    def fail_after_checkpoint(*args,**kwargs):
        original(*args,**kwargs)
        raise OSError('planted commit interruption')
    monkeypatch.setattr(implementation,'finish_in_transaction',fail_after_checkpoint)
    with pytest.raises(OSError,match='planted'):
        success(env['store'],a,'answer')
    with env['store']._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM conversation_checkpoints').fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM context_blocks WHERE semantic_type='model-response'").fetchone()[0]==0
        assert conn.execute('SELECT state FROM conversation_rounds').fetchone()[0]=='active'
        assert conn.execute('SELECT state FROM attempts').fetchone()[0]=='running'


@pytest.mark.contract
def test_steering_is_queued_and_survives_failure(lattice_env):
    env=lattice_env;cs,identity=create(env)
    r=admit(env,cs,identity,1);a=execute(env,cs,r['round_id'],'one')
    for content in ('first correction','second correction'):
        cs.steer(identity,ConversationSteer(content=content,expected_revision=cs.get(identity)['revision'],idempotency_key=content))
    env['store'].finish_attempt(a['attempt_id'],state=AttemptState.failed,failure_kind='rate_limit')
    data=ConversationStore(LatticeStore(env['config'])).get(identity)
    assert [s['content'] for s in data['steering']]==['first correction','second correction']
    assert all(s['state']=='queued' for s in data['steering'])
    assert len(data['rounds'])==1


@pytest.mark.contract
def test_success_combines_steering_without_changing_prior_request(lattice_env):
    env=lattice_env;cs,identity=create(env)
    r=admit(env,cs,identity,1);a=execute(env,cs,r['round_id'],'one')
    cs.steer(identity,ConversationSteer(content='follow up',expected_revision=cs.get(identity)['revision'],idempotency_key='steering-one'))
    success(env['store'],a,'answer')
    data=cs.get(identity)
    assert data['rounds'][0]['request']['prompt']=='request 1'
    assert data['rounds'][0]['state']=='completed'
    assert data['rounds'][1]['request']['prompt']=='follow up'
    assert data['rounds'][1]['state']=='queued'
    assert data['rounds'][1]['request']['steering_ids']
    assert data['steering'][0]['state']=='queued'
    assert data['steering'][0]['resulting_round_id']==data['rounds'][1]['round_id']
    execute(env,cs,data['rounds'][1]['round_id'],'follow up')
    assert cs.get(identity)['steering']==[]


@pytest.mark.contract
def test_completed_round_and_checkpoint_reject_rewrite(lattice_env):
    env=lattice_env;cs,identity=create(env)
    r=admit(env,cs,identity,1);a=execute(env,cs,r['round_id'],'one');success(env['store'],a,'answer')
    for statement in ("UPDATE conversation_rounds SET state='queued'",'DELETE FROM conversation_rounds',"UPDATE conversation_checkpoints SET content_json='{}'",'DELETE FROM conversation_checkpoints'):
        with pytest.raises(sqlite3.IntegrityError):
            with env['store'].transaction() as conn:conn.execute(statement)


@pytest.mark.contract
def test_adoption_references_original_history_and_cannot_grant_native_state(lattice_env):
    from conftest import completed_invocation
    from lattice.conversation_contracts import ConversationAdopt
    env=lattice_env
    original=completed_invocation(env,'old request','old response')
    cs,identity=create(env)
    v=ConversationAdopt(expected_revision=1,idempotency_key='adopt-original',invocation_ids=[original['invocation_id']])
    accepted=cs.adopt(identity,v)
    assert cs.adopt(identity,v)==accepted
    r=cs.get(identity)['rounds'][0]
    assert r['accepted_response_id']==original['output_block_id']
    assert r['request']['prompt']=='old request'
    assert r['executions'][0]['recovery']=={'mode':'adopted','native_eligible':False}
    assert env['store'].get_invocation(original['invocation_id'])['manifest_sha256']==original['manifest_sha256']


@pytest.mark.contract
def test_schema15_additive_migration_preserves_existing_rows(lattice_env):
    # Construct the old shape independently: no new table ever held a row.
    from lattice.conversation_schema import TABLES
    env=lattice_env
    with sqlite3.connect(env['config'].database,isolation_level=None) as conn:
        before=conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('DROP TRIGGER schema_migrations_no_delete')
        conn.execute('DELETE FROM schema_migrations WHERE version=16')
        for table in reversed(TABLES):
            conn.execute(f'DROP TABLE {table}')
    migrated=LatticeStore(env['config'])
    with migrated._connect() as conn:
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==16
        assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]==before
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]==0
