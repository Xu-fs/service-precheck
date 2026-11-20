"""ASGI请求观测：等流式响应结束再记录，不读取请求或响应正文。"""
import time
from uuid import uuid4

import anyio


class RequestLoggingMiddleware:
    def __init__(self, app, repository):
        self.app, self.repository = app, repository

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith("/static/"):
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        state["request_id"] = str(uuid4())
        start, status = time.monotonic(), 500

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []),
                           (b"x-request-id", state["request_id"].encode("ascii"))]}
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except Exception:
            state["error_code"] = "UNHANDLED_ERROR"
            raise
        finally:
            code = state.get("error_code")
            if not code and status >= 400:
                code = "HTTP_" + str(status)
            level = "error" if status >= 500 or code and status < 400 else "warning" if status >= 400 else "info"
            route = getattr(scope.get("route"), "path", None)
            with anyio.CancelScope(shield=True):
                await self.repository.record(method=scope["method"], route=route,
                    status_code=status, duration_ms=round((time.monotonic() - start) * 1000, 2),
                    request_id=state["request_id"], error_code=code, level=level)
