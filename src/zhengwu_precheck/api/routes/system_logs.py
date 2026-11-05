"""工程师只读技术查询入口；沿用工作人员身份和实时岗位授权。"""
from urllib.parse import urlencode

from fastapi import APIRouter, Request

from zhengwu_precheck.api.staff_dependencies import require_permission
from zhengwu_precheck.api.routes.staff import CSRF, csrf_value
from zhengwu_precheck.core.staff_permissions import workspace_context
from zhengwu_precheck.infrastructure.system_log_repository import TASK_NAMES, LEVEL_NAMES, display_time

router = APIRouter(prefix="/staff")


async def records(request, failures=False):
    await require_permission(request, "system.logs")
    repository = request.app.state.system_log_repo
    return await (repository.failures if failures else repository.logs)(dict(request.query_params))


@router.get("/api/system/logs")
async def logs_api(request: Request):
    return await records(request)


@router.get("/api/system/failures")
async def failures_api(request: Request):
    return await records(request, failures=True)


async def render(request, failures=False):
    identity = await require_permission(request, "system.logs")
    result = await records(request, failures)
    total_pages = max(1, (result["total"] + result["page_size"] - 1) // result["page_size"])
    pagination = []
    for page in (result["page"] - 1, result["page"] + 1):
        if 1 <= page <= total_pages:
            pagination.append({"page": page, "url": str(request.url.path) + "?" +
                urlencode({**dict(request.query_params), "page": page})})
    return request.app.state.templates.TemplateResponse(request=request, name="staff/system_logs.html", context={
        **workspace_context(identity), "module_name": "失败任务记录" if failures else "运行日志与错误查询",
        "csrf_token": csrf_value(request.cookies.get(CSRF, "")), "records": result,
        "filters": dict(request.query_params), "failures": failures, "pagination": pagination,
        "task_names": TASK_NAMES, "level_names": LEVEL_NAMES, "display_time": display_time,
        "total_pages": total_pages,
    })


@router.get("/logs")
async def logs_page(request: Request):
    return await render(request)


@router.get("/failures")
async def failures_page(request: Request):
    return await render(request, failures=True)
