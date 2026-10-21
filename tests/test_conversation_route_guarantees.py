"""Independent process-input and exposed-model oracles for exact recovery routes."""
import json
from types import SimpleNamespace

import pytest
from lattice.execution import adapters


SCHEMA = {'type': 'object', 'properties': {'response': {'type': 'string'}}, 'required': ['response'], 'additionalProperties': False}


def selected():
    return next(a for a in adapters.DEFAULT_ADAPTERS if a.provider_code == 'anthropic').select_model('claude-fable-5-1')


def fixture(monkeypatch, transcript, inspect=None):
    raw = ''.join(json.dumps(item) + '\n' for item in transcript)
    def process(command, **kwargs):
        if inspect:
            inspect(command, kwargs)
        kwargs['stdout_journal'].write_text(raw)
        return SimpleNamespace(returncode=0, stdout=raw, stderr='')
    monkeypatch.setattr(adapters.shutil, 'which', lambda _: '/fixture/claude')
    monkeypatch.setattr(adapters, '_cli_version', lambda _: 'fixture')
    monkeypatch.setattr(adapters, '_binary_sha256', lambda _: 'f' * 64)
    monkeypatch.setattr(adapters, 'run_bounded', process)


def invoke(root):
    return adapters.invoke_subscription(selected(), prompt='Preserve exact supplied history.', schema=SCHEMA, runs_dir=root / 'runs', subject='exact-route')


def events(model='claude-fable-5-1'):
    return [{'type': 'system', 'subtype': 'init', 'model': 'claude-fable-5-1', 'session_id': 'session-one'},
            {'type': 'assistant', 'message': {'model': model, 'content': [{'type': 'text', 'text': 'observed answer'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'response': 'retained'}}]


@pytest.mark.oracle
def test_provider_cannot_compact_or_select_a_fallback_model(tmp_path, monkeypatch):
    def oracle(command, kwargs):
        assert '--include-partial-messages' in command
        assert kwargs['env']['DISABLE_COMPACT'] == '1'
        assert kwargs['env']['CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK'] == '1'
        settings = json.loads(command[command.index('--settings') + 1])
        assert settings['availableModels'] == ['claude-fable-5-1']
        assert settings['autoCompactEnabled'] is False
    fixture(monkeypatch, events(), oracle)
    assert invoke(tmp_path)[0] == {'response': 'retained'}


@pytest.mark.oracle
@pytest.mark.parametrize('fault', ['model-switch', 'compaction'])
def test_valid_terminal_payload_cannot_hide_changed_execution(tmp_path, monkeypatch, fault):
    transcript = events('claude-opus-5' if fault == 'model-switch' else 'claude-fable-5-1')
    if fault == 'compaction':
        transcript.insert(1, {'type': 'system', 'subtype': 'compact_boundary', 'compact_metadata': {'trigger': 'auto'}})
    fixture(monkeypatch, transcript)
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        invoke(tmp_path)
    assert failure.value.kind == 'policy_refusal'
    assert fault.split('-')[0] in str(failure.value).lower()
    terminal = json.loads((failure.value.journal / 'terminal.json').read_bytes())
    assert terminal['status'] == 'failed'
    assert (failure.value.journal / 'stdout.log').read_text() == ''.join(json.dumps(item) + '\n' for item in transcript)


@pytest.mark.regression
def test_marked_claude_synthetic_error_is_not_a_model_switch(tmp_path, monkeypatch):
    from lattice.observations import normalize
    diagnostic = {'type': 'assistant', 'is_api_error_message': True, 'error': 'server_error',
                  'message': {'model': '<synthetic>', 'content': [{'type': 'text', 'text': 'API Error: ENOTFOUND'}]}}
    records = normalize(diagnostic)
    assert not any(record['kind'] == 'model' for record in records)
    assert any(record['kind'] == 'failure' for record in records)
    assert any(record.get('text') == 'API Error: ENOTFOUND' for record in records)
    # A recovered CLI error message does not change the identity of later
    # actual model output. The terminal contract still decides success.
    transcript = events()
    transcript.insert(1, diagnostic)
    fixture(monkeypatch, transcript)
    assert invoke(tmp_path)[0] == {'response': 'retained'}


@pytest.mark.oracle
@pytest.mark.parametrize('model,marked', [('<synthetic>', False), ('claude-opus-5', True)])
def test_synthetic_error_exception_cannot_hide_unmarked_or_real_model_switch(tmp_path, monkeypatch, model, marked):
    from lattice.observations import normalize
    diagnostic = {'type': 'assistant', 'is_api_error_message': marked, 'error': 'server_error',
                  'message': {'model': model, 'content': [{'type': 'text', 'text': 'API Error'}]}}
    assert any(record['kind'] == 'model' for record in normalize(diagnostic))
    transcript = events()
    transcript.insert(1, diagnostic)
    fixture(monkeypatch, transcript)
    with pytest.raises(adapters.SubscriptionInvocationFailed) as failure:
        invoke(tmp_path)
    assert failure.value.kind == 'policy_refusal'
