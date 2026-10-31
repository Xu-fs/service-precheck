"""提示词版本：真实SQLite事务、事项隔离和版本保留，不调用模型。"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.core.errors import AppError

AVAILABLE = importlib.util.find_spec("zhengwu_precheck.infrastructure.prompt_repository") is not None
if AVAILABLE:
    from zhengwu_precheck.infrastructure.prompt_repository import PromptRepository, DEFAULT_SYSTEM_PROMPT

MATTER = "residence-permit"
OTHER = "sole-trader-registration"


def identity(*grants):
    return {"user_id": "test-supervisor", "username": "supervisor", "grants": list(grants)}


def grant(role, matter):
    return {"role_code": role, "matter_id": matter}


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(AVAILABLE, "提示词版本模块尚未实现")
        self.temp = tempfile.TemporaryDirectory()
        self.chat = ChatRepository(Path(self.temp.name) / "prompts.db")
        await self.chat.initialize()
        self.repo = PromptRepository(self.chat.engine)
        await self.repo.initialize()
        self.person = identity(grant("supervisor", MATTER))

    async def asyncTearDown(self):
        if hasattr(self, "chat"):
            await self.chat.close()
            self.temp.cleanup()

    async def test_defaults_are_independent_and_initialization_preserves_versions(self):
        first = await self.repo.active(MATTER)
        other = await self.repo.active(OTHER)
        self.assertEqual(first["content"], DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(other["version"], 1)
        saved = await self.repo.save(self.person, MATTER, "新模板", 1, "修改教学提示")
        await self.repo.initialize()
        self.assertEqual((await self.repo.active(MATTER))["id"], saved["id"])
        self.assertEqual((await self.repo.active(OTHER))["id"], other["id"])

    async def test_restore_appends_and_retains_original_author_and_content(self):
        original = await self.repo.active(MATTER)
        await self.repo.save(self.person, MATTER, "新模板", 1, "调整")
        restored = await self.repo.restore(self.person, MATTER, 1, 2, "恢复初始模板")
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["content"], original["content"])
        self.assertNotEqual(restored["id"], original["id"])
        versions = await self.repo.history(self.person, MATTER)
        self.assertEqual([v["version"] for v in versions], [3, 2, 1])
        self.assertEqual(versions[-1]["author_id"], original["author_id"])

    async def test_optimistic_lock_allows_exactly_one_concurrent_writer(self):
        results = await asyncio.gather(*[
            self.repo.save(self.person, MATTER, content, 1, "并发修改")
            for content in ("模板甲", "模板乙")], return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        errors = [r for r in results if isinstance(r, AppError)]
        self.assertEqual([e.status for e in errors], [409])
        self.assertEqual(len(await self.repo.history(self.person, MATTER)), 2)

    async def test_same_grant_must_cover_action_and_matter(self):
        mixed = identity(grant("supervisor", MATTER), grant("engineer", OTHER))
        for operation in (lambda: self.repo.save(mixed, OTHER, "越权", 1, "修改"),
                          lambda: self.repo.history(mixed, OTHER),
                          lambda: self.repo.restore(mixed, OTHER, 1, 1, "恢复")):
            with self.assertRaises(AppError) as raised:
                await operation()
            self.assertEqual(raised.exception.status, 403)

    async def test_validation_and_missing_version_do_not_change_active(self):
        for content, comment in [("", "修改"), (" " * 5, "修改"), ("字" * 2001, "修改"), ("有效", " ")]:
            with self.assertRaises(AppError) as raised:
                await self.repo.save(self.person, MATTER, content, 1, comment)
            self.assertEqual(raised.exception.status, 422)
        with self.assertRaises(AppError) as raised:
            await self.repo.restore(self.person, MATTER, 999, 1, "恢复")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual((await self.repo.active(MATTER))["version"], 1)

    async def test_audit_contains_version_metadata_and_no_prompt_content(self):
        await self.repo.save(self.person, MATTER, "正文秘密标记", 1, "说明秘密标记")
        async with self.chat.engine.connect() as connection:
            rows = (await connection.execute(text("SELECT * FROM audit_logs WHERE entity_type='prompt'"))).mappings().all()
        self.assertTrue(rows)
        self.assertNotIn("正文秘密标记", str(rows))
        self.assertNotIn("说明秘密标记", str(rows))
        self.assertEqual(rows[-1]["actor_id"], self.person["user_id"])


class PromptRouteTests(PromptTests):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec("zhengwu_precheck.api.routes.prompts"), "提示词路由尚未实现")
        await super().asyncSetUp()
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from fastapi.templating import Jinja2Templates
        import httpx
        from types import SimpleNamespace
        from zhengwu_precheck.api.routes.prompts import router
        self.app = FastAPI()
        self.app.state.prompt_repo = self.repo
        self.app.state.templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "src/zhengwu_precheck/templates"))
        # 页面骨架由主应用装配；独立测试保留真实模板，只补最小父模板。
        from jinja2 import ChoiceLoader, DictLoader
        self.app.state.templates.env.loader = ChoiceLoader([
            self.app.state.templates.env.loader,
            DictLoader({"staff/base.html": "<main>{% block content %}{% endblock %}</main>"})])
        async def authenticate(token):
            if token != "test-access":
                raise AppError(401, "STAFF_AUTH_REQUIRED", "请先登录")
            return dict(self.person, grants=[dict(g, permissions=["prompt.manage", "workspace.view"]) for g in self.person["grants"]])
        self.app.state.staff_auth = SimpleNamespace(identity=authenticate)
        @self.app.exception_handler(AppError)
        async def error_handler(request, error):
            return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)
        self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.client.cookies.set("zw_staff_access", "test-access")
        self.client.cookies.set("zw_staff_csrf", "x" * 40)
        from zhengwu_precheck.utils.security import digest
        self.csrf = digest("staff-csrf:" + "x" * 40)

    async def asyncTearDown(self):
        if hasattr(self, "client"):
            await self.client.aclose()
        await super().asyncTearDown()

    async def test_json_save_history_restore_and_csrf(self):
        url = f"/staff/api/prompts/{MATTER}/versions"
        body = {"content": "路由新模板", "expected_version": 1, "comment": "测试路由"}
        self.assertEqual((await self.client.post(url, json=body)).status_code, 403)
        response = await self.client.post(url, json=body, headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], 2)
        versions = await self.client.get(url)
        self.assertEqual([v["version"] for v in versions.json()], [2, 1])
        response = await self.client.post(f"/staff/api/prompts/{MATTER}/restore", json={"version": 1, "expected_version": 2, "comment": "恢复"}, headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], 3)

    async def test_form_redirect_and_missing_authentication(self):
        response = await self.client.post(f"/staff/api/prompts/{MATTER}/versions", data={"content": "表单模板", "expected_version": "1", "comment": "表单保存", "csrf_token": self.csrf})
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual((await self.repo.active(MATTER))["content"], "表单模板")
        self.client.cookies.clear()
        self.assertEqual((await self.client.get(f"/staff/api/prompts/{MATTER}/versions")).status_code, 401)

    async def test_page_escapes_content_and_conflicting_form_shows_error(self):
        await self.repo.save(self.person, MATTER, "<script>alert(1)</script>", 1, "转义测试")
        page = await self.client.get("/staff/prompts")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.assertNotIn("<script>alert(1)</script>", page.text)
        self.assertNotIn(f'<option value="{OTHER}"', page.text)
        response = await self.client.post(f"/staff/api/prompts/{MATTER}/versions", data={"content": "旧页面保存", "expected_version": "1", "comment": "测试冲突", "csrf_token": self.csrf})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("已被其他人修改", response.text)
        denied = await self.client.get(f"/staff/prompts?matter_id={OTHER}")
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
