"""Native evidence tests; synthetic fixtures never qualify a live provider."""
import json
import time
from pathlib import Path
import pytest
from lattice import LatticeStore, LatticeConfig
from lattice.contracts import ProviderRoute, AttemptState
from lattice.conversation_contracts import ConversationCreate, ConversationContinue
from lattice.conversations import ConversationStore
from lattice.conversation_service import ConversationService
from lattice.native_recovery import capture_boundary, eligibility
from lattice.observations import ObservationStore
from test_conversation_recovery import attempt


@pytest.fixture
def native(lattice_env,tmp_path,monkeypatch):
    env=lattice_env
    workspace=tmp_path/'workspace';workspace.mkdir()
    provider_home=tmp_path/'provider';provider_home.mkdir()
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:provider_home))
    binary=tmp_path/'claude';binary.write_text('qualified synthetic executable')
    monkeypatch.setattr('lattice.native_recovery.shutil.which',lambda _:str(binary))
    route=ProviderRoute(provider='anthropic',model='sonnet',harness='claude',execution_profile='workspace-agent',workspace=str(workspace),sandbox='provider-permissions')
    cs=ConversationStore(env['store'])
    identity=cs.create(ConversationCreate(project_id=env['project']['project_id'],title='native fixture',owning_task_ref='parent',route=route,idempotency_key='native-child'))['conversation_id']
    from lattice.conversation_contracts import ConversationSend
    cs.send(identity,ConversationSend(prompt='native request',expected_revision=1,idempotency_key='native-request'))
    service=ConversationService(env['store'])
    sealed=service.seal(identity);a=attempt(env,sealed)
    session='11111111-1111-4111-8111-111111111111'
    path=provider_home/'.claude'/'projects'/'workspace'/f'{session}.jsonl';path.parent.mkdir(parents=True)
    row={'type':'user','sessionId':session,'uuid':'head-one','isSidechain':False,'cwd':str(workspace),'message':{'content':'native request'}}
    path.write_text(json.dumps(row)+'\n')
    ObservationStore(env['store']).append(a['attempt_id'],'stdout',json.dumps({'type':'system','subtype':'init','session_id':session,'model':'sonnet'}).encode()+b'\n')
    receipt={'native_session':{'id':session},'binary':str(binary)}
    receipt['conversation_native_boundary']=capture_boundary(env['store'],env['store'].get_invocation(sealed['invocation_id']),receipt)
    assert receipt['conversation_native_boundary']['eligible_evidence'] is True
    env['store'].finish_attempt(a['attempt_id'],state=AttemptState.interrupted,failure_kind='operator_interruption',runtime_receipt=receipt)
    return env,service,identity,sealed['invocation_id'],path,row


@pytest.mark.contract
def test_interrupted_native_boundary_requires_independent_qualification(native,monkeypatch):
    env,service,identity,parent,path,row=native
    assert service.preview(identity)['mode']=='portable'
    assert service.preview(identity,policy='native')['blockers'][0]['code']=='native_unqualified'
    monkeypatch.setattr('lattice.native_recovery.qualification',lambda *args:{'fixture':'independent synthetic gate'})
    preview=service.preview(identity)
    assert preview['native']['eligible'] is True
    assert preview['mode']=='native'
    assert preview['blockers']==[]
    recovered=service.seal(identity,continuation=ConversationContinue(expected_revision=preview['revision'],idempotency_key='native-continue',expected_preview_sha256=preview['preview_sha256']))
    assert env['store'].get_invocation(recovered['invocation_id'])['route']['native_parent_invocation_id']==parent
    a=attempt(env,recovered)
    assert a['state']=='leased'


@pytest.mark.regression
def test_native_head_advance_and_executable_drift_fail_closed(native,monkeypatch):
    env,service,identity,parent,path,row=native
    monkeypatch.setattr('lattice.native_recovery.qualification',lambda *args:{'fixture':'independent synthetic gate'})
    inv=env['store'].get_invocation(parent)
    assert eligibility(env['store'],inv)['eligible']
    original=path.read_text()
    path.write_text(original+json.dumps({**row,'uuid':'unexpected-head','parentUuid':'head-one'})+'\n')
    assert eligibility(env['store'],inv)['eligible'] is False
    assert service.preview(identity)['mode']=='portable'
    path.write_text(original)
    binary=Path(inv['attempts'][-1]['runtime_receipt']['binary'])
    binary.write_text('different executable')
    assert eligibility(env['store'],inv)['eligible'] is False


@pytest.mark.regression
def test_low_level_native_dispatch_cannot_bypass_conversation_effect_review(native,monkeypatch):
    from lattice.contracts import DraftCreate, DirectActorBinding, ContextSelection
    from lattice.store import LatticeConflict
    env,service,identity,parent,path,row=native
    monkeypatch.setattr('lattice.native_recovery.qualification',lambda *args:{'fixture':'independent synthetic gate'})
    original=env['store'].get_invocation(parent)
    route=ProviderRoute.model_validate({**original['route'],'execution_profile':'native-continuation','session_policy':'visible_native_continuation','continuation_mode':'resume','native_parent_invocation_id':parent,'native_session_id':row['sessionId']})
    draft=env['store'].create_draft(DraftCreate(project_id=env['project']['project_id'],actor_binding=DirectActorBinding(),prompt='unsafe low-level retry',route=route,parent_invocation_ids=[parent],context_mode='explicit',selections=[ContextSelection(block_id='placeholder',plane='current_prompt',label='Current request')]),retarget_current_prompt_selections=True)
    sealed=env['lattice'].seal(draft['draft_id'])
    with pytest.raises(LatticeConflict,match='inspected effect dispositions'):
        attempt(env,sealed)


@pytest.mark.regression
def test_declared_passing_qualification_without_raw_evidence_is_ineligible(native):
    from lattice.canonical import sha256_json
    from lattice.runtime_attestation import _digest,tree_inventory
    import lattice.native_recovery as implementation
    env,service,identity,parent,path,row=native
    inv=env['store'].get_invocation(parent)
    evidence=inv['attempts'][-1]['runtime_receipt']['conversation_native_boundary']
    receipt={'contract_version':'ConversationRecoveryQualification/1.0','route_sha256':implementation.route_identity(inv['route']),'native_verifier_sha256':_digest(Path(implementation.__file__))[0],'runtime_tree_sha256':sha256_json(tree_inventory(Path(implementation.__file__).parent)),'authenticated':True,'negative_controls_passed':True,'boundaries':{'completed':True,'interrupted':True},'evidence_refs':{'interrupted':'lattice:blob/'+'f'*64}}
    env['store'].record_provider_qualification(inv['route']['provider_profile_id'],evidence['binary_sha256'],receipt,state='passed')
    assert eligibility(env['store'],env['store'].get_invocation(parent))['eligible'] is False
