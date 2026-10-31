"""授权审计针对真实消息表及安全日志的回归测试。"""
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sqlalchemy import text
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from zhengwu_precheck.infrastructure.audit_repository import AuditRepository
        self.temp = tempfile.TemporaryDirectory()
        self.chat = ChatRepository(Path(self.temp.name) / "audit.sqlite3")
        await self.chat.initialize()
        self.staff = StaffRepository(self.chat.engine)
        await self.staff.initialize()
        from zhengwu_precheck.infrastructure.prompt_repository import PromptRepository
        from zhengwu_precheck.infrastructure.knowledge_repository import KnowledgeRepository
        await PromptRepository(self.chat.engine).initialize()
        await KnowledgeRepository(self.chat.engine, Path(self.temp.name)).initialize()
        self.repo = AuditRepository(self.chat.engine)
        await self.repo.initialize()
        self.identity = {"user_id": "super", "username": "super", "grants": [
            {"role_code": "supervisor", "matter_id": "residence-permit"}]}
        async with self.chat.engine.begin() as conn:
            await conn.execute(text("INSERT INTO users VALUES('super','super','test','active')"))
            await conn.execute(text("INSERT INTO staff_accounts(user_id) VALUES('super')"))
            await conn.execute(text("INSERT INTO conversations(id,anonymous_token_hash,matter_id,created_at) VALUES('c','fictional','sole-trader-registration',1)"))
            for mid, matter, outcome, result in [('m1', 'residence-permit', 'insufficient_evidence', '{}'),
                                                ('m2', 'sole-trader-registration', 'answered', '{}'),
                                                ('m3', 'residence-permit', 'answered', '{"reason":"version_conflict"}')]:
                await conn.execute(text("INSERT INTO messages(id,conversation_id,sender_type,content,status,request_id,matter_id,outcome,result_json,created_at) VALUES(:id,'c','assistant',:body,'complete',:id,:matter,:outcome,:result,10)"),
                                   {'id': mid, 'body': '正文' + mid, 'matter': matter, 'outcome': outcome, 'result': result})
            for eid, actor, entity in [('e1','other','c'),('e2','other','hidden'),('e3','super','hidden')]:
                await conn.execute(text("INSERT INTO audit_logs VALUES(:id,:actor,'conversation_access','conversation',:entity,'denied',:request,20)"), {'id': eid,'actor':actor,'entity':entity,'request':'m1' if eid == 'e1' else None})

    async def asyncTearDown(self):
        await self.chat.close()
        self.temp.cleanup()

    async def grant(self, level='body'):
        async with self.chat.engine.begin() as conn:
            await conn.execute(text("INSERT OR REPLACE INTO staff_record_grants VALUES('super','conversation','c',:level,'operator',NULL)"), {'level': level})

    async def test_sql_scope_and_record_grant_filter_before_count(self):
        self.assertEqual((await self.repo.list_records(self.identity, {'kind':'message'}))['total'], 0)
        await self.grant('summary')
        self.assertEqual((await self.repo.list_records(self.identity, {'kind':'message'}))['total'], 0)
        await self.grant()
        result = await self.repo.list_records(self.identity, {'kind':'message', 'page_size':1})
        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['items']), 1)
        detail = await self.repo.detail(self.identity, 'message:m1')
        self.assertEqual(detail['content'], '正文m1')
        self.assertNotIn('正文m2', str(detail))
        with self.assertRaises(Exception) as caught:
            await self.repo.detail(self.identity, 'message:m2')
        self.assertEqual(caught.exception.status, 404)

    async def test_risk_filters_events_and_no_synthetic_prechecks(self):
        await self.grant()
        self.assertEqual((await self.repo.list_records(self.identity, {'flag':'missing_evidence'}))['total'], 1)
        self.assertEqual((await self.repo.list_records(self.identity, {'flag':'version_conflict'}))['total'], 1)
        events = await self.repo.list_records(self.identity, {'kind':'event'})
        self.assertEqual({i['id'] for i in events['items']}, {'event:e1','event:e3'})
        scoped = await self.repo.list_records(self.identity, {'kind':'event','matter_id':'residence-permit'})
        self.assertEqual({i['id'] for i in scoped['items']}, {'event:e1'})
        self.assertEqual((await self.repo.list_records(self.identity, {'kind':'precheck'}))['total'], 0)
        self.identity['grants'] = []
        with self.assertRaises(Exception) as caught:
            await self.repo.list_records(self.identity, {})
        self.assertEqual(caught.exception.status, 403)

    async def test_flags_reviews_append_and_preserve_source(self):
        await self.grant()
        await self.repo.flag(self.identity, 'message:m1', 'abnormal_precheck', '人工发现异常，待核实')
        await self.repo.review(self.identity, 'message:m1', 'confirmed', '已复核，交人工处理')
        await self.repo.review(self.identity, 'message:m1', 'needs_followup', '补充复核意见')
        item = await self.repo.detail(self.identity, 'message:m1')
        self.assertEqual(len(item['flags']), 1)
        self.assertEqual(len(item['reviews']), 2)
        self.assertEqual(item['content'], '正文m1')
        self.assertEqual((await self.repo.list_records(self.identity, {'flag':'abnormal_precheck'}))['total'], 1)
        with self.assertRaises(Exception) as caught:
            await self.repo.flag(self.identity, 'message:m1', 'abnormal_precheck', '')
        self.assertEqual(caught.exception.status, 422)

    async def test_citations_accept_saved_matter_name_but_exclude_other_matter(self):
        await self.grant()
        async with self.chat.engine.begin() as conn:
            await conn.execute(text("INSERT INTO message_citations VALUES('citation-1','m1','chunk-1','教学指南','居住证申领','v1','已保存的依据')"))
            await conn.execute(text("INSERT INTO message_citations VALUES('citation-2','m1','chunk-2','其他指南','sole-trader-registration','v1','其他事项依据')"))
        item = await self.repo.detail(self.identity, 'message:m1')
        self.assertEqual([c['quote'] for c in item['citations']], ['已保存的依据'])

    async def test_mixed_permissions_and_expired_grants_do_not_expand_scope(self):
        await self.grant()
        self.identity['grants'].append({'role_code':'window','matter_id':'sole-trader-registration'})
        self.assertEqual((await self.repo.list_records(self.identity, {'kind':'message'}))['total'], 2)
        async with self.chat.engine.begin() as conn:
            await conn.execute(text("UPDATE staff_record_grants SET expires_at=1"))
        self.assertEqual((await self.repo.list_records(self.identity, {'kind':'message'}))['total'], 0)

    async def test_filters_and_api_mutations_require_csrf_and_valid_payload(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from zhengwu_precheck.api.routes.audits import router
        from zhengwu_precheck.core.errors import AppError
        from zhengwu_precheck.utils.security import digest
        await self.grant()
        app = FastAPI()
        app.state.audit_repo = self.repo
        identity = self.identity
        class TestAuth:
            async def identity(self, token):
                return identity
        app.state.staff_auth = TestAuth()
        @app.exception_handler(AppError)
        async def error_handler(request, error):
            return JSONResponse({'code':error.code}, status_code=error.status)
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            self.assertEqual((await client.get('/staff/api/audits/list?kind=message&matter_id=sole-trader-registration')).json()['total'], 0)
            self.assertEqual((await client.get('/staff/api/audits?kind=message&username=unknown')).json()['total'], 0)
            self.assertEqual((await client.get('/staff/api/audits?start=2026-10-31')).json()['total'], 0)
            url = '/staff/api/audits/message:m1/flags'
            self.assertEqual((await client.post(url,json={'kind':'missing_evidence','reason':'依据待补充'})).status_code, 403)
            token = 'a'*40
            client.cookies.set('zw_staff_csrf', token)
            client.headers['X-CSRF-Token'] = digest('staff-csrf:' + token)
            self.assertEqual((await client.post(url,json={'kind':[],'reason':'依据待补充'})).status_code, 422)
            self.assertEqual((await client.post(url,json={'kind':'missing_evidence','reason':'依据待补充'})).status_code, 200)
            review = await client.post('/staff/api/audits/message:m1/reviews', data={'csrf_token':digest('staff-csrf:' + token),'decision':'confirmed','comment':'人工复核确认'})
            self.assertEqual(review.status_code, 303)
            self.assertEqual(len((await client.get('/staff/api/audits/message:m1')).json()['reviews']), 1)


if __name__ == '__main__':
    unittest.main()
