"""工单独立仓储：责任切换、轮次任务、通知和留痕在同一短事务提交。

不读取材料正文、不调用模型、不创建预检或正式办件。SQL封装在此处，迁移其他
数据库时保留方法契约、唯一键与事务语义，将SQLite写锁改为行锁/条件更新。
"""
import asyncio
import hashlib
import json
import time
from uuid import uuid4

from sqlalchemy import text
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.chat import MATTERS
from zhengwu_precheck.core.services.ticket_clock import WorkCalendar, waiting_deadlines
from zhengwu_precheck.core.staff_permissions import allows

SCHEMA = [
    'CREATE TABLE IF NOT EXISTS ticket_schema_versions(version INTEGER PRIMARY KEY)',
    '''CREATE TABLE IF NOT EXISTS tickets(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), matter_id TEXT NOT NULL,
        scenario_id TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','processing','waiting_citizen','closed')),
        owner_id TEXT REFERENCES users(id),owner_role TEXT,version INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,accept_due REAL NOT NULL,first_reply_due REAL NOT NULL,
        first_reply_at REAL,round_no INTEGER NOT NULL DEFAULT 0,last_staff_reply REAL,close_due REAL,
        close_reason TEXT,calendar_snapshot TEXT NOT NULL,
        conversation_id TEXT REFERENCES conversations(id),message_id TEXT REFERENCES messages(id),
        precheck_id TEXT CHECK(precheck_id IS NULL),formal_case_id TEXT CHECK(formal_case_id IS NULL))''',
    '''CREATE TABLE IF NOT EXISTS ticket_messages(id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL REFERENCES tickets(id),
        actor_id TEXT NOT NULL REFERENCES users(id),actor_type TEXT NOT NULL,kind TEXT NOT NULL,
        content TEXT NOT NULL,created_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS ticket_transfers(id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL REFERENCES tickets(id),
        from_user_id TEXT NOT NULL REFERENCES users(id),target_user_id TEXT NOT NULL REFERENCES users(id),
        target_role TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,
        created_at REAL NOT NULL,decided_at REAL,decision_reason TEXT)''',
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_ticket_pending_transfer ON ticket_transfers(ticket_id) WHERE status='pending'",
    '''CREATE TABLE IF NOT EXISTS ticket_transitions(id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL REFERENCES tickets(id),
        actor_id TEXT,action TEXT NOT NULL,from_status TEXT,to_status TEXT NOT NULL,owner_id TEXT,
        round_no INTEGER NOT NULL,reason TEXT,occurred_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS ticket_notifications(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
        ticket_id TEXT NOT NULL REFERENCES tickets(id),kind TEXT NOT NULL,content TEXT NOT NULL,
        dedup_key TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,read_at REAL)''',
    '''CREATE TABLE IF NOT EXISTS ticket_jobs(id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL REFERENCES tickets(id),
        round_no INTEGER NOT NULL,kind TEXT NOT NULL,due_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
        UNIQUE(ticket_id,round_no,kind))''',
    '''CREATE TABLE IF NOT EXISTS ticket_operations(actor_id TEXT NOT NULL,op_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,PRIMARY KEY(actor_id,op_key))''',
    'CREATE INDEX IF NOT EXISTS ix_ticket_owner ON tickets(owner_id,status,created_at)',
    'CREATE INDEX IF NOT EXISTS ix_ticket_user ON tickets(user_id,created_at)',
    'CREATE INDEX IF NOT EXISTS ix_ticket_due ON ticket_jobs(status,due_at)',
]


def fail(code='TICKET_CONFLICT', message='工单状态已变化，请刷新后重试', status=409):
    raise AppError(status,code,message)


def field(data, name, limit=4000):
    value = data.get(name)
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        fail('INVALID_TICKET_INPUT','请填写有效的'+name+'字段',422)
    return value.strip()


class TicketRepository:
    def __init__(self, engine, calendar=None, clock=time.time):
        self.engine,self.calendar,self.clock = engine,calendar or WorkCalendar.configured(),clock
        self.stopping = asyncio.Event()

    async def initialize(self):
        async with self.engine.begin() as c:
            for sql in SCHEMA:
                await c.execute(text(sql))
            versions = (await c.execute(text('SELECT version FROM ticket_schema_versions'))).scalars().all()
            if versions and versions != [1]:
                raise RuntimeError('工单表版本不兼容，请先迁移')
            await c.execute(text('INSERT OR IGNORE INTO ticket_schema_versions VALUES(1)'))

    async def _lock(self,c):
        # SQLite先取写锁再读状态；多连接不能同时根据旧版本作出成功决策。
        await c.execute(text('UPDATE ticket_schema_versions SET version=version WHERE version=1'))

    async def _grants(self,c,uid):
        rows = (await c.execute(text('''SELECT g.role_code,g.matter_id,p.permission_code FROM user_role_scopes g
            JOIN staff_accounts a ON a.user_id=g.user_id JOIN users u ON u.id=g.user_id
            JOIN role_permissions p ON p.role_code=g.role_code
            WHERE g.user_id=:uid AND a.status='active' AND u.status='active'
            AND (g.valid_until IS NULL OR g.valid_until>:now)'''),{'uid':uid,'now':self.clock()})).mappings().all()
        groups={}
        for r in rows:
            g=groups.setdefault((r['role_code'],r['matter_id']),{'role_code':r['role_code'],'matter_id':r['matter_id'],'permissions':[]})
            g['permissions'].append(r['permission_code'])
        return list(groups.values())

    def _role(self,grants,role,matter,permission):
        return allows([g for g in grants if g['role_code']==role],permission,matter)

    async def _ticket(self,c,tid):
        row=(await c.execute(text('SELECT * FROM tickets WHERE id=:id'),{'id':tid})).mappings().first()
        if not row: fail('TICKET_NOT_FOUND','工单不存在或无权访问',404)
        return dict(row)

    async def _citizen(self,c,uid,t):
        if not uid: fail('LOGIN_REQUIRED','请先登录后使用个人工单',401)
        if uid!=t['user_id']: fail('TICKET_NOT_FOUND','工单不存在或无权访问',404)

    async def _replay(self,c,uid,operation,data):
        key=field(data,'idempotency_key',128)
        fingerprint=hashlib.sha256(json.dumps({'operation':operation,'data':data},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        row=(await c.execute(text('SELECT * FROM ticket_operations WHERE actor_id=:u AND op_key=:k'),{'u':uid,'k':key})).mappings().first()
        if row:
            if row['fingerprint']!=fingerprint: fail('IDEMPOTENCY_CONFLICT','重复提交标识已用于其他内容')
            return key,fingerprint,json.loads(row['result_json'])
        return key,fingerprint,None

    async def _save(self,c,uid,key,fingerprint,result):
        await c.execute(text('INSERT INTO ticket_operations VALUES(:u,:k,:f,:r)'),
            {'u':uid,'k':key,'f':fingerprint,'r':json.dumps(result,ensure_ascii=False)})
        return result

    async def _transition(self,c,t,actor,action,previous=None,reason=None):
        now=self.clock()
        await c.execute(text('INSERT INTO ticket_transitions VALUES(:id,:tid,:actor,:action,:old,:new,:owner,:round,:reason,:now)'),
            {'id':str(uuid4()),'tid':t['id'],'actor':actor,'action':action,'old':previous,'new':t['status'],
             'owner':t['owner_id'],'round':t['round_no'],'reason':reason,'now':now})
        await c.execute(text("INSERT INTO audit_logs VALUES(:id,:actor,:action,'ticket',:tid,'success',NULL,:now)"),
            {'id':str(uuid4()),'actor':actor,'action':'ticket_'+action,'tid':t['id'],'now':now})

    async def _notice(self,c,t,kind,key,content):
        await c.execute(text('''INSERT OR IGNORE INTO ticket_notifications
            VALUES(:id,:uid,:tid,:kind,:content,:key,:now,NULL)'''),
            {'id':str(uuid4()),'uid':t['user_id'],'tid':t['id'],'kind':kind,'content':content,'key':key,'now':self.clock()})

    async def _schedule(self,c,t,kind,due,round_no=0):
        await c.execute(text('INSERT INTO ticket_jobs(id,ticket_id,round_no,kind,due_at) VALUES(:id,:tid,:round,:kind,:due)'),
            {'id':str(uuid4()),'tid':t['id'],'round':round_no,'kind':kind,'due':due})

    async def create(self,user_id,data):
        if not user_id: fail('LOGIN_REQUIRED','请先登录后提交工单',401)
        if not isinstance(data,dict): fail('INVALID_TICKET_INPUT','请提供有效工单内容',422)
        if data.get('precheck_id') or data.get('formal_case_id'):
            fail('UNSUPPORTED_LINK','本阶段未接入预检记录与正式办件，不能填写对应编号',422)
        for name in ('conversation_id','message_id'):
            value=data.get(name)
            if value is not None and (not isinstance(value,str) or len(value)>128):
                fail('INVALID_RECORD_LINK','咨询关联编号格式不正确',422)
        matter,scenario=data.get('matter_id'),data.get('scenario_id')
        if not any(m['matter_id']==matter and m['scenario_id']==scenario for m in MATTERS):
            fail('INVALID_MATTER','请选择有效事项与教学情形',422)
        title,content=field(data,'title',120),field(data,'content')
        async with self.engine.begin() as c:
            await self._lock(c)
            key,fp,replay=await self._replay(c,user_id,'create',data)
            if replay:return replay
            # 只保存必要关联标识，不自动授予工作人员完整历史会话正文。
            cid,mid=data.get('conversation_id') or None,data.get('message_id') or None
            if cid:
                row=(await c.execute(text('SELECT * FROM conversations WHERE id=:id'),{'id':cid})).mappings().first()
                if not row or row['user_id']!=user_id or row['matter_id']!=matter or row['scenario_id']!=scenario or not row['confirmed_at']:
                    fail('INVALID_CONVERSATION_LINK','只能关联本人相同事项的已确认咨询',422)
            if mid:
                row=(await c.execute(text('SELECT * FROM messages WHERE id=:id'),{'id':mid})).mappings().first()
                if not cid or not row or row['conversation_id']!=cid or row['matter_id']!=matter:
                    fail('INVALID_MESSAGE_LINK','咨询消息关联无效',422)
            now=self.clock()
            t={'id':'TK-'+str(uuid4()),'user_id':user_id,'matter_id':matter,'scenario_id':scenario,'title':title,'content':content,
               'status':'queued','owner_id':None,'owner_role':None,'version':1,'created_at':now,
               'accept_due':self.calendar.accept_due(now),'first_reply_due':now+48*3600,'first_reply_at':None,
               'round_no':0,'last_staff_reply':None,'close_due':None,'close_reason':None,'calendar_snapshot':self.calendar.snapshot(),
               'conversation_id':cid,'message_id':mid,'precheck_id':None,'formal_case_id':None}
            await c.execute(text('INSERT INTO tickets('+','.join(t)+') VALUES('+','.join(':'+k for k in t)+')'),t)
            await self._transition(c,t,user_id,'created')
            await self._notice(c,t,'submitted',t['id']+':submitted','人工工单已提交窗口队列，不代表正式申请已受理。')
            for kind,due in [('accept_overdue',t['accept_due']),('reply_overdue',t['first_reply_due'])]:
                await self._schedule(c,t,kind,due)
            return await self._save(c,user_id,key,fp,t)

    async def _acl(self,c,identity,include_progress=True):
        """SQL先筛选权限+事项+处理关系，不能先分页再过滤，避免数量泄露。"""
        uid=identity['user_id']; grants=await self._grants(c,uid)
        clauses=[]; params={'viewer':uid}
        for i,g in enumerate(grants):
            p=set(g['permissions']); role=g['role_code']
            if g['matter_id']=='*': scope='1=1'
            else:
                scope=f't.matter_id=:scope{i}'; params[f'scope{i}']=g['matter_id']
            relationship='(t.owner_id=:viewer OR EXISTS(SELECT 1 FROM ticket_transfers tr WHERE tr.ticket_id=t.id AND (tr.from_user_id=:viewer OR tr.target_user_id=:viewer)))'
            if role=='hotline' and 'ticket.progress' in p and include_progress: relation='1=1'
            elif role=='window' and 'ticket.handle' in p: relation=f"(t.status='queued' OR {relationship})"
            elif role=='reviewer' and 'ticket.handle' in p: relation=relationship
            elif role=='director' and 'ticket.escalated' in p: relation=relationship
            else: continue
            clauses.append(f'({scope} AND {relation})')
        return '('+' OR '.join(clauses or ['0=1'])+')',params,grants

    def _summary(self,t):
        # 列表不含个人正文、原会话标识或日历明细。
        return {k:t[k] for k in ('id','matter_id','scenario_id','title','status','owner_id','owner_role','version','created_at',
             'accept_due','first_reply_due','first_reply_at','round_no','close_due','close_reason')}

    async def _list(self,c,where,params,filters):
        filters=filters or {}
        try: page=max(1,int(filters.get('page',1))); limit=min(100,max(1,int(filters.get('limit',20))))
        except (TypeError,ValueError):fail('INVALID_FILTER','分页格式不正确',422)
        for key in ('matter_id','status'):
            if filters.get(key):
                where+=f' AND t.{key}=:{key}'; params[key]=str(filters[key])
        if filters.get('view')=='mine': where+=' AND t.owner_id=:viewer'
        if filters.get('view')=='queue':where+=" AND t.status='queued' AND t.owner_id IS NULL"
        if filters.get('view')=='pending':where+=" AND EXISTS(SELECT 1 FROM ticket_transfers tr WHERE tr.ticket_id=t.id AND tr.target_user_id=:viewer AND tr.status='pending')"
        total=(await c.execute(text('SELECT count(*) FROM tickets t WHERE '+where),params)).scalar_one()
        rows=(await c.execute(text('SELECT t.* FROM tickets t WHERE '+where+' ORDER BY t.created_at DESC,t.id LIMIT :limit OFFSET :offset'),
            {**params,'limit':limit,'offset':(page-1)*limit})).mappings().all()
        return {'items':[self._summary(dict(r)) for r in rows],'total':total,'page':page,'pages':max(1,(total+limit-1)//limit)}

    async def citizen_list(self,user_id,filters=None):
        if not user_id:fail('LOGIN_REQUIRED','请先登录',401)
        async with self.engine.connect() as c:
            return await self._list(c,'t.user_id=:viewer',{'viewer':user_id},filters)

    async def staff_list(self,identity,filters=None):
        async with self.engine.connect() as c:
            acl,params,grants=await self._acl(c,identity)
            if not any(g['role_code'] in ('window','reviewer','hotline','director') and
                bool(set(g['permissions']) & {'ticket.handle','ticket.progress','ticket.escalated'}) for g in grants):
                fail('STAFF_PERMISSION_DENIED','当前岗位无权访问工单列表',403)
            return await self._list(c,acl,params,filters)

    async def _staff_visible(self,c,identity,tid):
        acl,params,grants=await self._acl(c,identity)
        row=(await c.execute(text('SELECT t.* FROM tickets t WHERE t.id=:id AND '+acl),{**params,'id':tid})).mappings().first()
        if not row: fail('TICKET_NOT_FOUND','工单不存在或无权访问',404)
        return dict(row),grants

    async def _detail(self,c,t,hotline=False):
        async def rows(table,order):
            return [dict(r) for r in (await c.execute(text('SELECT * FROM '+table+' WHERE ticket_id=:id ORDER BY '+order),{'id':t['id']})).mappings().all()]
        # 相同时钟值仍按实际插入顺序显示。迁移其他数据库时改为显式自增序列。
        messages=await rows('ticket_messages','created_at,rowid')
        transfers=await rows('ticket_transfers','created_at,rowid')
        transitions=await rows('ticket_transitions','occurred_at,rowid')
        if hotline:
            # 热线只看问题、进度和答复，不泄露内部转派原因、会话链接。
            ticket={**self._summary(t),'content':t['content']}
            transfers=[]
            transitions=[{k:r[k] for k in ('action','to_status','occurred_at')} for r in transitions]
        else: ticket=t
        return {'ticket':ticket,'messages':messages,'transfers':transfers,'transitions':transitions}

    async def citizen_detail(self,user_id,tid):
        async with self.engine.connect() as c:
            t=await self._ticket(c,tid);await self._citizen(c,user_id,t)
            return await self._detail(c,t)

    async def staff_detail(self,identity,tid):
        async with self.engine.connect() as c:
            t,grants=await self._staff_visible(c,identity,tid)
            # 完整详情必须由同一处理岗位的事项+记录关系共同授权；不能借热线
            # 的全事项进度权限再叠加一个无处理关系的审核岗位来获取内部正文。
            acl,params,_=await self._acl(c,identity,include_progress=False)
            handling=(await c.execute(text('SELECT 1 FROM tickets t WHERE t.id=:id AND '+acl),{**params,'id':tid})).first() is not None
            return await self._detail(c,t,hotline=not handling)

    def _version(self,t,data):
        value=data.get('expected_version')
        if isinstance(value,str) and value.isdigit():value=int(value)
        if isinstance(value,bool) or not isinstance(value,int):fail('VERSION_REQUIRED','请提供当前工单版本',422)
        if value!=t['version']:fail()

    def _owner(self,t,uid,grants):
        permission='ticket.escalated' if t['owner_role']=='director' else 'ticket.handle'
        if t['owner_id']!=uid or not self._role(grants,t['owner_role'],t['matter_id'],permission):
            fail('TICKET_HANDLER_REQUIRED','仅当前授权责任人可处理此工单',403)

    async def _cancel_round(self,c,t):
        await c.execute(text("UPDATE ticket_jobs SET status='cancelled' WHERE ticket_id=:id AND round_no=:round AND kind IN ('reminder7','reminder14','close') AND status='pending'"),
            {'id':t['id'],'round':t['round_no']})

    async def _update(self,c,t,values):
        old=t['version']; values={**values,'version':old+1}
        result=await c.execute(text('UPDATE tickets SET '+','.join(k+'=:'+k for k in values)+' WHERE id=:id AND version=:old'),{**values,'id':t['id'],'old':old})
        if result.rowcount!=1:fail()
        return {**t,**values}

    async def citizen_action(self,user_id,tid,action,data):
        if not isinstance(data,dict):fail('INVALID_TICKET_INPUT','请求格式不正确',422)
        async with self.engine.begin() as c:
            await self._lock(c)
            t=await self._ticket(c,tid);await self._citizen(c,user_id,t)
            key,fp,replay=await self._replay(c,user_id,tid+':'+action,data)
            if replay:return replay
            self._version(t,data)
            if t['status']=='closed' or (t['close_due'] is not None and self.clock()>=t['close_due']):fail('TICKET_CLOSED','工单已结束等待期，请新建工单')
            old=t['status']
            if action=='confirm':
                if t['status']!='waiting_citizen':fail()
                values={'status':'closed','close_reason':'confirmed_resolved','close_due':None}
            elif action=='message':
                content=field(data,'content')
                await c.execute(text("INSERT INTO ticket_messages VALUES(:id,:tid,:uid,'citizen','supplement',:body,:now)"),
                    {'id':str(uuid4()),'tid':tid,'uid':user_id,'body':content,'now':self.clock()})
                values={'status':'processing' if t['owner_id'] else 'queued','close_due':None}
            else:fail('INVALID_ACTION','工单操作无效',422)
            await self._cancel_round(c,t)
            t=await self._update(c,t,values)
            await self._transition(c,t,user_id,action,old)
            return await self._save(c,user_id,key,fp,t)

    async def _targets(self,c,t,grants,uid):
        self._owner(t,uid,grants)
        permission={'window':'ticket.transfer','reviewer':'ticket.dispatch'}.get(t['owner_role'])
        if not permission or not self._role(grants,t['owner_role'],t['matter_id'],permission):
            fail('TICKET_TRANSFER_DENIED','当前岗位未获转派权限',403)
        # 窗口专业异常只能先交审核员；审核员再分派实际处理人；升级主任始终手动。
        roles=['window','reviewer','director'] if t['owner_role']=='reviewer' else ['window','reviewer','director'] if t['owner_role']=='window' else ['window','reviewer']
        rows=(await c.execute(text('SELECT u.id,u.username FROM users u JOIN staff_accounts a ON a.user_id=u.id WHERE u.status=\'active\' AND a.status=\'active\' ORDER BY u.username'))).mappings().all()
        result=[]
        for r in rows:
            if r['id']==uid:continue
            target=await self._grants(c,r['id'])
            for role in roles:
                permission='ticket.escalated' if role=='director' else 'ticket.handle'
                if self._role(target,role,t['matter_id'],permission):
                    result.append({'user_id':r['id'],'username':r['username'],'role_code':role})
        return result

    async def assignees(self,identity,matter_id,ticket_id):
        async with self.engine.connect() as c:
            t,grants=await self._staff_visible(c,identity,ticket_id)
            if t['matter_id']!=matter_id:fail('INVALID_MATTER','事项不匹配',422)
            return await self._targets(c,t,grants,identity['user_id'])

    async def staff_action(self,identity,tid,action,data,transfer_id=None):
        if not isinstance(data,dict):fail('INVALID_TICKET_INPUT','请求格式不正确',422)
        uid=identity['user_id']
        async with self.engine.begin() as c:
            await self._lock(c)
            t,grants=await self._staff_visible(c,identity,tid)
            # 即使回放旧成功操作，也必须重新满足当前动作权限，防止降权后通过
            # 幂等缓存取回原先处理人可见的完整内容。
            if action=='accept':
                if not self._role(grants,'window',t['matter_id'],'ticket.handle'):
                    fail('TICKET_HANDLER_REQUIRED','新工单由窗口接手',403)
            elif action in ('reply','transfer','transfer_cancel'):
                self._owner(t,uid,grants)
                if action=='transfer':
                    permission={'window':'ticket.transfer','reviewer':'ticket.dispatch'}.get(t['owner_role'])
                    if not permission or not self._role(grants,t['owner_role'],t['matter_id'],permission):
                        fail('TICKET_TRANSFER_DENIED','当前岗位未获转派权限',403)
            elif action in ('transfer_accept','transfer_reject'):
                target=(await c.execute(text('SELECT target_user_id,target_role FROM ticket_transfers WHERE id=:id AND ticket_id=:tid'),{'id':transfer_id,'tid':tid})).mappings().first()
                p='ticket.escalated' if target and target['target_role']=='director' else 'ticket.handle'
                if not target or target['target_user_id']!=uid or not self._role(grants,target['target_role'],t['matter_id'],p):
                    fail('TICKET_HANDLER_REQUIRED','仅获授权接收人可确认此转派',403)
            key,fp,replay=await self._replay(c,uid,tid+':'+action+':'+str(transfer_id),data)
            if replay:return replay
            self._version(t,data)
            if t['status']=='closed' or (t['close_due'] is not None and self.clock()>=t['close_due']):fail('TICKET_CLOSED','工单已结束等待期')
            old=t['status'];values={};reason=None
            if action=='accept':
                if not self._role(grants,'window',t['matter_id'],'ticket.handle'):fail('TICKET_HANDLER_REQUIRED','新工单由窗口接手',403)
                if t['status']!='queued' or t['owner_id']:fail()
                values={'owner_id':uid,'owner_role':'window','status':'processing'}
            elif action=='transfer':
                self._owner(t,uid,grants)
                reason=field(data,'reason',1000)
                target,role=field(data,'target_user_id',128),field(data,'target_role',32)
                allowed=await self._targets(c,t,grants,uid)
                if not any(r['user_id']==target and r['role_code']==role for r in allowed):fail('INVALID_ASSIGNEE','接收人未获本事项处理授权',403)
                if (await c.execute(text("SELECT 1 FROM ticket_transfers WHERE ticket_id=:id AND status='pending'"),{'id':tid})).first():fail('TRANSFER_PENDING','已有待确认转派，请先接收、拒绝或撤回')
                await c.execute(text("INSERT INTO ticket_transfers VALUES(:id,:tid,:uid,:target,:role,:reason,'pending',:now,NULL,NULL)"),
                    {'id':str(uuid4()),'tid':tid,'uid':uid,'target':target,'role':role,'reason':reason,'now':self.clock()})
            elif action in ('transfer_accept','transfer_reject','transfer_cancel'):
                tr=(await c.execute(text('SELECT * FROM ticket_transfers WHERE id=:id AND ticket_id=:tid'),{'id':transfer_id,'tid':tid})).mappings().first()
                if not tr or tr['status']!='pending' or tr['from_user_id']!=t['owner_id']:fail()
                if action=='transfer_cancel':
                    self._owner(t,uid,grants)
                    status='cancelled';reason=field(data,'reason',1000)
                else:
                    p='ticket.escalated' if tr['target_role']=='director' else 'ticket.handle'
                    if uid!=tr['target_user_id'] or not self._role(grants,tr['target_role'],t['matter_id'],p):fail('TICKET_HANDLER_REQUIRED','仅获授权接收人可确认此转派',403)
                    status='accepted' if action=='transfer_accept' else 'rejected'
                    if status=='rejected':reason=field(data,'reason',1000)
                    else:values={'owner_id':uid,'owner_role':tr['target_role']}
                await c.execute(text('UPDATE ticket_transfers SET status=:status,decided_at=:now,decision_reason=:reason WHERE id=:id'),
                    {'status':status,'now':self.clock(),'reason':reason,'id':transfer_id})
            elif action=='reply':
                self._owner(t,uid,grants)
                content=field(data,'content')
                kind=data.get('kind')
                if kind not in ('supplement','result'):fail('INVALID_REPLY_KIND','请选择补充请求或处理结果',422)
                now=self.clock();deadlines=waiting_deadlines(now)
                await self._cancel_round(c,t)
                values={'status':'waiting_citizen','round_no':t['round_no']+1,'last_staff_reply':now,'close_due':deadlines['close'],
                        'first_reply_at':t['first_reply_at'] if t['first_reply_at'] is not None else now}
                await c.execute(text("INSERT INTO ticket_messages VALUES(:id,:tid,:uid,'staff',:kind,:content,:now)"),
                    {'id':str(uuid4()),'tid':tid,'uid':uid,'kind':kind,'content':content,'now':now})
                for k,due in deadlines.items():await self._schedule(c,t,k,due,values['round_no'])
                await self._notice(c,t,'staff_reply',tid+':reply:'+str(values['round_no']),'工作人员已答复，请查看并补充或确认；工单不替代正式审批。')
            else:fail('INVALID_ACTION','工单操作无效',422)
            t=await self._update(c,t,values)
            await self._transition(c,t,uid,action,old,reason)
            return await self._save(c,uid,key,fp,t)

    async def notifications(self,user_id):
        if not user_id:fail('LOGIN_REQUIRED','请先登录',401)
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(text('SELECT * FROM ticket_notifications WHERE user_id=:id ORDER BY created_at DESC,id'),{'id':user_id})).mappings().all()]

    async def read_notification(self,user_id,identifier):
        async with self.engine.begin() as c:
            changed=await c.execute(text('UPDATE ticket_notifications SET read_at=COALESCE(read_at,:now) WHERE id=:id AND user_id=:uid'),{'id':identifier,'uid':user_id,'now':self.clock()})
            if changed.rowcount!=1:fail('NOTIFICATION_NOT_FOUND','消息不存在或无权访问',404)
        return {'read':True}

    async def run_due(self):
        """短事务取一批任务并执行；通知与done同提交，宕机则整体回滚可恢复。

        先按due排序，确保长时间停机恢复时先补第14天提醒，再关闭；每个任务重新核对轮次。
        """
        async with self.engine.begin() as c:
            await self._lock(c)
            jobs=(await c.execute(text("SELECT * FROM ticket_jobs WHERE status='pending' AND due_at<=:now ORDER BY due_at,CASE kind WHEN 'close' THEN 1 ELSE 0 END,id LIMIT 100"),{'now':self.clock()})).mappings().all()
            for job in jobs:
                t=await self._ticket(c,job['ticket_id']);kind=job['kind']
                if kind in ('reminder7','reminder14','close'):
                    valid=t['status']=='waiting_citizen' and t['round_no']==job['round_no'] and t['close_due'] is not None
                    if valid and kind=='close':
                        if t['close_due']<=self.clock():
                            t=await self._update(c,t,{'status':'closed','close_reason':'default_complete','close_due':None})
                            await self._transition(c,t,None,'default_complete','waiting_citizen','第14天结束，超时默认完成，不代表实际问题解决')
                            await self._notice(c,t,kind,t['id']+':'+str(job['round_no'])+':close','工单已超时默认完成；不代表实际问题已解决或正式办件办结。')
                    elif valid:
                        await self._notice(c,t,kind,t['id']+':'+str(job['round_no'])+':'+kind,'第'+('7' if kind=='reminder7' else '14')+'天提醒：请查看答复，仍有问题可在关闭前补充。')
                elif ((kind=='accept_overdue' and t['owner_id'] is None) or (kind=='reply_overdue' and t['first_reply_at'] is None)) and t['status']!='closed':
                    # 超时仅留痕，不自动升级主任，不改写48小时原期限。
                    await self._transition(c,t,None,kind,t['status'],'已超过教学处理期限，需人工跟进')
                await c.execute(text("UPDATE ticket_jobs SET status='done' WHERE id=:id"),{'id':job['id']})
            return len(jobs)

    async def run(self):
        """轮询只是触发器，任务事实持久化在SQLite；重启后继续处理。"""
        while not self.stopping.is_set():
            try:
                while not self.stopping.is_set() and await self.run_due():pass
            except asyncio.CancelledError:raise
            except Exception:
                # 失败任务仍为pending；不将用户正文或内部响应写日志。
                pass
            try:
                await asyncio.wait_for(self.stopping.wait(),5)
            except TimeoutError:pass

    def stop(self):
        # 优雅关闭：不在aiosqlite建立连接/提交事务的途中取消，避免Windows
        # 后台连接线程仍持有文件句柄。当前短事务完成后立即退出。
        self.stopping.set()
