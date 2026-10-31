"""后台独立Cookie、CSRF与岗位依赖，不能借用办事人Cookie。"""
import hmac
from fastapi import Request
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import allows
from zhengwu_precheck.utils.security import digest


async def staff_identity(request: Request):
    return await request.app.state.staff_auth.identity(request.cookies.get("zw_staff_access"))


async def require_permission(request, permission, matter_id=None):
    identity = await staff_identity(request)
    if not allows(identity["grants"], permission, matter_id):
        await request.app.state.staff_repo.audit_denial(identity["user_id"], "staff_permission_denied", permission)
        raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位或事项授权不允许此操作")
    return identity


async def staff_csrf(request: Request):
    cookie = request.cookies.get("zw_staff_csrf", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied and "form" in request.headers.get("content-type", ""):
        form = await request.form(max_fields=20, max_files=1 if request.url.path == "/staff/api/knowledge/uploads" else 0, max_part_size=200000)
        supplied = str(form.get("csrf_token", ""))
    origin = request.headers.get("origin")
    valid_origin = not origin or origin.rstrip("/") == str(request.base_url).rstrip("/")
    if (not 32 <= len(cookie) <= 128 or not hmac.compare_digest(digest("staff-csrf:" + cookie).encode(), supplied.encode())
            or not valid_origin or request.headers.get("sec-fetch-site") == "cross-site"):
        raise AppError(403, "CSRF_FAILED", "请求校验失败，请刷新后台页面后重试")
    return cookie
