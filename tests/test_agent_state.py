import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.agent_repository import AgentRepository


class AgentStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = ChatRepository(Path(self.tmp.name) / 'db.sqlite3')
        await self.chat.initialize()
        async with self.chat.engine.begin() as conn:
            await conn.execute(text("INSERT INTO users VALUES ('alice','alice','test','active'),('bob','bob','test','active')"))
        self.now = 1000
        self.repo = AgentRepository(self.chat.engine, clock=lambda: self.now)
        await self.repo.initialize()
        await self.repo.initialize()

    async def asyncTearDown(self):
        await self.chat.close()
        self.tmp.cleanup()

    async def test_draft_confirmation_bound_to_user_and_version(self):
        draft = await self.repo.create_draft('alice', {'title': '咨询', 'content': '测试'})
        with self.assertRaises(AppError):
            await self.repo.confirm_draft('bob', draft['id'], draft['version'])
        confirmation = await self.repo.confirm_draft('alice', draft['id'], draft['version'])
        self.assertTrue(confirmation['id'])
        updated = await self.repo.update_draft('alice', draft['id'], draft['version'],
                                               {'title': '咨询', 'content': '修改后'})
        self.assertEqual(updated['version'], 2)
        with self.assertRaises(AppError):
            await self.repo.confirm_draft('alice', draft['id'], 1)

    async def test_empty_draft_cannot_be_confirmed(self):
        with self.assertRaises(AppError):
            await self.repo.create_draft('alice', {'title': '', 'content': '测试'})
