"""工作人员第一批契约测试：真实JWT、SQLite及ASGI，不调用模型。"""
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from uuid import uuid4

import httpx
import jwt
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
AVAILABLE = importlib.util.find_spec("zhengwu_precheck.core.staff_permissions") is not None
if AVAILABLE:
    from zhengwu_precheck.main import create_app
    from zhengwu_precheck.settings import Settings
from test_chat import FakeQA

SECRET = "test-only-signing-key-" + "a9" * 24


class StaffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(AVAILABLE, "尚未实现工作人员岗位与JWT入口")
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Settings(runtime_dir=Path(self.temp.name), staff_jwt_secret=SECRET), qa=FakeQA())
        self.lifecycle = self.app.router.lifespan_context(self.app)
        await self.lifecycle.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.clients = [self.client]
        await self.app.state.repo.create_user("window", "test-only-password")
        await self.app.state.staff_repo.grant_role("window", "window", "residence-permit", actor="test-operator")
        page = await self.client.get("/staff/login")
        self.csrf = re.search('name="csrf_token" value="([^"]+)"', page.text)[1]
        self.client.headers["X-CSRF-Token"] = self.csrf

    async def asyncTearDown(self):
        if hasattr(self, "clients"):
            for client in self.clients:
                await client.aclose()
            await self.lifecycle.__aexit__(None, None, None)
            self.temp.cleanup()

    async def login(self, username="window"):
        response = await self.client.post("/staff/api/auth/login", json={"username": username, "password": "test-only-password"})
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf
        return response

    async def test_login_issues_real_scoped_jwt_and_ssr_workspace(self):
        response = await self.login()
        cookie = next(c for c in self.client.cookies.jar if c.name == "zw_staff_access")
        self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))
        claims = jwt.decode(cookie.value, SECRET, algorithms=["HS256"], issuer="zhengwu-precheck", audience="zhengwu-staff")
        self.assertLessEqual(claims["exp"] - claims["iat"], 900)
        me = await self.client.get("/staff/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["grants"][0]["matter_id"], "residence-permit")
        self.assertNotIn("access_token", response.json())
        page = await self.client.get("/staff")
        self.assertEqual(page.status_code, 200)
        self.assertIn("我的工作台", page.text)
        self.assertIn("窗口工作人员", page.text)
        knowledge = await self.client.get("/staff/knowledge")
        self.assertEqual(knowledge.status_code, 200)
        self.assertIn("暂无授权资料", knowledge.text)
        self.assertNotIn("待后续批次开放", knowledge.text)
        self.assertNotIn('href="/staff/reviews"', page.text)
        self.assertNotIn('action="/staff/api/rules"', knowledge.text)

    async def test_citizen_cookie_and_unassigned_user_cannot_enter_staff(self):
        await self.app.state.repo.create_user("citizen", "test-only-password")
        citizen_token, _ = await self.app.state.repo.login("citizen", "test-only-password")
        self.client.cookies.set("zw_auth", citizen_token)
        self.assertEqual((await self.client.get("/staff/api/me")).status_code, 401)
        denied = await self.client.post("/staff/api/auth/login", json={"username": "citizen", "password": "test-only-password"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["error"]["code"], "INVALID_CREDENTIALS")

    async def test_wrong_signature_audience_expiry_and_missing_claim_are_rejected(self):
        await self.login()
        original = next(c.value for c in self.client.cookies.jar if c.name == "zw_staff_access")
        claims = jwt.decode(original, SECRET, algorithms=["HS256"], audience="zhengwu-staff")
        variants = [(dict(claims, aud="citizen"), SECRET), (dict(claims, exp=time.time()-10), SECRET), (claims, "bad-secret-" * 8)]
        missing = dict(claims); missing.pop("jti")
        variants.append((missing, SECRET))
        for payload, key in variants:
            self.client.cookies.set("zw_staff_access", jwt.encode(payload, key, algorithm="HS256"), domain="testserver.local", path="/staff")
            self.assertEqual((await self.client.get("/staff/api/me")).status_code, 401)

    async def test_refresh_rotates_once_and_logout_revokes_access(self):
        await self.login()
        old_access = next(c.value for c in self.client.cookies.jar if c.name == "zw_staff_access")
        old_refresh = next(c.value for c in self.client.cookies.jar if c.name == "zw_staff_refresh")
        refreshed = await self.client.post("/staff/api/auth/refresh")
        self.assertEqual(refreshed.status_code, 200)
        self.client.headers["X-CSRF-Token"] = refreshed.json()["csrf_token"]
        me = await self.client.get("/staff/api/me")
        self.assertEqual(me.status_code, 200)
        current_access = next(c.value for c in self.client.cookies.jar if c.name == "zw_staff_access")
        self.assertNotEqual(old_access, current_access)
        self.client.cookies.set("zw_staff_refresh", old_refresh, domain="testserver.local", path="/staff")
        self.assertEqual((await self.client.post("/staff/api/auth/refresh")).status_code, 401)
        self.assertEqual((await self.client.post("/staff/api/auth/logout")).status_code, 200)
        self.client.cookies.set("zw_staff_access", current_access, domain="testserver.local", path="/staff")
        self.assertEqual((await self.client.get("/staff/api/me")).status_code, 401)

    async def test_csrf_is_required_for_json_and_form(self):
        for options in [{"json": {"username": "window", "password": "test-only-password"}},
                        {"data": {"username": "window", "password": "test-only-password", "csrf_token": "bad"}}]:
            response = await self.client.post("/staff/api/auth/login", headers={"X-CSRF-Token": "bad"}, **options)
            self.assertEqual(response.status_code, 403)

    async def test_role_revocation_invalidates_existing_jwt_and_refresh(self):
        await self.login()
        await self.app.state.staff_repo.revoke_role("window", "window", "residence-permit", actor="test-operator")
        self.assertEqual((await self.client.get("/staff/api/me")).status_code, 401)
        self.assertEqual((await self.client.post("/staff/api/auth/refresh")).status_code, 401)

    async def test_engineer_is_not_business_administrator(self):
        await self.app.state.repo.create_user("engineer", "test-only-password")
        await self.app.state.staff_repo.grant_role("engineer", "engineer", "*", actor="test-operator")
        await self.login("engineer")
        self.assertEqual((await self.client.get("/staff/api/system/status")).status_code, 200)
        self.assertEqual((await self.client.get("/staff/knowledge")).status_code, 403)
        self.assertEqual((await self.client.get("/staff/prompts")).status_code, 403)
        me = (await self.client.get("/staff/api/me")).json()
        self.assertNotIn("conversation.read", me["permissions"])
        self.assertNotIn("material.view", me["permissions"])

    async def test_permission_and_scope_cannot_be_combined_from_different_grants(self):
        from zhengwu_precheck.core.staff_permissions import allows
        grants = [{"role_code": "owner", "matter_id": "residence-permit"},
                  {"role_code": "hotline", "matter_id": "sole-trader-registration"}]
        self.assertTrue(allows(grants, "rule.edit", "residence-permit"))
        self.assertFalse(allows(grants, "rule.edit", "sole-trader-registration"))
        self.assertFalse(allows(grants, "material.view", "sole-trader-registration"))

    async def test_conversation_requires_scope_explicit_record_grant_and_body_permission(self):
        # 使用现有真实会话表，无材料/工单假数据。
        from zhengwu_precheck.utils.security import digest
        who = {"user_id": None, "anonymous_hash": digest("test-visitor"), "key": "anon:" + digest("test-visitor")}
        turn = await self.app.state.repo.reserve(None, "教学问题", str(uuid4()), who)
        await self.app.state.repo.finish(turn, {"outcome": "insufficient_evidence", "text": "现有办事指南中未找到相关依据", "citations": []})
        sid = turn["session_id"]
        async with self.app.state.repo.engine.begin() as conn:
            await conn.execute(text("UPDATE conversations SET matter_id='residence-permit' WHERE id=:id"), {"id": sid})
            await conn.execute(text("UPDATE messages SET matter_id='residence-permit' WHERE conversation_id=:id"), {"id": sid})
            await conn.execute(text("""INSERT INTO messages(id,conversation_id,sender_type,content,status,request_id,matter_id,created_at)
                VALUES(:id,:sid,'user','工商事项历史正文','complete',:request,'sole-trader-registration',:now)"""),
                {"id": str(uuid4()), "sid": sid, "request": str(uuid4()), "now": time.time()})
            # 补全另一事项的完整轮次，避免仅因“未完成轮次”过滤而让范围测试假通过。
            foreign = (await conn.execute(text("SELECT id,request_id FROM messages WHERE conversation_id=:id AND content='工商事项历史正文'"), {"id": sid})).mappings().first()
            await conn.execute(text("""INSERT INTO messages(id,conversation_id,sender_type,content,status,request_id,reply_to_message_id,matter_id,outcome,created_at)
                VALUES(:id,:sid,'assistant','工商事项历史回答','complete',:request,:reply,'sole-trader-registration','answered',:now)"""),
                {"id": str(uuid4()), "sid": sid, "request": foreign["request_id"], "reply": foreign["id"], "now": time.time()})
        await self.login()
        url = f"/staff/api/conversations/{sid}"
        self.assertEqual((await self.client.get(url)).status_code, 404)
        await self.app.state.staff_repo.grant_record("window", sid, "summary", actor="test-operator")
        summary = await self.client.get(url)
        self.assertEqual(summary.status_code, 200)
        self.assertNotIn("messages", summary.json())
        self.assertEqual((await self.client.get(url + "/messages")).status_code, 404)
        await self.app.state.staff_repo.grant_record("window", sid, "body", actor="test-operator")
        body_result = await self.client.get(url + "/messages")
        self.assertEqual(body_result.status_code, 200)
        self.assertIn("教学问题", body_result.text)
        self.assertNotIn("工商事项历史正文", body_result.text)
        await self.app.state.repo.create_user("hotline", "test-only-password")
        await self.app.state.staff_repo.grant_role("hotline", "hotline", "residence-permit", actor="test-operator")
        await self.app.state.staff_repo.grant_record("hotline", sid, "body", actor="test-operator")
        await self.login("hotline")
        self.assertEqual((await self.client.get(url)).status_code, 200)
        self.assertEqual((await self.client.get(url + "/messages")).status_code, 403)
        await self.app.state.staff_repo.revoke_record("hotline", sid, actor="test-operator")
        self.assertEqual((await self.client.get(url)).status_code, 404)

    async def test_staff_jwt_does_not_authorize_citizen_routes_and_existing_data_survives(self):
        await self.login()
        staff_access = next(c.value for c in self.client.cookies.jar if c.name == "zw_staff_access")
        self.client.cookies.set("zw_auth", staff_access)
        response = await self.client.get("/bootstrap")
        self.assertEqual(response.status_code, 401)
        async with self.app.state.repo.engine.connect() as conn:
            count = (await conn.execute(text("SELECT COUNT(*) FROM users"))).scalar()
        self.assertEqual(count, 1)

    async def test_logout_using_pre_refresh_cookies_revokes_new_access(self):
        await self.login()
        old_access = next(c.value for c in self.client.cookies.jar if c.name == "zw_staff_access")
        old_refresh = next(c.value for c in self.client.cookies.jar if c.name == "zw_staff_refresh")
        self.assertEqual((await self.client.post("/staff/api/auth/refresh")).status_code, 200)
        current_access = next(c.value for c in self.client.cookies.jar if c.name == "zw_staff_access")
        self.client.cookies.set("zw_staff_access", old_access, domain="testserver.local", path="/staff")
        self.client.cookies.set("zw_staff_refresh", old_refresh, domain="testserver.local", path="/staff")
        self.assertEqual((await self.client.post("/staff/api/auth/logout")).status_code, 200)
        self.client.cookies.set("zw_staff_access", current_access, domain="testserver.local", path="/staff")
        self.assertEqual((await self.client.get("/staff/api/me")).status_code, 401)

    async def test_initialize_syncs_release_read_permission_on_existing_database(self):
        from zhengwu_precheck.core.staff_permissions import ROLE_PERMISSIONS
        async with self.app.state.repo.engine.begin() as conn:
            await conn.execute(text("DELETE FROM role_permissions WHERE permission_code='rule.release.read'"))
            await conn.execute(text("DELETE FROM permissions WHERE code='rule.release.read'"))
        await self.app.state.staff_repo.initialize()
        await self.app.state.staff_repo.initialize()
        async with self.app.state.repo.engine.connect() as conn:
            roles = (await conn.execute(text("SELECT role_code FROM role_permissions WHERE permission_code='rule.release.read' ORDER BY role_code"))).scalars().all()
            users = (await conn.execute(text("SELECT COUNT(*) FROM users WHERE username='window'"))).scalar_one()
        self.assertEqual(roles, ["owner", "supervisor"])
        self.assertEqual(users, 1)
        for role in ("owner", "supervisor"):
            self.assertIn("rule.release.read", ROLE_PERMISSIONS[role])
        for role in ("window", "hotline", "engineer", "reviewer", "director"):
            self.assertNotIn("rule.release.read", ROLE_PERMISSIONS[role])

    async def test_staff_tables_preserve_preexisting_citizen_data(self):
        from zhengwu_precheck.infrastructure.database import ChatRepository
        from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
        from zhengwu_precheck.utils.security import digest
        original = ChatRepository(Path(self.temp.name) / "old-version.sqlite3")
        try:
            await original.initialize()
            await original.create_user("existing", "test-only-existing-password")
            token, _ = await original.login("existing", "test-only-existing-password")
            who = {"user_id": None, "anonymous_hash": digest("legacy-visitor"), "key": "anon:" + digest("legacy-visitor")}
            turn = await original.reserve(None, "保留的历史问题", str(uuid4()), who)
            await original.finish(turn, {"outcome": "insufficient_evidence", "text": "历史回答", "citations": []})
            await StaffRepository(original.engine).initialize()
            self.assertIsNotNone(await original.authenticate(token))
            restored = await original.list_messages(turn["session_id"], who)
            self.assertEqual(restored["items"][0]["content"], "保留的历史问题")
            self.assertEqual(restored["items"][1]["content"], "历史回答")
        finally:
            await original.close()

    async def test_missing_staff_key_disables_only_staff_login(self):
        app = create_app(Settings(runtime_dir=Path(self.temp.name) / "unconfigured", staff_jwt_secret=None), qa=FakeQA())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                page = await client.get("/staff/login")
                self.assertIn("工作人员登录尚未配置", page.text)
                csrf = re.search('name="csrf_token" value="([^"]+)"', page.text)[1]
                result = await client.post("/staff/api/auth/login", headers={"X-CSRF-Token": csrf}, json={"username": "any", "password": "any"})
                self.assertEqual(result.status_code, 503)
                self.assertEqual((await client.get("/bootstrap")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
