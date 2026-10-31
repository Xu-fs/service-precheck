"""所有权与CSRF校验统一入口；登录失效不回落匿名身份。"""
import hmac
import time
from urllib.parse import urlsplit

from fastapi import Request
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.utils.security import digest


async def principal(request: Request):
    if token := request.cookies.get("zw_auth"):
        user = await request.app.state.repo.authenticate(token)
        if not user:
            raise AppError(401, "AUTH_EXPIRED", "登录已失效，请重新登录")
        return {"user_id": user["id"], "anonymous_hash": None, "key": "user:" + user["id"], "username": user["username"]}
    token = request.cookies.get("zw_anon", "")
    if len(token) < 32 or len(token) > 128:
        raise AppError(401, "VISITOR_CREDENTIAL_REQUIRED", "访问凭证已失效，请刷新页面")
    return {"user_id": None, "anonymous_hash": digest(token), "key": "anon:" + digest(token), "username": None}


def csrf(request: Request):
    token = request.cookies.get("zw_anon", "")
    expected = digest("csrf:" + token) if token else ""
    supplied = request.headers.get("X-CSRF-Token", "")
    origin = request.headers.get("origin")
    # 反向代理必须正确传递可信协议与Host；部署指南要求配置受信任代理。
    valid_origin = not origin or origin.rstrip("/") == str(request.base_url).rstrip("/")
    if (not expected or not hmac.compare_digest(expected, supplied) or not valid_origin
            or request.headers.get("sec-fetch-site") == "cross-site"):
        raise AppError(403, "CSRF_FAILED", "请求校验失败，请刷新页面后重试")


class RateLimiter:
    """单进程示例部署限流；多进程部署须替换共享限流存储。"""
    def __init__(self):
        self.entries = {}

    def check(self, key, count=20, window=60):
        now = time.monotonic()
        # 定期移除闲置键，避免匿名访问造成无界内存增长。
        if len(self.entries) > 1000:
            self.entries = {k: v for k, v in self.entries.items() if v and v[-1] > now - 600}
        times = [value for value in self.entries.get(key, []) if value > now - window]
        if len(times) >= count:
            raise AppError(429, "RATE_LIMITED", "请求过于频繁，请稍后重试", True)
        self.entries[key] = times + [now]
