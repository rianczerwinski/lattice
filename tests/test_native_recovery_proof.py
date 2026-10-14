"""Hand-authored raw journals challenge native-recovery proof admission."""
import copy
import hashlib
import json
from types import SimpleNamespace
import pytest
from lattice.native_recovery_proof import verify_native_case


@pytest.fixture
def evidence():
    blobs={}
    def put(value):
        raw=value if isinstance(value,bytes) else json.dumps(value).encode()
        key=hashlib.sha256(raw).hexdigest();blobs[key]=raw
        return 'lattice:blob/'+key
    route={'provider':'anthropic','model':'claude-fable-5-1','effort':'high','tools':['Read','Glob','Grep'],'permissions':['Read','Glob','Grep'],'workspace':'/fixture','sandbox':'provider-permissions','approval_policy':'never','network_policy':'disabled'}
    nonce='1376646dfe114fd983c15b5a7a54b101'
    def journal(name,continued):
        prompt=b'Recall the original token exactly.' if continued else ('Retain this token: '+nonce).encode()
        request={'invocation_id':name,'model':route['model'],'effort':'high','prompt_sha256':hashlib.sha256(prompt).hexdigest(),'metadata':{'binary_sha256':'b'*64,'effective_policy':{k:v for k,v in route.items() if k not in ('provider','model','effort')}}}
        request['metadata']['schema']={'type':'object','properties':{'response':{'type':'string'}},'required':['response'],'additionalProperties':False}
        request['metadata']['effective_policy'].update(execution_profile='native-continuation' if continued else 'workspace-agent',native_session_id='session-one' if continued else None,continuation_mode='resume' if continued else None)
        messages=[{'type':'system','subtype':'init','session_id':'session-one','model':route['model']},{'type':'assistant','message':{'content':'exposed work'}}]
        if continued:
            messages.append({'type':'result','subtype':'success','is_error':False,'structured_output':{'response':nonce}})
        return {'request':put(request),'prompt':put(prompt),'stdout':put(b''.join(json.dumps(m).encode()+b'\n' for m in messages)),'terminal':put({'invocation_id':name,'status':'success' if continued else 'interrupted','failure_kind':None if continued else 'operator_interruption'}),'checkpoint':put({'native_session':{'id':'session-one'}})}
    proof={'canary':nonce,'first':journal('first',False),'continued':journal('continued',True)}
    return SimpleNamespace(blobs=SimpleNamespace(get=lambda digest:blobs[digest])),put,blobs,route,proof


@pytest.mark.oracle
def test_raw_interrupted_native_evidence_is_accepted(evidence):
    store,put,blobs,route,proof=evidence
    assert verify_native_case(store,proof,route,'b'*64,'interrupted')=={'boundary':'interrupted','session_id':'session-one','first':'first','continued':'continued'}


@pytest.mark.oracle
def test_partial_native_activity_qualifies_only_before_terminal_output(evidence):
    store,put,blobs,route,proof=evidence
    messages=[json.loads(line) for line in blobs[proof['first']['stdout'].rsplit('/',1)[1]].splitlines()]
    messages[1]={'type':'stream_event','event':{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'retained partial'}}}
    proof['first']['stdout']=put(b''.join(json.dumps(m).encode()+b'\n' for m in messages))
    assert verify_native_case(store,proof,route,'b'*64,'interrupted')['boundary']=='interrupted'
    messages.append({'type':'result','subtype':'success','is_error':False,'structured_output':{'response':'already complete'}})
    proof['first']['stdout']=put(b''.join(json.dumps(m).encode()+b'\n' for m in messages))
    with pytest.raises(ValueError,match='after terminal output'):
        verify_native_case(store,proof,route,'b'*64,'interrupted')


@pytest.mark.oracle
@pytest.mark.parametrize('fault',['wrong_model','changed_authority','wrong_binary','no_interruption','wrong_session','missing_recall','leaked_canary','false_success','missing_bytes','switched_model','compaction','tool_retrieval'])
def test_native_proof_rejects_false_qualification(evidence,fault):
    store,put,blobs,route,proof=evidence
    proof=copy.deepcopy(proof)
    def read(part,name):
        return json.loads(blobs[proof[part][name].rsplit('/',1)[1]])
    if fault in ('wrong_model','changed_authority','wrong_binary'):
        value=read('continued','request')
        if fault=='wrong_model':value['model']='another-model'
        elif fault=='wrong_binary':value['metadata']['binary_sha256']='c'*64
        else:value['metadata']['effective_policy']['permissions'].append('Bash')
        proof['continued']['request']=put(value)
    elif fault=='no_interruption':
        value=read('first','terminal');value['status']='success';proof['first']['terminal']=put(value)
    elif fault=='wrong_session':
        proof['continued']['checkpoint']=put({'native_session':{'id':'another-session'}})
    elif fault=='leaked_canary':
        raw=('Recall '+proof['canary']).encode()
        value=read('continued','request');value['prompt_sha256']=hashlib.sha256(raw).hexdigest()
        proof['continued']['request']=put(value);proof['continued']['prompt']=put(raw)
    elif fault in ('missing_recall','false_success','switched_model','compaction','tool_retrieval'):
        messages=[json.loads(line) for line in blobs[proof['continued']['stdout'].rsplit('/',1)[1]].splitlines()]
        if fault=='missing_recall':messages[-1]['structured_output']['response']='forgotten'
        elif fault=='false_success':messages[-1]['is_error']=True
        elif fault=='switched_model':messages[1]['message']['model']='claude-opus-5'
        elif fault=='compaction':messages.insert(1,{'type':'system','subtype':'compact_boundary'})
        else:
            messages[1:1]=[{'type':'assistant','message':{'content':[{'type':'tool_use','id':'retrieved','name':'Read','input':{'file_path':'/private/prior-token.txt'}}]}}, {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'retrieved','content':proof['canary']}]}}]
        proof['continued']['stdout']=put(b''.join(json.dumps(m).encode()+b'\n' for m in messages))
    else:
        del blobs[proof['first']['stdout'].rsplit('/',1)[1]]
    with pytest.raises((ValueError,KeyError)):
        verify_native_case(store,proof,route,'b'*64,'interrupted')


@pytest.mark.oracle
def test_completed_round_proof_does_not_qualify_interruption(evidence):
    store,put,blobs,route,proof=evidence
    terminal=json.loads(blobs[proof['first']['terminal'].rsplit('/',1)[1]])
    terminal['status']='success';terminal['failure_kind']=None
    proof['first']['terminal']=put(terminal)
    raw=blobs[proof['first']['stdout'].rsplit('/',1)[1]]
    raw+=json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'response':'retained'}}).encode()+b'\n'
    proof['first']['stdout']=put(raw)
    assert verify_native_case(store,proof,route,'b'*64,'completed')['boundary']=='completed'
    with pytest.raises(ValueError,match='cancellation'):
        verify_native_case(store,proof,route,'b'*64,'interrupted')


@pytest.mark.oracle
@pytest.mark.parametrize('channel', ['schema', 'other_metadata', 'encoded_schema', 'missing_schema'])
def test_continuation_input_metadata_cannot_supply_the_recalled_canary(evidence, channel):
    store,put,blobs,route,proof=evidence
    request=json.loads(blobs[proof['continued']['request'].rsplit('/',1)[1]])
    if channel=='schema':
        request['metadata']['schema']={'type':'object','properties':{'response':{'const':proof['canary']}}}
    elif channel=='other_metadata':
        request['metadata']['alternate_input']={'text':proof['canary']}
    elif channel=='encoded_schema':
        import base64
        request['metadata']['schema']['description']='Decode this answer: '+base64.b64encode(proof['canary'].encode()).decode()
    else:
        request['metadata'].pop('schema')
    proof['continued']['request']=put(request)
    with pytest.raises(ValueError,match='canary.*continuation input|non-disclosing output schema'):
        verify_native_case(store,proof,route,'b'*64,'interrupted')


@pytest.mark.oracle
def test_qualification_and_snapshot_bind_all_raw_proof_bytes(evidence, lattice_env, tmp_path):
    from pathlib import Path
    from lattice.canonical import sha256_json
    from lattice.diagnostics import assess_data_root
    from lattice.execution import DEFAULT_ADAPTERS
    from lattice.native_recovery import qualification, route_identity
    from lattice.runtime_attestation import _digest, tree_inventory
    from lattice.snapshot import create_snapshot, restore_snapshot
    import lattice.native_recovery as implementation

    _, _, blobs, route, proof = evidence
    store = lattice_env['store']
    for digest, raw in blobs.items():
        assert store.blobs.put(raw) == digest
    proof_digest = store.blobs.put(json.dumps(proof).encode())
    route = {**route, 'provider_profile_id': next(a.profile_id for a in DEFAULT_ADAPTERS if a.provider_code == 'anthropic')}
    receipt = {
        'contract_version': 'ConversationRecoveryQualification/1.0',
        'route_sha256': route_identity(route),
        'native_verifier_sha256': _digest(Path(implementation.__file__))[0],
        'runtime_tree_sha256': sha256_json(tree_inventory(Path(implementation.__file__).parent)),
        'authenticated': True, 'negative_controls_passed': True,
        'boundaries': {'interrupted': True},
        'evidence_refs': {'interrupted': 'lattice:blob/' + proof_digest},
    }
    store.record_provider_qualification(route['provider_profile_id'], 'b' * 64, receipt, state='passed')
    assert qualification(store, route, 'b' * 64, 'interrupted') == receipt
    assert qualification(store, route, 'b' * 64, 'completed') is None
    source = assess_data_root(store.config.data_root)
    snapshot = create_snapshot(store.config.data_root, backup_root=tmp_path / 'backups', reason='raw-native-proof', source_revision=source['state_digest'], schema_version=source['schema_version'], state_digest=source['state_digest'])
    restored = tmp_path / 'restored'
    restore_snapshot(snapshot, restored)
    for digest, raw in blobs.items():
        assert (restored / 'blobs' / digest[:2] / digest).read_bytes() == raw
    assert assess_data_root(restored)['state_digest'] == source['state_digest']
    # Removing a leaf that only the nested qualification proof references must
    # invalidate both admission and snapshot reachability, not become an omission.
    leaf = proof['continued']['stdout'].rsplit('/', 1)[1]
    (store.config.blobs / leaf[:2] / leaf).unlink()
    assert qualification(store, route, 'b' * 64, 'interrupted') is None
    with pytest.raises(OSError, match='missing blob required'):
        assess_data_root(store.config.data_root)
