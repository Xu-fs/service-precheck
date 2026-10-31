import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_agent_routes as fixtures


class MaterialRouteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.AgentRouteTests.asyncSetUp
    asyncTearDown=fixtures.AgentRouteTests.asyncTearDown

    async def test_upload_transcription_precheck_and_rule_publication_gate(self):
        case=(await self.client.post('/agent/cases',json={})).json()['case_id']
        image=(Path(__file__).resolve().parents[1]/'data/materials/fictional-identity.png').read_bytes()
        response=await self.client.post(f'/agent/cases/{case}/materials',files={'file':('示例.png',image,'image/png')},data={'consent':'true'})
        self.assertEqual(response.status_code,200,response.text)
        item=response.json()
        corrected=await self.client.put('/agent/materials/'+item['id']+'/fields',json={
            'expected_version':item['version'],'kind':'identity','fields':{'name':'示例甲','address':'虚构路一号','date':'2026-10-01'}})
        self.assertEqual(corrected.status_code,200,corrected.text)
        mset=await self.client.post(f'/agent/cases/{case}/material-sets',json={'material_ids':[item['id']]})
        self.assertEqual(mset.status_code,200,mset.text)
        rule=(await self.client.get(f'/agent/cases/{case}/rules?matter_id=residence-permit&scenario_id=rental-demo')).json()
        selected=(await self.client.post(f'/agent/cases/{case}/selection',json={
            'matter_id':'residence-permit','scenario_id':'rental-demo','snapshot_id':rule['snapshot_id']})).json()
        checked=await self.client.post(f'/agent/cases/{case}/precheck',json={
            'material_set_id':mset.json()['id'],'rule_snapshot_id':rule['snapshot_id'],'expected_case_version':selected['version']})
        self.assertEqual(checked.status_code,200,checked.text)
        self.assertEqual(checked.json()['error']['code'],'PRECHECK_RULES_UNPUBLISHED')
        self.assertEqual((await self.client.delete('/agent/materials/'+item['id'])).status_code,200)
        self.assertEqual((await self.client.get('/agent/materials/'+item['id'])).status_code,404)

    async def test_anonymous_upload_rejected_before_storage(self):
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://testserver') as anonymous:
            boot=(await anonymous.get('/bootstrap')).json()
            case=(await anonymous.post('/agent/cases',headers={'X-CSRF-Token':boot['csrf_token']},json={})).json()['case_id']
            response=await anonymous.post(f'/agent/cases/{case}/materials',headers={'X-CSRF-Token':boot['csrf_token']},
                files={'file':('虚构.png',b'fake','image/png')},data={'consent':'true'})
            self.assertEqual(response.status_code,401,response.text)
