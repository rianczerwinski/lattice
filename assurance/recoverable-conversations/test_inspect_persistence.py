"""Hand-authored ledger fixtures challenge the independent persistence reader."""
import importlib.util
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import rfc8785


path = Path(__file__).with_name('inspect_persistence.py')
spec = importlib.util.spec_from_file_location('conversation_persistence_reader',path)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return rfc8785.dumps(value)


@pytest.fixture
def ledger(tmp_path):
    connection=sqlite3.connect(tmp_path/'lattice.sqlite3')
    connection.executescript('''
        CREATE TABLE conversations(conversation_id,project_id,owning_task_ref);
        CREATE TABLE conversation_rounds(round_id,conversation_id,ordinal,request_json,request_sha256,state,accepted_response_id,checkpoint_id);
        CREATE TABLE conversation_executions(round_id,ordinal,invocation_id);
        CREATE TABLE conversation_checkpoints(checkpoint_id,round_id,content_json,content_sha256);
        CREATE TABLE invocations(invocation_id,project_id,manifest_json,manifest_sha256,route_json,state,output_block_id);
        CREATE TABLE context_blocks(block_id,project_id,content_sha256,byte_count);
        CREATE TABLE attempts(attempt_id,invocation_id);
        CREATE TABLE events(seq,subject_id,event_type,payload_json);
        CREATE TABLE artifacts(artifact_id,project_id,content_sha256);
        CREATE TABLE conversation_steering(target_round_id,resulting_round_id,state,conversation_id,ordinal);
    ''')
    connection.execute("INSERT INTO conversations VALUES('child','project','task:replacement')")
    expected={'completed':[],'completed_count':10,'execution_count':11}
    previous=None
    for ordinal in range(1,12):
        request={'prompt':f'Original request {ordinal}'}
        request_sha=sha(encoded(request))
        manifest={'prompt':request['prompt'],'provider':'fixture'}
        manifest_sha=sha(encoded(manifest))
        route={'model':'synthetic'}
        connection.execute('INSERT INTO invocations VALUES(?,?,?,?,?,?,?)',(f'inv{ordinal}','project',encoded(manifest).decode(),manifest_sha,encoded(route).decode(),'succeeded' if ordinal<11 else 'interrupted',f'response{ordinal}' if ordinal<11 else None))
        connection.execute('INSERT INTO conversation_executions VALUES(?,?,?)',(f'round{ordinal}',1,f'inv{ordinal}'))
        if ordinal==11:
            connection.execute('INSERT INTO conversation_rounds VALUES(?,?,?,?,?,?,?,?)',(f'round{ordinal}','child',ordinal,encoded(request).decode(),request_sha,'needs_attention',None,None))
            continue
        raw=f'Original response {ordinal}'.encode()
        response_sha=sha(raw)
        directory=tmp_path/'blobs'/response_sha[:2];directory.mkdir(parents=True,exist_ok=True)
        (directory/response_sha).write_bytes(raw)
        connection.execute('INSERT INTO context_blocks VALUES(?,?,?,?)',(f'response{ordinal}','project',response_sha,len(raw)))
        checkpoint={'conversation_id':'child','round_id':f'round{ordinal}','ordinal':ordinal,'request_sha256':request_sha,
                    'response_id':f'response{ordinal}','response_sha256':response_sha,'previous':previous,
                    'invocation_id':f'inv{ordinal}','manifest_sha256':manifest_sha,'route':route,'event_cursor':0,'artifacts':[]}
        checkpoint_sha=sha(encoded(checkpoint))
        connection.execute('INSERT INTO conversation_checkpoints VALUES(?,?,?,?)',(f'checkpoint{ordinal}',f'round{ordinal}',encoded(checkpoint).decode(),checkpoint_sha))
        connection.execute('INSERT INTO conversation_rounds VALUES(?,?,?,?,?,?,?,?)',(f'round{ordinal}','child',ordinal,encoded(request).decode(),request_sha,'completed',f'response{ordinal}',f'checkpoint{ordinal}'))
        previous={'checkpoint_id':f'checkpoint{ordinal}','content_sha256':checkpoint_sha}
        expected['completed'].append({'ordinal':ordinal,'request_sha256':request_sha,'response_sha256':response_sha})
    connection.commit();connection.close()
    return tmp_path,expected


def test_completed_ten_remain_usable_after_eleventh_failure(ledger):
    root,expected=ledger
    result=reader.inspect(root,'child',expected)
    assert result['completed']==expected['completed']
    assert result['round_count']==11 and result['execution_count']==11
    assert result['owning_task_ref']=='task:replacement'
    assert result['persistence_verified'] and result['proof_limits']


@pytest.mark.parametrize('fault',['missing_response','self_consistent_replacement','checkpoint_chain','another_project','second_pending','duplicate_execution','false_completion'])
def test_false_persistence_claims_are_rejected(ledger,fault):
    root,expected=ledger
    with sqlite3.connect(root/'lattice.sqlite3') as connection:
        if fault=='missing_response':
            identity=expected['completed'][0]['response_sha256']
            (root/'blobs'/identity[:2]/identity).unlink()
        elif fault=='self_consistent_replacement':
            value={'prompt':'Rewritten original request'}
            identity=sha(encoded(value))
            connection.execute('UPDATE conversation_rounds SET request_json=?,request_sha256=? WHERE ordinal=1',(encoded(value).decode(),identity))
            checkpoint=json.loads(connection.execute("SELECT content_json FROM conversation_checkpoints WHERE checkpoint_id='checkpoint1'").fetchone()[0])
            checkpoint['request_sha256']=identity
            updated=sha(encoded(checkpoint))
            connection.execute("UPDATE conversation_checkpoints SET content_json=?,content_sha256=? WHERE checkpoint_id='checkpoint1'",(encoded(checkpoint).decode(),updated))
            # Keep the entire checkpoint chain self-consistent, so only the
            # independent frozen original history can reject this replacement.
            for ordinal in range(2,11):
                checkpoint=json.loads(connection.execute('SELECT content_json FROM conversation_checkpoints WHERE checkpoint_id=?',(f'checkpoint{ordinal}',)).fetchone()[0])
                checkpoint['previous']['content_sha256']=updated
                updated=sha(encoded(checkpoint))
                connection.execute('UPDATE conversation_checkpoints SET content_json=?,content_sha256=? WHERE checkpoint_id=?',(encoded(checkpoint).decode(),updated,f'checkpoint{ordinal}'))
        elif fault=='checkpoint_chain':
            value=json.loads(connection.execute("SELECT content_json FROM conversation_checkpoints WHERE checkpoint_id='checkpoint10'").fetchone()[0])
            value['previous']=None
            connection.execute("UPDATE conversation_checkpoints SET content_json=?,content_sha256=? WHERE checkpoint_id='checkpoint10'",(encoded(value).decode(),sha(encoded(value))))
        elif fault=='another_project':
            connection.execute("UPDATE invocations SET project_id='other' WHERE invocation_id='inv11'")
        elif fault=='second_pending':
            connection.execute("INSERT INTO conversation_rounds VALUES('round12','child',12,'{}',?,'queued',NULL,NULL)",(sha(b'{}'),))
        elif fault=='false_completion':
            connection.execute("UPDATE invocations SET state='failed' WHERE invocation_id='inv1'")
        else:
            connection.execute("INSERT INTO conversation_executions VALUES('round11',2,'inv1')")
    with pytest.raises((reader.PersistenceFailure,FileNotFoundError)):
        reader.inspect(root,'child',expected)


def test_admitted_observations_require_exact_offsets_and_bytes(ledger):
    root,expected=ledger
    raw=b'partial caf\xc3\xa9'
    identity=sha(raw)
    folder=root/'blobs'/identity[:2];folder.mkdir(parents=True,exist_ok=True)
    (folder/identity).write_bytes(raw)
    event={'channel':'stdout','offset':0,'sha256':identity,'content_ref':'lattice:blob/'+identity,'byte_count':len(raw)}
    with sqlite3.connect(root/'lattice.sqlite3') as connection:
        connection.execute("INSERT INTO attempts VALUES('attempt11','inv11')")
        connection.execute("INSERT INTO events VALUES(1,'attempt11','attempt.chunk',?)",(json.dumps(event),))
    assert reader.inspect(root,'child',expected)['admitted_bytes']==len(raw)
    event['offset']=1
    with sqlite3.connect(root/'lattice.sqlite3') as connection:
        connection.execute('UPDATE events SET payload_json=?',(json.dumps(event),))
    with pytest.raises(reader.PersistenceFailure,match='offset gap'):
        reader.inspect(root,'child',expected)


def test_duplicate_json_cannot_hide_unbound_request_bytes(ledger):
    root,expected=ledger
    with sqlite3.connect(root/'lattice.sqlite3') as connection:
        connection.execute('UPDATE conversation_rounds SET request_json=? WHERE ordinal=1',('{"prompt":"unbound","prompt":"Original request 1"}',))
    with pytest.raises(reader.PersistenceFailure,match='duplicate JSON'):
        reader.inspect(root,'child',expected)


def test_reader_accepts_actual_store_records_without_using_status_projection(tmp_path):
    import time
    from lattice import LatticeConfig,LatticeStore
    from lattice.contracts import ProjectCreate,ProviderRoute,AttemptState
    from lattice.conversation_contracts import ConversationCreate,ConversationSend
    from lattice.conversations import ConversationStore
    from lattice.conversation_service import ConversationService
    root=tmp_path/'actual-store'
    store=LatticeStore(LatticeConfig(data_root=root,axisvi_root=None))
    project=store.create_project(ProjectCreate(title='Reader qualification'))
    cs=ConversationStore(store)
    identity=cs.create(ConversationCreate(project_id=project['project_id'],title='Real schema child',owning_task_ref='task:independent',
        route=ProviderRoute(provider='openai',harness='codex',model='gpt-5.6-sol'),idempotency_key='actual-reader-child'))['conversation_id']
    service=ConversationService(store)
    for ordinal in (1,2):
        with store._connect() as connection:
            revision=connection.execute('SELECT revision FROM conversations WHERE conversation_id=?',(identity,)).fetchone()[0]
        cs.send(identity,ConversationSend(prompt=f'Hand-written request {ordinal}',expected_revision=revision,idempotency_key=f'actual-reader-{ordinal}'))
        execution=service.seal(identity)
        attempt=store.create_attempt(execution['invocation_id'],f'actual-dispatch-{ordinal}','oracle',int(time.time())+100)
        store.mark_attempt_running(attempt['attempt_id'])
        if ordinal==1:
            store.finish_successful_attempt(attempt['attempt_id'],response='Hand-written accepted answer',artifacts=[],journal_path='synthetic',runtime_receipt={})
        else:
            store.finish_attempt(attempt['attempt_id'],state=AttemptState.interrupted,failure_kind='operator_interruption')
    result=reader.inspect(root,identity)
    assert result['round_count']==2 and result['execution_count']==2
    assert len(result['completed'])==1
    assert result['completed'][0]['response_sha256']==sha(b'Hand-written accepted answer')
