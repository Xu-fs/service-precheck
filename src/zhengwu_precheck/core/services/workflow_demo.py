"""独立示例数据库中的逐步流程演示；不读取或操作工作库的工单、岗位或材料。"""
import asyncio
from pathlib import Path
import secrets
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text

from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.demo_cases import workflow_cases, workflow_steps
from zhengwu_precheck.infrastructure.agent_repository import AgentRepository
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository


ROLE_NAMES = {'citizen': '办事人', 'window': '窗口工作人员', 'reviewer': '业务审核员', 'director': '服务中心主任'}


class WorkflowDemo:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.repo = ChatRepository(self.directory / 'workflow-examples.sqlite3')
        self.staff = StaffRepository(self.repo.engine)
        self.tickets = TicketRepository(self.repo.engine)
        self.agent = AgentRepository(self.repo.engine)
        self.accounts = {}
        self.ready = False
        self.init_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()

    async def _ensure(self):
        async with self.init_lock:
            if self.ready:
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            await self.repo.initialize()
            await self.staff.initialize()
            await self.tickets.initialize()
            await self.agent.initialize()
            async with self.repo.engine.begin() as connection:
                await connection.execute(text('''CREATE TABLE IF NOT EXISTS workflow_demo_runs(
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, preset_id TEXT NOT NULL,
                    draft_id TEXT NOT NULL, ticket_id TEXT)'''))
            for role in ROLE_NAMES:
                username = 'demo_' + role
                user = await self.staff.user(username)
                if not user:
                    # 密码不用于登录；这些虚构身份只存在于独立演示库。
                    await self.repo.create_user(username, secrets.token_urlsafe(32))
                    user = await self.staff.user(username)
                self.accounts[role] = user['id']
                if role != 'citizen':
                    existing = {(grant['role_code'], grant['matter_id']) for grant in await self.staff.grants(user['id'])}
                    for matter in ('residence-permit', 'sole-trader-registration'):
                        if (role, matter) not in existing:
                            await self.staff.grant_role(username, role, matter, actor='isolated-workflow-example')
            self.ready = True

    @staticmethod
    def _case(preset_id):
        case = next((item for item in workflow_cases() if item['id'] == preset_id), None)
        if not case:
            raise AppError(422, 'INVALID_EXAMPLE', '请选择已有的预设案例')
        return case

    async def _run(self, owner_id, run_id):
        async with self.repo.engine.connect() as connection:
            row = (await connection.execute(text('SELECT * FROM workflow_demo_runs WHERE id=:id AND owner_id=:owner'),
                                           {'id': run_id, 'owner': owner_id})).mappings().first()
        if not row:
            raise AppError(404, 'EXAMPLE_NOT_FOUND', '示例记录不存在，请重新开始演示')
        return dict(row)

    async def create(self, owner_id, preset_id, key):
        case = self._case(preset_id)
        if not owner_id or not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise AppError(422, 'INVALID_EXAMPLE', '请提供有效的示例操作标识')
        await self._ensure()
        run_id = str(uuid5(NAMESPACE_URL, owner_id + ':' + key))
        async with self.action_lock:
            async with self.repo.engine.connect() as connection:
                row = (await connection.execute(text('SELECT * FROM workflow_demo_runs WHERE id=:id'), {'id': run_id})).mappings().first()
            if row and row['preset_id'] != preset_id:
                raise AppError(409, 'EXAMPLE_CHANGED', '本次操作标识已用于另一案例，请重新开始')
            if not row:
                payload = {name: case[name] for name in ('title', 'content', 'matter_id', 'scenario_id')}
                payload['shared_material_ids'] = []
                draft = await self.agent.create_draft(self.accounts['citizen'], payload)
                async with self.repo.engine.begin() as connection:
                    await connection.execute(text('INSERT OR IGNORE INTO workflow_demo_runs VALUES(:id,:owner,:preset,:draft,NULL)'),
                        {'id': run_id, 'owner': owner_id, 'preset': preset_id, 'draft': draft['id']})
            run = await self._run(owner_id, run_id)
            if not run['ticket_id']:
                draft = await self.agent.get_draft(self.accounts['citizen'], run['draft_id'])
                confirmation = await self.agent.confirm_draft(self.accounts['citizen'], draft['id'], draft['version'])
                ticket = await self.tickets.create_confirmed(self.accounts['citizen'], draft['id'], draft['version'],
                    confirmation['id'], 'workflow-demo:create:' + draft['id'])
                async with self.repo.engine.begin() as connection:
                    await connection.execute(text('UPDATE workflow_demo_runs SET ticket_id=:ticket WHERE id=:id'),
                        {'id': run_id, 'ticket': ticket['id']})
        return await self.detail(owner_id, run_id)

    async def detail(self, owner_id, run_id):
        await self._ensure()
        run = await self._run(owner_id, run_id)
        if not run['ticket_id']:
            raise AppError(409, 'EXAMPLE_STARTING', '示例正在创建，请稍后刷新')
        case = self._case(run['preset_id'])
        detail = await self.tickets.citizen_detail(self.accounts['citizen'], run['ticket_id'])
        steps = workflow_steps(case, self.accounts)
        completed = detail['ticket']['version'] - 1
        view = steps[max(0, completed - 1)]['role']
        messages = []
        roles_by_id = {uid: role for role, uid in self.accounts.items()}
        for message in detail['messages']:
            role = roles_by_id[message['actor_id']]
            messages.append({**message, 'role': role, 'role_name': ROLE_NAMES[role]})
        return {'id': run_id, 'preset_id': case['id'], 'title': case['title'], 'ticket': detail['ticket'],
                'completed_steps': completed, 'total_steps': len(steps), 'view_role': view, 'view_name': ROLE_NAMES[view],
                'next_label': steps[completed]['label'] if completed < len(steps) else '本轮示例已结案',
                'steps': [dict(role=step['role'], role_name=ROLE_NAMES[step['role']], label=step['label'],
                              text=step['data'].get('content') or step['data'].get('reason') or step['label']) for step in steps],
                'messages': messages, 'transfers': detail['transfers'], 'transitions': detail['transitions']}

    async def history(self, owner_id):
        await self._ensure()
        async with self.repo.engine.connect() as connection:
            rows = (await connection.execute(text('''SELECT r.id,t.title,t.status,t.created_at
                FROM workflow_demo_runs r JOIN tickets t ON t.id=r.ticket_id
                WHERE r.owner_id=:owner ORDER BY t.created_at DESC LIMIT 20'''), {'owner': owner_id})).mappings().all()
        return [dict(row) for row in rows]

    async def advance(self, owner_id, run_id, expected_step):
        if type(expected_step) is not int or not 0 <= expected_step <= 11:
            raise AppError(422, 'INVALID_EXAMPLE_STEP', '请提供有效的演示步骤')
        await self._ensure()
        async with self.action_lock:
            state = await self.detail(owner_id, run_id)
            current = state['completed_steps']
            # 重试同一步返回已执行结果，不多推进一步。
            if expected_step < current or current == state['total_steps']:
                return state
            if expected_step != current:
                raise AppError(409, 'EXAMPLE_CHANGED', '演示进度已变化，请刷新后继续')
            step = workflow_steps(self._case(state['preset_id']), self.accounts)[current]
            ticket = state['ticket']
            data = {**step['data'], 'expected_version': ticket['version'],
                    'idempotency_key': f'workflow-demo:{run_id}:{current}'}
            try:
                if step['role'] == 'citizen':
                    await self.tickets.citizen_action(self.accounts['citizen'], ticket['id'], step['action'], data)
                else:
                    actor = self.accounts[step['role']]
                    transfer_id = None
                    if step['action'] == 'transfer_accept':
                        transfer_id = next(item['id'] for item in state['transfers']
                            if item['status'] == 'pending' and item['target_role'] == step['role']
                            and item['target_user_id'] in (None, actor))
                    await self.tickets.staff_action({'user_id': actor}, ticket['id'], step['action'], data, transfer_id)
            except AppError as error:
                # 多服务进程同时提交同一步时，以数据库中的已完成版本为准。
                latest = await self.detail(owner_id, run_id)
                if error.status != 409 or latest['completed_steps'] <= current:
                    raise
            return await self.detail(owner_id, run_id)

    async def close(self):
        await self.repo.close()
