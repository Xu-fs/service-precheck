"""在读取分块请求时也执行总大小上限，不能只依赖Content-Length。"""
from zhengwu_precheck.core.errors import AppError


def body_limit(path):
    if path == "/staff/api/knowledge/uploads":
        return 21 * 1024 * 1024
    if path.startswith("/staff/api/knowledge/") or path == "/staff/api/rules":
        return 256 * 1024
    return 16384


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        maximum, received = body_limit(scope.get("path", "")), 0
        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    raise AppError(413, "REQUEST_TOO_LARGE", "请求内容超过大小限制，请拆分文档或缩短正文")
            return message
        await self.app(scope, bounded_receive, send)
