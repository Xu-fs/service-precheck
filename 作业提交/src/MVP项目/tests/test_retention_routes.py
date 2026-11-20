"""真实JWT/CSRF/SQLite权限与隔离虚构运行记录。"""
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import patch
from sqlalchemy import text
import test_staff as staff_fixture


class RetentionRouteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=staff_fixture.StaffTests.asyncSetUp
    asyncTearDown=staff_fixture.StaffTests.asyncTearDown
    login=staff_fixture.StaffTests.login

    def setup_record(self):
        directory=self.app.state.settings.runtime_dir/'retention-records'
        directory.mkdir(exist_ok=True)
        path=directory/'fictional.json'
        path.write_text(json.dumps(dict(id='e',category='evaluation',created_at=time.time()-91*86400,references=[],standalone=True,content='虚构评价')),encoding='utf-8')
        policy=dict(version='fictional-approved-v1',expires_at=time.time()+3600,classes={'evaluation':dict(days=90,action='archive_delete')})
        key='fictional-policy-secret-external-'+'x'*32
        signed=dict(policy=policy,signature=hmac.new(key.encode(),json.dumps(policy,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest())
        return path,key,dict(signed_policy=signed,approved={'e':hashlib.sha256(path.read_bytes()).hexdigest()},dry_run=True)

    async def mount(self):
        from zhengwu_precheck.api.routes.operations import router
        if not any(getattr(route,'path',None)=='/staff/api/retention/dispose' for route in self.app.routes):
            self.app.include_router(router)

    async def test_non_supervisor_denied_before_body_and_scoped_supervisor_not_enough(self):
        await self.mount()
        await self.login()
        response=await self.client.post('/staff/api/retention/dispose',content='not-json')
        self.assertEqual(response.status_code,403,response.text)
        await self.app.state.staff_repo.grant_role('window','supervisor','residence-permit',actor='test')
        await self.login()
        response=await self.client.post('/staff/api/retention/dispose',json={})
        self.assertEqual(response.status_code,403,response.text)

    async def test_valid_supervisor_dry_run_then_quiesced_deletion_and_audit(self):
        await self.mount()
        await self.app.state.staff_repo.grant_role('window','supervisor','*',actor='test')
        await self.login()
        path,key,payload=self.setup_record()
        with patch.dict(os.environ,{'ZHENGWU_RETENTION_POLICY_KEY':key}):
            dry=await self.client.post('/staff/api/retention/dispose',json=payload)
            self.assertEqual(dry.status_code,200,dry.text)
            self.assertTrue(path.exists())
            self.assertTrue(dry.json()['dry_run'])
            payload.update(dry_run=False,quiesced=True)
            executed=await self.client.post('/staff/api/retention/dispose',json=payload)
            self.assertEqual(executed.status_code,200,executed.text)
            self.assertFalse(path.exists())
            self.assertTrue((self.app.state.settings.runtime_dir/'retention-audit.jsonl').is_file())
        async with self.app.state.repo.engine.connect() as connection:
            actions=(await connection.execute(text("SELECT action FROM audit_logs WHERE action='retention_dispose'"))).all()
        self.assertEqual(len(actions),2)

    async def test_missing_key_and_execution_without_quiesce_fail_closed(self):
        await self.mount()
        await self.app.state.staff_repo.grant_role('window','supervisor','*',actor='test')
        await self.login()
        path,key,payload=self.setup_record()
        with patch.dict(os.environ,{'ZHENGWU_RETENTION_POLICY_KEY':''}):
            response=await self.client.post('/staff/api/retention/dispose',json=payload)
            self.assertEqual(response.status_code,503,response.text)
        with patch.dict(os.environ,{'ZHENGWU_RETENTION_POLICY_KEY':key}):
            payload['dry_run']=False
            response=await self.client.post('/staff/api/retention/dispose',json=payload)
            self.assertEqual(response.status_code,422,response.text)
        self.assertTrue(path.exists())
