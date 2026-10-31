"""自动演示使用独立示例记录，逐步复用实际工单流转。"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from zhengwu_precheck.settings import Settings


class WorkflowDemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('zhengwu_precheck.core.services.workflow_demo'),
                             '缺少网页自动演示服务')
        from zhengwu_precheck.core.services.workflow_demo import WorkflowDemo
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = WorkflowDemo(Path(self.temp.name) / 'workflow-demo')
        self.addAsyncCleanup(self.service.close)

    async def test_both_cases_have_real_messages_transfers_and_confirmed_closure(self):
        for preset in ('residence-supplement', 'business-address'):
            state = await self.service.create('viewer-one', preset, 'start-' + preset)
            run_id = state['id']
            self.assertEqual(state['completed_steps'], 0)
            self.assertEqual(state['view_role'], 'citizen')
            for step in range(11):
                state = await self.service.advance('viewer-one', run_id, step)
                self.assertEqual(state['completed_steps'], step + 1)
            self.assertEqual(state['ticket']['status'], 'closed')
            self.assertEqual(state['ticket']['close_reason'], 'confirmed_resolved')
            self.assertEqual([t['target_role'] for t in state['transfers']], ['reviewer', 'director'])
            self.assertTrue(all(t['status'] == 'accepted' for t in state['transfers']))
            self.assertEqual(len(state['messages']), 5)
            self.assertEqual(state['transitions'][-1]['action'], 'confirm')
            self.assertIsNone(state['ticket']['formal_case_id'])
        self.assertFalse((Path(self.temp.name) / 'chat.sqlite3').exists())

    async def test_start_and_step_retries_preserve_one_run_and_one_action(self):
        state = await self.service.create('viewer-one', 'residence-supplement', 'same-start-key')
        repeated = await self.service.create('viewer-one', 'residence-supplement', 'same-start-key')
        self.assertEqual(repeated['id'], state['id'])
        results = await asyncio.gather(*(self.service.advance('viewer-one', state['id'], 0) for _ in range(2)))
        self.assertTrue(all(result['completed_steps'] == 1 for result in results))
        current = await self.service.detail('viewer-one', state['id'])
        self.assertEqual(len(current['messages']), 1)
        fresh = await self.service.create('viewer-one', 'residence-supplement', 'fresh-start-key')
        self.assertNotEqual(fresh['ticket']['id'], state['ticket']['id'])
        self.assertEqual(fresh['completed_steps'], 0)

    async def test_progress_survives_service_restart(self):
        from zhengwu_precheck.core.services.workflow_demo import WorkflowDemo
        state = await self.service.create('viewer-one', 'business-address', 'restart-start')
        await self.service.advance('viewer-one', state['id'], 0)
        await self.service.close()
        self.service = WorkflowDemo(Path(self.temp.name) / 'workflow-demo')
        self.addAsyncCleanup(self.service.close)
        state = await self.service.detail('viewer-one', state['id'])
        self.assertEqual(state['completed_steps'], 1)
        state = await self.service.advance('viewer-one', state['id'], 1)
        self.assertEqual(state['view_role'], 'window')


class WorkflowDemoRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_offers_both_modes_and_demo_never_advances_working_tickets(self):
        from test_chat import FakeQA
        from zhengwu_precheck.main import create_app
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Settings(runtime_dir=Path(directory)), qa=FakeQA())
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                    page = await client.get('/examples/workflow')
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertIn('登录', page.text)
                    await app.state.repo.create_user('workflow-viewer', 'workflow-test-password')
                    await client.get('/')
                    boot = (await client.get('/bootstrap')).json()
                    headers = {'X-CSRF-Token': boot['csrf_token']}
                    response = await client.post('/auth/login', headers=headers,
                        json={'username': 'workflow-viewer', 'password': 'workflow-test-password'})
                    self.assertEqual(response.status_code, 200, response.text)
                    page = await client.get('/my/tickets')
                    self.assertIn('自动演示', page.text)
                    response = await client.post('/examples/api/workflows', headers=headers,
                        json={'preset_id': 'business-address', 'idempotency_key': 'route-create-demo'})
                    self.assertEqual(response.status_code, 200, response.text)
                    state = response.json()
                    for step in range(11):
                        response = await client.post('/examples/api/workflows/' + state['id'] + '/next',
                            headers=headers, json={'expected_step': step})
                        self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()['ticket']['status'], 'closed')
                    self.assertEqual((await client.get('/tickets')).json()['total'], 0)
                    async with app.state.repo.engine.connect() as connection:
                        self.assertEqual((await connection.execute(text('SELECT COUNT(*) FROM tickets'))).scalar(), 0)
