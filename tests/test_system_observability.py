"""工程师运维查询；账号、正文和故障均来自隔离的虚构测试环境。"""
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings
from test_chat import FakeQA, events


class SystemObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".venv" / "tmp")
        self.app = create_app(Settings(runtime_dir=Path(self.temp.name),
                                      staff_jwt_secret="observability-fixture-" + "x9" * 24), qa=FakeQA())

        @self.app.get("/fixture/unavailable")
        async def unavailable():
            raise AppError(503, "MODEL_UNAVAILABLE", "虚构上游故障详情", True)

        @self.app.get("/fixture/internal-error")
        async def internal_error():
            raise RuntimeError("虚构未处理异常详情")

        self.lifecycle = self.app.router.lifespan_context(self.app)
        await self.lifecycle.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifecycle.__aexit__(None, None, None)
        self.temp.cleanup()

    async def login(self, role="engineer"):
        await self.app.state.repo.create_user("operations-fixture", "operations-fixture-password")
        await self.app.state.staff_repo.grant_role("operations-fixture", role, "*", actor="fixture")
        page = await self.client.get("/staff/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
        response = await self.client.post("/staff/api/auth/login", data={
            "username": "operations-fixture", "password": "operations-fixture-password", "csrf_token": csrf})
        self.assertEqual(response.status_code, 303)

    async def test_engineer_can_find_persisted_request_by_response_id(self):
        await self.login()
        home = await self.client.get("/")
        request_id = home.headers.get("x-request-id")
        self.assertIsNotNone(request_id)
        self.assertEqual(str(UUID(request_id)), request_id)
        result = await self.client.get("/staff/api/system/logs", params={"request_id": request_id})
        self.assertEqual(result.status_code, 200)
        rows = result.json()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["route"], rows[0]["method"], rows[0]["status_code"]), ("/", "GET", 200))
        self.assertGreaterEqual(rows[0]["duration_ms"], 0)
        async with self.app.state.repo.engine.connect() as conn:
            saved = (await conn.execute(text("SELECT request_id FROM system_logs WHERE request_id=:id"),
                                        {"id": request_id})).scalar_one()
        self.assertEqual(saved, request_id)

    async def test_error_filters_match_the_failed_request(self):
        await self.login()
        failure = await self.client.get("/fixture/unavailable", params={"note": "虚构查询正文"})
        self.assertEqual(failure.status_code, 503)
        request_id = failure.json()["error"]["request_id"]
        result = await self.client.get("/staff/api/system/logs", params={
            "level": "error", "error_code": "MODEL_UNAVAILABLE", "request_id": request_id})
        self.assertEqual(result.status_code, 200)
        row = result.json()["items"][0]
        self.assertEqual((row["route"], row["level"], row["error_code"]),
                         ("/fixture/unavailable", "error", "MODEL_UNAVAILABLE"))
        self.assertEqual(failure.headers["x-request-id"], request_id)
        self.assertNotIn("虚构查询正文", result.text)
        self.assertNotIn("虚构上游故障详情", result.text)
        empty = await self.client.get("/staff/api/system/logs", params={"request_id": str(uuid4())})
        self.assertEqual(empty.json()["total"], 0)

    async def test_unhandled_error_response_exposes_only_correlatable_request_id(self):
        await self.login()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                   base_url="http://testserver", cookies=self.client.cookies)
        try:
            response = await client.get("/fixture/internal-error")
        finally:
            await client.aclose()
        self.assertEqual(response.status_code, 500)
        request_id = response.headers.get("x-request-id")
        self.assertEqual(str(UUID(request_id)), request_id)
        self.assertEqual(response.json()["error"]["request_id"], request_id)
        self.assertEqual(response.json()["error"]["code"], "UNHANDLED_ERROR")
        self.assertNotIn("虚构未处理异常详情", response.text)
        records = (await self.client.get("/staff/api/system/logs", params={"request_id": request_id})).json()
        self.assertEqual(records["items"][0]["error_code"], "UNHANDLED_ERROR")

    async def seed_failures(self):
        from zhengwu_precheck.utils.security import digest
        who = {"user_id": None, "anonymous_hash": digest("fictional-visitor"), "key": "anon:fixture"}
        turn = await self.app.state.repo.reserve(None, "虚构问答正文", str(uuid4()), who)
        await self.app.state.repo.fail(turn, "MODEL_TIMEOUT")
        job_id, version_id, document_id = (str(uuid4()) for _ in range(3))
        async with self.app.state.repo.engine.begin() as conn:
            await conn.execute(text("""INSERT INTO knowledge_documents
                (id,title,matter_id,scenario_id,kind,created_by,created_at)
                VALUES(:id,'虚构知识标题','residence-permit','rental-demo','guide','fixture',:now)"""),
                {"id": document_id, "now": time.time()})
            await conn.execute(text("""INSERT INTO knowledge_versions
                (id,document_id,version_no,rule_version,filename,source_uri,processing_status,state,created_by,created_at)
                VALUES(:id,:document,1,'fixture-v1','虚构文件.md','fictional','failed','draft','fixture',:now)"""),
                {"id": version_id, "document": document_id, "now": time.time()})
            await conn.execute(text("""INSERT INTO knowledge_jobs
                (id,version_id,status,stage,attempts,error_code,created_at,finished_at)
                VALUES(:id,:version,'failed','failed',2,'DOCUMENT_PROCESSING_FAILED',:now,:now)"""),
                {"id": job_id, "version": version_id, "now": time.time()})
        return turn, job_id

    async def test_failed_tasks_have_technical_fields_and_source_filter(self):
        await self.login()
        turn, job_id = await self.seed_failures()
        response = await self.client.get("/staff/api/system/failures")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["total"], 2)
        self.assertEqual({row["task_type"] for row in result["items"]}, {"chat", "knowledge"})
        for hidden in ("虚构问答正文", "虚构知识标题", "虚构文件.md", "conversation_id", "user_id"):
            self.assertNotIn(hidden, response.text)
        selected = await self.client.get("/staff/api/system/failures", params={"task_type": "knowledge"})
        self.assertEqual(selected.json()["total"], 1)
        self.assertEqual(selected.json()["items"][0]["task_id"], job_id)
        selected = await self.client.get("/staff/api/system/failures", params={"request_id": turn["request_id"]})
        self.assertEqual(selected.json()["items"][0]["error_code"], "MODEL_TIMEOUT")

    async def test_filters_and_pagination_preserve_total_and_time_range(self):
        await self.login()
        await self.seed_failures()
        first = (await self.client.get("/staff/api/system/failures", params={"page_size": 1})).json()
        self.assertEqual((first["total"], first["page"], len(first["items"])), (2, 1, 1))
        second = (await self.client.get("/staff/api/system/failures", params={"page_size": 1, "page": 2})).json()
        self.assertNotEqual(first["items"][0]["task_id"], second["items"][0]["task_id"])
        past = await self.client.get("/staff/api/system/failures", params={"end": "2000-01-01"})
        self.assertEqual(past.json()["total"], 0)
        invalid = await self.client.get("/staff/api/system/logs", params={"start": "2026-11-02", "end": "2026-11-01"})
        self.assertEqual(invalid.status_code, 422)

    async def test_workspaces_and_server_rendered_filters_are_usable(self):
        await self.login()
        workspace = await self.client.get("/staff")
        for slug in ("logs", "failures"):
            self.assertIn(f'href="/staff/{slug}"', workspace.text)
        await self.seed_failures()
        logs = await self.client.get("/staff/logs", params={"level": "error"})
        self.assertEqual(logs.status_code, 200)
        self.assertIn('name="request_id"', logs.text)
        tasks = await self.client.get("/staff/failures", params={"task_type": "chat"})
        self.assertEqual(tasks.status_code, 200)
        self.assertIn("MODEL_TIMEOUT", tasks.text)
        self.assertNotIn("DOCUMENT_PROCESSING_FAILED", tasks.text)

    async def test_worker_failure_is_persisted_without_exception_details(self):
        await self.login()
        await self.app.state.system_log_repo.task_failure("ticket", "TICKET_WORKER_FAILED")
        result = await self.client.get("/staff/api/system/failures", params={"task_type": "ticket"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["items"][0]["error_code"], "TICKET_WORKER_FAILED")
        logs = await self.client.get("/staff/api/system/logs", params={"error_code": "TICKET_WORKER_FAILED"})
        self.assertEqual(logs.json()["items"][0]["event"], "task_failed")

    async def test_stream_failure_correlates_http_log_with_failed_task(self):
        await self.login()
        bootstrap = (await self.client.get("/bootstrap")).json()
        headers = {"X-CSRF-Token": bootstrap["csrf_token"], "Idempotency-Key": str(uuid4())}
        first = await self.client.post("/chat", json={"question": "居住证需要什么材料？"}, headers=headers)
        prompt = next(data for kind, data in events(first) if kind == "matter_required")
        sid = prompt["session_id"]
        confirmed = await self.client.post(f"/sessions/{sid}/matter", json={
            "matter_id": "residence-permit", "scenario_id": "rental-demo",
            "confirmation_token": prompt["confirmation_token"]}, headers=headers)
        self.assertEqual(confirmed.status_code, 200)
        self.app.state.qa.fail = True
        failure = await self.client.post("/chat", json={"session_id": sid, "question": "示例申请表有哪些字段？"},
                                        headers={**headers, "Idempotency-Key": str(uuid4())})
        self.assertEqual(failure.status_code, 200)
        error = next(data for kind, data in events(failure) if kind == "error")
        self.assertEqual(error["code"], "MODEL_TIMEOUT")
        self.assertEqual(failure.headers["x-request-id"], error["request_id"])
        logs = (await self.client.get("/staff/api/system/logs", params={"request_id": error["request_id"]})).json()
        self.assertEqual(len(logs["items"]), 1)
        self.assertEqual((logs["items"][0]["level"], logs["items"][0]["error_code"]), ("error", "MODEL_TIMEOUT"))
        failures = (await self.client.get("/staff/api/system/failures", params={"request_id": error["request_id"]})).json()
        self.assertEqual(failures["total"], 1)

    async def test_existing_log_is_available_after_repository_reinitialization(self):
        await self.login()
        response = await self.client.get("/")
        from zhengwu_precheck.infrastructure.system_log_repository import SystemLogRepository
        from sqlalchemy.ext.asyncio import create_async_engine
        engine = create_async_engine("sqlite+aiosqlite:///" + (Path(self.temp.name) / "chat.sqlite3").as_posix())
        try:
            repository = SystemLogRepository(engine)
            await repository.initialize()
            result = await repository.logs({"request_id": response.headers["x-request-id"]})
            self.assertEqual(result["total"], 1)
        finally:
            await engine.dispose()

    async def test_business_workspace_keeps_its_existing_menu_and_access(self):
        await self.login("window")
        workspace = await self.client.get("/staff")
        self.assertNotIn('href="/staff/logs"', workspace.text)
        for path in ("/staff/logs", "/staff/failures", "/staff/api/system/logs", "/staff/api/system/failures"):
            self.assertEqual((await self.client.get(path)).status_code, 403)

    async def test_ticket_worker_failure_is_recorded_by_the_running_worker(self):
        await self.login()
        from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository
        worker = TicketRepository(self.app.state.repo.engine, system_log_repo=self.app.state.system_log_repo)

        async def unavailable_dependency():
            worker.stop()
            raise OSError("隔离测试的后台依赖不可用")

        worker.run_due = unavailable_dependency
        await worker.run()
        records = (await self.client.get("/staff/api/system/failures", params={"task_type": "ticket"})).json()
        self.assertEqual(records["total"], 1)
        self.assertEqual(records["items"][0]["error_code"], "TICKET_WORKER_FAILED")

    async def test_log_rotation_leaves_business_and_audit_records_intact(self):
        await self.login()
        turn, _ = await self.seed_failures()
        async with self.app.state.repo.engine.begin() as conn:
            await conn.execute(text("""INSERT INTO system_logs(id,occurred_at,level,event)
                VALUES(:id,:at,'info','request')"""), [
                {"id": str(uuid4()), "at": time.time() - 31 * 86400},
                *({"id": str(uuid4()), "at": time.time() - index} for index in range(20001))])
            audit_count = (await conn.execute(text("SELECT COUNT(*) FROM audit_logs"))).scalar_one()
        await self.app.state.system_log_repo.record(event="started")
        async with self.app.state.repo.engine.connect() as conn:
            self.assertEqual((await conn.execute(text("SELECT COUNT(*) FROM system_logs"))).scalar_one(), 20000)
            self.assertEqual((await conn.execute(text("SELECT COUNT(*) FROM audit_logs"))).scalar_one(), audit_count)
            status = (await conn.execute(text("SELECT status FROM messages WHERE id=:id"),
                                        {"id": turn["message_id"]})).scalar_one()
            self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()
