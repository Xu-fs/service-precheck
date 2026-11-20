"""预设案例走真实草稿、确认、工单消息和岗位流转。"""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from zhengwu_precheck.settings import Settings
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository


class DemoCaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('create_demo_cases'), '缺少两个预设案例的初始化入口')
        from create_demo_cases import provision_cases
        self.provision = provision_cases
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(runtime_dir=Path(self.tmp.name))

    async def details(self, runs):
        repo = ChatRepository(self.settings.runtime_dir / 'chat.sqlite3')
        try:
            citizen = await StaffRepository(repo.engine).user('demo_citizen')
            tickets = TicketRepository(repo.engine)
            return [await tickets.citizen_detail(citizen['id'], run['ticket_id']) for run in runs]
        finally:
            await repo.close()

    async def test_two_queued_examples_have_actual_citizen_messages_and_repeat_without_duplicates(self):
        first = await self.provision(self.settings)
        second = await self.provision(self.settings)
        self.assertEqual([r['ticket_id'] for r in first], [r['ticket_id'] for r in second])
        self.assertEqual(len(first), 2)
        details = await self.details(first)
        self.assertEqual({d['ticket']['matter_id'] for d in details}, {'residence-permit', 'sole-trader-registration'})
        for detail in details:
            self.assertEqual(detail['ticket']['status'], 'queued')
            self.assertIn('（示例）', detail['ticket']['title'])
            self.assertEqual(len(detail['messages']), 1)
            self.assertEqual(detail['messages'][0]['actor_type'], 'citizen')
            self.assertIsNone(detail['ticket']['formal_case_id'])

    async def test_both_examples_finish_with_two_accepted_transfers_and_citizen_confirmation(self):
        queued = await self.provision(self.settings)
        completed = await self.provision(self.settings, complete=True)
        self.assertEqual([r['ticket_id'] for r in queued], [r['ticket_id'] for r in completed])
        details = await self.details(completed)
        for detail in details:
            self.assertEqual(detail['ticket']['status'], 'closed')
            self.assertEqual(detail['ticket']['owner_role'], 'director')
            self.assertEqual(detail['ticket']['close_reason'], 'confirmed_resolved')
            self.assertEqual([t['target_role'] for t in detail['transfers']], ['reviewer', 'director'])
            self.assertTrue(all(t['status'] == 'accepted' for t in detail['transfers']))
            self.assertEqual([m['actor_type'] for m in detail['messages']], ['citizen', 'staff', 'citizen', 'staff', 'staff'])
            self.assertEqual([t['action'] for t in detail['transitions']].count('transfer_accept'), 2)
            self.assertEqual(detail['transitions'][-1]['action'], 'confirm')
        again = await self.provision(self.settings, complete=True)
        self.assertEqual(await self.details(again), details)

    async def test_new_run_preserves_completed_examples_and_creates_two_fresh_queued_cases(self):
        old = await self.provision(self.settings, complete=True)
        fresh = await self.provision(self.settings, new_run=True)
        self.assertTrue(set(r['ticket_id'] for r in old).isdisjoint(r['ticket_id'] for r in fresh))
        self.assertTrue(all(d['ticket']['status'] == 'closed' for d in await self.details(old)))
        self.assertTrue(all(d['ticket']['status'] == 'queued' for d in await self.details(fresh)))

    async def test_manual_handling_is_preserved_instead_of_being_overwritten_by_replay(self):
        from zhengwu_precheck.core.errors import AppError
        runs = await self.provision(self.settings)
        repo = ChatRepository(self.settings.runtime_dir / 'chat.sqlite3')
        try:
            window = await StaffRepository(repo.engine).user('demo_window')
            tickets = TicketRepository(repo.engine)
            current = (await self.details(runs))[0]['ticket']
            accepted = await tickets.staff_action({'user_id': window['id']}, current['id'], 'accept', {
                'expected_version': current['version'], 'idempotency_key': 'manual-example-window-accept'})
            with self.assertRaises(AppError):
                await self.provision(self.settings, complete=True)
            self.assertEqual((await self.details(runs))[0]['ticket'], accepted)
        finally:
            await repo.close()


class DemoCasePageTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_prefills_both_examples_without_submitting_records(self):
        import httpx
        from test_chat import FakeQA
        from zhengwu_precheck.main import create_app
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Settings(runtime_dir=Path(directory)), qa=FakeQA())
            async with app.router.lifespan_context(app):
                await app.state.repo.create_user('example-reader', 'example-test-password')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                    await client.get('/')
                    boot = (await client.get('/bootstrap')).json()
                    response = await client.post('/auth/login', headers={'X-CSRF-Token': boot['csrf_token']},
                        json={'username': 'example-reader', 'password': 'example-test-password'})
                    self.assertEqual(response.status_code, 200, response.text)
                    page = await client.get('/my/tickets')
                    self.assertIn('预设案例（示例）', page.text)
                    for key, matter in [('residence-supplement', 'residence-permit'), ('business-address', 'sole-trader-registration')]:
                        page = await client.get('/my/tickets', params={'example': key})
                        self.assertEqual(page.status_code, 200, page.text)
                        self.assertIn('（示例）', page.text)
                        self.assertRegex(page.text, 'value="' + matter + '"\\s+selected')
                        self.assertRegex(page.text, 'id="ticket-title"[^>]*value="[^"]+（示例）"')
                    self.assertEqual((await client.get('/tickets')).json()['total'], 0)
