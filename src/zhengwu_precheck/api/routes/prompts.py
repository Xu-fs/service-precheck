"""事项提示词版本管理页面和JSON/表单接口。"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from zhengwu_precheck.api.staff_dependencies import staff_csrf, staff_identity
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import allows, public_identity, workspace_context
from zhengwu_precheck.infrastructure.prompt_repository import MATTERS
from zhengwu_precheck.utils.security import digest

router = APIRouter(prefix="/staff")


def _is_form(request):
    return "form" in request.headers.get("content-type", "")


async def _body(request):
    if _is_form(request):
        # CSRF依赖已缓存FormData，沿用它而不将FormData误判为dict。
        data = dict(await request.form(max_fields=8, max_files=0))
        for key in ("version", "expected_version"):
            if key in data:
                try:
                    data[key] = int(data[key])
                except (ValueError, TypeError):
                    raise AppError(422, "INVALID_VERSION", "请提供有效版本号") from None
    else:
        try:
            data = await request.json()
        except ValueError:
            raise AppError(400, "INVALID_JSON", "请求内容不是有效JSON") from None
    if not isinstance(data, dict):
        raise AppError(422, "INVALID_REQUEST", "请提交有效的提示词信息")
    return data


async def _render(request, identity, matter_id, *, error=None, status=200):
    repository = request.app.state.prompt_repo
    versions = await repository.history(identity, matter_id)
    return request.app.state.templates.TemplateResponse(request=request, name="staff/prompts.html", status_code=status, context={
        **workspace_context(identity), "public_identity": public_identity(identity),
        "module_name": "提示词模板", "csrf_token": digest("staff-csrf:" + request.cookies.get("zw_staff_csrf", "")),
        "matters": {key: name for key, name in MATTERS.items() if allows(identity["grants"], "prompt.manage", key)},
        "matter_id": matter_id, "matter_name": MATTERS[matter_id], "versions": versions,
        "active": await repository.active(matter_id), "error": error,
    })


@router.get("/prompts")
async def prompt_page(request: Request, matter_id: str | None = None):
    try:
        identity = await staff_identity(request)
    except AppError as error:
        if error.status == 401:
            return RedirectResponse("/staff/login", status_code=303)
        raise
    if matter_id is None:
        matter_id = next((key for key in MATTERS if allows(identity["grants"], "prompt.manage", key)), "residence-permit")
    return await _render(request, identity, matter_id)


@router.get("/api/prompts/{matter_id}/versions")
async def prompt_versions(matter_id: str, request: Request):
    return await request.app.state.prompt_repo.history(await staff_identity(request), matter_id)


async def _write(request, matter_id, restore=False):
    await staff_csrf(request)
    identity = await staff_identity(request)
    data = await _body(request)
    try:
        repository = request.app.state.prompt_repo
        if restore:
            result = await repository.restore(identity, matter_id, data.get("version"), data.get("expected_version"), data.get("comment"))
        else:
            result = await repository.save(identity, matter_id, data.get("content"), data.get("expected_version"), data.get("comment"))
    except AppError as error:
        if _is_form(request) and error.status in {409, 422}:
            return await _render(request, identity, matter_id, error=error.message, status=error.status)
        raise
    return RedirectResponse(f"/staff/prompts?matter_id={matter_id}", status_code=303) if _is_form(request) else result


@router.post("/api/prompts/{matter_id}/versions")
async def save_prompt(matter_id: str, request: Request):
    return await _write(request, matter_id)


@router.post("/api/prompts/{matter_id}/restore")
async def restore_prompt(matter_id: str, request: Request):
    return await _write(request, matter_id, restore=True)
