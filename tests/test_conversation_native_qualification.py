"""Runner admission and cancellation oracles; no authenticated model calls."""
import importlib.util
import json
from pathlib import Path

import pytest
from test_native_recovery_proof import evidence as native_evidence


@pytest.fixture
def runner(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'ops' / 'qualify_conversation_native.py'
    spec = importlib.util.spec_from_file_location('native_qualification_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'checked_plan', lambda root: json.loads((root / 'plan.json').read_bytes()))
    return module


def plan(root, runner):
    fixture = root / 'synthetic-reading-fixture.txt'
    fixture.write_text('A square has four sides.\n')
    value = {'route': runner.ROUTE, 'schema': runner.NATIVE_RECALL_SCHEMA,
             'canaries': {'completed': '1' * 32, 'interrupted': '2' * 32}, 'timeout_seconds': 180,
             'interruption_source': {'path':str(fixture), 'sha256':runner.sha256_bytes(fixture.read_bytes())}}
    (root / 'plan.json').write_text(json.dumps(value))
    return value


def test_started_round_is_never_redispatched_after_lost_operator(runner, tmp_path, monkeypatch):
    plan(tmp_path, runner)
    dispatches = []

    def crashed(*args, **kwargs):
        dispatches.append(kwargs['prompt'])
        assert json.loads((tmp_path / 'completed-first.started.json').read_bytes())['prompt'] == kwargs['prompt']
        raise RuntimeError('lost parent after admission')

    monkeypatch.setattr(runner, 'invoke_subscription', crashed)
    with pytest.raises(RuntimeError, match='lost parent'):
        runner.run(tmp_path, 'completed-first')
    with pytest.raises(FileExistsError):
        runner.run(tmp_path, 'completed-first')
    assert len(dispatches) == 1
    assert not (tmp_path / 'completed-first.result.json').exists()


def test_cancel_uses_exposed_record_boundary_and_persists_once(runner, tmp_path, monkeypatch):
    plan(tmp_path, runner)
    for step in ('completed-first', 'completed-continued'):
        (tmp_path / (step + '.result.json')).write_text('{"passed":true}')
    journal = tmp_path / 'journal'
    journal.mkdir()
    (journal / 'request.json').write_text('{}')

    def interrupted(*args, **kwargs):
        assert str(tmp_path / 'synthetic-reading-fixture.txt') in kwargs['prompt']
        assert '/Users/rian/AXISVI/' not in kwargs['prompt']
        kwargs['on_journal'](journal)
        observe = kwargs['on_chunk']
        event = json.dumps({'type': 'assistant', 'message': {'content': 'é'}}, ensure_ascii=False).encode() + b'\n'
        boundary = event.index('é'.encode()) + 1
        observe('stdout', event[:boundary])
        assert not kwargs['cancel_event'].is_set()
        observe('stdout', event[boundary:])
        assert kwargs['cancel_event'].is_set()
        observe('stdout', event)
        raise runner.SubscriptionInvocationFailed('operator_interruption', 'real cancel seam', journal=journal)

    monkeypatch.setattr(runner, 'invoke_subscription', interrupted)
    runner.run(tmp_path, 'interrupted-first')
    result = json.loads((tmp_path / 'interrupted-first.result.json').read_bytes())
    assert result['passed'] and result['exposed'] and result['expected_interruption']
    assert json.loads((tmp_path / 'interrupted-first.cancel.json').read_bytes())['reason'] == 'exposed assistant activity'


def test_prepared_fixture_is_bound_and_drift_refuses_dispatch(runner,tmp_path,monkeypatch):
    manifest=tmp_path/'runtime.json';manifest.write_text('{"runtime_id":"fixture"}')
    binding={'runner_sha256':runner.sha256_bytes(Path(runner.__file__).read_bytes()),
             'runtime_manifest_path':str(manifest),'runtime_manifest_sha256':runner.sha256_bytes(manifest.read_bytes())}
    monkeypatch.setattr(runner,'runtime_binding',lambda:binding)
    root=tmp_path/'qualification';runner.prepare(root)
    prepared=json.loads((root/'plan.json').read_bytes());source=prepared['interruption_source']
    fixture=Path(source['path']);assert fixture.parent==root
    assert source['sha256']==runner.sha256_bytes(fixture.read_bytes())
    assert {'ref':source['path'],'revision':None,'sha256':source['sha256']} in prepared['generated_object']['sources']
    assert {'ref':str(manifest),'revision':None,'sha256':binding['runtime_manifest_sha256']} in prepared['generated_object']['sources']
    monkeypatch.undo()
    monkeypatch.setattr(runner,'runtime_binding',lambda:binding)
    runner.checked_plan(root)
    fixture.write_text('Changed material that was never admitted.\n')
    with pytest.raises(RuntimeError,match='fixture changed'):
        runner.run(root,'completed-first')
    assert not (root/'completed-first.started.json').exists()


def test_provider_failure_retains_cause_and_blocks_next_round(runner, tmp_path, monkeypatch):
    plan(tmp_path, runner)
    dispatches = []

    def failed(*args, **kwargs):
        dispatches.append(kwargs['prompt'])
        raise runner.SubscriptionInvocationFailed('rate_limit', 'subscription refused')

    monkeypatch.setattr(runner, 'invoke_subscription', failed)
    with pytest.raises(RuntimeError, match='Qualification stopped'):
        runner.run(tmp_path, 'completed-first')
    result = json.loads((tmp_path / 'completed-first.result.json').read_bytes())
    assert result['passed'] is False and result['failure']['kind'] == 'rate_limit'
    with pytest.raises(RuntimeError, match='earlier round'):
        runner.run(tmp_path, 'completed-continued')
    assert len(dispatches) == 1
    assert not (tmp_path / 'completed-continued.started.json').exists()


def test_source_environment_cannot_claim_installed_runtime(runner):
    with pytest.raises(RuntimeError, match='installed runtime Python'):
        runner.runtime_binding()


def test_replacement_operator_seals_completed_kernel_work_without_dispatch(runner,tmp_path,monkeypatch):
    plan(tmp_path,runner)
    calls=[]
    journal=tmp_path/'journal';journal.mkdir()
    original_write=runner.write_once
    def provider(*args,**kwargs):
        calls.append(kwargs['prompt'])
        (journal/'request.json').write_text(json.dumps({'prompt_sha256':runner.sha256_bytes(kwargs['prompt'].encode())}))
        kwargs['on_journal'](journal)
        events=[{'type':'system','subtype':'init','model':'claude-fable-5-1'}, {'type':'result','subtype':'success','is_error':False,'structured_output':{'response':'retained'}}]
        (journal/'stdout.log').write_text(''.join(json.dumps(m)+'\n' for m in events))
        (journal/'terminal.json').write_text('{"status":"success"}')
        return {'response':'retained'},journal
    def lost_ack(path,value):
        if path.name=='completed-first.result.json':
            raise RuntimeError('operator disappeared after kernel terminal')
        original_write(path,value)
    monkeypatch.setattr(runner,'invoke_subscription',provider)
    monkeypatch.setattr(runner,'write_once',lost_ack)
    with pytest.raises(RuntimeError,match='operator disappeared'):
        runner.run(tmp_path,'completed-first')
    monkeypatch.setattr(runner,'write_once',original_write)
    runner.reconcile(tmp_path,'completed-first')
    runner.reconcile(tmp_path,'completed-first')
    result=json.loads((tmp_path/'completed-first.result.json').read_bytes())
    assert result['passed'] and result['reconciled'] and result['response']=={'response':'retained'}
    assert len(calls)==1


def test_completed_native_capability_is_admitted_without_later_round_success(runner,tmp_path,monkeypatch,lattice_env,native_evidence):
    _,_,blobs,route,proof=native_evidence
    value=plan(tmp_path,runner)
    value['canaries']['completed']=proof['canary']
    value['binding']={'binary_sha256':'b'*64,'runtime_tree_sha256':runner.sha256_json(runner.tree_inventory(Path(runner.native.__file__).parent))}
    (tmp_path/'plan.json').write_text(json.dumps(value))
    for phase in ('first','continued'):
        journal=tmp_path/phase;journal.mkdir()
        for key,filename in [('request','request.json'),('prompt','prompt.txt'),('stdout','stdout.log'),('terminal','terminal.json'),('checkpoint','checkpoint.json')]:
            raw=blobs[proof[phase][key].rsplit('/',1)[1]]
            if key=='request':
                request=json.loads(raw)
                for field in ('workspace','tools','permissions','sandbox','approval_policy','network_policy'):
                    request['metadata']['effective_policy'][field]=runner.ROUTE[field]
                raw=json.dumps(request).encode()
            if key=='terminal' and phase=='first':
                raw=json.dumps({'invocation_id':'first','status':'success'}).encode()
            if key=='stdout' and phase=='first':
                raw+=json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'response':'retained'}}).encode()+b'\n'
            (journal/filename).write_bytes(raw)
        (tmp_path/('completed-'+phase+'.result.json')).write_text(json.dumps({'passed':True,'journal':str(journal)}))
    monkeypatch.setattr(runner,'LatticeStore',lambda config:lattice_env['store'])
    runner.admit(tmp_path,'completed')
    admitted=json.loads((tmp_path/'completed.qualification.json').read_bytes())
    assert admitted['receipt']['boundaries']=={'completed':True}
    assert not (tmp_path/'interrupted-first.result.json').exists()
    runner.admit(tmp_path,'completed')
    with lattice_env['store']._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM provider_qualifications').fetchone()[0]==1
