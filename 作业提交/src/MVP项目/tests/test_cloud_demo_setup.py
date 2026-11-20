"""云端教学初始化的归属保护与重复执行边界。"""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]


class CloudDemoSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_runtime_is_rejected_without_touching_data(self):
        from bootstrap_cloud_demo import bootstrap
        from zhengwu_precheck.settings import Settings
        with tempfile.TemporaryDirectory() as folder:
            runtime=Path(folder)
            original=runtime/'existing.txt'
            original.write_text('preserve',encoding='utf-8')
            with self.assertRaises(ValueError):
                await bootstrap(Settings(runtime_dir=runtime))
            self.assertEqual(original.read_text(), 'preserve')
            self.assertEqual(list(runtime.iterdir()), [original])

    async def test_real_cases_and_random_passwords_survive_repeat_setup(self):
        from bootstrap_cloud_demo import bootstrap
        from zhengwu_precheck.settings import Settings
        from zhengwu_precheck.infrastructure.database import ChatRepository
        from zhengwu_precheck.core.errors import AppError
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(runtime_dir=Path(folder))
            first=await bootstrap(settings)
            password=(Path(folder)/'demo-password.txt').read_text().strip()
            self.assertGreaterEqual(len(password),20)
            self.assertEqual(len(first['cases']),2)
            self.assertTrue(all(c['status']=='queued' for c in first['cases']))
            second=await bootstrap(Settings(runtime_dir=Path(folder)))
            self.assertEqual(first,second)
            self.assertEqual((Path(folder)/'demo-password.txt').read_text().strip(),password)
            repo=ChatRepository(Path(folder)/'chat.sqlite3')
            try:
                await repo.initialize()
                await repo.login('demo_citizen',password)
                with self.assertRaises(AppError):
                    await repo.login('demo_citizen','123456')
            finally:
                await repo.close()
