"""Fresh-operator journeys through the authenticated public conversation API."""
import json
import threading
import pytest
from fastapi.testclient import TestClient
from lattice.server import create_app
from lattice.security import ensure_cli_token
from lattice.execution import SubscriptionInvocationFailed
from test_supervisor import fake_journal


@pytest.mark.contract
def test_conversation_api_requires_authentication(lattice_env):
    app=create_app(lattice_env['config'])
    with TestClient(app,base_url='http://127.0.0.1:9010') as client:
        assert client.get('/api/v1/conversations',params={'project_id':lattice_env['project']['project_id']}).status_code==403


@pytest.mark.oracle
def test_replacement_operator_discovers_interrupts_and_continues(lattice_env,tmp_path,monkeypatch):
    started=threading.Event()
    terminal=threading.Event()
    actions=[]
    def provider(adapter,**kwargs):
        actions.append(kwargs['prompt'])
        directory=tmp_path/f'provider-{len(actions)}';directory.mkdir()
        journal=fake_journal(directory)
        if len(actions)==1:
            kwargs['on_chunk']('stdout',b'{"type":"assistant","message":{"content":"retained partial"}}\n')
            started.set()
            assert kwargs['cancel_event'].wait(10),'operator did not interrupt the fixture'
            terminal.set()
            raise SubscriptionInvocationFailed('operator_interruption','stopped',journal=journal)
        terminal.set()
        return {'response':'continued answer','artifacts':[]},journal
    monkeypatch.setattr('lattice.supervisor.invoke_subscription',provider)
    app=create_app(lattice_env['config'])
    token=ensure_cli_token(lattice_env['config'].operator_token)
    headers={'Authorization':f'Bearer {token}'}
    with TestClient(app,base_url='http://127.0.0.1:9010',headers=headers) as client:
        created=client.post('/api/v1/conversations',json={'project_id':lattice_env['project']['project_id'],'title':'recoverable child','owning_task_ref':'task:replaceable','route':lattice_env['route'].model_dump(mode='json'),'idempotency_key':'create-api-child'})
        assert created.status_code==200,created.text
        identity=created.json()['conversation_id'];url=f'/api/v1/conversations/{identity}'
        send={'prompt':'unfinished request','expected_revision':1,'idempotency_key':'request-api-one'}
        admitted=client.post(url+'/send',json=send)
        assert admitted.status_code==200,admitted.text
        assert started.wait(10)
        assert client.post(url+'/send',json=send).json()==admitted.json()
        listed=client.get('/api/v1/conversations',params={'project_id':lattice_env['project']['project_id'],'owning_task_ref':'task:replaceable'}).json()
        assert listed[0]['conversation_id']==identity
        state=client.get(url).json()
        stopped=client.post(url+'/interrupt',json={'expected_revision':state['revision'],'idempotency_key':'stop-api-child'})
        assert stopped.status_code==200,stopped.text
        assert terminal.wait(10)
        # Join the daemon-owned attempt through its loop, not an arbitrary sleep.
        async def settle():
            import asyncio
            await asyncio.gather(*list(app.state.supervisor._tasks.values()))
        client.portal.call(settle)
        preview=client.post(url+'/recovery-preview',json={}).json()
        assert preview['state']=='needs_attention'
        assert 'retained partial' in preview['history']['unfinished_observations'][0]['content']
        request={'expected_revision':preview['revision'],'expected_preview_sha256':preview['preview_sha256'],'idempotency_key':'continue-api-child'}
        resumed=client.post(url+'/continue',json=request)
        assert resumed.status_code==200,resumed.text
        client.portal.call(settle)
        assert client.post(url+'/continue',json=request).json()==resumed.json()
        final=client.get(url).json()
        assert final['rounds'][0]['response']=='continued answer'
        assert len(actions)==2
        assert 'retained partial' in actions[1]
        observed=client.get(url+'/observations',params={'after':admitted.json()['event_cursor']}).json()
        assert any(e['event_type']=='attempt.observation' for e in observed['items'])


@pytest.mark.contract
def test_cli_and_python_client_use_the_authoritative_conversation_api(lattice_env,monkeypatch,capsys):
    from lattice.client import LatticeClient
    from lattice.cli import parser
    from lattice.conversation_cli import command
    app=create_app(lattice_env['config'])
    token=ensure_cli_token(lattice_env['config'].operator_token)
    with TestClient(app,base_url='http://127.0.0.1:9010',headers={'Authorization':f'Bearer {token}'}) as http:
        # Substitute only the socket transport; real CLI parsing, client routing,
        # authenticated endpoints, revision guards and SQLite remain in the path.
        def request(_client,method,path,payload=None,**kwargs):
            response=http.request(method,path,json=payload)
            response.raise_for_status()
            return response.json()
        monkeypatch.setattr(LatticeClient,'_request',request)
        monkeypatch.setattr(LatticeClient,'from_environment',classmethod(lambda cls:cls('http://127.0.0.1:9010',token,'127.0.0.1:9010')))
        def cli(*args):
            result=command(parser().parse_args(['conversation',*args,'--json']))
            assert result==0
            return json.loads(capsys.readouterr().out)
        created=cli('create','--project',lattice_env['project']['project_id'],'--owner','task:cli','--title','CLI child','--provider','openai','--model','gpt-5.6-sol')
        identity=created['conversation_id']
        found=cli('list','--project',lattice_env['project']['project_id'],'--owner','task:cli')
        assert found[0]['conversation_id']==identity
        # Hold only daemon dispatch, so admission and pre-dispatch cancellation
        # can be inspected without paying for a real model call.
        monkeypatch.setattr(app.state.supervisor,'kick_conversations',lambda:None)
        sent=cli('send',identity,'request from CLI','--idempotency-key','cli-admission-key','--revision','1')
        state=cli('show',identity)
        assert state['rounds'][0]['request']['prompt']=='request from CLI'
        assert state['pending']['state']=='queued'
        assert cli('send',identity,'request from CLI','--idempotency-key','cli-admission-key','--revision','1')==sent
        cli('steer',identity,'retained correction')
        cli('interrupt',identity)
        preview=cli('continue',identity,'--preview')
        assert preview['state']=='needs_attention'
        assert preview['steering'][0]['content']=='retained correction'
        assert preview['mode']=='portable'
        cli('withdraw',identity)
        assert cli('show',identity)['rounds'][0]['state']=='withdrawn'
