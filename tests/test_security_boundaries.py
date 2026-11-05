"""截图第1—4项的本项目边界回归；仅使用隔离的虚构数据。"""

from contextlib import closing
from pathlib import Path
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.routing import APIRoute
from sqlalchemy import text

from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services import operations
from zhengwu_precheck.infrastructure.material_repository import MaterialRepository
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.agent_repository import AgentRepository
from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings
from zhengwu_precheck.utils.security import hash_password


class StorageBoundaryTests(unittest.TestCase):
    def test_directory_link_cannot_escape_for_knowledge_file_writes(self):
        from zhengwu_precheck.utils.storage_paths import private_path

        with tempfile.TemporaryDirectory() as folder:
            root, outside = Path(folder) / "private", Path(folder) / "outside"
            root.mkdir()
            outside.mkdir()
            link = root / "knowledge"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                    capture_output=True,
                    check=False,
                )
                if result.returncode:
                    self.skipTest("本机既不允许目录符号链接，也不允许目录联接")
            with self.assertRaises(AppError):
                private_path(root, "knowledge/new.pdf").write_bytes(b"fictional")
            self.assertFalse((outside / "new.pdf").exists())

    def test_invalid_material_identifier_is_a_controlled_not_found(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = MaterialRepository(None, Path(folder))
            for identifier in ("../outside", "not-a-uuid", "C:\\outside", None):
                with self.subTest(identifier=identifier):
                    with self.assertRaises(AppError) as caught:
                        repository._path(identifier)
                    self.assertEqual(caught.exception.status, 404)

    def test_private_path_rejects_absolute_parent_and_windows_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            # 新的统一约束必须存在，且实际拒绝越界，而非只检查源码包含某一行。
            from zhengwu_precheck.utils import storage_paths

            root = Path(folder)
            for key in ("../outside", "/outside", "C:\\outside", "C:outside", "..\\outside", "."):
                with self.subTest(key=key):
                    with self.assertRaises(AppError):
                        storage_paths.private_path(root, key)
            self.assertEqual(
                storage_paths.private_path(root, "knowledge/uploads/new.pdf"),
                root.resolve() / "knowledge/uploads/new.pdf",
            )

    def test_material_symlink_cannot_read_outside_private_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "private"
            root.mkdir()
            outside = Path(folder) / "outside.txt"
            outside.write_text("fictional-outside", encoding="utf-8")
            identifier = str(uuid4())
            try:
                (root / (identifier + ".bin")).symlink_to(outside)
            except OSError as error:
                self.skipTest(
                    f"本机未授予符号链接创建权限：{error.winerror if hasattr(error, 'winerror') else error.errno}"
                )
            with self.assertRaises(AppError):
                MaterialRepository(None, root)._path(identifier)
            self.assertEqual(outside.read_text(encoding="utf-8"), "fictional-outside")


class ReleaseCredentialTests(unittest.TestCase):
    def test_release_preflight_applies_public_password_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime"
            runtime.mkdir()
            cert, key = root / "cert.pem", root / "key.pem"
            cert.write_text("fictional-cert", encoding="utf-8")
            key.write_text("fictional-key", encoding="utf-8")
            environment = {
                "ZHENGWU_RUNTIME_DIR": str(runtime),
                "ZHENGWU_PUBLIC_URL": "https://example.test",
                "ZHENGWU_ALLOWED_HOSTS": "example.test",
                "ZHENGWU_STAFF_JWT_SECRET": "test-secret-" * 5,
                "ZHENGWU_TLS_CERT": str(cert),
                "ZHENGWU_TLS_KEY": str(key),
                "ZHENGWU_CONFIG_VERSION": "test",
                "ZHENGWU_SECURE_COOKIES": "true",
                "ZHENGWU_PRIVATE_STORAGE_CONFIRMED": "true",
            }
            with closing(sqlite3.connect(runtime / "chat.sqlite3")) as database:
                database.execute("CREATE TABLE users(username TEXT,password_hash TEXT,status TEXT)")
                database.execute(
                    "INSERT INTO users VALUES(?,?,?)",
                    ("demo_owner", hash_password("123456"), "active"),
                )
                database.commit()
            # 仅调整系统临时根的定位，使本测试能在自有隔离目录验证正式预检。
            with patch.object(
                operations.tempfile, "gettempdir", return_value=str(root / "os-temp")
            ):
                with self.assertRaisesRegex(ValueError, "公开口令"):
                    operations.release_preflight(environment, repository=root / "repository")

    def test_public_demo_password_blocks_release_without_changing_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            with closing(sqlite3.connect(runtime / "chat.sqlite3")) as database:
                database.execute("CREATE TABLE users(username TEXT,password_hash TEXT,status TEXT)")
                encoded = hash_password("123456")
                database.execute(
                    "INSERT INTO users VALUES(?,?,?)", ("demo_engineer", encoded, "active")
                )
                database.commit()
                with self.assertRaises(ValueError):
                    operations.check_demo_credentials(runtime)
                self.assertEqual(
                    database.execute("SELECT password_hash FROM users").fetchone()[0], encoded
                )
                database.execute("UPDATE users SET status='disabled'")
                database.commit()
                operations.check_demo_credentials(runtime)
                database.execute(
                    "UPDATE users SET status='active',password_hash=?",
                    (hash_password("fictional-private-password-2026"),),
                )
                database.commit()
                operations.check_demo_credentials(runtime)


class StaffAPIBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(
            Settings(runtime_dir=Path(self.tmp.name), staff_jwt_secret="test-boundary-" * 5)
        )
        await self.app.state.system_log_repo.initialize()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.app.state.repo.close()
        await self.app.state.qa.close()
        await self.app.state.workflow_demo.close()
        self.tmp.cleanup()

    async def test_new_staff_api_without_local_auth_still_requires_login(self):
        @self.app.get("/staff/api/security-boundary-probe")
        async def probe():
            return {"private": "fictional-private-record"}

        response = await self.client.get("/staff/api/security-boundary-probe")
        self.assertEqual(response.status_code, 401, response.text)
        self.assertNotIn("fictional-private-record", response.text)

    async def test_proxy_prefix_cannot_bypass_default_staff_auth(self):
        @self.app.get("/staff/api/prefixed-security-probe")
        async def probe():
            return {"private": "fictional-private-record"}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, root_path="/portal"),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/portal/staff/api/prefixed-security-probe")
            self.assertEqual(response.status_code, 401, response.text)

    async def test_all_existing_staff_business_apis_reject_anonymous_requests(self):
        public = {"/staff/api/auth/login", "/staff/api/auth/refresh", "/staff/api/auth/logout"}
        for route in self.app.routes:
            if (
                not isinstance(route, APIRoute)
                or not route.path.startswith("/staff/api/")
                or route.path in public
            ):
                continue
            path = re.sub(r"\{[^}]+\}", str(uuid4()), route.path)
            for method in route.methods:
                with self.subTest(method=method, path=route.path):
                    response = await self.client.request(
                        method, path, json={} if method != "GET" else None
                    )
                    self.assertEqual(response.status_code, 401, response.text)

    async def test_public_login_pages_do_not_disclose_demo_password(self):
        for path in ("/", "/staff/login"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("123456", response.text)

    async def test_example_hint_does_not_disclose_demo_password(self):
        page = self.app.state.templates.env.get_template("example_workflow_hint.html").render(
            workflow_example={"id": "fictional"},
            example_hint={
                "action": "accept",
                "label": "待接手",
                "account": "demo_window",
                "text": "fictional",
            },
        )
        self.assertNotIn("123456", page)

    async def test_normal_startup_does_not_create_default_admin(self):
        async with self.app.router.lifespan_context(self.app):
            async with self.app.state.repo.engine.connect() as connection:
                self.assertEqual(
                    (await connection.execute(text("SELECT COUNT(*) FROM users"))).scalar_one(), 0
                )
            with self.assertRaises(AppError) as caught:
                await self.app.state.staff_auth.login("admin", "123456")
            self.assertEqual(caught.exception.status, 401)


class FixedSortTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_sort_text_never_changes_sql_or_reads_other_users(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = ChatRepository(Path(folder) / "chat.sqlite3")
            try:
                await repository.initialize()
                tickets = TicketRepository(repository.engine)
                await tickets.initialize()
                drafts = AgentRepository(repository.engine)
                await drafts.initialize()
                async with repository.engine.begin() as connection:
                    await connection.execute(
                        text(
                            "INSERT INTO users VALUES('fictional','fictional','TEST-NOT-A-LOGIN-HASH','active'),"
                            "('other','other','TEST-NOT-A-LOGIN-HASH','active')"
                        )
                    )
                for user, title in (("fictional", "本人的虚构工单"), ("other", "其他人的虚构工单")):
                    draft = await drafts.create_draft(
                        user,
                        {
                            "title": title,
                            "content": "fictional content",
                            "matter_id": "residence-permit",
                            "scenario_id": "rental-demo",
                            "shared_material_ids": [],
                        },
                    )
                    confirmation = await drafts.confirm_draft(user, draft["id"], draft["version"])
                    await tickets.create_confirmed(
                        user,
                        draft["id"],
                        draft["version"],
                        confirmation["id"],
                        "test-create-" + user,
                    )
                for sort in (
                    "created_at DESC; DROP TABLE users;--",
                    "(SELECT password_hash FROM users)",
                ):
                    own = await tickets.citizen_list("fictional", {"sort": sort})
                    self.assertEqual([item["title"] for item in own["items"]], ["本人的虚构工单"])
                    result = await tickets.citizen_list(
                        "fictional", {"sort": sort, "status": "' OR 1=1 --"}
                    )
                    self.assertEqual(result["items"], [])
                    async with repository.engine.connect() as connection:
                        self.assertEqual(
                            (
                                await connection.execute(text("SELECT COUNT(*) FROM users"))
                            ).scalar_one(),
                            2,
                        )
            finally:
                await repository.close()
