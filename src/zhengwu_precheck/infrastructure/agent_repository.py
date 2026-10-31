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
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS agent_submissions (
                draft_id TEXT PRIMARY KEY REFERENCES agent_drafts(id),
                confirmation_id TEXT NOT NULL UNIQUE REFERENCES agent_draft_confirmations(id),
                ticket_id TEXT NOT NULL REFERENCES tickets(id), content_hash TEXT NOT NULL)'''))
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS agent_rule_selections (
                case_id TEXT PRIMARY KEY REFERENCES conversations(id), principal_key TEXT NOT NULL,
                version INTEGER NOT NULL, snapshot_id TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                confirmed_at REAL NOT NULL)'''))
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS agent_exceptions (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES conversations(id),
                principal_key TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL)'''))
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS agent_search_results (
                id TEXT PRIMARY KEY, principal_key TEXT NOT NULL, case_id TEXT NOT NULL REFERENCES conversations(id),
                snapshot_id TEXT NOT NULL, matter_id TEXT NOT NULL, scenario_id TEXT NOT NULL, created_at REAL NOT NULL)'''))

    @staticmethod
    def _payload(data):
        optional = {'matter_id', 'scenario_id', 'shared_material_ids', 'case_id', 'purpose',
                    'exception_ids', 'precheck_id', 'conversation_id', 'message_id', 'shared_material_versions'}
        if not isinstance(data, dict) or not {'title', 'content'} <= set(data) or set(data) - {'title', 'content'} - optional:
            raise AppError(422, 'INVALID_DRAFT', '草稿字段不正确')
        for key, limit in (('title', 120), ('content', 4000)):
            if not isinstance(data[key], str) or not data[key].strip() or len(data[key]) > limit:
                raise AppError(422, 'INVALID_DRAFT', '请填写有效的草稿内容')
        clean = {**data, **{k: data[k].strip() for k in ('title', 'content')}}
        for name in ('shared_material_ids', 'exception_ids'):
            ids = clean.get(name, [])
            if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i, str) or not 1 <= len(i) <= 128 for i in ids):
                raise AppError(422, 'INVALID_DRAFT', '共享资料或异常标识无效')
            if name in clean:
                clean[name] = sorted(set(ids))
        frozen=clean.get('shared_material_versions',[])
        if not isinstance(frozen,list) or len(frozen)>20 or any(not isinstance(v,dict) or
            not {'id','version','sha256'}<=set(v) or set(v)-{'id','version','sha256','filename'} or
            not isinstance(v['id'],str) or type(v['version']) is not int or v['version']<1 or
            not isinstance(v['sha256'],str) or len(v['sha256'])!=64 or
            ('filename' in v and (not isinstance(v['filename'],str) or len(v['filename'])>120)) for v in frozen):
            raise AppError(422,'INVALID_DRAFT','共享材料版本无效')
        for name in optional - {'shared_material_ids', 'exception_ids','shared_material_versions'}:
            if name in clean and (not isinstance(clean[name], str) or len(clean[name]) > 128):
                raise AppError(422, 'INVALID_DRAFT', '草稿关联字段无效')
        if clean.get('purpose', 'ordinary_consultation') not in {'ordinary_consultation', 'exception_review'}:
            raise AppError(422, 'INVALID_DRAFT', '草稿用途无效')
        payload = json.dumps(clean, ensure_ascii=False, sort_keys=True)
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
            await conn.execute(text('UPDATE agent_drafts SET version=version WHERE id=:id'),{'id':draft_id})
            submitted=(await conn.execute(text('SELECT draft_id FROM agent_submissions WHERE draft_id=:id'),{'id':draft_id})).first()
            if submitted:
                raise AppError(409,'DRAFT_ALREADY_SUBMITTED','草稿已提交，请在原工单继续补充')
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

    async def get_draft(self, user_id, draft_id):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text('SELECT * FROM agent_drafts WHERE id=:id AND user_id=:user'),
                                      {'id': draft_id, 'user': user_id})).mappings().first()
        if not row:
            raise AppError(404, 'DRAFT_NOT_FOUND', '草稿不存在或无权访问')
        return {**dict(row), 'data': json.loads(row['payload'])}

    async def case(self, conn, case_id, principal):
        row = (await conn.execute(text('SELECT * FROM conversations WHERE id=:id'), {'id': case_id})).mappings().first()
        if not row or (row['user_id'] != principal.get('user_id') if principal.get('user_id') else
                       row['user_id'] is not None or row['anonymous_token_hash'] != principal.get('anonymous_hash')):
            raise AppError(404, 'CASE_NOT_FOUND', '咨询不存在或无权访问')
        return dict(row)

    @staticmethod
    def snapshot_key(snapshot):
        frozen = {k: snapshot.get(k) for k in ('matter_id', 'scenario_id', 'managed', 'release_ids',
                                              'version_ids', 'conflict', 'local_signature', 'teaching_rule_digest')}
        raw = json.dumps(frozen, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest(), raw

    async def confirm_selection(self, case_id, principal, snapshot):
        sid, raw = self.snapshot_key(snapshot)
        if snapshot.get('conflict') or (snapshot.get('managed') and not snapshot.get('version_ids')):
            raise AppError(409, 'RULE_UNAVAILABLE', '规则不可用或版本冲突，请转人工')
        async with self.engine.begin() as conn:
            await conn.execute(text('UPDATE conversations SET created_at=created_at WHERE id=:id'), {'id': case_id})
            case = await self.case(conn, case_id, principal)
            if not case['confirmed_at'] or case['matter_id'] != snapshot['matter_id'] or case['scenario_id'] != snapshot['scenario_id']:
                raise AppError(409, 'SELECTION_REQUIRED', '请先确认事项和情形')
            await conn.execute(text('''INSERT INTO agent_rule_selections VALUES(:case,:who,1,:sid,:raw,:now)
                ON CONFLICT(case_id) DO UPDATE SET version=version+1,principal_key=:who,
                snapshot_id=:sid,snapshot_json=:raw,confirmed_at=:now'''),
                {'case': case_id, 'who': principal['key'], 'sid': sid, 'raw': raw, 'now': self.clock()})
            row = (await conn.execute(text('SELECT * FROM agent_rule_selections WHERE case_id=:id'), {'id': case_id})).mappings().one()
        return dict(row)

    async def selection(self, case_id, principal, snapshot, expected_version=None):
        async with self.engine.connect() as conn:
            case = await self.case(conn, case_id, principal)
            row = (await conn.execute(text('SELECT * FROM agent_rule_selections WHERE case_id=:id AND principal_key=:who'),
                                      {'id': case_id, 'who': principal['key']})).mappings().first()
        sid, _ = self.snapshot_key(snapshot)
        if not row or row['snapshot_id'] != sid or (expected_version is not None and row['version'] != expected_version) or case['matter_id'] != snapshot['matter_id']:
            raise AppError(409, 'SELECTION_CHANGED', '事项或规则已变化，请重新确认')
        return dict(row)

    async def record_exception(self, case_id, principal, kind, detail):
        if kind not in {'expired_history', 'missing_evidence', 'version_conflict', 'system_unavailable'}:
            raise AppError(422, 'INVALID_EXCEPTION', '异常类型无效')
        record = {'id': str(uuid4()), 'case': case_id, 'who': principal['key'], 'kind': kind,
                  'detail': str(detail)[:1000], 'now': self.clock()}
        async with self.engine.begin() as conn:
            await self.case(conn, case_id, principal)
            await conn.execute(text('INSERT INTO agent_exceptions VALUES(:id,:case,:who,:kind,:detail,:now)'), record)
        return record['id']
