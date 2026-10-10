"""Recovery fixtures whose expected histories do not use the compositor."""
import hashlib
import json
import time
import pytest
from lattice.conversation_service import ConversationService
from lattice.conversation_contracts import ConversationContinue, ConversationSteer
from lattice.contracts import AttemptState
from lattice.observations import ObservationStore
from lattice.store import LatticeConflict
from test_recoverable_conversations import create, admit, success


def attempt(env, receipt):
    a = env['store'].create_attempt(receipt['invocation_id'],'dispatch-'+receipt['invocation_id'],'oracle',int(time.time())+100)
    env['store'].mark_attempt_running(a['attempt_id'])
    return a


@pytest.mark.oracle
def test_preview_is_read_only_and_sealing_binds_history_atomically(lattice_env):
    env=lattice_env; cs,identity=create(env); service=ConversationService(env['store'])
    admit(env,cs,identity,1)
    before=env['store'].state_digest()
    preview=service.preview(identity,include_steering=False)
    assert preview['blockers']==[]
    assert env['store'].state_digest()==before
    sealed=service.seal(identity)
    a=attempt(env,sealed);success(env['store'],a,'exact first response')
    admit(env,cs,identity,2)
    preview=service.preview(identity,include_steering=False)
    assert preview['history']['completed_rounds'][0]['response']=='exact first response'
    second=service.seal(identity)
    compiled=env['lattice'].inspect(second['invocation_id'])['compiled_prompt']
    assert 'exact first response' in compiled
    assert 'request 1' in compiled and 'request 2' in compiled
    with env['store']._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM conversation_executions').fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM invocation_batch_members').fetchone()[0]==2


@pytest.mark.regression
def test_recovery_keeps_full_exposed_stream_and_rejects_stale_preview(lattice_env):
    env=lattice_env;cs,identity=create(env);service=ConversationService(env['store'])
    admit(env,cs,identity,1); sealed=service.seal(identity); a=attempt(env,sealed)
    messages=[{'type':'assistant','message':{'content':f'observation {i}'}} for i in range(501)]
    raw=b''.join(json.dumps(m).encode()+b'\n' for m in messages)
    ObservationStore(env['store']).append(a['attempt_id'],'stdout',raw)
    env['store'].finish_attempt(a['attempt_id'],state=AttemptState.failed,failure_kind='rate_limit')
    preview=service.preview(identity)
    assert preview['history']['unfinished_observations'][0]['content'].encode()==raw
    value=ConversationContinue(expected_revision=preview['revision'],idempotency_key='continue-first',expected_preview_sha256=preview['preview_sha256'])
    cs.steer(identity,ConversationSteer(content='correction',expected_revision=preview['revision'],idempotency_key='correction-one'))
    with pytest.raises(LatticeConflict,match='preview changed'):
        service.seal(identity,continuation=value)
    preview=service.preview(identity)
    value=value.model_copy(update={'expected_revision':preview['revision'],'expected_preview_sha256':preview['preview_sha256']})
    recovered=service.seal(identity,continuation=value)
    compiled=env['lattice'].inspect(recovered['invocation_id'])['compiled_prompt']
    assert 'observation 0' in compiled and 'observation 500' in compiled and 'correction' in compiled
    assert len(cs.get(identity)['rounds'])==1
    assert len(cs.get(identity)['rounds'][0]['executions'])==2
    assert service.seal(identity,continuation=value)==recovered
    assert cs.get(identity)['steering']==[]


@pytest.mark.regression
def test_unknown_mutating_effect_blocks_both_modes(lattice_env):
    env=lattice_env;cs,identity=create(env);service=ConversationService(env['store'])
    admit(env,cs,identity,1);a=attempt(env,service.seal(identity))
    raw=json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'effect','name':'Bash','input':{'command':'external mutation'}}]}}).encode()+b'\n'
    ObservationStore(env['store']).append(a['attempt_id'],'stdout',raw)
    env['store'].finish_attempt(a['attempt_id'],state=AttemptState.interrupted,failure_kind='operator_interruption')
    for policy in ('auto','portable','native'):
        preview=service.preview(identity,policy=policy)
        assert any(b['code']=='uncertain_mutating_effect' for b in preview['blockers'])
        with pytest.raises(LatticeConflict,match='uncertain_mutating_effect'):
            service.seal(identity,continuation=ConversationContinue(expected_revision=preview['revision'],idempotency_key='recover-'+policy,expected_preview_sha256=preview['preview_sha256'],policy=policy))


@pytest.mark.concurrency
def test_seal_rolls_back_batch_if_round_changed(lattice_env,monkeypatch):
    env=lattice_env;cs,identity=create(env);service=ConversationService(env['store'])
    admit(env,cs,identity,1)
    import lattice.conversations as persistence
    original=persistence.bind_in_transaction
    def refused(*args,**kwargs):
        original(*args,**kwargs)
        raise LatticeConflict('planted crash at atomic binding')
    monkeypatch.setattr(persistence,'bind_in_transaction',refused)
    with pytest.raises(LatticeConflict,match='planted'):
        service.seal(identity)
    with env['store']._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM invocations').fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM invocation_batches').fetchone()[0]==0
        assert conn.execute('SELECT COUNT(*) FROM conversation_executions').fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM context_blocks WHERE semantic_type='conversation-history'").fetchone()[0]==0


@pytest.mark.oracle
def test_twenty_rounds_replace_operator_without_redispatching_completed_work(lattice_env,tmp_path):
    from lattice import LatticeStore
    from lattice.conversations import ConversationStore
    env=lattice_env;cs,identity=create(env);service=ConversationService(env['store'])
    log=tmp_path/'independent-provider-actions.jsonl'
    first_hashes={i:hashlib.sha256(f'response {i}'.encode()).hexdigest() for i in range(1,11)}
    for i in range(1,21):
        admit(env,cs,identity,i)
        sealed=service.seal(identity);a=attempt(env,sealed)
        with log.open('a') as handle:
            handle.write(json.dumps({'request':i,'invocation':sealed['invocation_id']})+'\n')
        if i==11:
            env['store'].finish_attempt(a['attempt_id'],state=AttemptState.interrupted,failure_kind='operator_interruption')
            replacement=LatticeStore(env['config'])
            service=ConversationService(replacement)
            cs=ConversationStore(replacement)
            assert cs.list(env['project']['project_id'],'task:parent')[0]['conversation_id']==identity
            preview=service.preview(identity)
            sealed=service.seal(identity,continuation=ConversationContinue(expected_revision=preview['revision'],idempotency_key='replacement-recovery',expected_preview_sha256=preview['preview_sha256']))
            a=attempt(env,sealed)
            with log.open('a') as handle:
                handle.write(json.dumps({'request':i,'invocation':sealed['invocation_id']})+'\n')
        success(env['store'],a,f'response {i}')
    actions=[json.loads(line) for line in log.read_text().splitlines()]
    assert [item['request'] for item in actions]==list(range(1,12))+list(range(11,21))
    assert len({item['invocation'] for item in actions})==21
    with env['store']._connect() as conn:
        hashes={r[0]:r[1] for r in conn.execute('SELECT r.ordinal,b.content_sha256 FROM conversation_rounds r JOIN context_blocks b ON b.block_id=r.accepted_response_id WHERE r.ordinal<=10')}
        assert hashes==first_hashes
        assert conn.execute("SELECT COUNT(*) FROM conversation_rounds WHERE state='completed'").fetchone()[0]==20
        assert conn.execute('SELECT COUNT(*) FROM conversation_checkpoints').fetchone()[0]==20


@pytest.mark.oracle
def test_restored_history_continues_without_original_provider_home(lattice_env,tmp_path,monkeypatch):
    from lattice import LatticeConfig, LatticeStore
    from lattice.diagnostics import assess_data_root
    from lattice.snapshot import create_snapshot, restore_snapshot
    env=lattice_env;cs,identity=create(env);service=ConversationService(env['store'])
    admit(env,cs,identity,1); a=attempt(env,service.seal(identity));success(env['store'],a,'durable answer')
    admit(env,cs,identity,2); a=attempt(env,service.seal(identity))
    raw=b'{"type":"assistant","message":{"content":"partial work"}}\n'
    ObservationStore(env['store']).append(a['attempt_id'],'stdout',raw)
    env['store'].finish_attempt(a['attempt_id'],state=AttemptState.interrupted,failure_kind='operator_interruption')
    source=assess_data_root(env['config'].data_root)
    snapshot=create_snapshot(env['config'].data_root,backup_root=tmp_path/'snapshots',reason='conversation-oracle',source_revision=source['state_digest'],schema_version=source['schema_version'],state_digest=source['state_digest'])
    restored=tmp_path/'restored'
    restore_snapshot(snapshot,restored)
    assert assess_data_root(restored)['state_digest']==source['state_digest']
    empty_home=tmp_path/'empty-provider-home';empty_home.mkdir()
    monkeypatch.setenv('HOME',str(empty_home))
    replacement=ConversationService(LatticeStore(LatticeConfig(data_root=restored,axisvi_root=None)))
    preview=replacement.preview(identity)
    assert preview['blockers']==[]
    assert preview['native']['eligible'] is False
    assert preview['history']['completed_rounds'][0]['response']=='durable answer'
    assert preview['history']['unfinished_observations'][0]['content'].encode()==raw
    result=replacement.seal(identity,continuation=ConversationContinue(expected_revision=preview['revision'],idempotency_key='restored-recovery',expected_preview_sha256=preview['preview_sha256']))
    assert result['invocation_id']!=a['invocation_id']
    assert not list(empty_home.iterdir())


@pytest.mark.regression
def test_missing_observation_index_cannot_hide_uncertain_effect(lattice_env, monkeypatch):
    env=lattice_env;cs,identity=create(env);service=ConversationService(env['store'])
    admit(env,cs,identity,1);a=attempt(env,service.seal(identity))
    raw=json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'effect','name':'Bash','input':{'command':'external mutation'}}]}}).encode()+b'\n'
    monkeypatch.setattr(ObservationStore,'reindex',lambda *args:None)
    ObservationStore(env['store']).append(a['attempt_id'],'stdout',raw)
    env['store'].finish_attempt(a['attempt_id'],state=AttemptState.interrupted,failure_kind='operator_interruption')
    preview=service.preview(identity)
    blocker=next(b for b in preview['blockers'] if b['code']=='uncertain_mutating_effect')
    effect=blocker['effects'][0]
    disposition={'attempt_id':effect['attempt_id'],'tool_id':effect['tool_id'],'disposition':'do_not_repeat','evidence':'Outcome cannot be established; this action must not run again.'}
    preview=service.preview(identity,effect_dispositions=[disposition])
    assert preview['blockers']==[]
    request=ConversationContinue(expected_revision=preview['revision'],idempotency_key='explicit-disposition',expected_preview_sha256=preview['preview_sha256'],effect_dispositions=[disposition])
    sealed=service.seal(identity,continuation=request)
    compiled=env['lattice'].inspect(sealed['invocation_id'])['compiled_prompt']
    assert disposition['evidence'] in compiled
    assert 'do_not_repeat' in compiled
    assert 'external mutation' in compiled


@pytest.mark.regression
@pytest.mark.asyncio
async def test_dispatch_rejection_leaves_inspectable_attention_not_a_phantom_active_round(lattice_env):
    env=lattice_env;cs,identity=create(env)
    class RefusingSupervisor:
        async def execute(self,*args):
            raise LatticeConflict('native head changed before execution')
    service=ConversationService(env['store'],RefusingSupervisor())
    admit(env,cs,identity,1)
    with pytest.raises(LatticeConflict,match='native head changed'):
        await service.dispatch(identity)
    state=cs.get(identity)
    assert state['pending']['state']=='needs_attention'
    assert state['rounds'][0]['executions'][0]['attempts']==[]
    with env['store']._connect() as conn:
        assert conn.execute('SELECT state FROM invocations').fetchone()[0]=='failed'
    assert service.preview(identity)['blockers']==[]
