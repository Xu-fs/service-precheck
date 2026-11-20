"""按事项追加系统提示词版本；版本正文只留在专用版本表中。"""
import time
from uuid import uuid4

from sqlalchemy import text

from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import MATTER_NAMES, allows

DEFAULT_SYSTEM_PROMPT = "你是政务办事材料辅助助手，只根据提供且适用的办事指南、材料清单和预检规则回答。不确定时明确说‘现有办事指南中未找到相关依据’，回答附引用来源和版本，不得编造材料要求。你只能提供准备性提示，不判断证件真伪、法定资格或最终审批结果；依据不足、规则冲突或超出权限时转人工处理。"
MATTERS = {key: value for key, value in MATTER_NAMES.items() if key != "*"}


class PromptRepository:
    def __init__(self, engine):
        self.engine = engine

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("""CREATE TABLE IF NOT EXISTS prompt_versions(
                id TEXT PRIMARY KEY,matter_id TEXT NOT NULL,version INTEGER NOT NULL,
                content TEXT NOT NULL,author_id TEXT,created_at REAL NOT NULL,
                comment TEXT NOT NULL,restored_from INTEGER,UNIQUE(matter_id,version))"""))
            await conn.execute(text("""CREATE TABLE IF NOT EXISTS prompt_heads(
                matter_id TEXT PRIMARY KEY,version INTEGER NOT NULL)"""))
            for matter in MATTERS:
                await conn.execute(text("""INSERT OR IGNORE INTO prompt_versions
                    VALUES(:id,:matter,1,:content,NULL,:now,'初始示例模板',NULL)"""),
                    {"id": str(uuid4()), "matter": matter, "content": DEFAULT_SYSTEM_PROMPT, "now": time.time()})
                await conn.execute(text("INSERT OR IGNORE INTO prompt_heads VALUES(:matter,1)"), {"matter": matter})

    @staticmethod
    def _matter(matter_id):
        if matter_id not in MATTERS:
            raise AppError(404, "MATTER_NOT_FOUND", "事项不存在")

    async def _audit(self, conn, identity, action, entity, outcome="success"):
        await conn.execute(text("""INSERT INTO audit_logs
            VALUES(:id,:actor,:action,'prompt',:entity,:outcome,NULL,:now)"""),
            {"id": str(uuid4()), "actor": identity["user_id"], "action": action,
             "entity": entity, "outcome": outcome, "now": time.time()})

    async def _authorize(self, identity, matter_id):
        self._matter(matter_id)
        if not allows(identity["grants"], "prompt.manage", matter_id):
            async with self.engine.begin() as conn:
                await self._audit(conn, identity, "prompt_permission_denied", matter_id, "denied")
            raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位或事项授权不允许此操作")

    async def active(self, matter_id):
        self._matter(matter_id)
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("""SELECT v.* FROM prompt_versions v JOIN prompt_heads h
                ON h.matter_id=v.matter_id AND h.version=v.version WHERE v.matter_id=:matter"""),
                {"matter": matter_id})).mappings().first()
        if not row:
            raise AppError(503, "PROMPT_UNAVAILABLE", "事项提示词暂不可用，请转人工处理")
        return dict(row)

    @staticmethod
    def _validate(content, expected_version, comment):
        if not isinstance(content, str) or not content.strip() or len(content) > 2000:
            raise AppError(422, "INVALID_PROMPT", "提示词须为1至2000个字符")
        if not isinstance(comment, str) or not comment.strip() or len(comment) > 500:
            raise AppError(422, "INVALID_COMMENT", "请填写1至500个字符的修改说明")
        if type(expected_version) is not int or expected_version < 1:
            raise AppError(422, "INVALID_VERSION", "请提供有效的当前版本号")

    async def save(self, identity, matter_id, content, expected_version, comment):
        await self._authorize(identity, matter_id)
        self._validate(content, expected_version, comment)
        return await self._append(identity, matter_id, content, expected_version, comment)

    async def restore(self, identity, matter_id, version, expected_version, comment):
        await self._authorize(identity, matter_id)
        if type(version) is not int or version < 1:
            raise AppError(422, "INVALID_VERSION", "请选择有效的历史版本")
        async with self.engine.connect() as conn:
            content = (await conn.execute(text("SELECT content FROM prompt_versions WHERE matter_id=:matter AND version=:version"),
                {"matter": matter_id, "version": version})).scalar()
        if content is None:
            raise AppError(404, "PROMPT_VERSION_NOT_FOUND", "历史版本不存在")
        self._validate(content, expected_version, comment)
        return await self._append(identity, matter_id, content, expected_version, comment, version)

    async def _append(self, identity, matter_id, content, expected_version, comment, restored_from=None):
        values = {"id": str(uuid4()), "matter": matter_id, "version": expected_version + 1,
                  "expected": expected_version, "content": content, "author": identity["user_id"],
                  "now": time.time(), "comment": comment.strip(), "restored": restored_from}
        async with self.engine.begin() as conn:
            # 先用条件更新争用写锁；追加版本与生效指针切换同一事务提交或回滚。
            updated = await conn.execute(text("UPDATE prompt_heads SET version=:version WHERE matter_id=:matter AND version=:expected"), values)
            if updated.rowcount != 1:
                raise AppError(409, "PROMPT_VERSION_CONFLICT", "提示词已被其他人修改，请刷新后重试")
            await conn.execute(text("""INSERT INTO prompt_versions VALUES
                (:id,:matter,:version,:content,:author,:now,:comment,:restored)"""), values)
            await self._audit(conn, identity, "prompt_restored" if restored_from else "prompt_saved", values["id"])
            row = (await conn.execute(text("SELECT * FROM prompt_versions WHERE id=:id"), values)).mappings().one()
        return dict(row)

    async def history(self, identity, matter_id):
        await self._authorize(identity, matter_id)
        async with self.engine.begin() as conn:
            rows = (await conn.execute(text("SELECT * FROM prompt_versions WHERE matter_id=:matter ORDER BY version DESC"),
                                       {"matter": matter_id})).mappings().all()
            await self._audit(conn, identity, "prompt_history_read", matter_id)
        return [dict(row) for row in rows]
