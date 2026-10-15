"""Manually stepped installed conversation qualification through the public API.

Each mutation is saved before dispatch. Re-entry uses the identical idempotency
request; provider failure never triggers another model request. This controller
records observations, not an overall qualification verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from pathlib import Path

import lattice
from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, stamp
from lattice.client import LatticeClient
from lattice.config import DEFAULT_DATA_ROOT


def save(path, value):
    with path.open('xb') as stream:
        stream.write(canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def load(path):
    return json.loads(path.read_bytes())


def binding():
    runtime = (DEFAULT_DATA_ROOT / 'runtime/current').resolve()
    if not Path(lattice.__file__).resolve().is_relative_to(runtime):
        raise RuntimeError('Use the installed runtime Python.')
    return {'runtime': str(runtime), 'manifest_sha256': sha256_bytes((runtime / 'runtime.json').read_bytes()),
            'controller_sha256': sha256_bytes(Path(__file__).read_bytes())}


def prepare(root, project, provider):
    bound = binding()
    route = {'provider': provider, 'harness': 'claude' if provider == 'anthropic' else 'codex',
             'model': 'claude-fable-5-1' if provider == 'anthropic' else 'gpt-6-astra', 'effort': 'high',
             'execution_profile': 'workspace-agent', 'workspace': '/Users/rian/AXISVI',
             'tools': ['Read', 'Glob', 'Grep'] if provider == 'anthropic' else ['shell'],
             'permissions': ['Read', 'Glob', 'Grep'] if provider == 'anthropic' else [],
             'sandbox': 'provider-permissions' if provider == 'anthropic' else 'read-only',
             'approval_policy': 'never', 'network_policy': 'disabled'}
    root.mkdir(parents=True, exist_ok=False)
    (root / 'README.md').write_text('# Installed conversation journey\n\nCommands perform one named action each. Exact mutation requests precede dispatch; rerunning an admitted step replays its receipt. `inspect` discovers the child by its owning task. `interrupt` waits for exposed activity and preserves the redirect before cancellation. A watch timeout leaves daemon work running. Failure is retained and requires inspection, never an automatic model retry. Raw responses remain private. Independent persistence and manifest readers supply the verdict.\n')
    envelope = generated_object(kind='conversation-live-qualification-plan', schema_name='conversation-live-qualification-plan',
        subject='lattice:conversation-qualification:' + root.name, producer_name='conversation-live-qualification', producer_version='1.0.0',
        sources=[{'ref': str(Path(__file__).resolve()), 'revision': None, 'sha256': bound['controller_sha256']},
                 {'ref': bound['runtime'] + '/runtime.json', 'revision': None, 'sha256': bound['manifest_sha256']}])
    envelope['commitment'] = {'fields': ['binding', 'project_id', 'route', 'owning_task_ref', 'token'],
                              'sealed_at': stamp(), 'supersedes': None, 'change_reason': None}
    save(root / 'plan.json', {'generated_object': envelope, 'binding': bound, 'project_id': project, 'route': route,
        'owning_task_ref': 'qualification:' + root.name, 'token': secrets.token_hex(16),
        'proof_limits': ['A bounded real-provider journey is not a reliability estimate or closure receipt.']})
    print(json.dumps({'plan': str(root / 'plan.json')}))


def discover(client, plan):
    children = client.conversation_list(plan['project_id'], owning_task_ref=plan['owning_task_ref'])
    if len(children) != 1:
        raise RuntimeError('Owning-task discovery did not return exactly one child.')
    return children[0]['conversation_id']


def mutate(root, client, plan, step, action, request):
    path = root / (step + '.request.json')
    if path.exists():
        request = load(path)
    else:
        save(path, request)
    result = root / (step + '.receipt.json')
    if result.exists():
        return load(result)
    # The saved request is the only replay authority, including after a lost ACK.
    if action == 'create':
        value = client.conversation_create(request)
    else:
        value = client.conversation_mutate(discover(client, plan), action, request)
    save(result, value)
    return value


def operate(root, step):
    plan = load(root / 'plan.json')
    if plan['binding'] != binding():
        raise RuntimeError('Installed runtime or controller changed.')
    client = LatticeClient.from_environment()
    key = root.name + ':' + step
    saved_action = {'create': 'create', 'first': 'send', 'second': 'send', 'pending': 'send',
                    'interrupt': 'steer', 'continue': 'continue'}.get(step)
    if saved_action and (root / (step + '.request.json')).exists():
        print(json.dumps(mutate(root, client, plan, step, saved_action, {}), ensure_ascii=False))
        return
    if step == 'create':
        value = mutate(root, client, plan, step, 'create', {'project_id': plan['project_id'], 'title': 'Installed ' + plan['route']['model'] + ' continuity',
            'owning_task_ref': plan['owning_task_ref'], 'route': plan['route'], 'recovery_policy': 'portable', 'idempotency_key': key})
    else:
        identity = discover(client, plan)
        state = client.conversation_show(identity)
        request = {'expected_revision': state['revision'], 'idempotency_key': key}
        if step in ('first', 'second', 'pending'):
            ordinal = {'first': 1, 'second': 2, 'pending': 3}[step]
            if not (root / (step + '.request.json')).exists() and (state['pending'] or len(state['rounds']) != ordinal - 1):
                raise RuntimeError('Unexpected history boundary; inspect existing work.')
            if any(r['state'] != 'completed' or r.get('response', '').strip() != plan['token'] for r in state['rounds']):
                raise RuntimeError('Earlier exact-token round did not pass; no automatic retry.')
            prompt = {'first': 'Remember this unpredictable token: ' + plan['token'] + '. Return only that token, with no formatting.',
                      'second': 'Return only the original unpredictable token. Do not read files.',
                      'pending': 'Read /Users/rian/AXISVI/README.md, then inspect its referenced READMEs one at a time and write a long architectural account. Do not mutate files. The operator will interrupt after exposed work.'}[step]
            value = mutate(root, client, plan, step, 'send', {**request, 'prompt': prompt})
        elif step == 'continue':
            preview = client.conversation_preview(identity, policy='portable')
            save(root / ('preview-' + stamp() + '.json'), preview)
            if preview['blockers'] or preview.get('required_confirmations'):
                raise RuntimeError('Recovery requires inspection; retained preview supplies the exact blocker.')
            value = mutate(root, client, plan, step, 'continue', {**request, 'expected_revision': preview['revision'],
                'expected_preview_sha256': preview['preview_sha256'], 'policy': 'portable'})
        elif step == 'interrupt':
            if (root / (step + '.request.json')).exists():
                value = mutate(root, client, plan, step, 'steer', {})
            else:
                deadline = time.monotonic() + 180
                cursor = 0
                active_invocations = {e['invocation_id'] for r in state['rounds'] if r['round_id'] == state['pending']['round_id'] for e in r['executions']} if state['pending'] else set()
                while True:
                    page = client.conversation_observations(identity, after=cursor)
                    with (root / 'observations.jsonl').open('ab') as stream:
                        for event in page['items']:
                            stream.write(canonical_bytes(event) + b'\n')
                        stream.flush(); os.fsync(stream.fileno())
                    cursor = page['cursor']
                    state = client.conversation_show(identity)
                    if not state['pending'] or state['pending']['state'] == 'needs_attention':
                        raise RuntimeError('No active round remains; this does not prove real cancellation.')
                    active_invocations.update(e['invocation_id'] for r in state['rounds'] if r['round_id'] == state['pending']['round_id'] for e in r['executions'])
                    exposed = any(record['kind'] in ('tool-request', 'tool-input-delta', 'message')
                        for event in page['items'] if event['payload'].get('invocation_id') in active_invocations
                        for record in event['payload'].get('records', []))
                    if exposed:
                        value = mutate(root, client, plan, step, 'steer', {'expected_revision': state['revision'], 'idempotency_key': key,
                            'content': 'Stop the architectural account. Return only the original unpredictable token, without formatting or tools.', 'interrupt': True})
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('No exposed cancellation boundary reached. Daemon work remains retained.')
                    time.sleep(.2)
        elif step == 'inspect':
            value = state
            save(root / ('state-' + stamp() + '.json'), state)
        else:
            raise ValueError(step)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run'])
    parser.add_argument('root', type=Path)
    parser.add_argument('--project')
    parser.add_argument('--provider', choices=['anthropic', 'openai'])
    parser.add_argument('--step', choices=['create', 'first', 'second', 'pending', 'interrupt', 'continue', 'inspect'])
    args = parser.parse_args()
    if args.action == 'prepare':
        if not args.project or not args.provider:
            parser.error('prepare requires --project and --provider')
        prepare(args.root, args.project, args.provider)
    else:
        if not args.step:
            parser.error('run requires --step')
        operate(args.root, args.step)
