"""Exact retained records independently bound explicit recovery selection."""
import json

import pytest
from lattice.conversation_contracts import ConversationContinue, ConversationSend
from lattice.conversation_service import ConversationService
from lattice.contracts import AttemptState, ContextBlockCreate, ProviderRoute, BlockStatus
from lattice.store import LatticeConflict
from test_recoverable_conversations import create, admit, success
from test_conversation_recovery import attempt
from lattice.observations import ObservationStore


def waiting(env):
    cs, identity = create(env)
    service = ConversationService(env['store'])
    for index, answer in ((1, 'unique violet answer'), (2, 'unique amber answer')):
        admit(env, cs, identity, index)
        active = attempt(env, service.seal(identity))
        success(env['store'], active, answer)
    admit(env, cs, identity, 3)
    active = attempt(env, service.seal(identity))
    env['store'].finish_attempt(active['attempt_id'], state=AttemptState.failed, failure_kind='context_limit')
    return cs, identity, service, active


def continue_request(preview, **options):
    return ConversationContinue(expected_revision=preview['revision'], expected_preview_sha256=preview['preview_sha256'],
        idempotency_key='selected-context-recovery', **options)


@pytest.mark.oracle
def test_legacy_lost_acknowledgement_replays_before_new_preview_or_revision(lattice_env):
    from lattice.canonical import canonical_bytes, sha256_bytes
    cs,identity=create(lattice_env)
    admit(lattice_env,cs,identity,1)
    legacy={'action':'continue','expected_revision':1,'idempotency_key':'legacy-lost-ack','expected_preview_sha256':'1'*64,'policy':None,'include_steering':True,'effect_dispositions':[]}
    expected={'conversation_id':identity,'invocation_id':'original-durable-execution','round_id':'original-durable-round','revision':7,'event_cursor':42}
    with lattice_env['store'].transaction() as connection:
        connection.execute('INSERT INTO conversation_mutations VALUES(?,?,?,?,?)',(identity,'legacy-lost-ack',sha256_bytes(canonical_bytes(legacy)),canonical_bytes(expected).decode(),'20261014T000000'))
    request=ConversationContinue(**{key:value for key,value in legacy.items() if key!='action'})
    assert ConversationService(lattice_env['store']).seal(identity,continuation=request)==expected


@pytest.mark.oracle
def test_operator_selection_changes_context_without_changing_retained_history(lattice_env):
    env = lattice_env
    cs, identity, service, active = waiting(env)
    before = cs.get(identity)
    preview = service.preview(identity)
    assert preview['blockers'][0]['code'] == 'provider_context_limit'
    selection = {'omitted_round_ids': [before['rounds'][0]['round_id']], 'reason': 'Use only the second completed round in this continuation.'}
    preview = service.preview(identity, context_selection=selection)
    assert preview['blockers'] == []
    with pytest.raises(LatticeConflict, match='explicit context confirmation'):
        service.seal(identity, continuation=continue_request(preview, context_selection=selection))
    assert cs.get(identity)['revision'] == before['revision']
    request = continue_request(preview, context_selection=selection, confirmed_reason_sha256s=[r['reason_sha256'] for r in preview['required_confirmations']])
    recovered = service.seal(identity, continuation=request)
    compiled = env['lattice'].inspect(recovered['invocation_id'])['compiled_prompt']
    assert 'unique violet answer' not in compiled
    assert 'unique amber answer' in compiled and 'request 3' in compiled
    after = cs.get(identity)
    assert [(r['request_sha256'], r.get('response_sha256'), r.get('checkpoint_id')) for r in before['rounds']] == [(r['request_sha256'], r.get('response_sha256'), r.get('checkpoint_id')) for r in after['rounds']]
    assert service.seal(identity, continuation=request) == recovered
    a = attempt(env, recovered)
    success(env['store'], a, 'unique green new answer')
    admit(env, cs, identity, 4)
    next_round = service.seal(identity)
    compiled = env['lattice'].inspect(next_round['invocation_id'])['compiled_prompt']
    assert 'unique violet answer' not in compiled
    assert 'unique green new answer' in compiled


@pytest.mark.oracle
def test_excluding_observations_cannot_dispose_uncertain_mutation(lattice_env):
    env = lattice_env
    cs, identity = create(env)
    service = ConversationService(env['store'])
    admit(env, cs, identity, 1)
    a = attempt(env, service.seal(identity))
    raw = b'{"type":"assistant","message":{"content":[{"type":"tool_use","id":"write","name":"Bash","input":{"command":"write then disappear"}}]}}\n'
    ObservationStore(env['store']).append(a['attempt_id'], 'stdout', raw)
    env['store'].finish_attempt(a['attempt_id'], state=AttemptState.interrupted, failure_kind='operator_interruption')
    preview = service.preview(identity)
    selection = {'omitted_observation_ids': [preview['available_context']['observations'][0]['observation_id']], 'reason': 'Do not send the raw tool transcript.'}
    preview = service.preview(identity, context_selection=selection)
    assert preview['history']['unfinished_observations'] == []
    assert any(b['code'] == 'uncertain_mutating_effect' for b in preview['blockers'])
    with pytest.raises(LatticeConflict, match='uncertain_mutating_effect'):
        service.seal(identity, continuation=continue_request(preview, context_selection=selection))


@pytest.mark.oracle
def test_route_change_requires_preview_confirmation_and_becomes_default(lattice_env):
    env = lattice_env
    cs, identity, service, active = waiting(env)
    new = ProviderRoute(provider='anthropic', model='claude-fable-5-1', harness='claude').model_dump(mode='json')
    preview = service.preview(identity, route=new)
    assert preview['blockers'] == [] and preview['route_changed']
    with pytest.raises(LatticeConflict, match='explicit context confirmation'):
        service.seal(identity, continuation=continue_request(preview, route=new))
    result = service.seal(identity, continuation=continue_request(preview, route=new, confirmed_reason_sha256s=[r['reason_sha256'] for r in preview['required_confirmations']]))
    assert cs.get(identity)['route']['model'] == 'claude-fable-5-1'
    assert env['store'].get_invocation(active['invocation_id'])['route']['provider'] == 'openai'
    assert env['store'].get_invocation(result['invocation_id'])['route']['provider'] == 'anthropic'


@pytest.mark.oracle
def test_selection_cannot_name_other_history_or_omit_pending_request(lattice_env):
    cs, identity, service, active = waiting(lattice_env)
    with pytest.raises(LatticeConflict, match='unavailable evidence'):
        service.preview(identity, context_selection={'omitted_round_ids': [cs.get(identity)['rounds'][-1]['round_id']], 'reason': 'Try to exclude the unfinished operator request.'})


@pytest.mark.oracle
def test_context_ceiling_can_be_resolved_by_explicit_selection(lattice_env, monkeypatch):
    cs, identity, service, active = waiting(lattice_env)
    import lattice.compositor as compositor
    before = service.preview(identity)
    selection = {'omitted_round_ids': [r['round_id'] for r in before['available_context']['rounds']], 'reason': 'Exclude old answers while retaining their checkpoints.'}
    trimmed = service.preview(identity, context_selection=selection)
    original = service._prepare(before)[1]['members'][0]['compiled_prompt_preview']
    selected = service._prepare(trimmed)[1]['members'][0]['compiled_prompt_preview']
    assert len(selected.encode()) < len(original.encode())
    monkeypatch.setattr(compositor, 'MAX_COMPILED_CONTEXT_BYTES', len(selected.encode()) + 2)
    assert any(b['code'] == 'context_limit' for b in service.preview(identity)['blockers'])
    assert service.preview(identity, context_selection=selection)['blockers'] == []


@pytest.mark.oracle
def test_carried_selection_cannot_silently_accept_changed_context(lattice_env):
    env = lattice_env
    cs, identity = create(env)
    block = env['store'].create_context_block(ContextBlockCreate(project_id=env['project']['project_id'], semantic_type='operator-note', content='Keep this exact note.'))
    cs.send(identity, ConversationSend(prompt='Pending operator request', context_block_ids=[block['block_id']], expected_revision=1, idempotency_key='selected-note-request'))
    service = ConversationService(env['store'])
    a = attempt(env, service.seal(identity))
    env['store'].finish_attempt(a['attempt_id'], state=AttemptState.failed, failure_kind='rate_limit')
    selection = {'context_block_ids': [block['block_id']], 'reason': 'Explicitly keep the retained note.'}
    first = service.preview(identity, context_selection=selection)
    approvals = [r['reason_sha256'] for r in first['required_confirmations']]
    recovery = service.seal(identity, continuation=continue_request(first, context_selection=selection, confirmed_reason_sha256s=approvals))
    a = attempt(env, recovery)
    env['store'].finish_attempt(a['attempt_id'], state=AttemptState.failed, failure_kind='rate_limit')
    env['store'].set_block_status(block['block_id'], BlockStatus.disputed)
    assert any(b['code']=='context_binding' for b in service.preview(identity)['blockers'])
    fresh = service.preview(identity, context_selection=selection)
    assert fresh['blockers']==[]
    assert [r['reason_sha256'] for r in fresh['required_confirmations']] != approvals
    with pytest.raises(LatticeConflict, match='explicit context confirmation'):
        service.seal(identity, continuation=continue_request(fresh, context_selection=selection, confirmed_reason_sha256s=approvals).model_copy(update={'idempotency_key':'stale-context-approval'}))


@pytest.mark.contract
def test_authenticated_preview_accepts_selection_and_refuses_unconfirmed_admission(lattice_env):
    from fastapi.testclient import TestClient
    from lattice.server import create_app
    from lattice.security import ensure_cli_token
    cs, identity, service, active = waiting(lattice_env)
    token = ensure_cli_token(lattice_env['config'].operator_token)
    selection = {'omitted_round_ids':[cs.get(identity)['rounds'][0]['round_id']], 'reason':'Select the smaller retained history.'}
    with TestClient(create_app(lattice_env['config']),base_url='http://127.0.0.1:9010',headers={'Authorization':'Bearer '+token}) as client:
        result = client.post(f'/api/v1/conversations/{identity}/recovery-preview',json={'context_selection':selection})
        assert result.status_code==200, result.text
        preview = result.json()
        assert preview['blockers']==[] and preview['required_confirmations']
        result = client.post(f'/api/v1/conversations/{identity}/continue',json=continue_request(preview,context_selection=selection).model_dump(mode='json'))
        assert result.status_code==409 and 'explicit context confirmation' in result.text
        assert cs.get(identity)['pending']['state']=='needs_attention'
