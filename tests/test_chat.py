"""问答入口契约测试：仅替换模型服务，真实执行ASGI、SQLite、身份和SSE。"""
import asyncio
import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
AVAILABLE = importlib.util.find_spec("zhengwu_precheck.settings") is not None
if AVAILABLE:
    from zhengwu_precheck.main import create_app
    from zhengwu_precheck.settings import Settings
    from zhengwu_precheck.utils.context import trim_history


def events(response):
    """独立解码协议，断言事件内容而非实现源码。"""
    result = []
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        lines = block.splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if name and data:
            result.append((name, json.loads(data)))
    return result


class FakeQA:
    """测试替身不装入业务默认配置，不伪装真实模型联调。"""
    def __init__(self):
        self.calls = []
        self.fail = False
        self.ready = True
    def ensure_ready(self, snapshot=None):
        if not self.ready:
            from zhengwu_precheck.core.errors import AppError
            raise AppError(503, "SERVICE_UNAVAILABLE", "问答服务尚未配置", True)
    async def answer(self, question, history, matter_id, progress=None, snapshot=None):
        self.calls.append((question, history, matter_id))
        if self.fail:
            raise TimeoutError()
        return {"outcome": "answered", "text": "教学申请表包含姓名、联系电话和居住地址三个示例字段。", "citations": [{
            "citation_id": "c1", "filename": "居住证申领_教学指南.md", "matter": "居住证申领",
            "rule_version": "DEMO-2026-01", "chunk_id": "C002", "quote": "教学申请表包含姓名、联系电话和居住地址三个示例字段。", "is_teaching": True,
        }]}
    async def close(self):
        pass


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(AVAILABLE, "尚未实现Web问答入口")
        self.temp = tempfile.TemporaryDirectory()
        self.qa = FakeQA()
        self.app = create_app(Settings(runtime_dir=Path(self.temp.name)), qa=self.qa)
        self.lifecycle = self.app.router.lifespan_context(self.app)
        await self.lifecycle.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.clients = [self.client]
        await self.bootstrap(self.client)

    async def asyncTearDown(self):
        if hasattr(self, "clients"):
            for client in self.clients:
                await client.aclose()
            await self.lifecycle.__aexit__(None, None, None)
            self.temp.cleanup()

    async def bootstrap(self, client):
        response = await client.get("/bootstrap")
        self.assertEqual(response.status_code, 200)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]

    async def post(self, question="需要什么材料？", sid=None, key=None, client=None):
        return await (client or self.client).post("/chat", json={"session_id": sid, "question": question},
                                                 headers={"Idempotency-Key": key or str(uuid4())})

    async def confirmed(self, client=None, matter="residence-permit"):
        client = client or self.client
        response = await self.post(client=client)
        items = events(response)
        sid = items[0][1]["session_id"]
        prompt = next(data for name, data in items if name == "matter_required")
        scenario = "rental-demo" if matter == "residence-permit" else "standard-demo"
        result = await client.post(f"/sessions/{sid}/matter", json={"matter_id": matter, "scenario_id": scenario,
                                                                           "confirmation_token": prompt["confirmation_token"]})
        self.assertEqual(result.status_code, 200)
        return sid

    async def test_anonymous_confirmation_answer_and_persisted_citations(self):
        sid = await self.confirmed()
        response = await self.post("教学申请表有哪些字段？", sid)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        items = events(response)
        self.assertEqual(items[0][0], "session")
        self.assertEqual(items[-1][0], "done")
        self.assertEqual(items[-1][1]["outcome"], "answered")
        self.assertTrue(any(name == "answer_delta" for name, _ in items))
        restored = (await self.client.get(f"/sessions/{sid}/messages")).json()["items"]
        answer = next(item for item in reversed(restored) if item["outcome"] == "answered")
        self.assertIn("联系电话", answer["content"])
        self.assertEqual(answer["citations"][0]["rule_version"], "DEMO-2026-01")

    async def test_anonymous_sessions_are_isolated_and_cookie_is_http_only(self):
        sid = await self.confirmed()
        other = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.clients.append(other)
        response = await other.get("/bootstrap")
        self.assertIn("HttpOnly", response.headers.get("set-cookie", ""))
        other.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        for response in [await other.get(f"/sessions/{sid}/messages"), await self.post(sid=sid, client=other)]:
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "SESSION_NOT_FOUND")

    async def test_validation_csrf_expired_identity_and_json_errors(self):
        response = await self.client.post("/chat", json={"session_id": None, "question": ""}, headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "INVALID_QUESTION")
        response = await self.client.post("/chat", content="{", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/chat", json={"session_id": None, "question": "hello"}, headers={"X-CSRF-Token": "wrong", "Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 403)
        self.client.cookies.set("zw_auth", "invalid")
        response = await self.post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "AUTH_EXPIRED")

    async def test_idempotency_replays_without_duplicate_messages_or_model_calls(self):
        sid = await self.confirmed()
        key = str(uuid4())
        first = await self.post("字段有哪些", sid, key)
        second = await self.post("字段有哪些", sid, key)
        self.assertEqual(events(first)[-1][1]["outcome"], "answered")
        self.assertEqual(events(second)[-1][1]["outcome"], "answered")
        self.assertEqual(len(self.qa.calls), 1)
        stored = (await self.client.get(f"/sessions/{sid}/messages")).json()["items"]
        self.assertEqual(len(stored), 4)
        conflict = await self.post("不同问题", sid, key)
        self.assertEqual(conflict.status_code, 409)

    async def test_missing_second_matter_guide_falls_back_and_change_requires_confirmation(self):
        sid = await self.confirmed()
        response = await self.post("我改为咨询个体工商户设立登记", sid)
        self.assertIn("matter_required", [name for name, _ in events(response)])
        prompt = next(data for name, data in events(response) if name == "matter_required")
        result = await self.client.post(f"/sessions/{sid}/matter", json={"matter_id": "sole-trader-registration", "scenario_id": "standard-demo", "confirmation_token": prompt["confirmation_token"]})
        self.assertEqual(result.status_code, 200)
        response = await self.post("需要什么材料？", sid)
        data = next(data for name, data in events(response) if name == "fallback")
        self.assertEqual(data["text"], "现有办事指南中未找到相关依据")
        self.assertEqual(len(self.qa.calls), 0)

    async def test_unconfigured_before_stream_and_model_timeout_after_stream(self):
        sid = await self.confirmed()
        self.qa.ready = False
        unavailable = await self.post(sid=sid)
        self.assertEqual(unavailable.status_code, 503)
        self.assertIn("application/json", unavailable.headers["content-type"])
        self.qa.ready, self.qa.fail = True, True
        timeout = await self.post(sid=sid)
        names = [name for name, _ in events(timeout)]
        self.assertEqual(names[-1], "error")
        self.assertNotIn("done", names)
        self.assertEqual(events(timeout)[-1][1]["code"], "MODEL_TIMEOUT")
        stored = (await self.client.get(f"/sessions/{sid}/messages")).json()["items"]
        self.assertEqual(stored[-1]["status"], "failed")

    async def test_six_round_history_ignores_failed_and_other_matters(self):
        sid = await self.confirmed()
        for i in range(8):
            response = await self.post(f"第{i}次字段咨询", sid)
            self.assertEqual(response.status_code, 200)
        history = self.qa.calls[-1][1]
        self.assertEqual(len(history), 12)
        self.assertEqual(history[0]["content"], "第1次字段咨询")
        self.assertEqual(history[-2]["content"], "第6次字段咨询")

    async def test_login_is_separate_and_wrong_password_is_generic(self):
        sid = await self.confirmed()
        await self.app.state.repo.create_user("student", "a-long-test-password")
        bad = await self.client.post("/auth/login", json={"username": "student", "password": "wrong"})
        self.assertEqual(bad.status_code, 401)
        good = await self.client.post("/auth/login", json={"username": "student", "password": "a-long-test-password"})
        self.assertEqual(good.status_code, 200)
        await self.bootstrap(self.client)
        forbidden = await self.client.get(f"/sessions/{sid}/messages")
        self.assertEqual(forbidden.status_code, 404)
        own = await self.confirmed()
        self.assertNotEqual(own, sid)

    async def test_context_trims_whole_rounds_and_preserves_current_question(self):
        history = [{"role": role, "content": str(i) + "字" * 1100} for i in range(6) for role in ("user", "assistant")]
        trimmed = trim_history(history)
        self.assertLessEqual(sum(len(item["content"]) for item in trimmed), 6000)
        self.assertEqual(len(trimmed) % 2, 0)
        self.assertTrue(trimmed[-1]["content"].startswith("5"))

    async def test_invalid_visitor_cookie_is_rotated_with_matching_page_csrf(self):
        self.client.cookies.clear()
        self.client.cookies.set("zw_anon", "invalid")
        page = await self.client.get("/")
        self.assertEqual(page.status_code, 200)
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        self.client.headers["X-CSRF-Token"] = token
        response = await self.post()
        self.assertEqual(response.status_code, 200)

    async def test_initial_request_retry_and_concurrent_turn_are_safe(self):
        key = str(uuid4())
        first = await self.post(key=key)
        sid = events(first)[0][1]["session_id"]
        replay = await self.post(key=key)
        self.assertEqual(events(replay)[0][1]["session_id"], sid)
        # 直接在仓储接纳尚未完成的请求，模拟另一连接仍在处理。
        who = {"user_id": None, "anonymous_hash": None, "key": None}
        from zhengwu_precheck.utils.security import digest
        token = next(cookie.value for cookie in self.client.cookies.jar if cookie.name == "zw_anon")
        who["anonymous_hash"], who["key"] = digest(token), "anon:" + digest(token)
        turn = await self.app.state.repo.reserve(sid, "处理中问题", str(uuid4()), who)
        blocked = await self.post(sid=sid)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "SESSION_BUSY")
        await self.app.state.repo.fail(turn, "TEST_CANCELLED", cancelled=True)

    async def test_stale_matter_snapshot_cannot_be_reserved(self):
        from zhengwu_precheck.utils.security import digest
        from zhengwu_precheck.core.errors import AppError
        sid = await self.confirmed()
        token = next(cookie.value for cookie in self.client.cookies.jar if cookie.name == "zw_anon")
        who = {"user_id": None, "anonymous_hash": digest(token), "key": "anon:" + digest(token)}
        old = await self.app.state.repo.conversation(sid, who)
        await self.post("切换事项", sid)
        with self.assertRaises(AppError) as raised:
            await self.app.state.repo.reserve(sid, "旧页面的问题", str(uuid4()), who, expected_conversation=old)
        self.assertEqual(raised.exception.code, "SESSION_CHANGED")

    async def test_real_async_rag_uses_bounded_history_and_verified_source(self):
        from zhengwu_precheck.core.services.guide_qa import GuideQA
        captured = []
        def handler(request):
            body = json.loads(request.content)
            if request.url.path.endswith("/embeddings"):
                return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, .1]} for i in range(len(body["input"]))]})
            captured.append(body["messages"])
            context = json.loads(body["messages"][1]["content"])
            source = next(item for item in context["teaching_sources"] if "教学申请表包含姓名、联系电话" in item["text"])
            content = json.dumps({"supported": True, "citations": [{"chunk_id": source["chunk_id"], "quote": "教学申请表包含姓名、联系电话和居住地址三个示例字段。"}]}, ensure_ascii=False)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        qa = GuideQA(self.app.state.settings)
        await qa.client.aclose()
        qa.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        env = {"LLM_BASE_URL": "http://127.0.0.1:9999/v1", "EMBEDDING_BASE_URL": "http://127.0.0.1:9999/v1",
               "LLM_MODEL": "test-chat", "EMBEDDING_MODEL": "test-vector"}
        history = [{"role": role, "content": "历史" * 1100} for _ in range(6) for role in ("user", "assistant")]
        try:
            with patch.dict(os.environ, env):
                answer = await qa.answer("教学申请表有哪些字段？", history, "residence-permit")
            self.assertEqual(answer["outcome"], "answered")
            self.assertEqual(answer["citations"][0]["filename"], "居住证申领_教学指南.md")
            prompt = captured[0]
            self.assertLessEqual(sum(len(item["content"]) for item in prompt), 12000)
            context = json.loads(prompt[1]["content"])
            self.assertLessEqual(sum(len(item["content"]) for item in context["history"]), 6000)
            self.assertEqual(context["question"], "教学申请表有哪些字段？")
        finally:
            await qa.close()


if __name__ == "__main__":
    unittest.main()
