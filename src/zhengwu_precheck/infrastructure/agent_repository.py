"""智能体草稿和确认记录；此仓储本身不创建工单。"""
import hashlib
import json
import time
from uuid import uuid4

from sqlalchemy import text
from zhengwu_precheck.core.errors import AppError


class AgentRepository:
    def __init__(self, engine, clock=time.time):
        self.engine, self.clock = engine, clock

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS agent_drafts (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                version INTEGER NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL)'''))
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS agent_draft_confirmations (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                draft_id TEXT NOT NULL REFERENCES agent_drafts(id), draft_version INTEGER NOT NULL,
                content_hash TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at REAL)'''))

    @staticmethod
    def _payload(data):
        if not isinstance(data, dict) or set(data) != {'title', 'content'}:
            raise AppError(422, 'INVALID_DRAFT', '草稿字段不正确')
        for key, limit in (('title', 120), ('content', 4000)):
            if not isinstance(data[key], str) or not data[key].strip() or len(data[key]) > limit:
                raise AppError(422, 'INVALID_DRAFT', '请填写有效的草稿内容')
        payload = json.dumps({k: data[k].strip() for k in ('title', 'content')}, ensure_ascii=False, sort_keys=True)
        return payload, hashlib.sha256(payload.encode('utf-8')).hexdigest()

    async def create_draft(self, user_id, data):
        if not user_id:
            raise AppError(401, 'LOGIN_REQUIRED', '请先登录')
        payload, digest = self._payload(data)
        record = {'id': str(uuid4()), 'user_id': user_id, 'version': 1, 'payload': payload, 'content_hash': digest}
        async with self.engine.begin() as conn:
            await conn.execute(text('INSERT INTO agent_drafts VALUES (:id,:user_id,:version,:payload,:content_hash)'), record)
        return record

    async def update_draft(self, user_id, draft_id, version, data):
        payload, digest = self._payload(data)
        async with self.engine.begin() as conn:
            result = await conn.execute(text('''UPDATE agent_drafts SET version=version+1,payload=:payload,content_hash=:hash
                WHERE id=:id AND user_id=:user AND version=:version'''),
                {'payload': payload, 'hash': digest, 'id': draft_id, 'user': user_id, 'version': version})
            if result.rowcount != 1:
                raise AppError(409, 'DRAFT_CHANGED', '草稿不存在或已变化')
            await conn.execute(text('DELETE FROM agent_draft_confirmations WHERE draft_id=:id AND consumed_at IS NULL'), {'id': draft_id})
        return {'id': draft_id, 'user_id': user_id, 'version': version + 1, 'payload': payload, 'content_hash': digest}

    async def confirm_draft(self, user_id, draft_id, version):
        if not user_id:
            raise AppError(401, 'LOGIN_REQUIRED', '请先登录')
        async with self.engine.begin() as conn:
            await conn.execute(text('UPDATE agent_drafts SET version=version WHERE id=:id'), {'id': draft_id})
            row = (await conn.execute(text('SELECT * FROM agent_drafts WHERE id=:id AND user_id=:user AND version=:version'),
                                      {'id': draft_id, 'user': user_id, 'version': version})).mappings().first()
            if not row:
                raise AppError(409, 'DRAFT_CHANGED', '草稿不存在或已变化')
            record = {'id': str(uuid4()), 'user_id': user_id, 'draft_id': draft_id,
                      'draft_version': version, 'content_hash': row['content_hash'], 'expires_at': self.clock() + 600}
            await conn.execute(text('''INSERT INTO agent_draft_confirmations
                (id,user_id,draft_id,draft_version,content_hash,expires_at) VALUES
                (:id,:user_id,:draft_id,:draft_version,:content_hash,:expires_at)'''), record)
            return record
