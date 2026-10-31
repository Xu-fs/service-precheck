"""知识SSR与写接口契约测试：真实岗位/JWT/SQLite，外部嵌入使用替身。"""
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings
from zhengwu_precheck.core.guide_rag import ApiConfig
from test_chat import FakeQA


class RouteQA(FakeQA):
    def config(self, prefix):
        return ApiConfig("openai-compatible", "fixture", "http://127.0.0.1:9999/v1", "TEST_KEY")

    async def embed(self, values):
        import numpy as np
        return np.array([[1.0, 0.0] for _ in values], dtype="float32")


class KnowledgeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Settings(runtime_dir=Path(self.temp.name), staff_jwt_secret="route-test-only-key-" + "x9" * 24), qa=RouteQA())
        # 按测试显式执行队列，避免状态断言与后台worker竞争。
        self.app.state.knowledge_service.run = AsyncMock()
        self.lifecycle = self.app.router.lifespan_context(self.app)
        await self.lifecycle.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url="http://testserver")
        for role in ("owner", "supervisor", "window", "hotline", "engineer"):
            await self.app.state.repo.create_user(role, "route-test-password")
            await self.app.state.staff_repo.grant_role(role, role, "*" if role == "engineer" else "residence-permit", actor="test-fixture")
        await self.login("owner")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifecycle.__aexit__(None, None, None)
        self.temp.cleanup()

    async def login(self, role):
        self.client.headers.pop("X-CSRF-Token", None)
        page = await self.client.get("/staff/login")
        self.csrf = re.search('name="csrf_token" value="([^"]+)"', page.text)[1]
        response = await self.client.post("/staff/api/auth/login", headers={"X-CSRF-Token": self.csrf}, json={"username": role, "password": "route-test-password"})
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf

    def rule(self, **extra):
        return {"title": "教学规则", "matter_id": "residence-permit", "scenario_id": "rental-demo", "kind": "rule",
                "rule_version": "DEMO-v1", "source_uri": "自编教学资料", "content": "教学申请表需要填写地址。", "is_teaching": True, **extra}

    async def uploaded(self):
        response = await self.client.post("/staff/api/rules", json=self.rule())
        self.assertEqual(response.status_code, 200, response.text)
        await self.app.state.knowledge_service.run_once()
        return response.json()

    async def release(self, uploaded=None, **extra):
        uploaded = uploaded or await self.uploaded()
        response = await self.client.post("/staff/api/releases", json={"title": "教学发布包", "version_ids": [uploaded["version_id"]], **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    async def test_json_and_form_csrf_and_cross_origin_are_rejected(self):
        self.client.headers.pop("X-CSRF-Token")
        for options in ({"json": self.rule()}, {"data": self.rule(is_teaching="true", csrf_token="bad")},
                        {"json": self.rule(), "headers": {"X-CSRF-Token": self.csrf, "Origin": "http://foreign.test"}}):
            response = await self.client.post("/staff/api/rules", **options)
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["error"]["code"], "CSRF_FAILED")

    async def test_each_write_route_rejects_missing_json_and_form_csrf(self):
        self.client.headers.pop("X-CSRF-Token")
        for path in ("/staff/api/knowledge/uploads", "/staff/api/rules", "/staff/api/knowledge/doc/versions",
                     "/staff/api/knowledge/doc/versions/version/text", "/staff/api/knowledge/doc/versions/version/reprocess",
                     "/staff/api/knowledge/doc/archive", "/staff/api/releases", "/staff/api/releases/release/submit",
                     "/staff/api/feedback", "/staff/api/feedback/feedback/resolve"):
            for options in ({"json": {}}, {"data": {"csrf_token": "bad"}}):
                response = await self.client.post(path, **options)
                self.assertEqual(response.status_code, 403, (path, response.text))
                self.assertEqual(response.json()["error"]["code"], "CSRF_FAILED")

    async def test_form_upload_uses_csrf_and_workflow_defaults_to_staff(self):
        self.client.headers.pop("X-CSRF-Token")
        fields = self.rule(kind="workflow", is_teaching="true", csrf_token=self.csrf)
        fields.pop("content")
        response = await self.client.post("/staff/api/knowledge/uploads", data=fields, files={"file": ("教学.md", "内部教学流程。".encode(), "text/markdown")})
        self.assertEqual(response.status_code, 303, response.text)
        version_id = response.headers["location"].rsplit("/", 1)[1]
        async with self.app.state.repo.engine.connect() as conn:
            visibility = (await conn.execute(text("SELECT d.visibility FROM knowledge_documents d JOIN knowledge_versions v ON v.document_id=d.id WHERE v.id=:id"), {"id": version_id})).scalar_one()
        self.assertEqual(visibility, "staff")
        page = await self.client.get("/staff/knowledge/new")
        self.assertIn('<option value="">按类型默认', page.text)

    async def test_workflow_public_upload_is_rejected_without_creating_document(self):
        fields = self.rule(kind="workflow", is_teaching="true", visibility="public")
        fields.pop("content")
        response = await self.client.post("/staff/api/knowledge/uploads", data=fields, files={"file": ("教学.md", "内部教学流程。".encode(), "text/markdown")})
        self.assertEqual(response.status_code, 422, response.text)
        async with self.app.state.repo.engine.connect() as conn:
            count = (await conn.execute(text("SELECT COUNT(*) FROM knowledge_documents"))).scalar_one()
        self.assertEqual(count, 0)

    async def test_job_status_hides_internal_lease_token_and_enforces_scope(self):
        uploaded = await self.uploaded()
        async with self.app.state.repo.engine.begin() as conn:
            await conn.execute(text("UPDATE knowledge_jobs SET lease_token='internal-lease-test' WHERE id=:id"), {"id": uploaded["job_id"]})
        response = await self.client.get("/staff/api/jobs/" + uploaded["job_id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], uploaded["job_id"])
        self.assertNotIn("lease_token", response.json())
        self.assertNotIn("internal-lease-test", response.text)
        await self.login("engineer")
        self.assertEqual((await self.client.get("/staff/api/jobs/" + uploaded["job_id"])).status_code, 404)

    async def test_audit_ssr_shows_frozen_versions_and_does_not_backfill_old_record(self):
        release_id = await self.release()
        response = await self.client.post(f"/staff/api/releases/{release_id}/submit", json={"comment": "提交教学发布包", "expected_revision": 1})
        self.assertEqual(response.status_code, 200, response.text)
        await self.login("supervisor")
        for action, revision in (("approve", 2), ("publish", 3)):
            response = await self.client.post(f"/staff/api/releases/{release_id}/{action}", json={"comment": "监督员核对教学版本", "expected_revision": revision})
            self.assertEqual(response.status_code, 200, response.text)
        from zhengwu_precheck.utils.security import digest
        principal = {"user_id": None, "anonymous_hash": digest("audit-route-test"), "key": "anon:" + digest("audit-route-test")}
        old = await self.app.state.repo.reserve(None, "旧教学问题", str(uuid4()), principal)
        await self.app.state.repo.finish(old, {"outcome": "insufficient_evidence", "text": "旧教学回答", "citations": []})
        async with self.app.state.repo.engine.begin() as conn:
            await conn.execute(text("UPDATE conversations SET matter_id='residence-permit',scenario_id='rental-demo' WHERE id=:id"), {"id": old["session_id"]})
            await conn.execute(text("UPDATE messages SET matter_id='residence-permit' WHERE conversation_id=:id"), {"id": old["session_id"]})
        turn = await self.app.state.repo.reserve(old["session_id"], "新教学问题", str(uuid4()), principal, context_factory=self.app.state.knowledge_repo.capture)
        snapshot = turn.pop("_snapshot")
        await self.app.state.repo.finish(turn, {"outcome": "insufficient_evidence", "text": "新教学回答", "citations": []})
        await self.app.state.staff_repo.grant_record("supervisor", old["session_id"], "body", actor="test-fixture")
        response = await self.client.get("/staff/audits/message:" + turn["message_id"])
        self.assertEqual(response.status_code, 200, response.text)
        for value in ("接纳时版本快照", snapshot["prompt"]["id"], release_id, snapshot["version_ids"][0]):
            self.assertIn(value, response.text)
        self.assertNotIn(snapshot["prompt"]["content"], response.text)
        old_page = await self.client.get("/staff/audits/message:" + old["message_id"])
        self.assertEqual(old_page.status_code, 200, old_page.text)
        self.assertIn("此旧记录未保存接纳时版本快照", old_page.text)
        self.assertNotIn(release_id, old_page.text)

    async def test_required_positive_integer_locks_reject_bool_float_and_missing(self):
        uploaded = await self.uploaded()
        url = f"/staff/api/knowledge/{uploaded['id']}/versions/{uploaded['version_id']}/text"
        release_id = await self.release(uploaded)
        missing = await self.client.post(url, json={"content": "新的教学正文。", "rule_version": "DEMO-v2"})
        self.assertEqual(missing.status_code, 422, missing.text)
        missing = await self.client.post(f"/staff/api/releases/{release_id}/submit", json={"comment": "提交"})
        self.assertEqual(missing.status_code, 422, missing.text)
        for value in (None, "", True, 1.5, {}, [], 0, -1, 2 ** 64):
            response = await self.client.post(url, json={"content": "新的教学正文。", "rule_version": "DEMO-v2", "expected_latest": value})
            self.assertEqual(response.status_code, 422, response.text)
            action = await self.client.post(f"/staff/api/releases/{release_id}/submit", json={"comment": "提交", "expected_revision": value})
            self.assertEqual(action.status_code, 422, action.text)

    async def test_new_version_parent_match_and_stale_lock_preserve_history(self):
        uploaded = await self.uploaded()
        payload = {"content": "新的教学正文。", "rule_version": "DEMO-v2", "expected_latest": 1}
        wrong = await self.client.post(f"/staff/api/knowledge/wrong-parent/versions/{uploaded['version_id']}/text", json=payload)
        self.assertEqual(wrong.status_code, 404, wrong.text)
        url = f"/staff/api/knowledge/{uploaded['id']}/versions"
        response = await self.client.post(url, json={**payload, "base_version_id": uploaded["version_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        stale = await self.client.post(url, json={**payload, "rule_version": "DEMO-v3", "base_version_id": uploaded["version_id"]})
        self.assertEqual(stale.status_code, 409, stale.text)
        old = await self.client.get("/staff/knowledge/versions/" + uploaded["version_id"])
        self.assertIn("教学申请表需要填写地址。", old.text)
        wrong_retry = await self.client.post(f"/staff/api/knowledge/wrong-parent/versions/{uploaded['version_id']}/reprocess", json={})
        self.assertEqual(wrong_retry.status_code, 404, wrong_retry.text)

    async def test_invalid_nested_fields_and_version_ids_are_422_not_500(self):
        for field, value in (("kind", {}), ("title", []), ("source_uri", {}), ("rule_version", False), ("is_teaching", 1)):
            response = await self.client.post("/staff/api/rules", json=self.rule(**{field: value}))
            self.assertEqual(response.status_code, 422, (field, response.text))
        for versions in ([{}], [True], [1], [["nested"]], {"id": "bad"}):
            response = await self.client.post("/staff/api/releases", json={"title": "教学包", "version_ids": versions})
            self.assertEqual(response.status_code, 422, response.text)

    async def test_timezone_offsets_preserved_and_invalid_times_rejected(self):
        uploaded = await self.uploaded()
        release_id = await self.release(uploaded, effective_from="2030-01-01T00:00:00+00:00", effective_to="2030-01-02T00:00:00+00:00")
        async with self.app.state.repo.engine.connect() as conn:
            start = (await conn.execute(text("SELECT effective_from FROM knowledge_releases WHERE id=:id"), {"id": release_id})).scalar_one()
        self.assertEqual(start, datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
        for value in (True, float("inf"), float("nan"), {}, [], "invalid", "999999-01-01"):
            if isinstance(value, float):
                import json
                response = await self.client.post("/staff/api/releases", content=json.dumps({"title": "教学包", "version_ids": [uploaded["version_id"]], "effective_from": value}), headers={"Content-Type": "application/json"})
            else:
                response = await self.client.post("/staff/api/releases", json={"title": "教学包", "version_ids": [uploaded["version_id"]], "effective_from": value})
            self.assertEqual(response.status_code, 422, response.text)

    async def test_release_menu_entry_and_details_use_same_permission(self):
        release_id = await self.release()
        for role in ("window", "hotline", "engineer"):
            await self.login(role)
            page = await self.client.get("/staff")
            self.assertNotIn('href="/staff/reviews"', page.text)
            self.assertEqual((await self.client.get("/staff/reviews")).status_code, 403)
            self.assertEqual((await self.client.get("/staff/reviews/" + release_id)).status_code, 403)
        await self.login("supervisor")
        self.assertEqual((await self.client.get("/staff/reviews")).status_code, 200)
        self.assertEqual((await self.client.get("/staff/reviews/" + release_id)).status_code, 200)

    async def test_release_read_scope_not_combined_with_hotline_scope(self):
        await self.app.state.staff_repo.grant_role("owner", "owner", "sole-trader-registration", actor="test-fixture")
        await self.login("owner")
        response = await self.client.post("/staff/api/rules", json=self.rule(title="工商教学草稿", matter_id="sole-trader-registration", scenario_id="standard-demo"))
        self.assertEqual(response.status_code, 200, response.text)
        await self.app.state.knowledge_service.run_once()
        release_id = await self.release(response.json(), title="工商教学包")
        await self.app.state.staff_repo.revoke_role("owner", "owner", "sole-trader-registration", actor="test-fixture")
        await self.app.state.staff_repo.grant_role("owner", "hotline", "sole-trader-registration", actor="test-fixture")
        await self.login("owner")
        page = await self.client.get("/staff/reviews")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn("工商教学包", page.text)
        self.assertNotIn("工商教学草稿", page.text)
        self.assertEqual((await self.client.get("/staff/reviews/" + release_id)).status_code, 404)

    async def test_window_cannot_modify_and_other_scope_cannot_read(self):
        uploaded = await self.uploaded()
        await self.login("window")
        self.assertEqual((await self.client.post("/staff/api/rules", json=self.rule())).status_code, 403)
        self.assertEqual((await self.client.get("/staff/knowledge/new")).status_code, 403)
        await self.app.state.staff_repo.revoke_role("window", "window", "residence-permit", actor="test-fixture")
        await self.app.state.staff_repo.grant_role("window", "window", "sole-trader-registration", actor="test-fixture")
        await self.login("window")
        self.assertEqual((await self.client.get("/staff/knowledge/versions/" + uploaded["version_id"])).status_code, 404)

    async def test_release_form_repeated_ids_and_state_lock(self):
        uploaded = await self.uploaded()
        second = await self.uploaded()
        self.client.headers.pop("X-CSRF-Token")
        response = await self.client.post("/staff/api/releases", data={"title": "表单教学包", "version_ids": [uploaded["version_id"], second["version_id"]], "csrf_token": self.csrf})
        self.assertEqual(response.status_code, 303, response.text)
        location = response.headers["location"]
        page = await self.client.get(location)
        self.assertIn('name="expected_revision" value="1"', page.text)
        self.assertEqual(page.text.count('/staff/knowledge/versions/'), 2)
        missing = await self.client.post("/staff/api/releases/" + location.rsplit("/", 1)[1] + "/submit", data={"csrf_token": self.csrf, "comment": "教学提交"})
        self.assertEqual(missing.status_code, 422, missing.text)
        response = await self.client.post("/staff/api/releases/" + location.rsplit("/", 1)[1] + "/submit", data={"csrf_token": self.csrf, "expected_revision": "1", "comment": "教学提交"})
        self.assertEqual(response.status_code, 303, response.text)
        stale = await self.client.post("/staff/api/releases/" + location.rsplit("/", 1)[1] + "/submit", data={"csrf_token": self.csrf, "expected_revision": "1", "comment": "重复教学提交"})
        self.assertEqual(stale.status_code, 409, stale.text)


if __name__ == "__main__":
    unittest.main()
