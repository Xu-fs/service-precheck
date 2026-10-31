"""工作人员认证独立于办事人登录；JWT仅证明登录，实时授权来自数据库。"""
import asyncio
import hmac
import secrets
import time
from uuid import uuid4
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.utils.security import digest, verify_password
from zhengwu_precheck.utils.staff_jwt import StaffJWT


class StaffAuth:
    def __init__(self, repository, secret):
        self.repo, self.jwt = repository, StaffJWT(secret)

    def valid_session(self, row):
        return (row and row["revoked_at"] is None and row["absolute_expires"] > time.time()
                and row["user_status"] == "active" and row["staff_status"] == "active"
                and row["current_version"] == row["auth_version"])

    async def login(self, username, password):
        self.jwt.ensure_ready()
        row = await self.repo.user(username)
        encoded = row["password_hash"] if row else "scrypt$" + "00" * 16 + "$" + "00" * 64
        password_ok = await asyncio.to_thread(verify_password, password, encoded)
        grants = await self.repo.grants(row["id"]) if row else []
        if not row or not password_ok or row["status"] != "active" or row["staff_status"] != "active" or not grants:
            await self.repo.audit_denial(None, "staff_login_failed")
            raise AppError(401, "INVALID_CREDENTIALS", "账号或密码不正确，或未获工作人员授权")
        now = time.time()
        refresh, csrf_cookie, jti = secrets.token_urlsafe(48), secrets.token_urlsafe(32), str(uuid4())
        session_id = str(uuid4())
        await self.repo.insert_session({"id": session_id, "user_id": row["id"], "refresh_hash": digest(refresh),
            "csrf_hash": digest(csrf_cookie), "access_jti": jti, "auth_version": row["auth_version"], "absolute_expires": now + 8 * 3600, "created_at": now})
        token = self.jwt.issue(row["id"], session_id, row["auth_version"], 0, now + 8 * 3600, jti)
        return {"access": token, "refresh": refresh, "csrf_cookie": csrf_cookie, "expires_in": 900}

    async def identity(self, token, *, allow_expired=False):
        if not token:
            raise AppError(401, "STAFF_AUTH_REQUIRED", "请先登录工作人员后台")
        claims = self.jwt.decode(token, allow_expired=allow_expired)
        row = await self.repo.session(session_id=claims["sid"])
        if (not self.valid_session(row) or row["user_id"] != claims["sub"] or row["access_jti"] != claims["jti"]
                or row["auth_version"] != claims["ver"] or row["rotation"] != claims["rot"] or claims["exp"] > row["absolute_expires"]):
            raise AppError(401, "STAFF_AUTH_EXPIRED", "工作人员登录已失效，请重新登录")
        grants = await self.repo.grants(row["user_id"])
        if not grants:
            raise AppError(401, "STAFF_AUTH_EXPIRED", "工作人员授权已失效，请重新登录")
        return {"user_id": row["user_id"], "username": row["username"], "grants": grants, "session": row}

    async def refresh(self, token, csrf_cookie):
        self.jwt.ensure_ready()
        row = await self.repo.session(refresh_hash=digest(token)) if token else None
        if (not self.valid_session(row) or not csrf_cookie or not hmac.compare_digest(row["csrf_hash"], digest(csrf_cookie))
                or not await self.repo.grants(row["user_id"])):
            raise AppError(401, "STAFF_AUTH_EXPIRED", "工作人员登录已失效，请重新登录")
        refresh, jti = secrets.token_urlsafe(48), str(uuid4())
        await self.repo.rotate(row, digest(refresh), jti)
        access = self.jwt.issue(row["user_id"], row["id"], row["auth_version"], row["rotation"] + 1, row["absolute_expires"], jti)
        return {"access": access, "refresh": refresh, "csrf_cookie": csrf_cookie,
                "expires_in": min(900, max(0, int(row["absolute_expires"] - time.time())))}

    async def logout(self, access, refresh, csrf_cookie):
        row = None
        if access:
            try:
                # 退出只撤销同一会话族，允许刷新前的签名JWT；不借此授予读取或写入权限。
                claims = self.jwt.decode(access, allow_expired=True)
                candidate = await self.repo.session(session_id=claims["sid"])
                if candidate and candidate["user_id"] == claims["sub"]:
                    row = candidate
            except AppError:
                pass
        if not row and refresh:
            row = await self.repo.session(refresh_hash=digest(refresh))
        if not row and refresh and csrf_cookie:
            # JWT Cookie已过期而刷新/退出并发时，绑定的随机HttpOnly CSRF凭证仍可撤销本会话。
            # 此入口仅能注销，不能刷新令牌或读取记录。
            row = await self.repo.session(csrf_hash=digest(csrf_cookie))
        if row and csrf_cookie and hmac.compare_digest(row["csrf_hash"], digest(csrf_cookie)):
            await self.repo.revoke_session(row["id"])
