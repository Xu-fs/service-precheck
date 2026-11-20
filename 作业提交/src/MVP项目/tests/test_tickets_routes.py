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
        return {'ticket': {'id': ticket_id, 'title': '<script>bad()</script>', 'content': '示例问题', 'matter_id': 'residence-permit', 'status': 'processing', 'owner_id': identity['user_id'], 'owner_role': 'window', 'version': 1}, 'messages': [], 'transfers': [], 'transitions': []}
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
        result = await self.client.post('/staff/api/tickets/TK-test/replies', json={'content':'示例答复','kind':'result','idempotency_key':'test-key-123'}, headers={'X-CSRF-Token':token})
        self.assertEqual(result.status_code, 422, result.text)

    async def test_staff_form_business_error_shows_dialog_on_ticket_page_and_json_stays_json(self):
        from zhengwu_precheck.core.errors import AppError
        token = await self.login_staff()
        self.app.state.ticket_repo.staff_action = AsyncMock(side_effect=AppError(
            409, 'TRANSFER_PENDING', '转派待确认，请撤回转派后再操作'))
        data = {'expected_version':1, 'idempotency_key':'pending-dialog-example',
                'target_role':'reviewer', 'reason':'示例转派'}
        result = await self.client.post('/staff/api/tickets/TK-test/transfers',
                                        data={**data, 'csrf_token':token})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn('text/html', result.headers['content-type'])
        self.assertIn('id="ticket-error-dialog"', result.text)
        self.assertIn('转派待确认，请撤回转派后再操作', result.text)
        self.assertIn('返回工单列表', result.text)
        self.assertNotIn('{"error":', result.text)
        result = await self.client.post('/staff/api/tickets/TK-test/transfers', json=data,
                                        headers={'X-CSRF-Token':token})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()['error']['code'], 'TRANSFER_PENDING')

    async def test_native_action_error_still_has_dialog_when_detail_is_unavailable(self):
        from zhengwu_precheck.core.errors import AppError
        token = await self.login_staff()
        error = AppError(404, 'TICKET_NOT_FOUND', '工单不存在或不在授权范围')
        self.app.state.ticket_repo.staff_action = AsyncMock(side_effect=error)
        self.app.state.ticket_repo.staff_detail = AsyncMock(side_effect=error)
        result = await self.client.post('/staff/api/tickets/TK-test/transfers', data={
            'csrf_token':token, 'expected_version':1, 'idempotency_key':'unavailable-detail-example'})
        self.assertEqual(result.status_code, 404)
        self.assertIn('text/html', result.headers['content-type'])
        self.assertIn('id="ticket-error-dialog"', result.text)
        self.assertIn(error.message, result.text)
        self.assertIn('返回工单列表', result.text)
        self.assertNotIn('示例问题', result.text)

    async def test_native_action_login_error_has_dialog_and_json_contract_stays_unchanged(self):
        from zhengwu_precheck.core.errors import AppError
        token = await self.login_staff()
        self.app.state.staff_auth.identity = AsyncMock(side_effect=AppError(
            401, 'STAFF_LOGIN_REQUIRED', '登录已过期，请重新登录'))
        data = {'expected_version':1, 'idempotency_key':'expired-login-example'}
        result = await self.client.post('/staff/api/tickets/TK-test/replies',
                                        data={**data, 'csrf_token':token})
        self.assertEqual(result.status_code, 401)
        self.assertIn('text/html', result.headers['content-type'])
        self.assertIn('id="ticket-error-dialog"', result.text)
        self.assertIn('登录已过期，请重新登录', result.text)
        self.assertIn('重新登录', result.text)
        result = await self.client.post('/staff/api/tickets/TK-test/replies', json=data,
                                        headers={'X-CSRF-Token':token})
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.json()['error']['code'], 'STAFF_LOGIN_REQUIRED')




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

    async def test_transfer_form_shows_roles_once_and_any_authorized_reviewer_can_accept(self):
        await self.app.state.repo.create_user('second-reviewer', 'ticket-test-password')
        await self.app.state.staff_repo.grant_role('second-reviewer', 'reviewer', 'residence-permit', actor='test')
        window = await self.staff_client('ticket-window')
        reviewers = [await self.staff_client(name) for name in ('integration-reviewer', 'second-reviewer')]
        uid = (await window.get('/staff/api/me')).json()['user_id']
        ticket = await self.app.state.ticket_repo.create_manual(uid, {
            'matter_id':'residence-permit', 'scenario_id':'rental-demo', 'title':'岗位转派示例',
            'content':'虚构工单，验证按岗位接手。', 'idempotency_key':'role-form-create'})
        response = await window.post('/staff/api/tickets/' + ticket['id'] + '/accept', json={
            'expected_version':ticket['version'], 'idempotency_key':'role-form-window-accept'})
        self.assertEqual(response.status_code, 200, response.text)
        ticket = response.json()
        page = await window.get('/staff/tickets/' + ticket['id'])
        select = re.search(r'<select name="target_role" required>(.*?)</select>', page.text, re.S)
        self.assertIsNotNone(select, page.text)
        options = re.findall(r'<option value="([^"]+)"[^>]*>(.*?)</option>', select[1])
        self.assertEqual(options, [('reviewer','业务审核员'), ('director','服务中心主任')])
        self.assertNotIn('integration-reviewer', select[1])
        self.assertNotIn('second-reviewer', select[1])
        response = await window.post('/staff/api/tickets/' + ticket['id'] + '/transfers', data={
            'csrf_token':window.headers['X-CSRF-Token'], 'expected_version':str(ticket['version']),
            'idempotency_key':'role-form-transfer', 'target_role':'reviewer', 'reason':'交业务审核岗位'})
        self.assertEqual(response.status_code, 303, response.text)
        for reviewer in reviewers:
            page = await reviewer.get('/staff/tickets/' + ticket['id'])
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIn('确认接收</button>', page.text)
        detail = (await reviewers[1].get('/staff/api/tickets/' + ticket['id'])).json()
        transfer = detail['transfers'][-1]
        response = await reviewers[1].post('/staff/api/tickets/' + ticket['id'] + '/transfers/' + transfer['id'] + '/accept',
            json={'expected_version':detail['ticket']['version'], 'idempotency_key':'role-form-reviewer-accept'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['owner_id'], (await reviewers[1].get('/staff/api/me')).json()['user_id'])

    async def test_direct_submission_creates_queued_ticket_and_form_retry_returns_same_ticket(self):
        boot = (await self.client.get('/bootstrap')).json()
        headers = {'X-CSRF-Token': boot['csrf_token']}
        login = await self.client.post('/auth/login', headers=headers,
            json={'username':'ticket-window','password':'ticket-test-password'})
        self.assertEqual(login.status_code, 200, login.text)
        payload = {'matter_id':'residence-permit','scenario_id':'rental-demo',
                   'title':'直接提交的示例问题','content':'请人工核对示例地址字段。',
                   'idempotency_key':'direct-submit-example-123'}
        response = await self.client.post('/tickets/submit', headers=headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        ticket = response.json()
        self.assertEqual(ticket['status'], 'queued')
        self.assertEqual(ticket['title'], '直接提交的示例问题')
        self.assertEqual(ticket['content'], '请人工核对示例地址字段。')
        self.assertEqual(ticket['first_reply_due'] - ticket['created_at'], 48 * 3600)
        native = await self.client.post('/tickets/submit', data={**payload, 'csrf_token':boot['csrf_token']})
        self.assertEqual(native.status_code, 303, native.text)
        self.assertEqual(native.headers['location'], '/my/tickets?ticket_id=' + ticket['id'] + '#ticket-detail')
        self.assertEqual((await self.client.get('/tickets')).json()['total'], 1)
        detail = (await self.client.get('/tickets/' + ticket['id'])).json()
        self.assertEqual([step['action'] for step in detail['transitions']], ['created'])
        self.assertEqual(len((await self.client.get('/notifications')).json()), 1)
        window = await self.staff_client('ticket-window')
        accepted = await window.post('/staff/api/tickets/' + ticket['id'] + '/accept',
            json={'expected_version':1, 'idempotency_key':'direct-window-accept-123'})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()['status'], 'processing')

    async def test_ticket_link_opens_separate_ticket_detail_page(self):
        boot = (await self.client.get('/bootstrap')).json()
        headers = {'X-CSRF-Token': boot['csrf_token']}
        login = await self.client.post('/auth/login', headers=headers,
            json={'username':'ticket-window','password':'ticket-test-password'})
        self.assertEqual(login.status_code, 200, login.text)
        created = await self.client.post('/tickets/submit', headers=headers, json={
            'matter_id':'residence-permit', 'scenario_id':'rental-demo',
            'title':'独立详情页示例', 'content':'请查看这个虚构示例工单。',
            'idempotency_key':'separate-detail-example'})
        self.assertEqual(created.status_code, 200, created.text)
        ticket = created.json()
        listing = await self.client.get('/my/tickets')
        self.assertIn('id="create-title"', listing.text)
        self.assertNotIn('站内消息', listing.text)
        link = re.search(r'<a href="([^"]+)">独立详情页示例</a>', listing.text)[1]
        page = await self.client.get(link)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('<h1>工单详情</h1>', page.text)
        self.assertIn(ticket['title'], page.text)
        self.assertIn(ticket['id'], page.text)
        self.assertIn('请查看这个虚构示例工单。', page.text)
        self.assertNotIn('id="create-title"', page.text)
        self.assertNotIn('<h2>我的工单列表</h2>', page.text)
        self.assertNotIn('<h2>站内消息</h2>', page.text)
        self.assertIn('href="/my/tickets">返回我的工单', page.text)
        response = await self.client.post('/tickets/' + ticket['id'] + '/messages', data={
            'csrf_token':boot['csrf_token'], 'expected_version':str(ticket['version']),
            'idempotency_key':'separate-detail-supplement', 'content':'补充的虚构示例内容'})
        self.assertEqual(response.status_code, 303, response.text)
        updated = await self.client.get(response.headers['location'])
        self.assertIn('<h1>工单详情</h1>', updated.text)
        self.assertIn('补充的虚构示例内容', updated.text)
        self.assertNotIn('id="create-title"', updated.text)

    async def test_notification_title_and_delete_json_and_native_form(self):
        boot = (await self.client.get('/bootstrap')).json()
        headers = {'X-CSRF-Token':boot['csrf_token']}
        await self.client.post('/auth/login', headers=headers,
            json={'username':'ticket-window','password':'ticket-test-password'})
        for native in (False, True):
            created = await self.client.post('/tickets/submit', headers=headers, json={
                'matter_id':'residence-permit', 'scenario_id':'rental-demo',
                'title':'通知标题与删除示例', 'content':'虚构示例：仅删除通知，保留工单。',
                'idempotency_key':'delete-notification-create-' + str(native)})
            self.assertEqual(created.status_code, 200, created.text)
            ticket = created.json()
            notice = (await self.client.get('/notifications')).json()[0]
            self.assertEqual(notice['ticket_title'], ticket['title'])
            data = {'expected_version':0, 'idempotency_key':'delete-notification-' + str(native)}
            options = {'data':{**data, 'csrf_token':boot['csrf_token']}} if native else {'json':data}
            response = await self.client.post('/notifications/' + notice['id'] + '/delete',
                                              headers=headers, **options)
            self.assertEqual(response.status_code, 303 if native else 200, response.text)
            if native:
                self.assertEqual(response.headers['location'], '/#notification-panel')
            else:
                self.assertEqual(response.json(), {'deleted':True})
            self.assertEqual((await self.client.get('/notifications')).json(), [])
            detail = (await self.client.get('/tickets/' + ticket['id'])).json()
            self.assertEqual(detail['ticket']['status'], 'queued')
            self.assertEqual([tr['action'] for tr in detail['transitions']], ['created'])

    async def test_notifications_show_current_ticket_completion_status(self):
        boot = (await self.client.get('/bootstrap')).json()
        headers = {'X-CSRF-Token': boot['csrf_token']}
        login = await self.client.post('/auth/login', headers=headers,
            json={'username':'ticket-window','password':'ticket-test-password'})
        self.assertEqual(login.status_code, 200, login.text)
        window = await self.staff_client('ticket-window')

        for completion in ('confirmed_resolved', 'default_complete'):
            with self.subTest(completion=completion):
                created = await self.client.post('/tickets/submit', headers=headers, json={
                    'matter_id':'residence-permit', 'scenario_id':'rental-demo',
                    'title':'通知状态示例', 'content':'仅使用虚构信息验证工单状态。',
                    'idempotency_key':'notification-create-' + completion})
                self.assertEqual(created.status_code, 200, created.text)
                ticket = created.json()
                notes = (await self.client.get('/notifications')).json()
                note = next(n for n in notes if n['ticket_id'] == ticket['id'])
                read = await self.client.post('/notifications/' + note['id'] + '/read', headers=headers,
                    json={'expected_version':0, 'idempotency_key':'notification-read-' + completion})
                self.assertEqual(read.status_code, 200, read.text)

                async def notification():
                    notes = (await self.client.get('/notifications')).json()
                    return next(n for n in notes if n['id'] == note['id'])

                self.assertEqual((await notification())['ticket_status'], 'queued')
                current = (await self.client.get('/tickets/' + ticket['id'])).json()['ticket']
                self.assertEqual(current['status'], 'queued')

                for suffix, extra in (('/accept', {}), ('/replies', {'content':'虚构示例答复', 'kind':'result'})):
                    response = await window.post('/staff/api/tickets/' + ticket['id'] + suffix,
                        json={'expected_version':ticket['version'],
                              'idempotency_key':'notification-' + completion + suffix, **extra})
                    self.assertEqual(response.status_code, 200, response.text)
                    ticket = response.json()
                self.assertEqual((await notification())['ticket_status'], 'waiting_citizen')

                if completion == 'confirmed_resolved':
                    response = await self.client.post('/tickets/' + ticket['id'] + '/confirm-resolved',
                        headers=headers, json={'expected_version':ticket['version'],
                                               'idempotency_key':'notification-confirm-example'})
                    self.assertEqual(response.status_code, 200, response.text)
                else:
                    self.app.state.ticket_repo.clock = lambda: ticket['close_due']
                    await self.app.state.ticket_repo.run_due()
                notice = await notification()
                self.assertEqual(notice['ticket_status'], 'closed')
                self.assertEqual(notice['ticket_close_reason'], completion)
                notes = (await self.client.get('/notifications')).json()
                self.assertTrue(next(n for n in notes if n['id'] == note['id'])['read_at'])

    async def test_direct_submission_invalid_fields_leave_no_partial_record(self):
        boot = (await self.client.get('/bootstrap')).json()
        headers = {'X-CSRF-Token': boot['csrf_token']}
        await self.client.post('/auth/login', headers=headers,
            json={'username':'ticket-window','password':'ticket-test-password'})
        payload = {'matter_id':'residence-permit','scenario_id':'rental-demo',
                   'title':'示例问题','content':'示例内容','idempotency_key':'direct-invalid-example-123'}
        for changes in ({'title':''}, {'content':''}, {'scenario_id':'unknown-scenario'}):
            with self.subTest(changes=changes):
                response = await self.client.post('/tickets/submit', headers=headers, json={**payload, **changes})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual((await self.client.get('/tickets')).json()['total'], 0)
        self.assertEqual((await self.client.get('/notifications')).json(), [])
        corrected = await self.client.post('/tickets/submit', headers=headers, json=payload)
        self.assertEqual(corrected.status_code, 200, corrected.text)

    async def test_real_two_sided_workflow_and_object_authorization(self):
        import secrets
        boot = (await self.client.get('/bootstrap')).json()
        headers = {'X-CSRF-Token': boot['csrf_token']}
        login = await self.client.post('/auth/login', headers=headers, json={'username':'ticket-window','password':'ticket-test-password'})
        self.assertEqual(login.status_code, 200, login.text)
        draft=(await self.client.post('/agent/drafts',headers=headers,json={'matter_id':'residence-permit','scenario_id':'rental-demo','title':'示例工单','content':'仅虚构问题','shared_material_ids':[]})).json()
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
        await staff_write(window, '/transfers', target_user_id=reviewer_id, target_role='reviewer', reason='专业示例问题')
        detail = (await reviewer.get('/staff/api/tickets/' + tid)).json()
        self.assertNotEqual(detail['ticket']['owner_id'], reviewer_id)
        transfer_id = detail['transfers'][-1]['id']
        await staff_write(reviewer, '/transfers/' + transfer_id + '/accept')
        await staff_write(reviewer, '/replies', content='请补充示例信息', kind='supplement')
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
        await staff_write(reviewer, '/transfers', target_user_id=director_id, target_role='director', reason='手动升级示例异常')
        detail = (await director.get('/staff/api/tickets/' + tid)).json()
        await staff_write(director, '/transfers/' + detail['transfers'][-1]['id'] + '/accept')
        page = await director.get('/staff/tickets/' + tid)
        self.assertEqual(page.status_code, 200, page.text)
        await staff_write(director, '/replies', content='示例处理结果', kind='result')
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


