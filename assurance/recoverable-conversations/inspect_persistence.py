"""Read conversation persistence independently of Lattice writers and projections.

This is a bounded persistence oracle, not a substitute for installed/provider,
browser, authority or restoration qualification. It never changes the store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import rfc8785
from lattice.canonical import generated_object


class PersistenceFailure(ValueError):
    pass


def require(condition, detail):
    if not condition:
        raise PersistenceFailure(detail)


def load(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(PersistenceFailure('nonfinite JSON value')))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def object_digest(value):
    return digest(rfc8785.dumps(value))


def inspect(root, conversation_id, expected=None):
    root = Path(root).resolve()
    def blob(identity):
        require(isinstance(identity,str) and re.fullmatch('[0-9a-f]{64}',identity), 'invalid blob identity')
        raw = (root / 'blobs' / identity[:2] / identity).read_bytes()
        require(digest(raw)==identity, 'blob bytes differ from identity: '+identity)
        return raw
    database = root / 'lattice.sqlite3'
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        conversation = connection.execute('SELECT * FROM conversations WHERE conversation_id=?',(conversation_id,)).fetchone()
        require(conversation is not None,'conversation is absent')
        rounds = connection.execute('SELECT * FROM conversation_rounds WHERE conversation_id=? ORDER BY ordinal',(conversation_id,)).fetchall()
        require([r['ordinal'] for r in rounds]==list(range(1,len(rounds)+1)),'round ordinal gap')
        require(sum(r['state'] in ('queued','active','needs_attention') for r in rounds)<=1,'multiple unfinished rounds')
        completed = []
        seen_invocations = set()
        previous_checkpoint = None
        cursors = {}
        observation_count = 0
        executions_count = 0
        sources = [{'ref':str(Path(__file__).resolve()),'revision':None,'sha256':digest(Path(__file__).read_bytes())}]
        latest_event = connection.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        for row in rounds:
            require(object_digest(load(row['request_json']))==row['request_sha256'],'request identity changed')
            sources.append({'ref':'lattice:round/'+row['round_id'],'revision':None,'sha256':row['request_sha256']})
            executions = connection.execute('SELECT * FROM conversation_executions WHERE round_id=? ORDER BY ordinal',(row['round_id'],)).fetchall()
            require([e['ordinal'] for e in executions]==list(range(1,len(executions)+1)),'execution ordinal gap')
            for execution in executions:
                identity = execution['invocation_id']
                require(identity not in seen_invocations,'invocation bound more than once')
                seen_invocations.add(identity)
                inv = connection.execute('SELECT * FROM invocations WHERE invocation_id=?',(identity,)).fetchone()
                require(inv is not None and inv['project_id']==conversation['project_id'],'execution crosses project boundary')
                manifest = load(inv['manifest_json'])
                require(manifest.get('manifest_sha256',inv['manifest_sha256'])==inv['manifest_sha256'],'manifest self-reference changed')
                require(object_digest({key:value for key,value in manifest.items() if key!='manifest_sha256'})==inv['manifest_sha256'],'invocation manifest identity changed')
                sources.append({'ref':'lattice:invocation/'+identity,'revision':None,'sha256':inv['manifest_sha256']})
                executions_count += 1
                events = connection.execute("SELECT e.subject_id,e.payload_json FROM events e JOIN attempts a ON a.attempt_id=e.subject_id WHERE a.invocation_id=? AND e.event_type='attempt.chunk' ORDER BY e.seq",(identity,)).fetchall()
                for event in events:
                    value = load(event['payload_json'])
                    key = (event['subject_id'],value['channel'])
                    offset = cursors.get(key,0)
                    require(value.get('offset',offset)==offset,'observation offset gap or replay')
                    raw = blob(value['sha256'])
                    require(len(raw)==value['byte_count'],'observation byte count changed')
                    require(value['content_ref']=='lattice:blob/'+value['sha256'],'observation source reference changed')
                    cursors[key] = offset+len(raw)
                    observation_count += 1
            if row['state']!='completed':
                require(row['accepted_response_id'] is None and row['checkpoint_id'] is None,'unfinished round claims completion')
                continue
            response = connection.execute('SELECT * FROM context_blocks WHERE block_id=?',(row['accepted_response_id'],)).fetchone()
            checkpoint = connection.execute('SELECT * FROM conversation_checkpoints WHERE checkpoint_id=?',(row['checkpoint_id'],)).fetchone()
            require(response is not None and checkpoint is not None,'completed round lost response or checkpoint')
            require(response['project_id']==conversation['project_id'],'response crosses project boundary')
            require(checkpoint['round_id']==row['round_id'],'checkpoint belongs to another round')
            raw_response = blob(response['content_sha256'])
            require(len(raw_response)==response['byte_count'],'response byte count changed')
            value = load(checkpoint['content_json'])
            require(object_digest(value)==checkpoint['content_sha256'],'checkpoint identity changed')
            require((value['conversation_id'],value['round_id'],value['ordinal'])==(conversation_id,row['round_id'],row['ordinal']),'checkpoint history boundary changed')
            require(value['request_sha256']==row['request_sha256'] and value['response_id']==row['accepted_response_id'] and value['response_sha256']==response['content_sha256'],'checkpoint content binding changed')
            require(value['previous']==previous_checkpoint,'checkpoint chain changed')
            require(value['invocation_id'] in [e['invocation_id'] for e in executions],'checkpoint references another execution')
            inv = connection.execute('SELECT manifest_sha256,route_json,state,output_block_id FROM invocations WHERE invocation_id=?',(value['invocation_id'],)).fetchone()
            require(inv['state'] in ('succeeded','imported') and inv['output_block_id']==row['accepted_response_id'],'checkpoint references an unaccepted execution')
            require(value['manifest_sha256']==inv['manifest_sha256'] and value['route']==load(inv['route_json']),'checkpoint execution binding changed')
            require(type(value['event_cursor']) is int and 0<=value['event_cursor']<=latest_event,'checkpoint cursor exceeds admitted history')
            for reference in value['artifacts']:
                artifact = connection.execute('SELECT content_sha256,project_id FROM artifacts WHERE artifact_id=?',(reference['artifact_id'],)).fetchone()
                require(artifact is not None and artifact['project_id']==conversation['project_id'] and artifact['content_sha256']==reference['content_sha256'],'checkpoint artifact changed')
                blob(reference['content_sha256'])
            completed.append({'ordinal':row['ordinal'],'request_sha256':row['request_sha256'],'response_sha256':response['content_sha256']})
            sources.extend([{'ref':'lattice:checkpoint/'+checkpoint['checkpoint_id'],'revision':None,'sha256':checkpoint['content_sha256']},
                            {'ref':'lattice:blob/'+response['content_sha256'],'revision':None,'sha256':response['content_sha256']}])
            previous_checkpoint = {'checkpoint_id':checkpoint['checkpoint_id'],'content_sha256':checkpoint['content_sha256']}
        steering = connection.execute('SELECT * FROM conversation_steering WHERE conversation_id=? ORDER BY ordinal',(conversation_id,)).fetchall()
        for item in steering:
            require(item['target_round_id'] in [r['round_id'] for r in rounds],'steering target belongs to another conversation')
            if item['resulting_round_id'] is not None:
                require(item['resulting_round_id'] in [r['round_id'] for r in rounds],'steering result belongs to another conversation')
            require(item['state'] in ('queued','dispatched','superseded'),'unknown steering disposition')
            require(item['state']!='dispatched' or item['resulting_round_id'] is not None,'dispatched steering has no admitted round')
        if expected is not None:
            if 'owning_task_ref' in expected:
                require(conversation['owning_task_ref']==expected['owning_task_ref'],'owning task changed')
            by_ordinal = {r['ordinal']:r for r in completed}
            for original in expected['completed']:
                require(by_ordinal.get(original['ordinal'])==original,'hand-authored completed history changed')
            if 'execution_count' in expected:
                require(executions_count==expected['execution_count'],'execution association count differs')
            if 'completed_count' in expected:
                require(len(completed)==expected['completed_count'],'completed history count differs')
        envelope = generated_object(kind='conversation-persistence-inspection',schema_name='conversation-persistence-inspection',role='snapshot',
            subject='lattice:conversation/'+conversation_id,sources=sources,producer_name='independent-conversation-reader',producer_version='1.0.0')
        return {'generated_object':envelope,'expected_history_sha256':object_digest(expected) if expected is not None else None,
                'conversation_id':conversation_id,'owning_task_ref':conversation['owning_task_ref'],'round_count':len(rounds),
                'completed':completed,'execution_count':executions_count,'observation_chunks':observation_count,
                'admitted_bytes':sum(cursors.values()),'latest_checkpoint':previous_checkpoint,'persistence_verified':True,
                'proof_limits':['Execution associations do not prove external dispatch counts; use an independent provider action log.',
                                'Does not establish provider authentication, native recall, browser behavior, external-effect truth or overall completion.']}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('data_root',type=Path)
    parser.add_argument('conversation_id')
    parser.add_argument('--expected',type=Path)
    args = parser.parse_args()
    expected = load(args.expected.read_bytes()) if args.expected else None
    print(json.dumps(inspect(args.data_root,args.conversation_id,expected),indent=2))
