"""Authenticated conversation controls under the existing operator middleware."""
from __future__ import annotations

import asyncio
from typing import Literal
from fastapi import APIRouter, Query
from pydantic import Field
from .contracts import StrictModel
from .conversation_contracts import (ConversationCreate, ConversationSend, ConversationSteer,
    ConversationContinue, ConversationMutation, ConversationAdopt, EffectDisposition)
from .conversations import ConversationStore
from .conversation_service import ConversationService


class RecoveryPreview(StrictModel):
    policy: Literal['auto','native','portable'] | None = None
    include_steering: bool = True
    effect_dispositions: list[EffectDisposition] = Field(default_factory=list,max_length=1000)


def conversation_router(store, supervisor):
    router=APIRouter(prefix='/api/v1')
    conversations=ConversationStore(store)
    service=ConversationService(store,supervisor)

    @router.post('/conversations')
    async def create(value: ConversationCreate):
        return conversations.create(value)

    @router.get('/conversations')
    async def list_conversations(project_id: str, owning_task_ref: str | None = None, after: str = '', limit: int = Query(100,ge=1,le=500)):
        return conversations.list(project_id,owning_task_ref,after=after,limit=limit)

    @router.get('/conversations/{conversation_id}')
    async def read(conversation_id: str, after: int = Query(0,ge=0), limit: int = Query(100,ge=1,le=500)):
        return conversations.get(conversation_id,after=after,limit=limit)

    @router.get('/conversations/{conversation_id}/observations')
    async def observations(conversation_id: str, after: int = Query(0,ge=0), limit: int = Query(100,ge=1,le=500)):
        return conversations.observations(conversation_id,after=after,limit=limit)

    @router.post('/conversations/{conversation_id}/send')
    async def send(conversation_id: str, value: ConversationSend):
        result=conversations.send(conversation_id,value)
        supervisor.kick_conversations()
        return result

    @router.post('/conversations/{conversation_id}/steer')
    async def steer(conversation_id: str, value: ConversationSteer):
        result=conversations.steer(conversation_id,value)
        if value.interrupt and result.get('attempt_id'):
            await asyncio.shield(supervisor.cancel(result['attempt_id']))
        return result

    @router.post('/conversations/{conversation_id}/interrupt')
    async def interrupt(conversation_id: str, value: ConversationMutation):
        result=conversations.interrupt(conversation_id,value)
        if result.get('attempt_id'):
            await asyncio.shield(supervisor.cancel(result['attempt_id']))
        return result

    @router.post('/conversations/{conversation_id}/recovery-preview')
    async def preview(conversation_id: str, value: RecoveryPreview):
        return service.preview(conversation_id,**value.model_dump(mode='json'))

    @router.post('/conversations/{conversation_id}/continue')
    async def continue_conversation(conversation_id: str, value: ConversationContinue):
        task=asyncio.create_task(service.dispatch(conversation_id,continuation=value),name=f'continue-{conversation_id}')
        supervisor._conversation_dispatch_tasks.add(task)
        task.add_done_callback(supervisor._conversation_dispatch_tasks.discard)
        return await asyncio.shield(task)

    @router.post('/conversations/{conversation_id}/withdraw')
    async def withdraw(conversation_id: str, value: ConversationMutation):
        return conversations.withdraw(conversation_id,value)

    @router.post('/conversations/{conversation_id}/adopt')
    async def adopt(conversation_id: str, value: ConversationAdopt):
        return conversations.adopt(conversation_id,value)

    return router
