"""Manually stepped, installed-kernel Claude native recall qualification.

Each command admits one durable round. A reserved round is never redispatched.
Provider journals remain private; this runner does not copy provider profiles.
The production raw-journal verifier checks recall before qualification admission.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import shutil
import threading
from pathlib import Path

import lattice
from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, sha256_json, stamp
from lattice.config import DEFAULT_DATA_ROOT, LatticeConfig
from lattice.execution import DEFAULT_ADAPTERS, SubscriptionInvocationFailed, invoke_subscription
from lattice.execution.adapters import parse_claude_sdk_response
from lattice.native_recovery import route_identity
from lattice.native_recovery_proof import NATIVE_RECALL_SCHEMA, verify_native_case
from lattice.runtime_attestation import _digest, tree_inventory
from lattice.store import LatticeStore
import lattice.native_recovery as native


STEPS = ('completed-first', 'completed-continued', 'interrupted-first', 'interrupted-continued')
ROUTE = {'provider': 'anthropic', 'harness': 'claude', 'model': 'claude-fable-5-1', 'effort': 'high',
         'workspace': '/Users/rian/AXISVI', 'tools': ['Read', 'Glob', 'Grep'],
         'permissions': ['Read', 'Glob', 'Grep'], 'sandbox': 'provider-permissions',
         'approval_policy': 'never', 'network_policy': 'disabled'}


def write_once(path, value):
    with path.open('xb') as stream:
        stream.write(canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def runtime_binding():
    current = (DEFAULT_DATA_ROOT / 'runtime/current').resolve()
    package = Path(lattice.__file__).resolve().parent
    if not (current / 'runtime.json').is_file() or not package.is_relative_to(current):
        raise RuntimeError('Use the installed runtime Python, not the source environment.')
    binary = Path(shutil.which('claude') or '').resolve(strict=True)
    return {'runtime': json.loads((current / 'runtime.json').read_bytes()),
            'runtime_tree_sha256': sha256_json(tree_inventory(package)),
            'binary': str(binary), 'binary_sha256': _digest(binary)[0],
            'runner_sha256': sha256_bytes(Path(__file__).read_bytes())}


def prepare(root):
    binding = runtime_binding()
    root.mkdir(parents=True, exist_ok=False)
    (root / 'README.md').write_text('# Native conversation qualification\n\nEach named command reserves one round before execution. Its journal and result survive replacement of the operator. Inspect a reserved unfinished round; never repeat it. Completion and interruption qualify separate boundaries, with a token absent from all continuation input channels. Raw transcripts remain in this private directory.\n')
    canaries = {boundary: secrets.token_hex(16) for boundary in ('completed', 'interrupted')}
    envelope = generated_object(kind='conversation-native-qualification-plan', schema_name='conversation-native-qualification-plan',
        subject='lattice:native-qualification:' + root.name, producer_name='lattice-native-qualification', producer_version='1.0.0',
        sources=[{'ref': str(Path(__file__).resolve()), 'revision': None, 'sha256': binding['runner_sha256']}])
    envelope['commitment'] = {'fields': ['binding', 'route', 'steps', 'canaries', 'schema'], 'sealed_at': stamp(), 'supersedes': None, 'change_reason': None}
    write_once(root / 'plan.json', {'generated_object': envelope, 'binding': binding, 'route': ROUTE, 'steps': STEPS,
        'canaries': canaries, 'schema': NATIVE_RECALL_SCHEMA, 'timeout_seconds': 180,
        'proof_limits': ['This is a bounded authenticated capability check, not a reliability estimate.',
                         'Only exposed provider computation can be recovered. No external mutations are authorized.']})
    print(json.dumps({'plan': str(root / 'plan.json'), 'remaining': STEPS}))


def checked_plan(root):
    plan = json.loads((root / 'plan.json').read_bytes())
    if plan['binding'] != runtime_binding():
        raise RuntimeError('Installed runtime, executable or qualification runner changed.')
    return plan


def run(root, step):
    plan = checked_plan(root)
    index = STEPS.index(step)
    for earlier in STEPS[:index]:
        if json.loads((root / (earlier + '.result.json')).read_bytes())['passed'] is not True:
            raise RuntimeError('An earlier round did not pass; no automatic retry.')
    boundary, phase = step.split('-')
    continued = phase == 'continued'
    session = None
    if continued:
        previous = json.loads((root / (boundary + '-first.result.json')).read_bytes())
        checkpoint = json.loads((Path(previous['journal']) / 'checkpoint.json').read_bytes())
        session = checkpoint['native_session']['id']
        prompt = 'Recall the original unpredictable token. Return exactly that token as the response string. Do not read files or infer a new token.'
    else:
        prompt = 'Remember this unpredictable token for the next request: ' + plan['canaries'][boundary] + '. '
        prompt += ('Return response="retained".' if boundary == 'completed' else
                   'First read /Users/rian/AXISVI/README.md. Then inspect its referenced README files one at a time and formulate a long architectural account before returning response="retained". The operator will interrupt this synthetic request after exposed activity; do not perform any mutations.')
    request = {'step': step, 'prompt': prompt, 'session': session, 'plan_sha256': sha256_bytes((root / 'plan.json').read_bytes())}
    # Reserving before dispatch means replacement operators cannot purchase a
    # duplicate after lost acknowledgement or an interrupted runner.
    write_once(root / (step + '.started.json'), {**request, 'started_at': stamp()})
    adapter = next(a for a in DEFAULT_ADAPTERS if a.provider_code == 'anthropic').select_model(plan['route']['model'])
    cancel = threading.Event()
    tail = bytearray()
    journal = None
    exposed = False

    def bound(path):
        nonlocal journal
        journal = path
        write_once(root / (step + '.journal.json'), {'journal': str(path), 'request_sha256': sha256_bytes((path / 'request.json').read_bytes())})

    def observe(channel, raw):
        nonlocal exposed
        if channel != 'stdout' or continued or boundary != 'interrupted' or exposed:
            return
        tail.extend(raw)
        while b'\n' in tail:
            line, _, remaining = tail.partition(b'\n')
            tail[:] = remaining
            if not line.strip():
                continue
            event = json.loads(line)
            delta = event.get('event', {}).get('delta', {}) if isinstance(event.get('event'), dict) else {}
            partial = event.get('type')=='stream_event' and isinstance(delta, dict) and delta.get('type') in ('text_delta','input_json_delta') and bool(delta.get('text') or delta.get('partial_json'))
            if event.get('type') == 'assistant' or partial:
                exposed = True
                write_once(root / (step + '.cancel.json'), {'at': stamp(), 'event_sha256': sha256_bytes(bytes(line)), 'reason': 'exposed assistant activity'})
                cancel.set()
                return

    failure = None
    response = None
    try:
        response, journal = invoke_subscription(adapter, prompt=prompt, schema=plan['schema'], runs_dir=root / 'journals',
            subject='conversation-native-qualification:' + step, effort='high', timeout=plan['timeout_seconds'],
            cancel_event=cancel, on_chunk=observe, on_journal=bound,
            execution_profile='native-continuation' if continued else 'workspace-agent',
            session_policy='visible_native_continuation' if continued else 'fresh',
            continuation_mode='resume' if continued else None, native_session_id=session,
            tools=ROUTE['tools'], permissions=ROUTE['permissions'], workspace=ROUTE['workspace'],
            sandbox=ROUTE['sandbox'], approval_policy='never', network_policy='disabled')
    except SubscriptionInvocationFailed as error:
        journal = error.journal or journal
        failure = {'kind': error.kind, 'detail': str(error)}
    expected_interruption = boundary == 'interrupted' and not continued
    passed = ((failure or {}).get('kind') == 'operator_interruption' and exposed) if expected_interruption else failure is None
    if continued and response != {'response': plan['canaries'][boundary]}:
        passed = False
    write_once(root / (step + '.result.json'), {'at': stamp(), 'passed': passed, 'journal': str(journal) if journal else None,
        'response': response, 'failure': failure, 'expected_interruption': expected_interruption, 'exposed': exposed})
    print(json.dumps({'step': step, 'passed': passed, 'journal': str(journal), 'failure': failure}))
    if not passed:
        raise RuntimeError('Qualification stopped; inspect retained evidence before any further provider request.')


def admit(root, selected_boundary):
    plan = checked_plan(root)
    store = LatticeStore(LatticeConfig.from_environment())
    adapter = next(a for a in DEFAULT_ADAPTERS if a.provider_code == 'anthropic').select_model(plan['route']['model'])
    evidence_refs = {}
    negatives = []
    route = {**plan['route'], 'provider_profile_id': adapter.profile_id}
    for boundary in (selected_boundary,):
        proof = {'canary': plan['canaries'][boundary]}
        for phase, name in (('first', 'first'), ('continued', 'continued')):
            result = json.loads((root / (boundary + '-' + phase + '.result.json')).read_bytes())
            if result['passed'] is not True:
                raise RuntimeError('A required native boundary is unresolved.')
            journal = Path(result['journal'])
            proof[name] = {key: 'lattice:blob/' + store.blobs.put((journal / filename).read_bytes()) for key, filename in
                           [('request', 'request.json'), ('prompt', 'prompt.txt'), ('stdout', 'stdout.log'), ('terminal', 'terminal.json'), ('checkpoint', 'checkpoint.json')]}
        verify_native_case(store, proof, route, plan['binding']['binary_sha256'], boundary)
        false = copy.deepcopy(proof)
        false['canary'] = ('0' if proof['canary'][0]!='0' else '1') + proof['canary'][1:]
        try:
            verify_native_case(store, false, route, plan['binding']['binary_sha256'], boundary)
        except ValueError as error:
            negatives.append({'boundary': boundary, 'mutation': 'wrong canary', 'refusal': str(error)})
        else:
            raise RuntimeError('Independent verifier accepted a false recall proof.')
        evidence_refs[boundary] = 'lattice:blob/' + store.blobs.put(canonical_bytes(proof))
    envelope = generated_object(kind='conversation-native-qualification', schema_name='conversation-native-qualification',
        subject='lattice:native-qualification:' + root.name + ':' + selected_boundary, role='snapshot', producer_name='lattice-native-qualification', producer_version='1.0.0',
        sources=[{'ref': str(root / 'plan.json'), 'revision': None, 'sha256': sha256_bytes((root / 'plan.json').read_bytes())},
                 *[{'ref': ref, 'revision': None, 'sha256': ref.rsplit('/', 1)[1]} for ref in evidence_refs.values()]])
    receipt = {'generated_object': envelope, 'contract_version': 'ConversationRecoveryQualification/1.0', 'route_sha256': route_identity(route),
        'native_verifier_sha256': _digest(Path(native.__file__))[0], 'runtime_tree_sha256': plan['binding']['runtime_tree_sha256'],
        'authenticated': True, 'negative_controls_passed': True, 'boundaries': {selected_boundary:True},
        'evidence_refs': evidence_refs, 'negative_controls': negatives, 'binding': plan['binding']}
    intent = root / (selected_boundary + '.qualification-intent.json')
    if not intent.exists():
        write_once(intent, receipt)
    receipt = json.loads(intent.read_bytes())
    admitted = store.record_provider_qualification(adapter.profile_id, plan['binding']['binary_sha256'], receipt, state='passed')
    destination = root / (selected_boundary + '.qualification.json')
    if not destination.exists():
        write_once(destination, admitted)
    print(json.dumps({'qualification_id': admitted['qualification_id'], 'boundaries': receipt['boundaries']}))


def reconcile(root, step):
    """Finish a lost operator acknowledgement from the existing kernel journal."""
    plan = checked_plan(root)
    result_path = root / (step + '.result.json')
    if result_path.exists():
        print(json.dumps({'step':step,'retained':json.loads(result_path.read_bytes())['passed']}))
        return
    intent = json.loads((root / (step + '.started.json')).read_bytes())
    bound = json.loads((root / (step + '.journal.json')).read_bytes())
    journal = Path(bound['journal'])
    raw_request = (journal / 'request.json').read_bytes()
    request = json.loads(raw_request)
    if sha256_bytes(raw_request)!=bound['request_sha256'] or request['prompt_sha256']!=sha256_bytes(intent['prompt'].encode()):
        raise RuntimeError('The reserved request differs from the retained kernel journal.')
    terminal = json.loads((journal / 'terminal.json').read_bytes())
    response = None
    interrupted = step=='interrupted-first'
    failure = None if terminal['status']=='success' else {'kind':terminal.get('failure_kind'),'detail':'Retained kernel terminal'}
    passed = terminal['status']=='success'
    if passed:
        response = parse_claude_sdk_response((journal / 'stdout.log').read_text(),plan['schema'],expected_model=plan['route']['model'])
        if step.endswith('-continued'):
            passed = response=={'response':plan['canaries'][step.split('-')[0]]}
    if interrupted:
        cancel = json.loads((root / (step + '.cancel.json')).read_bytes())
        lines = (journal / 'stdout.log').read_bytes().split(b'\n')
        exposed = any(sha256_bytes(line)==cancel['event_sha256'] for line in lines)
        passed = terminal['status']=='interrupted' and terminal.get('failure_kind')=='operator_interruption' and exposed
    else:
        exposed = False
    write_once(result_path,{'at':stamp(),'passed':passed,'journal':str(journal),'response':response,'failure':failure,'expected_interruption':interrupted,'exposed':exposed,'reconciled':True})
    print(json.dumps({'step':step,'passed':passed,'reconciled':True,'redispatched':False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'admit', 'reconcile'))
    parser.add_argument('root', type=Path)
    parser.add_argument('--step', choices=STEPS)
    parser.add_argument('--boundary',choices=('completed','interrupted'))
    args = parser.parse_args()
    if args.action in ('run','reconcile') and args.step is None:
        parser.error('run and reconcile require --step')
    if args.action=='admit' and args.boundary is None:
        parser.error('admit requires --boundary')
    {'prepare': lambda: prepare(args.root), 'run': lambda: run(args.root, args.step), 'admit': lambda: admit(args.root,args.boundary), 'reconcile':lambda:reconcile(args.root,args.step)}[args.action]()
