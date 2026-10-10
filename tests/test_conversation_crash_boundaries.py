"""SIGKILL boundary oracles inspect SQLite independently of status projections."""
from contextlib import contextmanager
import json
import multiprocessing
import os
import signal

import pytest
from lattice import LatticeStore
from lattice.conversation_contracts import ConversationSend
from lattice.conversations import ConversationStore
from lattice.conversation_service import ConversationService
from lattice.observations import ObservationStore
from test_recoverable_conversations import create, admit
from test_conversation_recovery import attempt


RAW = b'{"type":"assistant","message":{"content":"persisted before index"}}\n'


def killed_unit(config, identity, operation, boundary, attempt_id):
    store = LatticeStore(config)
    conversations = ConversationStore(store)
    original = store.transaction

    @contextmanager
    def interrupted_transaction():
        with original() as conn:
            yield conn
            if boundary == 'before_commit':
                os.kill(os.getpid(), signal.SIGKILL)
        if boundary == 'after_commit':
            os.kill(os.getpid(), signal.SIGKILL)

    store.transaction = interrupted_transaction
    if operation == 'send':
        conversations.send(identity, ConversationSend(prompt='durable pending request', expected_revision=1, idempotency_key='kill-boundary-send'))
    elif operation == 'seal':
        ConversationService(store).seal(identity)
    elif operation == 'complete':
        store.finish_successful_attempt(attempt_id, response='accepted before lost acknowledgement', artifacts=[], journal_path='oracle', runtime_receipt={'fixture':'process-boundary'})
    elif operation == 'observe':
        ObservationStore(store).append(attempt_id, 'stdout', RAW)
    raise AssertionError('failure injection did not reach its boundary')


@pytest.mark.oracle
@pytest.mark.parametrize('operation', ['send','seal','complete','observe'])
@pytest.mark.parametrize('boundary', ['before_commit','after_commit'])
def test_process_death_preserves_atomic_boundaries(lattice_env, operation, boundary):
    env = lattice_env
    conversations, identity = create(env)
    attempt_id = None
    if operation != 'send':
        admit(env, conversations, identity, 1)
    if operation in ('complete','observe'):
        attempt_id = attempt(env, ConversationService(env['store']).seal(identity))['attempt_id']
    process = multiprocessing.get_context('spawn').Process(target=killed_unit, args=(env['config'],identity,operation,boundary,attempt_id))
    process.start()
    process.join(20)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail('boundary worker did not reach its named persistence boundary')
    assert process.exitcode == -signal.SIGKILL
    committed = boundary == 'after_commit'
    # Read base tables and blobs, without asking the implementation to calculate
    # the expected history or interpret whether its transaction succeeded.
    with env['store']._connect() as conn:
        if operation == 'send':
            rows = conn.execute('SELECT request_json FROM conversation_rounds').fetchall()
            assert len(rows) == int(committed)
            if committed:
                assert json.loads(rows[0][0])['prompt'] == 'durable pending request'
        elif operation == 'seal':
            for table in ('invocations','invocation_batches','conversation_executions'):
                assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == int(committed)
            assert conn.execute('SELECT state FROM conversation_rounds').fetchone()[0] == ('active' if committed else 'queued')
        elif operation == 'complete':
            row = conn.execute('SELECT state,accepted_response_id,checkpoint_id FROM conversation_rounds').fetchone()
            assert row[0] == ('completed' if committed else 'active')
            assert bool(row[1]) == bool(row[2]) == committed
            assert conn.execute('SELECT COUNT(*) FROM conversation_checkpoints').fetchone()[0] == int(committed)
            if committed:
                digest = conn.execute('SELECT content_sha256 FROM context_blocks WHERE block_id=?',(row[1],)).fetchone()[0]
                assert env['store'].blobs.get(digest) == b'accepted before lost acknowledgement'
        else:
            rows = conn.execute("SELECT payload_json FROM events WHERE event_type='attempt.chunk'").fetchall()
            assert len(rows) == int(committed)
            assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='attempt.observation'").fetchone()[0] == 0
            if committed:
                assert env['store'].blobs.get(json.loads(rows[0][0])['sha256']) == RAW
    replacement = LatticeStore(env['config'])
    if operation == 'send' and committed:
        replay = ConversationStore(replacement).send(identity, ConversationSend(prompt='durable pending request',expected_revision=1,idempotency_key='kill-boundary-send'))
        assert replay['round_id']
        with replacement._connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM conversation_rounds').fetchone()[0] == 1
    if operation == 'observe' and committed:
        ObservationStore(replacement).reindex(attempt_id,'stdout')
        with replacement._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='attempt.observation'").fetchone()[0] == 1
