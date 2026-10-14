"""Lossless observation admission over the existing append-only event ledger.

Raw bytes precede normalized records. Stable channel offsets permit deterministic
re-indexing after a crash between journal fsync and event admission. Incomplete
UTF-8/JSON tails remain bytes, never invented completed provider messages.
"""
from __future__ import annotations

import json
from pathlib import Path

from .canonical import strict_json_loads
from .store import LatticeConflict


def normalize(message: dict) -> list[dict]:
    """Extract exposed protocol facts, without promoting their content to authority."""
    kind = message.get('type')
    records = []
    if kind == 'system' and message.get('subtype') == 'init':
        records.append({'kind': 'session', 'session_id': message.get('session_id'), 'model': message.get('model')})
    elif kind == 'thread.started':
        records.append({'kind': 'session', 'session_id': message.get('thread_id')})
    if kind in ('assistant', 'user'):
        native = message.get('message')
        content = native.get('content', []) if isinstance(native, dict) else []
        if isinstance(content, str):
            records.append({'kind': 'message', 'role': kind, 'text': content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use':
                    records.append({'kind': 'tool-request', 'id': block.get('id'), 'name': block.get('name'), 'arguments': block.get('input')})
                elif block.get('type') == 'tool_result':
                    records.append({'kind': 'tool-result', 'id': block.get('tool_use_id'), 'result': block.get('content'), 'is_error': block.get('is_error', False)})
                elif block.get('type') == 'text':
                    records.append({'kind': 'message', 'role': kind, 'text': block.get('text')})
    elif kind in ('item.started', 'item.updated', 'item.completed'):
        item = message.get('item', {})
        if isinstance(item, dict):
            item_kind = item.get('type')
            if item_kind == 'agent_message':
                records.append({'kind': 'message', 'role': 'assistant', 'text': item.get('text'), 'partial': kind != 'item.completed'})
            elif item_kind in ('command_execution', 'file_change', 'mcp_tool_call', 'web_search'):
                record={'kind': 'tool-result' if kind == 'item.completed' else 'tool-request', 'id': item.get('id'), 'name': item_kind, 'detail': item}
                if kind=='item.updated':
                    record['progress_update']=True
                elif kind=='item.completed':
                    record['is_error']=item.get('status') in ('failed','declined') or item.get('exit_code') not in (None,0) or bool(item.get('error'))
                records.append(record)
    elif kind == 'result':
        records.append({'kind': 'terminal', 'success': message.get('is_error') is False and message.get('subtype') == 'success'})
    elif kind in ('error', 'turn.failed'):
        records.append({'kind': 'failure', 'detail': message})
    elif kind == 'stream_event':
        records.append({'kind': 'partial', 'detail': message.get('event')})
    return records


class ObservationStore:
    def __init__(self, store):
        self.store = store

    def _events(self, conn, attempt_id, event_type):
        return [dict(r) | {'payload': json.loads(r['payload_json'])} for r in conn.execute(
            'SELECT seq,payload_json FROM events WHERE subject_id=? AND event_type=? ORDER BY seq', (attempt_id, event_type))]

    def bytes(self, attempt_id: str, channel: str) -> bytes:
        with self.store._connect() as conn:
            return self._bytes(conn, attempt_id, channel)

    def _bytes(self, conn, attempt_id, channel):
        pieces = []
        offset = 0
        for event in self._events(conn, attempt_id, 'attempt.chunk'):
            value = event['payload']
            if value['channel'] != channel:
                continue
            chunk = self.store.blobs.get(value['sha256'])
            if value.get('offset', offset) != offset or len(chunk) != value['byte_count']:
                raise LatticeConflict('observation stream has a gap or changed bytes')
            pieces.append(chunk)
            offset += len(chunk)
        return b''.join(pieces)

    def append(self, attempt_id: str, channel: str, chunk: bytes, *, offset: int | None = None):
        digest = self.store.blobs.put(chunk)
        with self.store.transaction() as conn:
            attempt = self.store._row(conn.execute('SELECT a.invocation_id,i.project_id FROM attempts a JOIN invocations i USING(invocation_id) WHERE attempt_id=?', (attempt_id,)).fetchone(), attempt_id)
            size = conn.execute("SELECT COALESCE(SUM(json_extract(payload_json,'$.byte_count')),0) FROM events WHERE subject_id=? AND event_type='attempt.chunk' AND json_extract(payload_json,'$.channel')=?", (attempt_id, channel)).fetchone()[0]
            start = size if offset is None else offset
            if start < size:
                prior = self._bytes(conn, attempt_id, channel)
                if prior[start:start+len(chunk)] != chunk or start+len(chunk)>size:
                    raise LatticeConflict('observation replay disagrees with admitted bytes')
            else:
                if start != size:
                    raise LatticeConflict('observation admission cannot skip bytes')
                self.store._event(conn, project_id=attempt['project_id'], event_type='attempt.chunk', subject_type='attempt', subject_id=attempt_id, payload={
                    'invocation_id':attempt['invocation_id'], 'channel':channel,'offset':start,'sha256':digest,'content_ref':f'lattice:blob/{digest}', 'byte_count':len(chunk), 'preview':chunk.decode('utf-8', errors='replace')[:1000]})
        self.reindex(attempt_id, channel)

    def reindex(self, attempt_id: str, channel: str):
        with self.store.transaction() as conn:
            offset = conn.execute("SELECT COALESCE(MAX(json_extract(payload_json,'$.end_offset')),0) FROM events WHERE subject_id=? AND event_type='attempt.observation' AND json_extract(payload_json,'$.channel')=?", (attempt_id, channel)).fetchone()[0]
            chunks = [json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM events WHERE subject_id=? AND event_type='attempt.chunk' AND json_extract(payload_json,'$.channel')=? AND (json_extract(payload_json,'$.offset') IS NULL OR json_extract(payload_json,'$.offset')+json_extract(payload_json,'$.byte_count')>?) ORDER BY seq", (attempt_id,channel,offset))]
            if any('offset' not in item for item in chunks):
                data = self._bytes(conn, attempt_id, channel)[offset:]
            else:
                data = b''.join(self.store.blobs.get(item['sha256'])[max(0,offset-item['offset']):] for item in chunks)
            attempt = conn.execute('SELECT a.invocation_id,i.project_id FROM attempts a JOIN invocations i USING(invocation_id) WHERE attempt_id=?', (attempt_id,)).fetchone()
            for fragment in data.split(b'\n')[:-1]:
                line = fragment + b'\n'
                # JSONL is delimited by LF only; Unicode separators are content.
                if not line.endswith(b'\n'):
                    break
                end = offset + len(line)
                try:
                    message = strict_json_loads(line.decode('utf-8', errors='strict'))
                    records = normalize(message) if isinstance(message, dict) else []
                    framing = 'json-object' if isinstance(message, dict) else 'nonobject'
                except (UnicodeDecodeError, ValueError, RecursionError):
                    records = []
                    framing = 'unparsed'
                digest = self.store.blobs.put(line)
                self.store._event(conn, project_id=attempt['project_id'], event_type='attempt.observation', subject_type='attempt', subject_id=attempt_id, payload={
                    'invocation_id':attempt['invocation_id'],'channel':channel,'offset':offset,'end_offset':end,
                    'source_ref':f'lattice:blob/{digest}','sha256':digest,'framing':framing,'records':records,'authority':'provider-evidence'})
                offset = end

    def reconcile_journal(self, attempt_id: str, root: Path):
        """Import only the unadmitted suffix, checking every already admitted byte."""
        for channel in ('stdout', 'stderr'):
            path = root / (channel+'.log')
            if not path.exists():
                continue
            raw = path.read_bytes()
            prior = self.bytes(attempt_id, channel)
            if not raw.startswith(prior):
                raise LatticeConflict('persisted journal disagrees with admitted observation prefix')
            if len(raw)>len(prior):
                self.append(attempt_id, channel, raw[len(prior):], offset=len(prior))
            else:
                self.reindex(attempt_id, channel)
