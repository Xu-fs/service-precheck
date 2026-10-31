"""SQLite异步仓储：业务层只调用方法，SQL和事务封装在本模块。

首期没有既有业务数据库，因此版本1创建已确认表的问答子集；不创建材料或工单假实现。
未来数据库迁移可替换此仓储，并通过版本化迁移保留表关系与快照语义。
"""
import asyncio
import json
from pathlib import Path
import secrets
import time
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.utils.context import trim_history
from zhengwu_precheck.utils.security import digest, hash_password, verify_password

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS schema_versions (version INTEGER PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active')",
    "CREATE TABLE IF NOT EXISTS auth_sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), token_hash TEXT UNIQUE NOT NULL, expires_at REAL NOT NULL, revoked_at REAL)",
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), anonymous_token_hash TEXT,
        matter_id TEXT, scenario_id TEXT, confirmed_at REAL, confirmation_hash TEXT,
        confirmation_expires REAL, active_request_id TEXT, lease_until REAL, created_at REAL NOT NULL,
        CHECK ((user_id IS NULL) != (anonymous_token_hash IS NULL)))""",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
        sender_type TEXT NOT NULL CHECK(sender_type IN ('user','assistant')), content TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('pending','complete','failed','cancelled')),
        request_id TEXT NOT NULL, reply_to_message_id TEXT REFERENCES messages(id),
        outcome TEXT, error_code TEXT, matter_id TEXT, principal_key TEXT, idempotency_key TEXT,
        question_hash TEXT, result_json TEXT, created_at REAL NOT NULL,
        UNIQUE(principal_key, idempotency_key))""",
    """CREATE TABLE IF NOT EXISTS message_citations (
        id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id), chunk_id TEXT NOT NULL,
        filename_snapshot TEXT NOT NULL, matter_snapshot TEXT NOT NULL, rule_version_snapshot TEXT NOT NULL,
        quote TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS audit_logs (
        id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL, entity_type TEXT,
        entity_id TEXT, outcome TEXT, request_id TEXT, occurred_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id, created_at)",
]


class ChatRepository:
    def __init__(self, path: Path, lease_seconds: int = 180):
        self.lease_seconds = lease_seconds
        self.engine = create_async_engine("sqlite+aiosqlite:///" + path.as_posix(), connect_args={"timeout": 5})
        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def initialize(self):
        async with self.engine.begin() as conn:
            for statement in SCHEMA:
                await conn.execute(text(statement))
            versions = (await conn.execute(text("SELECT version FROM schema_versions"))).scalars().all()
            if versions and versions != [1]:
                raise RuntimeError("数据库版本不兼容，须先执行显式迁移")
            await conn.execute(text("INSERT OR IGNORE INTO schema_versions VALUES (1)"))
            await self._recover(conn)

    async def close(self):
        await self.engine.dispose()

    async def _recover(self, conn):
        now = time.time()
        await conn.execute(text("""UPDATE messages SET status='failed',error_code='REQUEST_INTERRUPTED'
            WHERE status='pending' AND conversation_id IN
            (SELECT id FROM conversations WHERE lease_until <= :now)"""), {"now": now})
        await conn.execute(text("UPDATE conversations SET active_request_id=NULL,lease_until=NULL WHERE lease_until<=:now"), {"now": now})

    async def audit(self, action, actor, entity=None, outcome="denied", request_id=None):
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO audit_logs VALUES (:id,:actor,:action,'conversation',:entity,:outcome,:request,:now)"),
                               {"id": str(uuid4()), "actor": actor, "action": action, "entity": entity, "outcome": outcome, "request": request_id, "now": time.time()})

    async def authenticate(self, token: str):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("""SELECT u.id,u.username FROM auth_sessions s JOIN users u ON s.user_id=u.id
                WHERE s.token_hash=:hash AND s.expires_at>:now AND s.revoked_at IS NULL AND u.status='active'"""),
                {"hash": digest(token), "now": time.time()})).mappings().first()
        return dict(row) if row else None

    async def create_user(self, username: str, password: str):
        encoded = await asyncio.to_thread(hash_password, password)
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES (:id,:name,:password)"),
                               {"id": str(uuid4()), "name": username, "password": encoded})

    async def login(self, username: str, password: str):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT * FROM users WHERE username=:name AND status='active'"), {"name": username})).mappings().first()
        # 不存在的用户也执行同成本哈希，避免暴露账号存在性。
        encoded = row["password_hash"] if row else "scrypt$" + "00" * 16 + "$" + "00" * 64
        valid = await asyncio.to_thread(verify_password, password, encoded)
        if not row or not valid:
            raise AppError(401, "INVALID_CREDENTIALS", "账号或密码不正确")
        token = secrets.token_urlsafe(32)
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO auth_sessions(id,user_id,token_hash,expires_at) VALUES (:id,:user,:hash,:expiry)"),
                               {"id": str(uuid4()), "user": row["id"], "hash": digest(token), "expiry": time.time() + 8 * 3600})
        return token, row["username"]

    async def logout(self, token):
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE auth_sessions SET revoked_at=:now WHERE token_hash=:hash"), {"now": time.time(), "hash": digest(token)})

    async def conversation(self, sid: str, principal: dict):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT * FROM conversations WHERE id=:id"), {"id": sid})).mappings().first()
        allowed = row and ((principal["user_id"] and row["user_id"] == principal["user_id"])
                           or (not principal["user_id"] and row["user_id"] is None and row["anonymous_token_hash"] == principal["anonymous_hash"]))
        if not allowed:
            await self.audit("conversation_access", principal["key"], sid)
            raise AppError(404, "SESSION_NOT_FOUND", "会话不存在或无权访问")
        return dict(row)

    async def existing(self, principal: dict, key: str):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT * FROM messages WHERE principal_key=:owner AND idempotency_key=:key"), {"owner": principal["key"], "key": key})).mappings().first()
        return dict(row) if row else None

    async def reserve(self, sid: str | None, question: str, key: str, principal: dict, *, expected_conversation=None, context_factory=None):
        if sid:
            await self.conversation(sid, principal)
        sid, request_id, user_id, answer_id = sid or str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        now = time.time()
        snapshot = None
        async with self.engine.begin() as conn:
            # 写锁先于读检查，确保并发进程不能同时接纳同一个幂等键。
            await conn.execute(text("UPDATE schema_versions SET version=version WHERE version=1"))
            await self._recover(conn)
            if expected_conversation is not None:
                current = (await conn.execute(text("SELECT * FROM conversations WHERE id=:id"), {"id": sid})).mappings().first()
                fields = ("matter_id", "scenario_id", "confirmed_at", "confirmation_hash")
                if not current or any(current[field] != expected_conversation[field] for field in fields):
                    raise AppError(409, "SESSION_CHANGED", "事项已在其他页面变更，请刷新会话后重新提问", True)
            duplicate = (await conn.execute(text("SELECT id FROM messages WHERE principal_key=:owner AND idempotency_key=:key"), {"owner": principal["key"], "key": key})).first()
            if duplicate:
                raise AppError(409, "REQUEST_IN_PROGRESS", "该请求已被接纳，请查询消息状态", True)
            await conn.execute(text("""INSERT OR IGNORE INTO conversations(id,user_id,anonymous_token_hash,created_at)
                VALUES (:id,:user,:anon,:now)"""), {"id": sid, "user": principal["user_id"], "anon": None if principal["user_id"] else principal["anonymous_hash"], "now": now})
            changed = await conn.execute(text("""UPDATE conversations SET active_request_id=:request,lease_until=:expiry
                WHERE id=:id AND active_request_id IS NULL"""), {"request": request_id, "expiry": now + self.lease_seconds, "id": sid})
            if changed.rowcount != 1:
                raise AppError(409, "SESSION_BUSY", "当前会话已有回答正在处理，请稍后重试", True)
            current_conversation = (await conn.execute(text("SELECT matter_id,scenario_id FROM conversations WHERE id=:id"), {"id": sid})).mappings().one()
            matter = current_conversation["matter_id"]
            for identifier, role, content, reply, owner, ikey in [
                (user_id, "user", question, None, None, None),
                (answer_id, "assistant", "", user_id, principal["key"], key),
            ]:
                await conn.execute(text("""INSERT INTO messages(id,conversation_id,sender_type,content,status,request_id,
                    reply_to_message_id,matter_id,principal_key,idempotency_key,question_hash,created_at)
                    VALUES (:id,:sid,:role,:content,:status,:request,:reply,:matter,:owner,:key,:hash,:now)"""),
                    {"id": identifier, "sid": sid, "role": role, "content": content, "status": "pending" if role == "assistant" else "complete",
                     "request": request_id, "reply": reply, "matter": matter, "owner": owner, "key": ikey, "hash": digest(question), "now": now + (0.000001 if role == "assistant" else 0)})
            if context_factory and matter:
                snapshot = await context_factory(conn, matter, current_conversation["scenario_id"])
                await conn.execute(text("INSERT INTO qa_request_snapshots VALUES(:request,:message,:matter,:scenario,:prompt,:content,:knowledge,:now)"),
                    {"request": request_id, "message": answer_id, "matter": matter, "scenario": current_conversation["scenario_id"],
                     "prompt": snapshot["prompt"]["id"], "content": snapshot["prompt"]["content"], "knowledge": json.dumps(snapshot, ensure_ascii=False), "now": now})
        result = {"session_id": sid, "request_id": request_id, "message_id": answer_id, "user_message_id": user_id}
        if snapshot is not None:
            result["_snapshot"] = snapshot
        return result

    async def history(self, sid: str, matter: str):
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("""SELECT u.content AS question,a.content AS answer FROM messages a
                JOIN messages u ON a.reply_to_message_id=u.id WHERE a.conversation_id=:id AND a.status='complete'
                AND a.outcome IN ('answered','insufficient_evidence') AND a.matter_id=:matter
                ORDER BY a.created_at DESC LIMIT 6"""), {"id": sid, "matter": matter})).mappings().all()
        return trim_history([item for row in reversed(rows) for item in [
            {"role": "user", "content": row["question"]}, {"role": "assistant", "content": row["answer"]}]])

    async def set_confirmation(self, turn):
        token = secrets.token_urlsafe(32)
        async with self.engine.begin() as conn:
            await conn.execute(text("""UPDATE conversations SET matter_id=NULL,scenario_id=NULL,confirmed_at=NULL,
                confirmation_hash=:hash,confirmation_expires=:expiry WHERE id=:id AND active_request_id=:request"""),
                {"hash": digest(token), "expiry": time.time() + 600, "id": turn["session_id"], "request": turn["request_id"]})
        return token

    async def confirm(self, sid, principal, matter, scenario, token):
        await self.conversation(sid, principal)
        async with self.engine.begin() as conn:
            changed = await conn.execute(text("""UPDATE conversations SET matter_id=:matter,scenario_id=:scenario,confirmed_at=:now,
                confirmation_hash=NULL,confirmation_expires=NULL WHERE id=:id AND confirmation_hash=:hash
                AND confirmation_expires>:now AND active_request_id IS NULL"""),
                {"matter": matter, "scenario": scenario, "now": time.time(), "id": sid, "hash": digest(token)})
            if changed.rowcount != 1:
                raise AppError(409, "CONFIRMATION_EXPIRED", "事项确认已失效或会话正在处理，请重新提问")

    async def heartbeat(self, turn):
        async with self.engine.begin() as conn:
            changed = await conn.execute(text("UPDATE conversations SET lease_until=:expiry WHERE id=:id AND active_request_id=:request"),
                {"expiry": time.time() + self.lease_seconds, "id": turn["session_id"], "request": turn["request_id"]})
            return changed.rowcount == 1

    async def finish(self, turn, result):
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE schema_versions SET version=version WHERE version=1"))
            active = (await conn.execute(text("SELECT active_request_id FROM conversations WHERE id=:id"), {"id": turn["session_id"]})).scalar()
            if active != turn["request_id"]:
                raise AppError(409, "REQUEST_INTERRUPTED", "请求已中断，请重新提问", True)
            await conn.execute(text("""UPDATE messages SET content=:content,status='complete',outcome=:outcome,result_json=:result
                WHERE id=:id AND status='pending'"""), {"content": result.get("text", ""), "outcome": result["outcome"],
                "result": json.dumps(result, ensure_ascii=False), "id": turn["message_id"]})
            if result["outcome"] == "needs_matter_confirmation":
                # 切换事项的控制轮次尚未确定适用范围，不能继承会话上一事项的归属。
                await conn.execute(text("UPDATE messages SET matter_id=NULL WHERE conversation_id=:sid AND request_id=:request"),
                                   {"sid": turn["session_id"], "request": turn["request_id"]})
            for citation in result.get("citations", []):
                await conn.execute(text("INSERT INTO message_citations VALUES (:id,:message,:chunk,:file,:matter,:version,:quote)"),
                    {"id": str(uuid4()), "message": turn["message_id"], "chunk": citation["chunk_id"], "file": citation["filename"],
                     "matter": citation["matter"], "version": citation["rule_version"], "quote": citation["quote"]})
            await conn.execute(text("UPDATE conversations SET active_request_id=NULL,lease_until=NULL WHERE id=:id"), {"id": turn["session_id"]})
            await conn.execute(text("INSERT INTO audit_logs VALUES (:id,NULL,'chat_result','conversation',:entity,:outcome,:request,:now)"),
                {"id": str(uuid4()), "entity": turn["session_id"], "outcome": result["outcome"], "request": turn["request_id"], "now": time.time()})

    async def fail(self, turn, code, cancelled=False):
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE messages SET status=:status,error_code=:code WHERE id=:id AND status='pending'"),
                               {"status": "cancelled" if cancelled else "failed", "code": code, "id": turn["message_id"]})
            await conn.execute(text("UPDATE conversations SET active_request_id=NULL,lease_until=NULL WHERE id=:id AND active_request_id=:request"),
                               {"id": turn["session_id"], "request": turn["request_id"]})

    async def list_messages(self, sid, principal, before=None, limit=40):
        conversation = await self.conversation(sid, principal)
        async with self.engine.begin() as conn:
            await self._recover(conn)
            # 以时间和ID的组合游标分页，避免同一时间戳下漏项。
            rows = (await conn.execute(text("""SELECT * FROM messages WHERE conversation_id=:id
                AND (:before IS NULL OR created_at < (SELECT created_at FROM messages WHERE id=:before AND conversation_id=:id)
                OR (created_at=(SELECT created_at FROM messages WHERE id=:before AND conversation_id=:id) AND id<:before))
                ORDER BY created_at DESC,id DESC LIMIT :limit"""), {"id": sid, "before": before, "limit": limit})).mappings().all()
        items = []
        for row in reversed(rows):
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            items.append({"id": row["id"], "sender_type": row["sender_type"], "content": row["content"], "status": row["status"],
                          "outcome": row["outcome"], "error_code": row["error_code"], "request_id": row["request_id"],
                          "citations": result.get("citations", []), "result": result, "created_at": row["created_at"]})
        from zhengwu_precheck.core.services.chat import MATTERS
        matter_name = next((item["name"] for item in MATTERS if item["matter_id"] == conversation["matter_id"]), None)
        return {"items": items, "next_cursor": rows[-1]["id"] if len(rows) == limit else None,
                "matter_id": conversation["matter_id"], "matter_name": matter_name}
