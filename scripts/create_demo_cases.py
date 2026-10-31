"""显式创建或演示两个虚构案例；复用现有确认、工单和授权服务。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from sqlalchemy import text
from create_demo_accounts import provision
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.demo_cases import workflow_cases, workflow_steps
from zhengwu_precheck.infrastructure.agent_repository import AgentRepository
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository
from zhengwu_precheck.settings import Settings


async def _draft(repo, agent, case, citizen, run_key):
    async with repo.engine.begin() as conn:
        row = (await conn.execute(text('SELECT * FROM demo_case_runs WHERE run_key=:key'), {'key': run_key})).mappings().first()
    if not row:
        payload = {key: case[key] for key in ('title', 'content', 'matter_id', 'scenario_id')}
        payload['shared_material_ids'] = []
        draft = await agent.create_draft(citizen, payload)
        async with repo.engine.begin() as conn:
            await conn.execute(text('INSERT OR IGNORE INTO demo_case_runs VALUES(:key,:preset,:user,:draft)'),
                {'key': run_key, 'preset': case['id'], 'user': citizen, 'draft': draft['id']})
            row = (await conn.execute(text('SELECT * FROM demo_case_runs WHERE run_key=:key'), {'key': run_key})).mappings().one()
    if row['user_id'] != citizen or row['preset_id'] != case['id']:
        raise ValueError('示例归属记录不匹配，已保留现有资料。')
    draft = await agent.get_draft(citizen, row['draft_id'])
    if any(draft['data'].get(key) != case[key] for key in ('title', 'content', 'matter_id', 'scenario_id')):
        raise ValueError('示例草稿已被修改，保留现有内容；请使用 --new 创建新案例。')
    return draft


async def _advance(repo, tickets, case, accounts, ticket, complete):
    citizen = accounts['citizen']
    actions = [(step['role'], step['action'], step['data']) for step in workflow_steps(case, accounts)]
    # 这里只读取本案例的既有操作回执；每个新动作仍交给原业务服务校验授权和版本。
    for index, (role, action, payload) in enumerate(actions if complete else actions[:1], 1):
        actor = accounts[role]
        key = f'demo-case:{ticket["id"]}:{index}'
        async with repo.engine.connect() as conn:
            receipt = (await conn.execute(text('SELECT result_json FROM ticket_operations WHERE actor_id=:actor AND op_key=:key'),
                {'actor': actor, 'key': key})).scalar()
        if receipt:
            ticket = json.loads(receipt)
            continue
        data = {'expected_version': ticket['version'], 'idempotency_key': key, **payload}
        if role == 'citizen':
            ticket = await tickets.citizen_action(citizen, ticket['id'], action, data)
        else:
            transfer_id = None
            if action == 'transfer_accept':
                detail = await tickets.staff_detail({'user_id': actor}, ticket['id'])
                transfer_id = next(tr['id'] for tr in detail['transfers']
                    if tr['status'] == 'pending' and tr['target_role'] == role
                    and tr['target_user_id'] in (None, actor))
            ticket = await tickets.staff_action({'user_id': actor}, ticket['id'], action, data, transfer_id)
    return (await tickets.citizen_detail(citizen, ticket['id']))['ticket']


async def provision_cases(settings, *, complete=False, new_run=False):
    accounts = {account['role']: account['user_id'] for account in await provision(settings)}
    repo = ChatRepository(settings.runtime_dir / 'chat.sqlite3')
    try:
        await repo.initialize()
        await StaffRepository(repo.engine).initialize()
        tickets, agent = TicketRepository(repo.engine), AgentRepository(repo.engine)
        await tickets.initialize()
        await agent.initialize()
        async with repo.engine.begin() as conn:
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS demo_case_runs(
                run_key TEXT PRIMARY KEY,preset_id TEXT NOT NULL,user_id TEXT NOT NULL REFERENCES users(id),
                draft_id TEXT NOT NULL REFERENCES agent_drafts(id))'''))
        run = str(uuid4()) if new_run else 'default'
        results = []
        for case in workflow_cases():
            run_key = case['id'] + ':' + run
            draft = await _draft(repo, agent, case, accounts['citizen'], run_key)
            confirmation = await agent.confirm_draft(accounts['citizen'], draft['id'], draft['version'])
            ticket = await tickets.create_confirmed(accounts['citizen'], draft['id'], draft['version'],
                confirmation['id'], 'demo-case:create:' + draft['id'])
            current = (await tickets.citizen_detail(accounts['citizen'], ticket['id']))['ticket']
            if any(current[key] != case[key] for key in ('title', 'content', 'matter_id', 'scenario_id')):
                raise ValueError('示例工单内容已变化，保留现有记录；请使用 --new 创建新案例。')
            ticket = await _advance(repo, tickets, case, accounts, ticket, complete)
            results.append({'preset_id': case['id'], 'title': case['title'], 'ticket_id': ticket['id'], 'status': ticket['status']})
        return results
    finally:
        await repo.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='创建两个可逐级处理并结案的预设案例（示例）')
    parser.add_argument('--complete', action='store_true', help='用示例账号顺序执行全部步骤并确认结案')
    parser.add_argument('--new', action='store_true', dest='new_run', help='保留原案例，再创建一对新案例')
    args = parser.parse_args(argv)
    settings = Settings()
    try:
        runs = asyncio.run(provision_cases(settings, complete=args.complete, new_run=args.new_run))
    except (AppError, OSError, ValueError) as error:
        message = error.message if isinstance(error, AppError) else str(error)
        print('示例案例操作停止：' + message + '；已保留现有记录。若已手动处理，请用 --new 新建示例。', file=sys.stderr)
        return 1
    print('两个虚构预设案例已准备；新建示例账号默认密码为 123456，已有密码保持原样。')
    for run in runs:
        print(f'{run["title"]}：{run["status"]} /my/tickets?ticket_id={run["ticket_id"]}')
    print('办事人：demo_citizen；窗口：demo_window；审核员：demo_reviewer；主任：demo_director。')
    print('首次创建进入窗口待办；--complete 可完整演示，--new 可保留旧记录再生成新案例。')
    print('运行库：' + str(settings.runtime_dir / 'chat.sqlite3'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
