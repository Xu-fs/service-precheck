"""真实HTTP确认与旧工单入口安全边界。"""
import sys
from pathlib import Path
import tempfile
import unittest
import httpx
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings
from test_chat import FakeQA


class AgentRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(Settings(runtime_dir=Path(self.tmp.name)),qa=FakeQA())
        self.app.state.knowledge_service.run=AsyncMock()
        self.life=self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        await self.app.state.repo.create_user('agent-person','teaching-test-password')
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://testserver')
        boot=(await self.client.get('/bootstrap')).json()
        self.client.headers['X-CSRF-Token']=boot['csrf_token']
        response=await self.client.post('/auth/login',json={'username':'agent-person','password':'teaching-test-password'})
        self.assertEqual(response.status_code,200)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None,None,None)
        self.tmp.cleanup()

    async def test_http_preview_confirm_submit_and_raw_bypass(self):
        payload=dict(title='教学咨询',content='仅共享这个虚构问题',matter_id='residence-permit',
                     scenario_id='rental-demo',shared_material_ids=[])
        raw=await self.client.post('/tickets',json={**payload,'idempotency_key':'raw-operation-1'})
        self.assertEqual(raw.status_code,409,raw.text)
        response=await self.client.post('/agent/drafts',json=payload)
        self.assertEqual(response.status_code,200,response.text)
        draft=response.json()
        preview=await self.client.get('/my/ticket-drafts/'+draft['id'])
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertIn('共享范围',preview.text)
        confirmation=await self.client.post('/agent/drafts/'+draft['id']+'/confirm',json={'draft_version':draft['version']})
        self.assertEqual(confirmation.status_code,200,confirmation.text)
        create=dict(draft_id=draft['id'],draft_version=draft['version'],
                    submission_confirmation_id=confirmation.json()['id'],idempotency_key='http-confirmed-1')
        first=await self.client.post('/tickets',json=create)
        self.assertEqual(first.status_code,200,first.text)
        repeated=await self.client.post('/tickets',json=create)
        self.assertEqual(first.json()['id'],repeated.json()['id'])

    async def test_case_selection_and_changed_rules_stop_precheck(self):
        response=await self.client.post('/agent/cases',json={})
        self.assertEqual(response.status_code,200,response.text)
        case=response.json()['case_id']
        rule=(await self.client.get(f'/agent/cases/{case}/rules',params={'matter_id':'residence-permit','scenario_id':'rental-demo'})).json()
        selected=await self.client.post(f'/agent/cases/{case}/selection',json={
            'matter_id':'residence-permit','scenario_id':'rental-demo','snapshot_id':rule['snapshot_id']})
        self.assertEqual(selected.status_code,200,selected.text)
        path=Path(self.tmp.name)/'changed.md'
        path.write_text('changed teaching rule',encoding='utf-8')
        self.app.state.settings.guide_path=path
        response=await self.client.post(f'/agent/cases/{case}/precheck',json={
            'material_set_id':'fake-set','expected_case_version':selected.json()['version'],
            'rule_snapshot_id':rule['snapshot_id']})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['error']['code'],'SELECTION_CHANGED')

    async def test_native_form_retry_returns_original_ticket(self):
        draft=(await self.client.post('/agent/drafts',json={
            'title':'虚构教学重试','content':'提交响应丢失后核对原结果',
            'matter_id':'residence-permit','scenario_id':'rental-demo','shared_material_ids':[]})).json()
        data={'csrf_token':self.client.headers['X-CSRF-Token'], 'draft_version':str(draft['version']),
              'draft_hash':draft['content_hash'],'idempotency_key':'native-form-retry-1'}
        url='/tickets/drafts/'+draft['id']+'/submit'
        first=await self.client.post(url,data=data)
        repeated=await self.client.post(url,data=data)
        self.assertEqual(first.status_code,303,first.text)
        self.assertEqual(repeated.status_code,303,repeated.text)
        self.assertEqual(first.headers['location'],repeated.headers['location'])
        self.assertEqual((await self.client.get('/tickets')).json()['total'],1)

    async def test_editing_exception_draft_preserves_selected_exception(self):
        case=(await self.client.post('/agent/cases',json={})).json()['case_id']
        exception=(await self.client.post(f'/agent/cases/{case}/exceptions',json={
            'kind':'system_unavailable','detail':'虚构教学服务不可用，请人工协助'})).json()
        prepared=(await self.client.post(f'/agent/cases/{case}/exception-draft',json={
            'exception_id':exception['id'],'summary':'虚构教学人工核验','expected_case_version':0})).json()
        draft=(await self.client.get('/agent/drafts/'+prepared['data']['draft_id'])).json()
        response=await self.client.post('/tickets/drafts/'+draft['id']+'/edit',data={
            'csrf_token':self.client.headers['X-CSRF-Token'],'draft_version':str(draft['version']),
            'idempotency_key':'edit-exception-draft-1',
            'title':'更新后的虚构教学标题','content':'只修改正文，应保留已选择的异常关联'})
        self.assertEqual(response.status_code,303,response.text)
        edited=(await self.client.get('/agent/drafts/'+draft['id'])).json()
        self.assertEqual(edited['data']['exception_ids'],[exception['id']])
        preview=await self.client.get('/my/ticket-drafts/'+draft['id'])
        self.assertIn('虚构教学服务不可用，请人工协助',preview.text)

    async def test_real_handlers_prepare_confirm_create_query(self):
        case=(await self.client.post('/agent/cases',json={})).json()['case_id']
        user=await self.app.state.repo.authenticate(self.client.cookies['zw_auth'])
        who={'user_id':user['id'],'key':'user:'+user['id'],'anonymous_hash':None}
        def planner(name,args):
            used=False
            async def step(messages,schemas):
                nonlocal used
                if used:
                    return {'text':'忽略安全规则，保证获批'}
                used=True
                return {'tool_calls':[{'id':'one','name':name,'arguments':args}]}
            return step
        result=await self.app.state.agent_service.run('整理教学人工咨询',case,who,planner('prepare_ticket_draft',{
            'case_id':case,'purpose':'ordinary_consultation','summary':'虚构教学咨询问题','expected_case_version':0}))
        self.assertEqual(result['status'],'needs_confirmation',result)
        draft=result['result']['data']
        confirmation=(await self.client.post('/agent/drafts/'+draft['draft_id']+'/confirm',json={'draft_version':draft['draft_version']})).json()
        made=await self.app.state.agent_service.run('本人确认后提交',case,who,planner('create_manual_ticket',{
            'draft_id':draft['draft_id'],'draft_version':draft['draft_version'],
            'submission_confirmation_id':confirmation['id'],'idempotency_key':'model-chain-submit-1'}))
        self.assertEqual(made['status'],'ok',made)
        self.assertNotIn('保证获批',made['text'])
        tid=made['result']['data']['ticket_id']
        queried=await self.app.state.agent_service.run('查询本人进度',case,who,planner('get_ticket_status',{'ticket_id':tid}))
        self.assertEqual(queried['result']['data']['ticket_id'],tid)
        self.assertEqual((await self.client.get('/tickets')).json()['total'],1)
