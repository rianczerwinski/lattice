"""Ordinary conversation controls; JSON remains an optional automation surface."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from .client import LatticeClient
from .contracts import ProviderRoute


def display(value, *, as_json=False):
    if as_json:
        print(json.dumps(value,ensure_ascii=False,indent=2))
    elif isinstance(value,list):
        for item in value:
            print(f"{item['conversation_id']}  {item['title']}  [{item['owning_task_ref']}]")
        if not value:
            print('No conversations found.')
    elif 'rounds' in value:
        print(f"{value['title']} — {value['conversation_id']} (revision {value['revision']})")
        for r in value['rounds']:
            print(f"\nRound {r['ordinal']} · {r['state']}\nYou: {r['request']['prompt']}")
            if 'response' in r:
                print(f"\nModel: {r['response']}")
            for execution in r['executions']:
                for attempt in execution['attempts']:
                    if attempt.get('failure_kind'):
                        print(f"Attention: {attempt['failure_kind']}: {attempt.get('error_text') or ''}")
        for instruction in value['steering']:
            print(f"\nQueued steering: {instruction['content']}")
        if value['checkpoint']:
            print(f"\nCheckpoint: {value['checkpoint']['checkpoint_id']}")
        if value['has_more']:
            print(f"More rounds: show with --after {value['rounds'][-1]['ordinal']}")
    else:
        print(f"Conversation: {value.get('conversation_id','')}")
        for key in ('round_id','steering_id','invocation_id','revision','event_cursor'):
            if key in value:
                print(f"{key.replace('_',' ').capitalize()}: {value[key]}")


def command(args):
    client=LatticeClient.from_environment()
    action=args.conversation_action
    if action=='create':
        if args.route:
            route=ProviderRoute.model_validate_json(Path(args.route).read_text()).model_dump(mode='json')
        else:
            if not args.model:
                raise ValueError('create requires --model or an exact --route file')
            options={'provider':args.provider,'model':args.model,'harness':{'anthropic':'claude','openai':'codex'}[args.provider]}
            if args.workspace:
                options.update(execution_profile='workspace-agent',workspace=str(Path(args.workspace).resolve()),
                    tools=['Read','Glob','Grep'] if args.provider=='anthropic' else ['shell'],
                    permissions=['Read','Glob','Grep'] if args.provider=='anthropic' else [],
                    sandbox='provider-permissions' if args.provider=='anthropic' else 'read-only')
            route=ProviderRoute(**options).model_dump(mode='json')
        result=client.conversation_create({'project_id':args.project,'title':args.title,'owning_task_ref':args.owner,
            'route':route,'recovery_policy':args.policy,'parent_conversation_id':args.parent,
            'idempotency_key':args.idempotency_key or str(uuid.uuid4())})
    elif action=='list':
        result=client.conversation_list(args.project,owning_task_ref=args.owner,after=args.after,limit=args.limit)
    elif action=='show':
        result=client.conversation_show(args.conversation_id,after=args.after,limit=args.limit)
    elif action=='watch':
        deadline=time.monotonic()+args.timeout
        cursor=args.after
        while True:
            page=client.conversation_observations(args.conversation_id,after=cursor)
            for event in page['items']:
                if args.json:
                    print(json.dumps(event,ensure_ascii=False),flush=True)
                elif event['event_type']=='attempt.chunk':
                    print(event['payload']['preview'],end='',flush=True)
                elif event['event_type'].startswith('conversation.'):
                    print('\n'+event['event_type'].removeprefix('conversation.'),flush=True)
            cursor=page['cursor']
            if page['has_more']:
                continue
            state=client.conversation_show(args.conversation_id)
            live=state['pending']
            if not state['pending_round_id'] or (live and live['state']=='needs_attention'):
                display(state,as_json=args.json)
                return 2 if live else 0
            if time.monotonic()>=deadline:
                print(f'Watch ended; execution remains owned by Lattice. Observation cursor: {cursor}')
                return 0
            time.sleep(.5)
    else:
        key=args.idempotency_key or str(uuid.uuid4())
        if action=='continue':
            effects=json.loads(Path(args.effects).read_text()) if args.effects else []
            preview=client.conversation_preview(args.conversation_id,policy=args.policy,include_steering=not args.exclude_steering,effect_dispositions=effects)
            if args.preview or preview['blockers']:
                if args.json:
                    print(json.dumps(preview,ensure_ascii=False,indent=2))
                else:
                    print(f"Continuation: {preview['mode']}\n{preview['maximum_remaining_loss']}")
                    for blocker in preview['blockers']:
                        print(f"{blocker['code']}: {blocker['detail']}")
                return 2 if preview['blockers'] else 0
            result=client.conversation_continue(args.conversation_id,{'expected_revision':preview['revision'],'idempotency_key':key,
                'expected_preview_sha256':preview['preview_sha256'],'policy':args.policy,'include_steering':not args.exclude_steering,'effect_dispositions':effects})
        else:
            state=client.conversation_show(args.conversation_id,limit=1)
            request={'expected_revision':state['revision'] if args.revision is None else args.revision,'idempotency_key':key}
            if action in ('send','steer'):
                text=Path(args.file).read_text() if args.file else args.text
                if not text:
                    raise ValueError('provide text or --file')
                request['prompt' if action=='send' else 'content']=text
                if action=='steer':
                    request['interrupt']=args.interrupt
            if action=='adopt':
                request['invocation_ids']=args.invocations
            result=client.conversation_mutate(args.conversation_id,action,request)
    display(result,as_json=args.json)
    return 0


def add_parser(subparsers):
    root=subparsers.add_parser('conversation',help='discover and operate durable child conversations')
    actions=root.add_subparsers(dest='conversation_action',required=True)
    for name in ('create','list','show','send','steer','interrupt','continue','watch','withdraw','adopt'):
        item=actions.add_parser(name)
        item.set_defaults(handler=command)
        item.add_argument('--json',action='store_true')
        if name not in ('create','list'):
            item.add_argument('conversation_id')
        if name in ('create','send','steer','interrupt','continue','withdraw','adopt'):
            item.add_argument('--idempotency-key')
        if name in ('send','steer','interrupt','withdraw','adopt'):
            item.add_argument('--revision',type=int)
        if name in ('send','steer'):
            item.add_argument('text',nargs='?')
            item.add_argument('--file')
        if name=='steer':
            item.add_argument('--interrupt',action='store_true',help='save this redirect before stopping the active request')
        if name in ('create','list'):
            item.add_argument('--project',required=True)
            item.add_argument('--owner',required=name=='create')
        if name=='create':
            item.add_argument('--title',required=True)
            item.add_argument('--route')
            item.add_argument('--provider',choices=['anthropic','openai'],default='anthropic')
            item.add_argument('--model')
            item.add_argument('--workspace',help='grant read-only workspace access')
            item.add_argument('--parent')
            item.add_argument('--policy',choices=['auto','native','portable'],default='auto')
        if name in ('list','show'):
            item.add_argument('--after',type=str if name=='list' else int,default='' if name=='list' else 0)
            item.add_argument('--limit',type=int,default=100)
        if name=='continue':
            item.add_argument('--policy',choices=['auto','native','portable'])
            item.add_argument('--preview',action='store_true')
            item.add_argument('--exclude-steering',action='store_true')
            item.add_argument('--effects',help='JSON file with inspected uncertain-effect dispositions')
        if name=='watch':
            item.add_argument('--after',type=int,default=0)
            item.add_argument('--timeout',type=float,default=1800)
        if name=='adopt':
            item.add_argument('invocations',nargs='+')
