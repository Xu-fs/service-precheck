"""按授权读取真实对话与拒绝事件；风险标记和复核以追加记录保存。"""
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import text, bindparam
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import allows, MATTER_NAMES

FLAGS = {'missing_evidence': '缺少依据', 'version_conflict': '规则版本冲突',
         'abnormal_precheck': '异常预检（人工标记）', 'suspected_unauthorized': '疑似越权'}
DECISIONS = {'confirmed': '确认风险', 'dismissed': '排除风险', 'needs_followup': '需继续跟进'}


class AuditRepository:
    def __init__(self, engine):
        self.engine = engine

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("""CREATE TABLE IF NOT EXISTS audit_flags(
                id TEXT PRIMARY KEY,record_id TEXT NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,
                actor_id TEXT NOT NULL,created_at REAL NOT NULL)"""))
            await conn.execute(text("""CREATE TABLE IF NOT EXISTS audit_reviews(
                id TEXT PRIMARY KEY,record_id TEXT NOT NULL,decision TEXT NOT NULL,comment TEXT NOT NULL,
                actor_id TEXT NOT NULL,created_at REAL NOT NULL)"""))
            for table in ('audit_flags', 'audit_reviews'):
                await conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_record ON {table}(record_id,created_at)"))

    def _source(self, identity):
        if not allows(identity['grants'], 'audit.review'):
            raise AppError(403, 'STAFF_PERMISSION_DENIED', '当前岗位无权审计记录')
        scopes = [g['matter_id'] for g in identity['grants'] if allows([g], 'audit.review')]
        params = {'viewer': identity['user_id'], 'now': time.time(), 'scopes': scopes, 'all_scope': int('*' in scopes)}
        scope = "{alias}.matter_id IS NOT NULL AND (:all_scope=1 OR {alias}.matter_id IN :scopes)"
        grant = """EXISTS(SELECT 1 FROM staff_record_grants g WHERE g.user_id=:viewer
            AND g.resource_type='conversation' AND g.resource_id=c.id AND g.access_level='body'
            AND (g.expires_at IS NULL OR g.expires_at>:now))"""
        event_scope = f"""({grant} AND ((a.request_id IS NULL AND {scope.format(alias='c')}) OR
                EXISTS(SELECT 1 FROM authorized_messages am WHERE am.conversation_id=c.id AND am.request_id=a.request_id)))"""
        query = f"""WITH authorized_messages AS (
            SELECT m.*,c.user_id,u.username FROM messages m JOIN conversations c ON c.id=m.conversation_id
            LEFT JOIN users u ON u.id=c.user_id WHERE m.sender_type='assistant' AND m.status!='pending'
            AND {scope.format(alias='m')} AND {grant}
        ), records AS (
            SELECT 'message:'||m.id AS id,'message' AS kind,m.conversation_id,m.matter_id,m.user_id,m.username,
                m.created_at,m.outcome,m.error_code,m.result_json,NULL AS action,NULL AS actor_id,
                CASE WHEN m.outcome='insufficient_evidence' THEN 1 ELSE 0 END AS missing_evidence,
                CASE WHEN m.error_code='RULE_VERSION_CONFLICT' OR
                    json_extract(CASE WHEN json_valid(m.result_json) THEN m.result_json ELSE '{{}}' END,'$.reason')='version_conflict'
                    THEN 1 ELSE 0 END AS version_conflict
            FROM authorized_messages m
            UNION ALL
            SELECT 'event:'||a.id,'event',CASE WHEN c.id IS NOT NULL THEN c.id ELSE NULL END,
                CASE WHEN {event_scope} THEN COALESCE(
                    (SELECT am.matter_id FROM authorized_messages am WHERE am.conversation_id=c.id
                        AND am.request_id=a.request_id ORDER BY am.created_at,am.id LIMIT 1),c.matter_id) ELSE NULL END,
                CASE WHEN {event_scope} THEN c.user_id ELSE NULL END,
                CASE WHEN {event_scope} THEN u.username ELSE NULL END,
                a.occurred_at,a.outcome,NULL,NULL,a.action,a.actor_id,0,0
            FROM audit_logs a LEFT JOIN conversations c ON a.entity_type='conversation' AND c.id=a.entity_id
            LEFT JOIN users u ON u.id=c.user_id
            WHERE a.outcome='denied' AND (a.actor_id=:viewer OR {event_scope})
        ) """
        return query, params

    @staticmethod
    def _statement(sql):
        return text(sql).bindparams(bindparam('scopes', expanding=True))

    @staticmethod
    def _date(value, end=False):
        try:
            parsed = datetime.strptime(str(value), '%Y-%m-%d').replace(tzinfo=timezone(timedelta(hours=8)))
            return (parsed + timedelta(days=1) if end else parsed).timestamp()
        except ValueError:
            raise AppError(422, 'INVALID_REQUEST', '日期应为YYYY-MM-DD') from None

    async def list_records(self, identity, filters=None):
        source, params = self._source(identity)
        filters = filters or {}
        try:
            page = max(1, int(filters.get('page') or 1))
            size = min(100, max(1, int(filters.get('page_size') or 20)))
        except (ValueError, TypeError):
            raise AppError(422, 'INVALID_REQUEST', '分页参数无效') from None
        clauses = ['1=1']
        kind = filters.get('kind')
        if kind:
            if kind not in {'message', 'event', 'precheck'}:
                raise AppError(422, 'INVALID_REQUEST', '记录类型无效')
            clauses.append('r.kind=:kind')
            params['kind'] = kind
        for key in ('matter_id', 'user_id', 'username'):
            if filters.get(key):
                clauses.append(f'r.{key}=:{key}')
                params[key] = str(filters[key])
        if filters.get('start'):
            clauses.append('r.created_at>=:start')
            params['start'] = self._date(filters['start'])
        if filters.get('end'):
            clauses.append('r.created_at<:end')
            params['end'] = self._date(filters['end'], True)
        flag = filters.get('flag')
        if flag:
            if flag not in FLAGS:
                raise AppError(422, 'INVALID_REQUEST', '风险类别无效')
            automatic = {'missing_evidence':'r.missing_evidence=1', 'version_conflict':'r.version_conflict=1',
                         'suspected_unauthorized':"r.kind='event'"}.get(flag, '0=1')
            clauses.append(f'({automatic} OR EXISTS(SELECT 1 FROM audit_flags f WHERE f.record_id=r.id AND f.kind=:flag))')
            params['flag'] = flag
        where = ' AND '.join(clauses)
        async with self.engine.connect() as conn:
            total = (await conn.execute(self._statement(source + f'SELECT COUNT(*) FROM records r WHERE {where}'), params)).scalar_one()
            rows = (await conn.execute(self._statement(source + f'SELECT r.* FROM records r WHERE {where} ORDER BY r.created_at DESC,r.id LIMIT :limit OFFSET :offset'),
                                       {**params, 'limit':size, 'offset':(page-1)*size})).mappings().all()
        items = [self._public(dict(row)) for row in rows]
        return {'items':items, 'total':total, 'page':page, 'page_size':size,
                'pages':(total+size-1)//size, 'precheck_available':False}

    @staticmethod
    def _public(row):
        row.pop('result_json', None)
        row['person_label'] = row.get('username') or ('匿名会话 ' + row['conversation_id'] if row.get('conversation_id') else '未关联办事人')
        return row

    async def _detail(self, conn, identity, record_id):
        source, params = self._source(identity)
        params['record'] = record_id
        row = (await conn.execute(self._statement(source + 'SELECT * FROM records WHERE id=:record'), params)).mappings().first()
        if not row:
            raise AppError(404, 'RECORD_NOT_FOUND', '记录不存在或不在授权范围内')
        result = self._public(dict(row))
        if result['kind'] == 'message':
            message_id = record_id.split(':', 1)[1]
            result['content'] = (await conn.execute(text('SELECT content FROM messages WHERE id=:id'), {'id':message_id})).scalar_one()
            result['citations'] = [dict(r) for r in (await conn.execute(text('SELECT filename_snapshot,matter_snapshot,rule_version_snapshot,quote FROM message_citations WHERE message_id=:id AND matter_snapshot IN (:matter,:matter_name)'),
                {'id':message_id, 'matter':result['matter_id'], 'matter_name':MATTER_NAMES.get(result['matter_id'], result['matter_id'])})).mappings().all()]
            # 输入问题只读取同事项快照，绝不按当前会话事项回填历史。
            question = (await conn.execute(text("""SELECT q.content FROM messages a JOIN messages q ON q.id=a.reply_to_message_id
                AND q.conversation_id=a.conversation_id AND q.matter_id=a.matter_id WHERE a.id=:id"""), {'id':message_id})).scalar()
            result['question'] = question
            import json
            snapshot = (await conn.execute(text("SELECT * FROM qa_request_snapshots WHERE message_id=:id"), {'id':message_id})).mappings().first()
            if snapshot:
                knowledge = json.loads(snapshot['knowledge_json'])
                result['knowledge_snapshot'] = {key: knowledge[key] for key in ('matter_id', 'scenario_id', 'managed', 'conflict', 'release_ids', 'version_ids')}
                result['knowledge_snapshot'].update(prompt_version_id=snapshot['prompt_version_id'], prompt_version=knowledge['prompt']['version'])
            else:
                result['knowledge_snapshot'] = None  # 老记录不能按新模板或新规则回填。
        for table, key in [('audit_flags','flags'), ('audit_reviews','reviews')]:
            result[key] = [dict(r) for r in (await conn.execute(text(f'SELECT * FROM {table} WHERE record_id=:id ORDER BY created_at,id'), {'id':record_id})).mappings().all()]
        return result

    async def detail(self, identity, record_id):
        async with self.engine.begin() as conn:
            result = await self._detail(conn, identity, record_id)
            await self._log(conn, identity, 'audit_record_read', record_id)
            return result

    async def _log(self, conn, identity, action, record_id):
        await conn.execute(text("INSERT INTO audit_logs VALUES(:id,:actor,:action,'audit_record',:record,'success',NULL,:now)"),
                           {'id':str(uuid4()), 'actor':identity['user_id'], 'action':action, 'record':record_id, 'now':time.time()})

    async def _append(self, identity, record_id, value, note, *, reviewing=False):
        options = DECISIONS if reviewing else FLAGS
        if not isinstance(value, str) or value not in options or not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise AppError(422, 'INVALID_REQUEST', '请选择有效类别并填写1至2000字的原因或复核意见')
        table = 'audit_reviews' if reviewing else 'audit_flags'
        column, note_column = ('decision','comment') if reviewing else ('kind','reason')
        async with self.engine.begin() as conn:
            # 先取得写锁，再检查当前显式授权，防止并发撤销授权后继续追加。
            await conn.execute(text(f'UPDATE {table} SET id=id WHERE 0'))
            await self._detail(conn, identity, record_id)
            values = {'id':str(uuid4()), 'record':record_id, 'value':value, 'note':note.strip(), 'actor':identity['user_id'], 'now':time.time()}
            await conn.execute(text(f'INSERT INTO {table}(id,record_id,{column},{note_column},actor_id,created_at) VALUES(:id,:record,:value,:note,:actor,:now)'), values)
            await self._log(conn, identity, 'audit_review_added' if reviewing else 'audit_flag_added', record_id)
        return {'id':values['id'], 'record_id':record_id}

    async def flag(self, identity, record_id, kind, reason):
        return await self._append(identity, record_id, kind, reason)

    async def review(self, identity, record_id, decision, comment):
        return await self._append(identity, record_id, decision, comment, reviewing=True)
