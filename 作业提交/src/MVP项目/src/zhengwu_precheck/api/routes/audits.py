"""授权审计与人工复核页面及API。"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from zhengwu_precheck.api.staff_dependencies import staff_identity, staff_csrf, require_permission
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import workspace_context
from zhengwu_precheck.utils.security import digest
from zhengwu_precheck.infrastructure.audit_repository import FLAGS, DECISIONS

router = APIRouter(prefix='/staff')


async def render(request, template, **context):
    identity = await require_permission(request, 'audit.review')
    return request.app.state.templates.TemplateResponse(request=request, name=template, context={
        **workspace_context(identity),
        'csrf_token':digest('staff-csrf:' + request.cookies.get('zw_staff_csrf', '')),
        'module_name':'对话与预检审计', 'flag_options':FLAGS, 'decision_options':DECISIONS, **context})


@router.get('/audits')
async def audit_page(request: Request):
    identity = await require_permission(request, 'audit.review')
    filters = dict(request.query_params)
    records = await request.app.state.audit_repo.list_records(identity, filters)
    pagination = []
    for page in (records['page']-1, records['page']+1):
        if 1 <= page <= records['pages']:
            pagination.append({'page':page, 'url':str(request.url.include_query_params(page=page))})
    return await render(request, 'staff/audits.html', records=records, filters=filters, pagination=pagination)


@router.get('/audits/{record_id}')
async def audit_detail_page(record_id: str, request: Request):
    identity = await require_permission(request, 'audit.review')
    record = await request.app.state.audit_repo.detail(identity, record_id)
    return await render(request, 'staff/audit_detail.html', record=record)


@router.get('/api/audits')
@router.get('/api/audits/list')
async def audit_list(request: Request):
    return await request.app.state.audit_repo.list_records(await staff_identity(request), dict(request.query_params))


@router.get('/api/audits/{record_id}')
async def audit_detail(record_id: str, request: Request):
    return await request.app.state.audit_repo.detail(await staff_identity(request), record_id)


async def mutation(request, record_id, reviewing=False):
    await staff_csrf(request)
    identity = await staff_identity(request)
    form = 'form' in request.headers.get('content-type', '')
    try:
        data = dict(await request.form()) if form else await request.json()
    except ValueError:
        raise AppError(400, 'INVALID_JSON', '请求内容不是有效JSON') from None
    if not isinstance(data, dict):
        raise AppError(422, 'INVALID_REQUEST', '请提供有效操作内容')
    if reviewing:
        result = await request.app.state.audit_repo.review(identity, record_id, data.get('decision'), data.get('comment'))
    else:
        result = await request.app.state.audit_repo.flag(identity, record_id, data.get('kind'), data.get('reason'))
    return RedirectResponse('/staff/audits/' + record_id, status_code=303) if form else result


@router.post('/api/audits/{record_id}/flags')
async def audit_flag(record_id: str, request: Request):
    return await mutation(request, record_id)


@router.post('/api/audits/{record_id}/reviews')
async def audit_review(record_id: str, request: Request):
    return await mutation(request, record_id, True)
