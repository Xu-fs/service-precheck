"""真实SQLite验证提交确认，不能以模型文本或旧入口绕过。"""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository
from zhengwu_precheck.infrastructure.agent_repository import AgentRepository
from zhengwu_precheck.core.errors import AppError


class ConfirmedTicketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = ChatRepository(Path(self.tmp.name) / 'db.sqlite3')
        await self.chat.initialize()
        self.staff = StaffRepository(self.chat.engine)
        await self.staff.initialize()
        async with self.chat.engine.begin() as c:
            await c.execute(text("INSERT INTO users VALUES('alice','alice','TEST','active'),('bob','bob','TEST','active')"))
        self.now = 1000
        self.agent = AgentRepository(self.chat.engine, clock=lambda: self.now)
        await self.agent.initialize()
        self.tickets = TicketRepository(self.chat.engine, clock=lambda: self.now)
        await self.tickets.initialize()
        self.payload = dict(title='示例咨询', content='只共享这段虚构问题', matter_id='residence-permit',
                            scenario_id='rental-demo', shared_material_ids=[])

    async def asyncTearDown(self):
        await self.chat.close()
        self.tmp.cleanup()

    async def prepare(self, payload=None):
        draft = await self.agent.create_draft('alice', payload or self.payload)
        confirmation = await self.agent.confirm_draft('alice', draft['id'], draft['version'])
        return draft, confirmation

    async def submit(self, draft, confirmation, key='confirmed-key-1', user='alice'):
        return await self.tickets.create_confirmed(user, draft['id'], draft['version'], confirmation['id'], key)

    async def test_legacy_create_requires_real_confirmation(self):
        with self.assertRaises(AppError) as error:
            await self.tickets.create('alice', {**self.payload, 'idempotency_key':'raw-key-123'})
        self.assertEqual(error.exception.code, 'SUBMISSION_CONFIRMATION_REQUIRED')

    async def test_changed_expired_and_cross_user_confirmations_rejected(self):
        draft, confirmation = await self.prepare()
        with self.assertRaises(AppError):
            await self.submit(draft, confirmation, user='bob')
        self.now = 1601
        with self.assertRaises(AppError):
            await self.submit(draft, confirmation)
        self.now = 1000
        await self.agent.update_draft('alice', draft['id'], 1, {**self.payload, 'content':'改过的内容'})
        with self.assertRaises(AppError):
            await self.submit(draft, confirmation)
        self.assertEqual((await self.tickets.citizen_list('alice'))['total'], 0)

    async def test_concurrent_and_new_key_repetition_creates_once(self):
        draft, confirmation = await self.prepare()
        results = await asyncio.gather(self.submit(draft, confirmation), self.submit(draft, confirmation))
        self.assertEqual(results[0]['id'], results[1]['id'])
        repeated = await self.submit(draft, confirmation, key='another-key-123')
        self.assertEqual(repeated['id'], results[0]['id'])
        self.assertEqual((await self.tickets.citizen_list('alice'))['total'], 1)

    async def test_unknown_matter_is_not_visible_to_ordinary_wildcard(self):
        await self.staff.grant_role('bob', 'window', '*', actor='test')
        draft, confirmation = await self.prepare({**self.payload, 'matter_id':'unassigned', 'scenario_id':'manual-triage'})
        ticket = await self.submit(draft, confirmation)
        identity = {'user_id':'bob'}
        self.assertEqual((await self.tickets.staff_list(identity))['total'], 0)
        await self.staff.grant_role('bob', 'window', 'unassigned', actor='test')
        self.assertEqual((await self.tickets.staff_list(identity))['total'], 1)
        accepted = await self.tickets.staff_action(identity, ticket['id'], 'accept',
            dict(expected_version=1, idempotency_key='triage-accept-123'))
        self.assertEqual(accepted['owner_id'], 'bob')

    async def test_material_changes_invalidate_frozen_sharing(self):
        from zhengwu_precheck.infrastructure.material_repository import MaterialRepository
        materials=MaterialRepository(self.chat.engine,Path(self.tmp.name)/'private')
        await materials.initialize()
        async with self.chat.engine.begin() as c:
            await c.execute(text("INSERT INTO conversations(id,user_id,created_at) VALUES('case-a','alice',1000)"))
        image=(Path(__file__).resolve().parents[1]/'data/materials/fictional-identity.png').read_bytes()
        material=await materials.store('alice','case-a','虚构.png',image,True)
        payload={**self.payload,'case_id':'case-a','shared_material_ids':[material['id']],
            'shared_material_versions':[{'id':material['id'],'version':material['version'],'sha256':material['sha256']}]}
        draft,confirmation=await self.prepare(payload)
        await materials.correct_fields('alice',material['id'],material['version'],'identity',{'name':'示例甲'})
        with self.assertRaises(AppError) as error:
            await self.submit(draft,confirmation)
        self.assertEqual(error.exception.code,'MATERIAL_CHANGED')
