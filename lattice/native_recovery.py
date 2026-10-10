"""Separately qualified native continuation evidence for conversation rounds.

Provider state is referenced and checked in place, never copied into backups.
An observed session ID is necessary but not sufficient: the exact native head,
workspace, executable, model, authority and qualified boundary must still match.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .canonical import canonical_bytes, sha256_bytes, sha256_json, strict_json_loads
from .runtime_attestation import _digest, tree_inventory


AUTHORITY_KEYS = ('provider','model','harness','effort','tools','permissions','workspace','sandbox','approval_policy','network_policy')


def route_identity(route):
    return sha256_json({key:route.get(key) for key in AUTHORITY_KEYS})


def _claude_head(path: Path, session_id: str, workspace: str):
    before = path.stat()
    # Bound this inspection by the execution kernel's existing output limit.
    if before.st_size > 64*1024*1024:
        raise ValueError('native transcript exceeds qualified inspection limit')
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
        raise ValueError('native session changed during head inspection')
    messages = []
    for line in raw.split(b'\n'):
        if not line.strip():
            continue
        value = strict_json_loads(line)
        if not isinstance(value,dict):
            raise ValueError('native session has a nonobject record')
        if value.get('sessionId') not in (None, session_id):
            raise ValueError('native session contains a different session identity')
        if value.get('type') in ('user','assistant'):
            if value.get('sessionId')!=session_id or value.get('isSidechain') is not False or not value.get('uuid'):
                raise ValueError('native conversation head is ambiguous')
            if Path(value.get('cwd','')).resolve()!=Path(workspace).resolve():
                raise ValueError('native workspace differs from the admitted route')
            messages.append(value)
    if not messages:
        raise ValueError('native session has no recoverable conversation messages')
    return {'head_uuid':messages[-1]['uuid'],'history_sha256':sha256_bytes(canonical_bytes(messages)),'message_count':len(messages)}


def capture_boundary(store, invocation, runtime_receipt):
    route=invocation['route']
    if route['provider']!='anthropic' or route['execution_profile'] not in ('workspace-agent','native-continuation'):
        return {'eligible_evidence':False,'reason':'This route did not create qualified persistent Claude state.'}
    session=(runtime_receipt.get('native_session') or {}).get('id')
    if not isinstance(session,str) or re.fullmatch(r'[0-9a-fA-F-]{36}',session) is None:
        return {'eligible_evidence':False,'reason':'No observed native session UUID.'}
    try:
        with store._connect() as conn:
            observations = [json.loads(row[0]) for row in conn.execute("SELECT e.payload_json FROM events e JOIN attempts a ON a.attempt_id=e.subject_id WHERE a.invocation_id=? AND e.event_type='attempt.observation' ORDER BY e.seq", (invocation['invocation_id'],))]
        identities = {(record.get('session_id'),record.get('model')) for event in observations for record in event['records'] if record['kind']=='session'}
        if identities != {(session,route['model'])}:
            raise ValueError('Exposed native session/model identity is absent, ambiguous, or differs from the exact route.')
        # Claude's local project-session layout is checked by live qualification;
        # use only an exact observed UUID, never recursive profile copying.
        root=Path.home()/'.claude'/'projects'
        paths=list(root.glob('*/'+session+'.jsonl'))
        if len(paths)!=1 or paths[0].is_symlink():
            raise ValueError('native session path is absent or ambiguous')
        head=_claude_head(paths[0],session,route['workspace'])
        binary=Path(runtime_receipt['binary']).resolve(strict=True)
        return {'eligible_evidence':True,'session_id':session,'path':str(paths[0].resolve()),
                'data_root':str(store.config.data_root.resolve()),'route_sha256':route_identity(route),
                'binary':str(binary),'binary_sha256':_digest(binary)[0],**head}
    except (OSError,ValueError,KeyError,TypeError) as error:
        return {'eligible_evidence':False,'reason':str(error)}


def qualification(store, route, binary_sha256, boundary):
    """Only a live, version-bound recovery qualification can authorize this mode."""
    with store._connect() as conn:
        rows=conn.execute("SELECT receipt_json FROM provider_qualifications WHERE profile_id=? AND binary_sha256=? AND state='passed' ORDER BY created_at DESC", (route['provider_profile_id'],binary_sha256)).fetchall()
    code_sha256=_digest(Path(__file__))[0]
    for row in rows:
        value=json.loads(row[0])
        if (value.get('contract_version')=='ConversationRecoveryQualification/1.0'
                and value.get('route_sha256')==route_identity(route)
                and value.get('native_verifier_sha256')==code_sha256
                and value.get('authenticated') is True
                and value.get('negative_controls_passed') is True
                and value.get('boundaries',{}).get(boundary) is True
                and value.get('runtime_tree_sha256')==sha256_json(tree_inventory(Path(__file__).parent))
                and value.get('evidence_refs')):
            from .native_recovery_proof import verify_native_case
            try:
                proof_ref=value['evidence_refs'][boundary]
                if not isinstance(proof_ref,str) or re.fullmatch(r'lattice:blob/[0-9a-f]{64}',proof_ref) is None:
                    continue
                proof_raw=store.blobs.get(proof_ref.rsplit('/',1)[1])
                if sha256_bytes(proof_raw)!=proof_ref.rsplit('/',1)[1]:
                    continue
                verify_native_case(store,strict_json_loads(proof_raw),route,binary_sha256,boundary)
            except (OSError,ValueError,KeyError,TypeError):
                continue
            return value
    return None


def eligibility(store, invocation):
    route=invocation['route']
    attempts=invocation.get('attempts',[])
    if not attempts:
        return {'eligible':False,'reason':'No native execution has supplied a boundary.'}
    attempt=attempts[-1]
    receipt=attempt.get('runtime_receipt') or {}
    evidence=receipt.get('conversation_native_boundary') or {}
    def refused(reason):
        return {'eligible':False,'reason':reason}
    if not evidence.get('eligible_evidence'):
        return refused(evidence.get('reason','Native state was not bound at the execution boundary.'))
    if evidence['data_root']!=str(store.config.data_root.resolve()):
        return refused('Restored state requires independent native-state qualification at this data root.')
    if evidence['route_sha256']!=route_identity(route):
        return refused('Native authority or model binding changed.')
    try:
        binary=shutil.which(route['harness'])
        if binary is None or str(Path(binary).resolve())!=evidence['binary'] or _digest(Path(binary))[0]!=evidence['binary_sha256']:
            return refused('Native executable changed since the boundary.')
        head=_claude_head(Path(evidence['path']),evidence['session_id'],route['workspace'])
        if any(head[key]!=evidence[key] for key in head):
            return refused('Native session advanced outside the selected boundary.')
        with store._connect() as conn:
            state=store._native_session_state(conn,route['provider'],evidence['session_id'])
        if state is None or state['invocation_id']!=invocation['invocation_id']:
            return refused('Selected invocation is not the native session head.')
        boundary='completed' if attempt['state']=='succeeded' else 'interrupted'
        proof=qualification(store,route,evidence['binary_sha256'],boundary)
        if proof is None:
            return refused('This executable, route and native boundary lack live recovery qualification.')
        return {'eligible':True,'session_id':evidence['session_id'],'invocation_id':invocation['invocation_id'],
                'boundary':boundary,'head_sha256':head['history_sha256'],'qualification':proof}
    except (OSError,ValueError,KeyError,TypeError) as error:
        return refused(str(error))
