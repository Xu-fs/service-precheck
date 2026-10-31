"""第一批工作人员后台：真正的SSR登录与授权页面，后续模块明确显示未开放。"""
import secrets
from uuid import UUID
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from zhengwu_precheck.api.schemas import LoginInput
from zhengwu_precheck.api.staff_dependencies import staff_csrf, staff_identity, require_permission
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import MODULES, allows, public_identity
from zhengwu_precheck.utils.security import digest

router = APIRouter(prefix="/staff")
ACCESS, REFRESH, CSRF = "zw_staff_access", "zw_staff_refresh", "zw_staff_csrf"


def is_form(request):
    return "form" in request.headers.get("content-type", "")


def csrf_value(cookie):
    return digest("staff-csrf:" + cookie)


def cookie(response, request, name, value, age):
    response.set_cookie(name, value, httponly=True, secure=request.app.state.settings.secure_cookies,
                        samesite="lax", path="/staff", max_age=age)


def login_page(request, *, error=None, status=200):
    token = request.cookies.get(CSRF, "")
    if not 32 <= len(token) <= 128:
        token = secrets.token_urlsafe(32)
    configured = bool(request.app.state.settings.staff_jwt_secret and len(request.app.state.settings.staff_jwt_secret.encode()) >= 32)
    response = request.app.state.templates.TemplateResponse(request=request, name="staff/login.html", status_code=status,
        context={"csrf_token": csrf_value(token), "error": error, "configured": configured, "can_refresh": bool(request.cookies.get(REFRESH))})
    cookie(response, request, CSRF, token, 8 * 3600)
    return response


@router.get("/login")
async def login_screen(request: Request):
    return login_page(request)


@router.post("/api/auth/login")
async def login(request: Request):
    await staff_csrf(request)
    ip = request.client.host if request.client else "unknown"
    request.app.state.limiter.check("staff-login:" + ip, 5, 60)
    try:
        if is_form(request):
            data = dict(await request.form())
            data.pop("csrf_token", None)
        else:
            try:
                data = await request.json()
            except ValueError:
                raise AppError(400, "INVALID_JSON", "请求内容不是有效JSON") from None
        try:
            body = LoginInput.model_validate(data)
        except ValidationError:
            raise AppError(422, "INVALID_REQUEST", "请输入有效的账号和密码") from None
        values = await request.app.state.staff_auth.login(body.username, body.password)
    except AppError as error:
        if is_form(request):
            return login_page(request, error=error.message, status=error.status)
        raise
    response = RedirectResponse("/staff", status_code=303) if is_form(request) else JSONResponse({"identity": "staff", "csrf_token": csrf_value(values["csrf_cookie"]), "expires_in": values["expires_in"]})
    cookie(response, request, ACCESS, values["access"], values["expires_in"])
    cookie(response, request, REFRESH, values["refresh"], 8 * 3600)
    cookie(response, request, CSRF, values["csrf_cookie"], 8 * 3600)
    return response


@router.post("/api/auth/refresh")
async def refresh(request: Request):
    csrf_cookie = await staff_csrf(request)
    request.app.state.limiter.check("staff-refresh:" + digest(request.cookies.get(REFRESH, "")), 12, 60)
    try:
        values = await request.app.state.staff_auth.refresh(request.cookies.get(REFRESH), csrf_cookie)
    except AppError as error:
        if is_form(request):
            return login_page(request, error=error.message, status=error.status)
        raise
    response = RedirectResponse("/staff", status_code=303) if is_form(request) else JSONResponse({"identity": "staff", "csrf_token": csrf_value(values["csrf_cookie"]), "expires_in": values["expires_in"]})
    cookie(response, request, ACCESS, values["access"], values["expires_in"])
    # 刷新不延长数据库内8小时绝对期限；Cookie同样只保留剩余时间。
    row = await request.app.state.staff_repo.session(refresh_hash=digest(values["refresh"]))
    import time
    remaining = max(0, int(row["absolute_expires"] - time.time()))
    cookie(response, request, REFRESH, values["refresh"], remaining)
    return response


@router.post("/api/auth/logout")
async def logout(request: Request):
    csrf_cookie = await staff_csrf(request)
    await request.app.state.staff_auth.logout(request.cookies.get(ACCESS), request.cookies.get(REFRESH), csrf_cookie)
    response = RedirectResponse("/staff/login", status_code=303) if is_form(request) else JSONResponse({"identity": "anonymous"})
    for name in (ACCESS, REFRESH, CSRF):
        response.delete_cookie(name, path="/staff")
    return response


@router.get("/api/me")
async def me(request: Request):
    return public_identity(await staff_identity(request))


@router.get("/api/system/status")
async def system_status(request: Request):
    await require_permission(request, "system.health")
    # 只返回技术状态，不包含用户列表、材料、对话或密钥信息。
    return {"database": "connected", "staff_schema_version": 1, "staff_batch": 1}


@router.get("/api/conversations/{conversation_id}")
async def conversation_summary(conversation_id: UUID, request: Request):
    return await request.app.state.staff_repo.record(await staff_identity(request), str(conversation_id))


@router.get("/api/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: UUID, request: Request):
    return await request.app.state.staff_repo.record(await staff_identity(request), str(conversation_id), body=True)


async def render_workspace(request, module=None):
    try:
        identity = await require_permission(request, module["permission"] if module else "workspace.view")
    except AppError as error:
        if error.status == 401:
            return RedirectResponse("/staff/login", status_code=303)
        raise
    visible = [item for item in MODULES if allows(identity["grants"], item["permission"])]
    return request.app.state.templates.TemplateResponse(request=request, name="staff/workspace.html", context={
        "identity": public_identity(identity), "modules": visible, "module": module,
        "csrf_token": csrf_value(request.cookies.get(CSRF, "")), "system_health": allows(identity["grants"], "system.health"),
    })


@router.get("")
async def workspace(request: Request):
    return await render_workspace(request)


@router.get("/{module_slug}")
async def upcoming(module_slug: str, request: Request):
    module = next((item for item in MODULES if item["slug"] == module_slug), None)
    if not module:
        raise AppError(404, "NOT_FOUND", "该后台页面不存在")
    return await render_workspace(request, module)
