"""工单路由边界：真实身份与CSRF，仓储使用契约夹具以隔离并行实现。"""
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings
from test_chat import FakeQA

class TicketFixture:
    async def citizen_list(self, user_id, filters):
        return {'items': [], 'total': 0, 'page': 1, 'pages': 1}
    async def notifications(self, user_id):
        return []
    async def staff_list(self, identity, filters):
        return {'items': [], 'total': 0, 'page': 1, 'pages': 1}
    async def staff_detail(self, identity, ticket_id):
        return {'ticket': {'id': ticket_id, 'title': '<script>bad()</script>', 'content': '教学问题', 'matter_id': 'residence-permit', 'status': 'processing', 'owner_id': identity['user_id'], 'owner_role': 'window', 'version': 1}, 'messages': [], 'transfers': [], 'transitions': []}
    async def assignees(self, identity, matter_id, ticket_id):
        return []
    async def staff_action(self, identity, ticket_id, action, data, transfer_id=None):
        return {'id': ticket_id, 'version': 2}

class TicketRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Settings(runtime_dir=Path(self.temp.name), staff_jwt_secret='ticket-test-key-' + 'x9' * 24), qa=FakeQA())
        self.app.state.knowledge_service.run = AsyncMock()
        self.lifecycle = self.app.router.lifespan_context(self.app)
        await self.lifecycle.__aenter__()
        self.app.state.ticket_repo = TicketFixture()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')
        await self.app.state.repo.create_user('ticket-window', 'ticket-test-password')
        await self.app.state.staff_repo.grant_role('ticket-window', 'window', 'residence-permit', actor='test')
    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifecycle.__aexit__(None, None, None)
        self.temp.cleanup()
    async def login_staff(self):
        page = await self.client.get('/staff/login')
        token = re.search('name="csrf_token" value="([^"]+)"', page.text)[1]
        result = await self.client.post('/staff/api/auth/login', headers={'X-CSRF-Token': token}, json={'username':'ticket-window','password':'ticket-test-password'})
        return result.json()['csrf_token']
    async def test_anonymous_api_requires_login_but_page_offers_login(self):
        await self.client.get('/')
        result = await self.client.get('/tickets')
        self.assertEqual(result.status_code, 401, result.text)
        self.assertIn('error', result.json())
        page = await self.client.get('/my/tickets')
        self.assertEqual(page.status_code, 200)
        self.assertIn('登录', page.text)
    async def test_all_staff_actions_require_csrf_including_forms(self):
        await self.login_staff()
        for action in ('accept','transfers','replies'):
            for options in ({'json': {}}, {'data': {'csrf_token':'bad'}}):
                result = await self.client.post('/staff/api/tickets/TK-test/' + action, **options)
                self.assertEqual(result.status_code, 403, result.text)
    async def test_staff_detail_escapes_user_content_and_form_redirects(self):
        token = await self.login_staff()
        result = await self.client.get('/staff/tickets/TK-test')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn('<script>bad()</script>', result.text)
        self.assertIn('&lt;script&gt;', result.text)
        result = await self.client.post('/staff/api/tickets/TK-test/accept', data={'csrf_token':token,'expected_version':'1','idempotency_key':'test-action-123'})
        self.assertEqual(result.status_code, 303, result.text)
        self.assertEqual(result.headers['location'], '/staff/tickets/TK-test')
    async def test_malformed_json_and_missing_version_have_unified_errors(self):
        token = await self.login_staff()
        result = await self.client.post('/staff/api/tickets/TK-test/replies', content='{bad', headers={'Content-Type':'application/json','X-CSRF-Token':token})
        self.assertEqual(result.status_code, 400, result.text)
        result = await self.client.post('/staff/api/tickets/TK-test/replies', json={'content':'教学答复','kind':'result','idempotency_key':'test-key-123'}, headers={'X-CSRF-Token':token})
        self.assertEqual(result.status_code, 422, result.text)




class TicketIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """使用真实SQLite仓储、JWT、Cookie、CSRF走完两端闭环。"""
    async def asyncSetUp(self):
        await TicketRouteTests.asyncSetUp(self)
        from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository
        self.app.state.ticket_repo = TicketRepository(self.app.state.repo.engine)
        await self.app.state.ticket_repo.initialize()
        self.staff_clients = []
        for role, matter in [('reviewer','residence-permit'),('director','residence-permit'),('hotline','residence-permit'),('engineer','*'),('window','sole-trader-registration')]:
            username = 'integration-' + role
            await self.app.state.repo.create_user(username, 'ticket-test-password')
            await self.app.state.staff_repo.grant_role(username, role, matter, actor='test')
    async def asyncTearDown(self):
        for client in self.staff_clients:
            await client.aclose()
        await TicketRouteTests.asyncTearDown(self)
    async def staff_client(self, username):
        from zhengwu_precheck.utils.security import digest
        values = await self.app.state.staff_auth.login(username, 'ticket-test-password')
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')
        client.cookies.set('zw_staff_access', values['access'], path='/staff')
        client.cookies.set('zw_staff_csrf', values['csrf_cookie'], path='/staff')
        client.headers['X-CSRF-Token'] = digest('staff-csrf:' + values['csrf_cookie'])
        self.staff_clients.append(client)
        return client
    async def test_real_two_sided_workflow_and_object_authorization(self):
        import secrets
        boot = (await self.client.get('/bootstrap')).json()
        headers = {'X-CSRF-Token': boot['csrf_token']}
        login = await self.client.post('/auth/login', headers=headers, json={'username':'ticket-window','password':'ticket-test-password'})
        self.assertEqual(login.status_code, 200, login.text)
        draft=(await self.client.post('/agent/drafts',headers=headers,json={'matter_id':'residence-permit','scenario_id':'rental-demo','title':'教学工单','content':'仅虚构问题','shared_material_ids':[]})).json()
        confirmation=(await self.client.post('/agent/drafts/'+draft['id']+'/confirm',headers=headers,json={'draft_version':draft['version']})).json()
        submit={'draft_id':draft['id'],'draft_version':draft['version'],'submission_confirmation_id':confirmation['id'],'idempotency_key':'integration-create-123'}
        created = await self.client.post('/tickets', headers=headers, json=submit)
        self.assertEqual(created.status_code, 200, created.text)
        ticket = created.json()
        tid = ticket['id']
        self.assertIsNone(ticket['precheck_id'])
        self.assertIsNone(ticket['formal_case_id'])
        # 原生表单复用相同幂等键，证明无脚本创建后备与JSON提交不会重复建单。
        form = {'csrf_token':boot['csrf_token'],**{k:str(v) for k,v in submit.items()}}
        native = await self.client.post('/tickets', data=form)
        self.assertEqual(native.status_code, 303, native.text)
        self.assertIn(tid, native.headers['location'])
        blocked = await self.client.post('/tickets', data=form, headers={'Origin':'http://foreign.test'})
        self.assertEqual(blocked.status_code, 403, blocked.text)
        self.assertEqual((await self.client.get('/tickets')).json()['total'], 1)
        page = await self.client.get('/my/tickets?ticket_id=' + tid)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('仅虚构问题', page.text)
        window = await self.staff_client('ticket-window')
        reviewer = await self.staff_client('integration-reviewer')
        director = await self.staff_client('integration-director')
        hotline = await self.staff_client('integration-hotline')
        engineer = await self.staff_client('integration-engineer')
        wrong_matter = await self.staff_client('integration-window')
        self.assertEqual((await engineer.get('/staff/api/tickets')).status_code, 403)
        self.assertEqual((await wrong_matter.get('/staff/api/tickets/' + tid)).status_code, 404)
        self.assertEqual((await director.get('/staff/api/tickets/' + tid)).status_code, 404)
        async def staff_write(client, suffix, **data):
            nonlocal ticket
            result = await client.post('/staff/api/tickets/' + tid + suffix, json={'expected_version':ticket['version'], 'idempotency_key':secrets.token_hex(16), **data})
            self.assertEqual(result.status_code, 200, result.text)
            ticket = result.json()
            return ticket
        await staff_write(window, '/accept')
        page = await window.get('/staff/tickets/' + tid)
        self.assertEqual(page.status_code, 200, page.text)
        reviewer_id = (await reviewer.get('/staff/api/me')).json()['user_id']
        await staff_write(window, '/transfers', target_user_id=reviewer_id, target_role='reviewer', reason='专业教学问题')
        detail = (await reviewer.get('/staff/api/tickets/' + tid)).json()
        self.assertNotEqual(detail['ticket']['owner_id'], reviewer_id)
        transfer_id = detail['transfers'][-1]['id']
        await staff_write(reviewer, '/transfers/' + transfer_id + '/accept')
        await staff_write(reviewer, '/replies', content='请补充教学信息', kind='supplement')
        self.assertEqual(ticket['status'], 'waiting_citizen')
        result = await self.client.post('/tickets/' + tid + '/messages', headers=headers, json={'expected_version':ticket['version'],'idempotency_key':'citizen-message-123','content':'补充的虚构信息'})
        self.assertEqual(result.status_code, 200, result.text)
        ticket = result.json()
        self.assertEqual(ticket['status'], 'processing')
        self.assertIsNone(ticket['close_due'])
        hot = await hotline.get('/staff/api/tickets/' + tid)
        self.assertEqual(hot.status_code, 200, hot.text)
        self.assertEqual(hot.json()['transfers'], [])
        denied = await hotline.post('/staff/api/tickets/' + tid + '/replies', json={'expected_version':ticket['version'],'idempotency_key':'hotline-denied-123','content':'越权答复','kind':'result'})
        self.assertEqual(denied.status_code, 403, denied.text)
        director_id = (await director.get('/staff/api/me')).json()['user_id']
        await staff_write(reviewer, '/transfers', target_user_id=director_id, target_role='director', reason='手动升级教学异常')
        detail = (await director.get('/staff/api/tickets/' + tid)).json()
        await staff_write(director, '/transfers/' + detail['transfers'][-1]['id'] + '/accept')
        page = await director.get('/staff/tickets/' + tid)
        self.assertEqual(page.status_code, 200, page.text)
        await staff_write(director, '/replies', content='教学处理结果', kind='result')
        result = await self.client.post('/tickets/' + tid + '/confirm-resolved', headers=headers, json={'expected_version':ticket['version'],'idempotency_key':'citizen-confirm-123'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['close_reason'], 'confirmed_resolved')
        notes = (await self.client.get('/notifications')).json()
        self.assertTrue(notes)
        read = await self.client.post('/notifications/' + notes[0]['id'] + '/read', headers=headers, json={'expected_version':0,'idempotency_key':'read-notification-123'})
        self.assertEqual(read.status_code, 200, read.text)
        self.assertTrue((await self.client.get('/notifications')).json()[0]['read_at'])
        # 新登录用户不能继承同浏览器上个账号的工单访问。
        await self.app.state.repo.create_user('another-citizen', 'ticket-test-password')
        await self.client.post('/auth/login', headers=headers, json={'username':'another-citizen','password':'ticket-test-password'})
        self.assertEqual((await self.client.get('/tickets/' + tid)).status_code, 404)


