"""示例账号初始化：真实数据库登录、重复执行和现有账号保护。"""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from zhengwu_precheck.settings import Settings
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.core.services.staff_auth import StaffAuth


class DemoAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_eight_accounts_login_with_requested_password_and_rerun_preserves_ids(self):
        self.assertIsNotNone(importlib.util.find_spec("create_demo_accounts"), "缺少示例账号初始化入口")
        from create_demo_accounts import provision
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(runtime_dir=Path(directory), staff_jwt_secret=None)
            first = await provision(settings)
            second = await provision(settings)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 8)
            repo = ChatRepository(Path(directory) / "chat.sqlite3")
            staff = StaffRepository(repo.engine)
            try:
                auth = StaffAuth(staff, Settings(runtime_dir=Path(directory)).staff_jwt_secret)
                for role in ("citizen", "window", "reviewer", "owner", "supervisor", "hotline", "engineer", "director"):
                    username = "demo_" + role
                    token, name = await repo.login(username, "123456")
                    self.assertEqual(name, username)
                    await repo.logout(token)
                    row = await staff.user(username)
                    grants = await staff.grants(row["id"])
                    if role == "citizen":
                        self.assertEqual(grants, [])
                    else:
                        values = await auth.login(username, "123456")
                        identity = await auth.identity(values["access"])
                        self.assertEqual({g["role_code"] for g in identity["grants"]}, {role})
                        self.assertEqual({g["matter_id"] for g in grants}, {"residence-permit", "sole-trader-registration"})
                        await auth.logout(values["access"], values["refresh"], values["csrf_cookie"])
            finally:
                await repo.close()

    async def test_existing_named_account_is_preserved_without_creating_other_accounts(self):
        self.assertIsNotNone(importlib.util.find_spec("create_demo_accounts"), "缺少示例账号初始化入口")
        from create_demo_accounts import provision
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(runtime_dir=Path(directory))
            repo = ChatRepository(Path(directory) / "chat.sqlite3")
            try:
                await repo.initialize()
                await repo.create_user("demo_owner", "existing-password-unchanged")
                with self.assertRaisesRegex(ValueError, "已有账号"):
                    await provision(settings)
                token, _ = await repo.login("demo_owner", "existing-password-unchanged")
                await repo.logout(token)
                staff = StaffRepository(repo.engine)
                self.assertIsNone(await staff.user("demo_citizen"))
                owner = await staff.user("demo_owner")
                self.assertEqual(await staff.grants(owner["id"]), [])
            finally:
                await repo.close()


class LocalStaffKeyTests(unittest.TestCase):
    def test_runtime_key_survives_restart_and_environment_has_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            local_key = "local-test-signing-key-" + "a" * 40
            (path / "staff-jwt.key").write_text(local_key, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings(runtime_dir=path).staff_jwt_secret, local_key)
                self.assertEqual(Settings(runtime_dir=path).staff_jwt_secret, local_key)
            with patch.dict(os.environ, {"ZHENGWU_STAFF_JWT_SECRET": "external-test-key-" + "b" * 40}):
                self.assertEqual(Settings(runtime_dir=path).staff_jwt_secret, "external-test-key-" + "b" * 40)


if __name__ == "__main__":
    unittest.main()
