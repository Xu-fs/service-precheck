"""Real HTTP+SQLite+review workflow; only the embedding HTTP endpoint is a fixture."""
import sys
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
import test_published_qa as published_fixture


class TeachingWorkflowTests(unittest.IsolatedAsyncioTestCase):
    bootstrap=published_fixture.PublishedQATests.bootstrap
    model=published_fixture.PublishedQATests.model
    published=published_fixture.PublishedQATests.published
    asyncTearDown=published_fixture.PublishedQATests.asyncTearDown

    async def asyncSetUp(self):
        await published_fixture.PublishedQATests.asyncSetUp(self)
        await self.app.state.repo.create_user('fictional-citizen','fictional-password-only')
        response=await self.client.post('/auth/login',json={'username':'fictional-citizen','password':'fictional-password-only'})
        self.assertEqual(response.status_code,200,response.text)
        await self.bootstrap(self.client)

    async def checked(self,method,url,status=200,**kwargs):
        response=await self.client.request(method,url,**kwargs)
        self.assertEqual(response.status_code,status,response.text)
        return response.json()

    async def publish_candidate(self,matter):
        residence=matter=='residence-permit'
        scenario='rental-demo' if residence else 'standard-demo'
        filename='居住证申领_示例指南_v1.md' if residence else '个体工商户设立登记_示例指南_v1.md'
        item=await self.repo.upload(self.owner,title='虚构示例指南候选',matter_id=matter,scenario_id=scenario,
            kind='guide',filename=filename,source_uri='data/materials/'+filename,rule_version='teaching-2026-10-v1',
            raw=(ROOT/'data/materials'/filename).read_bytes(),is_teaching=True,visibility='public')
        await self.app.state.knowledge_service.run_once()
        self.assertEqual((await self.repo.get_version(self.owner,item['version_id']))['processing_status'],'ready')
        return await self.published(item)

    async def choose(self,matter,scenario):
        case=(await self.checked('POST','/agent/cases'))['case_id']
        rules=await self.checked('GET',f'/agent/cases/{case}/rules',params={'matter_id':matter,'scenario_id':scenario})
        selection=await self.checked('POST',f'/agent/cases/{case}/selection',json={'matter_id':matter,'scenario_id':scenario,'snapshot_id':rules['snapshot_id']})
        return case,rules,selection

    async def add_material(self,case,kind,name='示例甲'):
        item=await self.checked('POST',f'/agent/cases/{case}/materials',data={'consent':'true'},
            files={'file':('fictional-identity.png',(ROOT/'data/materials/fictional-identity.png').read_bytes(),'image/png')})
        self.assertNotIn('path',item)
        return await self.checked('PUT',f"/agent/materials/{item['id']}/fields",json={'expected_version':item['version'],'kind':kind,'fields':{'name':name,'address':'虚构路一号','date':'2026-10-01'}})

    async def check_set(self,case,rules,selection,items):
        mset=await self.checked('POST',f'/agent/cases/{case}/material-sets',json={'material_ids':[x['id'] for x in items]})
        body={'material_set_id':mset['id'],'rule_snapshot_id':rules['snapshot_id'],'expected_case_version':selection['version']}
        result=await self.checked('POST',f'/agent/cases/{case}/precheck',json=body)
        self.assertEqual(result['status'],'ok',result)
        return result['data'],body

    async def test_two_matters_review_to_upload_precheck_and_stale_evidence(self):
        for matter,scenario,kinds in [
            ('residence-permit','rental-demo',['identity','residence','application']),
            ('sole-trader-registration','standard-demo',['identity','application','business_address'])]:
            with self.subTest(matter=matter):
                release=await self.publish_candidate(matter)
                case,rules,selection=await self.choose(matter,scenario)
                materials=[await self.add_material(case,kind) for kind in kinds]
                positive,body=await self.check_set(case,rules,selection,materials)
                self.assertEqual(positive['result'],'未发现清单问题')
                self.assertEqual(positive['selection_snapshot_id'],rules['snapshot_id'])
                self.assertEqual(positive['teaching_rule_version'],'teaching-2026-10-v1')
                self.assertTrue(positive['evidence'])
                fetched=await self.checked('GET',f"/agent/prechecks/{positive['id']}")
                self.assertEqual(fetched['id'],positive['id'])
                missing,_=await self.check_set(case,rules,selection,materials[:-1])
                self.assertEqual(missing['result'],'疑似缺项')
                changed=await self.checked('PUT',f"/agent/materials/{materials[0]['id']}/fields",json={'expected_version':materials[0]['version'],'kind':'identity','fields':{'name':'示例乙','address':'虚构路一号','date':'2026-10-01'}})
                stale=await self.checked('GET',f"/agent/prechecks/{positive['id']}",status=409)
                self.assertEqual(stale['error']['code'],'MATERIAL_SET_STALE')
                conflict,_=await self.check_set(case,rules,selection,[changed,*materials[1:]])
                self.assertEqual(conflict['result'],'信息待核实')
                await self.checked('DELETE',f"/agent/materials/{materials[-1]['id']}")
                revoked=await self.checked('GET',f"/agent/prechecks/{conflict['id']}",status=409)
                self.assertEqual(revoked['error']['code'],'MATERIAL_SET_STALE')
                await self.repo.release_action(self.supervisor,release['id'],'retire','虚构示例版本停用')
                stale_rule=await self.client.post(f'/agent/cases/{case}/precheck',json=body)
                self.assertEqual(stale_rule.status_code,409,stale_rule.text)


    async def test_same_version_label_with_different_guide_bytes_cannot_authorize_precheck(self):
        item=await self.repo.upload(self.owner,title='虚构不匹配指南',matter_id='sole-trader-registration',scenario_id='standard-demo',
            kind='guide',filename='unrelated.md',source_uri='虚构本地测试',rule_version='teaching-2026-10-v1',
            raw='虚构但内容不匹配的示例指南，版本名称不能作为规则批准凭证。'.encode('utf-8'),is_teaching=True,visibility='public')
        await self.app.state.knowledge_service.run_once()
        await self.published(item)
        case,rules,selection=await self.choose('sole-trader-registration','standard-demo')
        mset=await self.checked('POST',f'/agent/cases/{case}/material-sets',json={'material_ids':[]})
        result=await self.checked('POST',f'/agent/cases/{case}/precheck',json={'material_set_id':mset['id'],'rule_snapshot_id':rules['snapshot_id'],'expected_case_version':selection['version']})
        self.assertNotEqual(result['status'],'ok')
        self.assertEqual(result['error']['code'],'PRECHECK_RULES_UNPUBLISHED')

    async def test_shared_precheck_and_materials_reach_staff_detail(self):
        await self.publish_candidate('residence-permit')
        case,rules,selection=await self.choose('residence-permit','rental-demo')
        items=[await self.add_material(case,kind) for kind in ['identity','residence','application']]
        checked,_=await self.check_set(case,rules,selection,items)
        draft=await self.checked('POST','/agent/drafts',json={
            'title':'虚构示例共享','content':'请人工核对已共享材料和示例预检摘要',
            'matter_id':'residence-permit','scenario_id':'rental-demo','case_id':case,
            'shared_material_ids':[items[0]['id']],'precheck_id':checked['id']})
        confirmation=await self.checked('POST',f"/agent/drafts/{draft['id']}/confirm",json={'draft_version':draft['version']})
        ticket=await self.checked('POST','/tickets',json={
            'draft_id':draft['id'],'draft_version':draft['version'],
            'submission_confirmation_id':confirmation['id'],'idempotency_key':'shared-staff-flow-1'})
        await self.app.state.repo.create_user('fictional-window','fictional-password-only')
        async with self.app.state.repo.engine.connect() as conn:
            from sqlalchemy import text
            uid=(await conn.execute(text("SELECT id FROM users WHERE username='fictional-window'"))).scalar_one()
        await self.app.state.staff_repo.grant_role('fictional-window','window','residence-permit',actor='test')
        detail=await self.app.state.ticket_repo.staff_detail({'user_id':uid},ticket['id'])
        shared=detail.get('shared_context',{})
        self.assertEqual(shared.get('precheck',{}).get('id'),checked['id'])
        self.assertEqual(shared['precheck']['result'],'未发现清单问题')
        self.assertEqual([m['id'] for m in shared['materials']],[items[0]['id']])
        self.assertTrue(shared['materials'][0]['available'])
        self.assertNotIn('fields',str(shared['precheck']))
        citizen=await self.client.get('/my/tickets',params={'ticket_id':ticket['id']})
        self.assertEqual(citizen.status_code,200,citizen.text)
        self.assertIn(checked['id'],citizen.text)
        self.assertIn('下载已授权材料',citizen.text)
        from zhengwu_precheck.core.services.staff_auth import StaffAuth
        import re
        self.app.state.staff_auth=StaffAuth(self.app.state.staff_repo,'fictional-staff-page-key-'+'x9'*24)
        page=await self.client.get('/staff/login')
        csrf=re.search('name="csrf_token" value="([^"]+)"',page.text)[1]
        login=await self.client.post('/staff/api/auth/login',headers={'X-CSRF-Token':csrf},json={
            'username':'fictional-window','password':'fictional-password-only'})
        self.assertEqual(login.status_code,200,login.text)
        page=await self.client.get('/staff/tickets/'+ticket['id'])
        self.assertEqual(page.status_code,200,page.text)
        self.assertIn(checked['id'],page.text)
        self.assertIn('/staff/api/tickets/'+ticket['id']+'/materials/'+items[0]['id'],page.text)

    async def test_material_edit_during_precheck_requires_fresh_check(self):
        from unittest.mock import patch
        await self.publish_candidate('residence-permit')
        case,rules,selection=await self.choose('residence-permit','rental-demo')
        items=[await self.add_material(case,kind) for kind in ['identity','residence','application']]
        mset=await self.checked('POST',f'/agent/cases/{case}/material-sets',json={'material_ids':[x['id'] for x in items]})
        repository=self.app.state.material_repo
        original=repository.precheck
        async def changed(*args,**kwargs):
            result=await original(*args,**kwargs)
            await repository.correct_fields(result['user_id'] if 'user_id' in result else args[0],
                items[0]['id'],items[0]['version'],'identity',{'name':'示例乙'})
            return result
        with patch.object(repository,'precheck',side_effect=changed):
            result=await self.checked('POST',f'/agent/cases/{case}/precheck',json={
                'material_set_id':mset['id'],'rule_snapshot_id':rules['snapshot_id'],
                'expected_case_version':selection['version']})
        self.assertNotEqual(result['status'],'ok',result)
        self.assertEqual(result['error']['code'],'MATERIAL_SET_STALE')
