"""The controller must never convert lost acknowledgements into new work."""
import importlib.util
from pathlib import Path

import pytest


path = Path(__file__).with_name('qualify_live_conversation.py')
spec = importlib.util.spec_from_file_location('live_conversation_controller', path)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def test_lost_ack_replays_exact_saved_preview_before_reading_changed_state(tmp_path, monkeypatch):
    bound = {'fixture': True}
    controller.save(tmp_path / 'plan.json', {'binding': bound, 'project_id': 'project', 'owning_task_ref': 'task'})
    original = {'expected_revision': 3, 'idempotency_key': 'original', 'expected_preview_sha256': 'a' * 64, 'policy': 'portable'}
    controller.save(tmp_path / 'continue.request.json', original)
    calls = []
    class Client:
        def conversation_list(self, project, **options):
            assert project == 'project' and options['owning_task_ref'] == 'task'
            return [{'conversation_id': 'child'}]
        def conversation_mutate(self, identity, action, request):
            calls.append((identity, action, request))
            return {'revision': 4, 'invocation_id': 'original-invocation'}
        def conversation_show(self, identity):
            raise AssertionError('A completed replay must not depend on the current round.')
        def conversation_preview(self, identity, **options):
            raise AssertionError('A completed replay must not compose a new recovery.')
    monkeypatch.setattr(controller, 'binding', lambda: bound)
    monkeypatch.setattr(controller.LatticeClient, 'from_environment', lambda: Client())
    controller.operate(tmp_path, 'continue')
    controller.operate(tmp_path, 'continue')
    assert calls == [('child', 'continue', original)]


def test_mutation_is_durable_before_dispatch_and_lost_ack_uses_same_bytes(tmp_path):
    request = {'expected_revision': 1, 'idempotency_key': 'fixed', 'prompt': 'immutable request'}
    calls = []
    class Client:
        def conversation_list(self, *args, **kwargs):
            return [{'conversation_id': 'child'}]
        def conversation_mutate(self, identity, action, value):
            assert controller.load(tmp_path / 'first.request.json') == request
            calls.append(value)
            if len(calls) == 1:
                raise OSError('Lost acknowledgement after admission')
            return {'round_id': 'same-round'}
    plan = {'project_id': 'project', 'owning_task_ref': 'task'}
    with pytest.raises(OSError):
        controller.mutate(tmp_path, Client(), plan, 'first', 'send', request)
    result = controller.mutate(tmp_path, Client(), plan, 'first', 'send', {'prompt': 'changed'})
    assert result == {'round_id': 'same-round'} and calls == [request, request]


def test_source_environment_cannot_dispatch_qualification():
    with pytest.raises(RuntimeError, match='installed runtime'):
        controller.binding()


def test_cancellation_requires_current_round_activity_not_earlier_messages(tmp_path, monkeypatch):
    bound = {'fixture': True}
    controller.save(tmp_path / 'plan.json', {'binding': bound, 'project_id': 'project', 'owning_task_ref': 'task'})
    pages = []
    calls = []
    class Client:
        def conversation_list(self, *args, **kwargs):
            return [{'conversation_id': 'child'}]
        def conversation_show(self, identity):
            return {'revision': 8, 'pending': {'round_id': 'third', 'state': 'active'},
                    'rounds': [{'round_id': 'first', 'executions': [{'invocation_id': 'old'}]},
                               {'round_id': 'third', 'executions': [{'invocation_id': 'active'}]}]}
        def conversation_observations(self, identity, after):
            pages.append(after)
            return {'cursor': len(pages), 'items': [{'payload': {'invocation_id': 'old' if len(pages) == 1 else 'active',
                    'records': [{'kind': 'message', 'role': 'assistant', 'text': 'exposed'}]}}]}
        def conversation_mutate(self, identity, action, request):
            assert len(pages) == 2
            assert action == 'steer' and request['interrupt'] is True
            assert controller.load(tmp_path / 'interrupt.request.json') == request
            calls.append(request)
            return {'steering_id': 'redirect'}
    monkeypatch.setattr(controller, 'binding', lambda: bound)
    monkeypatch.setattr(controller.LatticeClient, 'from_environment', lambda: Client())
    monkeypatch.setattr(controller.time, 'sleep', lambda duration: None)
    controller.operate(tmp_path, 'interrupt')
    assert len(calls) == 1 and pages == [0, 1]
