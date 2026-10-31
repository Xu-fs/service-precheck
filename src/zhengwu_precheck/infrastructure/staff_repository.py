"""工作人员附加表及仓储。独立版本号，不覆盖已有用户、对话和登录表。"""
import json
import time
from uuid import uuid4
from sqlalchemy import text, bindparam
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import ROLES, ROLE_PERMISSIONS, MATTER_NAMES, allows

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS staff_schema_versions(version INTEGER PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS roles(code TEXT PRIMARY KEY,name TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS permissions(code TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS role_permissions(role_code TEXT REFERENCES roles(code),permission_code TEXT REFERENCES permissions(code),PRIMARY KEY(role_code,permission_code))",
    "CREATE TABLE IF NOT EXISTS staff_accounts(user_id TEXT PRIMARY KEY REFERENCES users(id),auth_version INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'active')",
    """CREATE TABLE IF NOT EXISTS user_role_scopes(user_id TEXT REFERENCES staff_accounts(user_id),role_code TEXT REFERENCES roles(code),
       matter_id TEXT NOT NULL,valid_until REAL,PRIMARY KEY(user_id,role_code,matter_id))""",
    """CREATE TABLE IF NOT EXISTS staff_sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES staff_accounts(user_id),
       refresh_hash TEXT UNIQUE NOT NULL,csrf_hash TEXT NOT NULL,access_jti TEXT NOT NULL,auth_version INTEGER NOT NULL,
       rotation INTEGER NOT NULL DEFAULT 0,absolute_expires REAL NOT NULL,revoked_at REAL,created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS staff_record_grants(user_id TEXT REFERENCES staff_accounts(user_id),resource_type TEXT NOT NULL,
       resource_id TEXT NOT NULL,access_level TEXT NOT NULL CHECK(access_level IN ('summary','body')),
       granted_by TEXT NOT NULL,expires_at REAL,PRIMARY KEY(user_id,resource_type,resource_id))""",
]


class StaffRepository:
    def __init__(self, engine):
        self.engine = engine

    async def initialize(self):
        async with self.engine.begin() as conn:
            for statement in SCHEMA:
                await conn.execute(text(statement))
            versions = (await conn.execute(text("SELECT version FROM staff_schema_versions"))).scalars().all()
            if versions and versions != [1]:
                raise RuntimeError("工作人员表版本不兼容，请先迁移")
            await conn.execute(text("INSERT OR IGNORE INTO staff_schema_versions VALUES(1)"))
            for code, name in ROLES.items():
                await conn.execute(text("INSERT OR IGNORE INTO roles VALUES(:code,:name)"), {"code": code, "name": name})
                for permission in ROLE_PERMISSIONS[code]:
                    await conn.execute(text("INSERT OR IGNORE INTO permissions VALUES(:code)"), {"code": permission})
                    await conn.execute(text("INSERT OR IGNORE INTO role_permissions VALUES(:role,:permission)"), {"role": code, "permission": permission})

    async def _audit(self, conn, actor, action, entity, outcome="success"):
        await conn.execute(text("INSERT INTO audit_logs VALUES(:id,:actor,:action,'staff_authorization',:entity,:outcome,NULL,:now)"),
                           {"id": str(uuid4()), "actor": actor, "action": action, "entity": entity, "outcome": outcome, "now": time.time()})

    async def audit_denial(self, user_id, action, entity=None):
        async with self.engine.begin() as conn:
            await self._audit(conn, user_id, action, entity, "denied")

    async def user(self, username):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT u.*,a.auth_version,a.status AS staff_status FROM users u LEFT JOIN staff_accounts a ON a.user_id=u.id WHERE u.username=:name"), {"name": username})).mappings().first()
        return dict(row) if row else None

    async def grants(self, user_id):
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("""SELECT g.role_code,g.matter_id,p.permission_code FROM user_role_scopes g
                JOIN role_permissions p ON p.role_code=g.role_code WHERE g.user_id=:id
                AND (g.valid_until IS NULL OR g.valid_until>:now) ORDER BY g.role_code,g.matter_id,p.permission_code"""), {"id": user_id, "now": time.time()})).mappings().all()
        grouped = {}
        for row in rows:
            entry = grouped.setdefault((row["role_code"], row["matter_id"]), {"role_code": row["role_code"], "role_name": ROLES[row["role_code"]],
                "matter_id": row["matter_id"], "matter_name": MATTER_NAMES.get(row["matter_id"], "未知事项"), "permissions": []})
            entry["permissions"].append(row["permission_code"])
        return list(grouped.values())

    async def grant_role(self, username, role, matter, *, actor):
        if role not in ROLES or matter not in MATTER_NAMES:
            raise ValueError("岗位或事项无效，全部事项必须显式传入*")
        async with self.engine.begin() as conn:
            user_id = (await conn.execute(text("SELECT id FROM users WHERE username=:name"), {"name": username})).scalar()
            if not user_id:
                raise ValueError("账号不存在，请先创建独立账号")
            await conn.execute(text("INSERT OR IGNORE INTO staff_accounts(user_id) VALUES(:id)"), {"id": user_id})
            await conn.execute(text("INSERT OR IGNORE INTO user_role_scopes(user_id,role_code,matter_id) VALUES(:id,:role,:matter)"), {"id": user_id, "role": role, "matter": matter})
            await conn.execute(text("UPDATE staff_accounts SET auth_version=auth_version+1 WHERE user_id=:id"), {"id": user_id})
            await self._audit(conn, actor, "staff_role_granted", json.dumps({"user_id": user_id, "role": role, "matter": matter}))

    async def revoke_role(self, username, role, matter, *, actor):
        async with self.engine.begin() as conn:
            user_id = (await conn.execute(text("SELECT id FROM users WHERE username=:name"), {"name": username})).scalar()
            if not user_id:
                raise ValueError("账号不存在")
            await conn.execute(text("DELETE FROM user_role_scopes WHERE user_id=:id AND role_code=:role AND matter_id=:matter"), {"id": user_id, "role": role, "matter": matter})
            await conn.execute(text("UPDATE staff_accounts SET auth_version=auth_version+1 WHERE user_id=:id"), {"id": user_id})
            await self._audit(conn, actor, "staff_role_revoked", json.dumps({"user_id": user_id, "role": role, "matter": matter}))

    async def disable(self, username, *, actor):
        async with self.engine.begin() as conn:
            user_id = (await conn.execute(text("SELECT id FROM users WHERE username=:name"), {"name": username})).scalar()
            if not user_id:
                raise ValueError("账号不存在")
            await conn.execute(text("UPDATE staff_accounts SET status='disabled',auth_version=auth_version+1 WHERE user_id=:id"), {"id": user_id})
            await self._audit(conn, actor, "staff_disabled", user_id)

    async def grant_record(self, username, conversation_id, level, *, actor):
        if level not in {"summary", "body"}:
            raise ValueError("记录权限只能是summary或body")
        async with self.engine.begin() as conn:
            user_id = (await conn.execute(text("SELECT user_id FROM staff_accounts a JOIN users u ON u.id=a.user_id WHERE username=:name"), {"name": username})).scalar()
            record = (await conn.execute(text("SELECT id FROM conversations WHERE id=:id"), {"id": conversation_id})).scalar()
            if not user_id or not record:
                raise ValueError("工作人员或会话不存在")
            await conn.execute(text("""INSERT INTO staff_record_grants VALUES(:user,'conversation',:id,:level,:actor,NULL)
                ON CONFLICT(user_id,resource_type,resource_id) DO UPDATE SET access_level=excluded.access_level,granted_by=excluded.granted_by,expires_at=NULL"""),
                {"user": user_id, "id": conversation_id, "level": level, "actor": actor})
            await self._audit(conn, actor, "staff_record_granted", conversation_id)

    async def revoke_record(self, username, conversation_id, *, actor):
        async with self.engine.begin() as conn:
            await conn.execute(text("DELETE FROM staff_record_grants WHERE user_id=(SELECT id FROM users WHERE username=:name) AND resource_type='conversation' AND resource_id=:id"), {"name": username, "id": conversation_id})
            await self._audit(conn, actor, "staff_record_revoked", conversation_id)

    async def insert_session(self, values):
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE staff_accounts SET auth_version=auth_version WHERE user_id=:user_id"), values)
            valid = (await conn.execute(text("SELECT 1 FROM staff_accounts a JOIN users u ON u.id=a.user_id WHERE a.user_id=:user_id AND a.auth_version=:auth_version AND a.status='active' AND u.status='active'"), values)).first()
            if not valid:
                raise AppError(401, "STAFF_AUTH_EXPIRED", "授权已变更，请重新登录")
            await conn.execute(text("""INSERT INTO staff_sessions(id,user_id,refresh_hash,csrf_hash,access_jti,auth_version,absolute_expires,created_at)
                VALUES(:id,:user_id,:refresh_hash,:csrf_hash,:access_jti,:auth_version,:absolute_expires,:created_at)"""), values)
            await self._audit(conn, values["user_id"], "staff_login", values["id"])

    async def session(self, session_id=None, refresh_hash=None, csrf_hash=None):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("""SELECT s.*,u.username,u.status AS user_status,a.status AS staff_status,a.auth_version AS current_version
                FROM staff_sessions s JOIN staff_accounts a ON a.user_id=s.user_id JOIN users u ON u.id=s.user_id
                WHERE (:id IS NOT NULL AND s.id=:id) OR (:hash IS NOT NULL AND s.refresh_hash=:hash)
                OR (:csrf IS NOT NULL AND s.csrf_hash=:csrf)"""), {"id": session_id, "hash": refresh_hash, "csrf": csrf_hash})).mappings().first()
        return dict(row) if row else None

    async def rotate(self, old, new_hash, new_jti):
        async with self.engine.begin() as conn:
            result = await conn.execute(text("""UPDATE staff_sessions SET refresh_hash=:new,access_jti=:jti,rotation=rotation+1
                WHERE id=:id AND refresh_hash=:old AND rotation=:rotation AND revoked_at IS NULL AND absolute_expires>:now
                AND EXISTS(SELECT 1 FROM staff_accounts a JOIN users u ON u.id=a.user_id WHERE a.user_id=staff_sessions.user_id
                AND a.auth_version=staff_sessions.auth_version AND a.status='active' AND u.status='active')"""),
                {"new": new_hash, "jti": new_jti, "id": old["id"], "old": old["refresh_hash"], "rotation": old["rotation"], "now": time.time()})
            if result.rowcount != 1:
                raise AppError(401, "STAFF_AUTH_EXPIRED", "登录刷新已失效，请重新登录")
            await self._audit(conn, old["user_id"], "staff_refresh", old["id"])

    async def revoke_session(self, session_id):
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE staff_sessions SET revoked_at=:now WHERE id=:id"), {"now": time.time(), "id": session_id})
            await self._audit(conn, None, "staff_logout", session_id)

    async def record(self, identity, conversation_id, body=False):
        permission = "conversation.read" if body else "conversation.summary"
        if not allows(identity["grants"], permission):
            await self.audit_denial(identity["user_id"], "staff_record_permission_denied", conversation_id)
            raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位无权查看此类记录")
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("""SELECT c.id,c.matter_id,c.created_at,g.access_level FROM conversations c
                JOIN staff_record_grants g ON g.resource_type='conversation' AND g.resource_id=c.id
                WHERE c.id=:id AND g.user_id=:user AND (g.expires_at IS NULL OR g.expires_at>:now)"""),
                {"id": conversation_id, "user": identity["user_id"], "now": time.time()})).mappings().first()
        if (not row or not row["matter_id"] or not allows(identity["grants"], permission, row["matter_id"])
                or (body and row["access_level"] != "body")):
            await self.audit_denial(identity["user_id"], "staff_record_scope_denied", conversation_id)
            raise AppError(404, "RECORD_NOT_FOUND", "记录不存在或不在授权范围内")
        result = {"id": row["id"], "matter_id": row["matter_id"], "created_at": row["created_at"]}
        if body:
            scopes = [grant["matter_id"] for grant in identity["grants"] if allows([grant], "conversation.read")]
            # 按消息快照筛选，不能用会话当前事项覆盖切换前历史；只展示已完成且事项明确的轮次。
            query = text("""SELECT m.id,m.sender_type,m.content,m.status,m.created_at FROM messages m
                WHERE m.conversation_id=:id AND m.matter_id IS NOT NULL
                AND (:all_scopes=1 OR m.matter_id IN :scopes)
                AND EXISTS(SELECT 1 FROM messages a WHERE a.conversation_id=m.conversation_id
                    AND a.request_id=m.request_id AND a.sender_type='assistant' AND a.status='complete'
                    AND a.outcome IN ('answered','insufficient_evidence') AND a.matter_id=m.matter_id)
                ORDER BY m.created_at,m.id LIMIT 100""").bindparams(bindparam("scopes", expanding=True))
            async with self.engine.connect() as conn:
                rows = (await conn.execute(query, {"id": conversation_id, "all_scopes": int("*" in scopes), "scopes": scopes})).mappings().all()
                result["messages"] = [dict(item) for item in rows]
        async with self.engine.begin() as conn:
            await self._audit(conn, identity["user_id"], "staff_record_read", conversation_id)
        return result
