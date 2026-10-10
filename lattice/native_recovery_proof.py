"""Independent raw-journal checks for native continuation qualification.

A provider must recover an unpredictable canary present only in the original
request. Declared success flags and a session ID alone cannot qualify recovery.
The validator reads immutable blobs, not the producer's rendered status.
"""
from __future__ import annotations
import re
from .canonical import sha256_bytes, strict_json_loads


def _blob(store, reference):
    if not isinstance(reference,str) or re.fullmatch(r'lattice:blob/[0-9a-f]{64}',reference) is None:
        raise ValueError('native qualification requires an exact blob reference')
    raw=store.blobs.get(reference.rsplit('/',1)[1])
    if sha256_bytes(raw)!=reference.rsplit('/',1)[1]:
        raise ValueError('native qualification evidence bytes changed')
    return raw


def _journal(store, refs, route, binary_sha256):
    raw={name:_blob(store,refs[name]) for name in ('request','prompt','stdout','terminal','checkpoint')}
    request=strict_json_loads(raw['request'])
    terminal=strict_json_loads(raw['terminal'])
    checkpoint=strict_json_loads(raw['checkpoint'])
    metadata=request['metadata']
    policy=metadata['effective_policy']
    if request['model']!=route['model'] or request['effort']!=route['effort'] or metadata['binary_sha256']!=binary_sha256:
        raise ValueError('native qualification model, effort or executable differs')
    if request['prompt_sha256']!=sha256_bytes(raw['prompt']):
        raise ValueError('native qualification prompt binding differs')
    for key in ('tools','permissions','workspace','sandbox','approval_policy','network_policy'):
        if policy[key]!=route[key]:
            raise ValueError('native qualification changed '+key)
    messages=[strict_json_loads(line) for line in raw['stdout'].split(b'\n') if line.strip()]
    if any(not isinstance(item,dict) for item in messages):
        raise ValueError('native qualification stream contains a nonobject')
    identities={(m.get('session_id'),m.get('model')) for m in messages if m.get('type')=='system' and m.get('subtype')=='init'}
    session=checkpoint.get('native_session',{}).get('id')
    if not session or identities!={(session,route['model'])}:
        raise ValueError('native qualification lacks unambiguous observed session/model identity')
    if terminal['invocation_id']!=request['invocation_id']:
        raise ValueError('native qualification joined unrelated terminal and request')
    return request,policy,messages,terminal,session,raw


def verify_native_case(store, proof, route, binary_sha256, boundary):
    if route['provider']!='anthropic' or boundary not in ('completed','interrupted'):
        raise ValueError('this provider boundary is not qualified by this verifier')
    nonce=proof['canary']
    if not isinstance(nonce,str) or re.fullmatch(r'[0-9a-f]{32}',nonce) is None:
        raise ValueError('native canary must be a fixed unpredictable 128-bit token')
    first=_journal(store,proof['first'],route,binary_sha256)
    resumed=_journal(store,proof['continued'],route,binary_sha256)
    if first[0]['invocation_id']==resumed[0]['invocation_id']:
        raise ValueError('native qualification reused one execution as two rounds')
    if nonce.encode() not in first[5]['prompt'] or nonce.encode() in resumed[5]['prompt']:
        raise ValueError('native canary was absent initially or leaked into the continuation request')
    # No alternate prompt or system channel may supply the answer to the recall
    # test. This deliberately bounds qualification to the ordinary route used.
    for item in (first,resumed):
        if item[0].get('provider_prompt_sha256') or item[0].get('system_prompt_sha256'):
            raise ValueError('native canary qualification has an uninspected alternate input channel')
    if first[1]['execution_profile']!='workspace-agent' or first[1].get('native_session_id'):
        raise ValueError('native qualification must start a fresh workspace session')
    if resumed[1]['execution_profile']!='native-continuation' or resumed[1]['continuation_mode']!='resume':
        raise ValueError('native qualification did not exercise resume')
    if first[4]!=resumed[4] or resumed[1]['native_session_id']!=first[4]:
        raise ValueError('native qualification resumed another session')
    if boundary=='completed':
        if first[3]['status']!='success':
            raise ValueError('ordinary-round qualification lacks successful first completion')
        _response(first[2])
    elif first[3]['status']!='interrupted' or first[3].get('failure_kind')!='operator_interruption':
        raise ValueError('interrupted qualification never exercised cancellation')
    if not any(m.get('type')=='assistant' for m in first[2]):
        raise ValueError('native qualification cancelled before exposed model activity')
    if resumed[3]['status']!='success':
        raise ValueError('native continuation did not complete')
    payload=_response(resumed[2])
    if payload.get('response')!=nonce:
        raise ValueError('native continuation did not recover the original canary exactly')
    return {'boundary':boundary,'session_id':first[4],'first':first[0]['invocation_id'],'continued':resumed[0]['invocation_id']}


def _response(messages):
    terminals=[m for m in messages if m.get('type')=='result']
    if len(terminals)!=1:
        raise ValueError('native qualification requires exactly one terminal result')
    terminal=terminals[0]
    if terminal.get('is_error') is not False or terminal.get('subtype')!='success':
        raise ValueError('native terminal was not successful')
    if terminal.get('terminal_reason') not in (None,'completed') or terminal.get('origin') not in (None,{'kind':'human'}):
        raise ValueError('native qualification accepted an interrupted or injected terminal')
    if any(m.get('type')!='system' or m.get('subtype')!='session_state_changed' for m in messages[messages.index(terminal)+1:]):
        raise ValueError('native qualification stream continued after its terminal result')
    payload=terminal.get('structured_output')
    if payload is None:
        payload=strict_json_loads(terminal['result'])
    if not isinstance(payload,dict):
        raise ValueError('native terminal did not contain its contracted response')
    return payload
