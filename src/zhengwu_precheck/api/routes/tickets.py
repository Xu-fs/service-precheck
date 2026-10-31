"""人工工单JSON接口与无脚本可用的SSR页面；业务权限由仓储逐对象检查。"""
import secrets
from datetime import datetime
from urllib.parse import urlencode, quote
from zhengwu_precheck.core.services.ticket_clock import SHANGHAI

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from zhengwu_precheck.api.dependencies import csrf, principal
from zhengwu_precheck.api.staff_dependencies import staff_csrf, staff_identity
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.chat import MATTERS
from zhengwu_precheck.core.staff_permissions import MODULES, ROLE_PERMISSIONS, allows, public_identity
from zhengwu_precheck.utils.security import digest

router = APIRouter()
STATUS = {'queued': '窗口待接手', 'processing': '处理中', 'waiting_citizen': '待办事人确认', 'closed': '已完成'}


def _form(request):
    return 'form' in request.headers.get('content-type', '')


async def _body(request, *, action=False, citizen=False):
    if _form(request):
        data = dict(await request.form(max_fields=20, max_files=0))
    else:
        try:
            data = await request.json()
        except ValueError:
            raise AppError(400, 'INVALID_JSON', '请求内容不是有效JSON') from None
    if not isinstance(data, dict):
        raise AppError(422, 'INVALID_REQUEST', '请提交有效的工单信息')
    if citizen:
        # 仅对表单构造同源请求头，复用现有CSRF校验；不跳过Origin和跨站检查。
        checked = request
        if _form(request) and not request.headers.get('x-csrf-token'):
            scope = dict(request.scope)
            scope['headers'] = list(request.scope['headers']) + [(b'x-csrf-token', str(data.get('csrf_token', '')).encode())]
            checked = Request(scope)
        csrf(checked)
    data.pop('csrf_token', None)
    if action:
        version = data.get('expected_version')
        if _form(request):
            try:
                version = int(version)
            except (TypeError, ValueError):
                version = None
        if type(version) is not int or version < 0:
            raise AppError(422, 'INVALID_VERSION', '请提供有效版本号并刷新后重试')
        data['expected_version'] = version
    key = data.get('idempotency_key')
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise AppError(422, 'INVALID_REQUEST', '请提供有效的操作标识')
    return data


async def _citizen(request):
    who = await principal(request)
    if not who['user_id']:
        raise AppError(401, 'LOGIN_REQUIRED', '请先登录，再提交或查看个人工单')
    return who


def _filters(request):
    filters = {k: request.query_params[k] for k in ('status', 'matter_id', 'scope', 'q') if request.query_params.get(k)}
    for name, default, maximum in (('page', 1, 100000), ('page_size', 20, 100)):
        try:
            value = int(request.query_params.get(name, default))
        except ValueError:
            raise AppError(422, 'INVALID_REQUEST', '分页参数不正确') from None
        if not 1 <= value <= maximum:
            raise AppError(422, 'INVALID_REQUEST', '分页参数超出范围')
        filters['limit' if name == 'page_size' else name] = value
    if 'scope' in filters:
        filters['view'] = filters.pop('scope')
    return filters


def _time(value):
    return datetime.fromtimestamp(value, SHANGHAI).strftime('%Y-%m-%d %H:%M') if value else '—'


def _common(request):
    return {'statuses': STATUS, 'matters': MATTERS, 'time_text': _time, 'new_key': lambda: secrets.token_urlsafe(24)}


async def _staff_context(request, identity):
    return {**_common(request), 'identity': public_identity(identity),
            'modules': [m for m in MODULES if allows(identity['grants'], m['permission'])],
            'module_name': '工单处理', 'csrf_token': digest('staff-csrf:' + request.cookies.get('zw_staff_csrf', ''))}


@router.get('/tickets')
async def citizen_list(request: Request):
    who = await _citizen(request)
    return await request.app.state.ticket_repo.citizen_list(who['user_id'], _filters(request))


@router.post('/tickets')
async def create(request: Request):
    data = await _body(request, citizen=True)
    who = await _citizen(request)
    if set(data) - {'matter_id','scenario_id','title','content','idempotency_key','conversation_id','message_id'}:
        raise AppError(422, 'INVALID_REQUEST', '工单只接收问题信息，附件、预检与正式办件关联暂未开放')
    result = await request.app.state.ticket_repo.create(who['user_id'], data)
    return RedirectResponse('/my/tickets?ticket_id=' + quote(result['id']), 303) if _form(request) else result


@router.get('/tickets/{ticket_id}')
async def citizen_detail(ticket_id: str, request: Request):
    who = await _citizen(request)
    return await request.app.state.ticket_repo.citizen_detail(who['user_id'], ticket_id)


async def _citizen_action(ticket_id, request, action):
    data = await _body(request, action=True, citizen=True)
    who = await _citizen(request)
    result = await request.app.state.ticket_repo.citizen_action(who['user_id'], ticket_id, action, data)
    return RedirectResponse('/my/tickets?ticket_id=' + quote(ticket_id), 303) if _form(request) else result


@router.post('/tickets/{ticket_id}/messages')
async def citizen_message(ticket_id: str, request: Request):
    return await _citizen_action(ticket_id, request, 'message')


@router.post('/tickets/{ticket_id}/confirm-resolved')
async def citizen_confirm(ticket_id: str, request: Request):
    return await _citizen_action(ticket_id, request, 'confirm')


@router.get('/notifications')
async def notifications(request: Request):
    who = await _citizen(request)
    return await request.app.state.ticket_repo.notifications(who['user_id'])


@router.post('/notifications/{notification_id}/read')
async def read_notification(notification_id: str, request: Request):
    await _body(request, action=True, citizen=True)
    who = await _citizen(request)
    result = await request.app.state.ticket_repo.read_notification(who['user_id'], notification_id)
    return RedirectResponse('/my/tickets', 303) if _form(request) else result


@router.get('/my/tickets')
async def citizen_page(request: Request):
    token = request.cookies.get('zw_anon', '')
    rotate = not 32 <= len(token) <= 128
    if rotate:
        token = secrets.token_urlsafe(32)
    context = {**_common(request), 'csrf_token': digest('csrf:' + token), 'who': None, 'detail': None, 'result': None, 'notifications': [], 'login_error': None}
    try:
        who = await _citizen(request)
        context.update(who=who, result=await request.app.state.ticket_repo.citizen_list(who['user_id'], _filters(request)), notifications=await request.app.state.ticket_repo.notifications(who['user_id']))
        if ticket_id := request.query_params.get('ticket_id'):
            context['detail'] = await request.app.state.ticket_repo.citizen_detail(who['user_id'], ticket_id)
    except AppError as error:
        if error.status != 401:
            raise
        context['login_error'] = error.message if request.cookies.get('zw_auth') else '请先登录，再提交和查看自己的工单。'
    context['page_link'] = lambda page: '/my/tickets?' + urlencode({**request.query_params, 'page': page})
    # 仅预填关联标识；真正归属和事项一致性在创建事务中校验。
    context['prefill'] = {key: request.query_params.get(key, '') for key in ('conversation_id','message_id','matter_id','scenario_id')}
    response = request.app.state.templates.TemplateResponse(request=request, name='tickets.html', context=context)
    if rotate:
        response.set_cookie('zw_anon', token, httponly=True, secure=request.app.state.settings.secure_cookies, samesite='lax', max_age=30*86400, path='/')
    return response


@router.get('/staff/api/tickets')
async def staff_list(request: Request):
    return await request.app.state.ticket_repo.staff_list(await staff_identity(request), _filters(request))


@router.get('/staff/api/assignees')
async def assignees(request: Request, matter_id: str, ticket_id: str):
    return await request.app.state.ticket_repo.assignees(await staff_identity(request), matter_id, ticket_id)


@router.get('/staff/api/tickets/{ticket_id}')
async def staff_detail(ticket_id: str, request: Request):
    return await request.app.state.ticket_repo.staff_detail(await staff_identity(request), ticket_id)


@router.get('/staff/api/tickets/{ticket_id}/transitions')
async def transitions(ticket_id: str, request: Request):
    detail = await request.app.state.ticket_repo.staff_detail(await staff_identity(request), ticket_id)
    return detail.get('transitions', [])


async def _staff_action(ticket_id, request, action, transfer_id=None):
    await staff_csrf(request)
    identity = await staff_identity(request)
    data = await _body(request, action=True)
    # SSR选择项合并为一个值，避免用户把甲的身份和乙的岗位意外拼接。
    if target := data.pop('target', None):
        try:
            data['target_user_id'], data['target_role'] = target.split('|', 1)
        except ValueError:
            raise AppError(422, 'INVALID_REQUEST', '请选择有效接收人') from None
    result = await request.app.state.ticket_repo.staff_action(identity, ticket_id, action, data, transfer_id)
    return RedirectResponse('/staff/tickets/' + quote(ticket_id), 303) if _form(request) else result


@router.post('/staff/api/tickets/{ticket_id}/accept')
async def accept(ticket_id: str, request: Request):
    return await _staff_action(ticket_id, request, 'accept')


@router.post('/staff/api/tickets/{ticket_id}/transfers')
async def transfer(ticket_id: str, request: Request):
    return await _staff_action(ticket_id, request, 'transfer')


@router.post('/staff/api/tickets/{ticket_id}/transfers/{transfer_id}/accept')
async def transfer_accept(ticket_id: str, transfer_id: str, request: Request):
    return await _staff_action(ticket_id, request, 'transfer_accept', transfer_id)


@router.post('/staff/api/tickets/{ticket_id}/transfers/{transfer_id}/reject')
async def transfer_reject(ticket_id: str, transfer_id: str, request: Request):
    return await _staff_action(ticket_id, request, 'transfer_reject', transfer_id)


@router.post('/staff/api/tickets/{ticket_id}/transfers/{transfer_id}/cancel')
async def transfer_cancel(ticket_id: str, transfer_id: str, request: Request):
    return await _staff_action(ticket_id, request, 'transfer_cancel', transfer_id)


@router.post('/staff/api/tickets/{ticket_id}/replies')
async def reply(ticket_id: str, request: Request):
    return await _staff_action(ticket_id, request, 'reply')


@router.get('/staff/tickets')
async def staff_page(request: Request):
    try:
        identity = await staff_identity(request)
    except AppError as error:
        if error.status == 401:
            return RedirectResponse('/staff/login', 303)
        raise
    context = await _staff_context(request, identity)
    context.update(result=await request.app.state.ticket_repo.staff_list(identity, _filters(request)), filters={**_filters(request), 'scope': request.query_params.get('scope', 'all')})
    context['page_link'] = lambda page: '/staff/tickets?' + urlencode({**request.query_params, 'page': page})
    context['can_accept'] = lambda ticket: allows(identity['grants'], 'ticket.handle', ticket['matter_id']) and any(g['role_code'] == 'window' and g['matter_id'] in ('*', ticket['matter_id']) for g in identity['grants'])
    return request.app.state.templates.TemplateResponse(request=request, name='staff/tickets.html', context=context)


@router.get('/staff/tickets/{ticket_id}')
async def staff_detail_page(ticket_id: str, request: Request):
    try:
        identity = await staff_identity(request)
    except AppError as error:
        if error.status == 401:
            return RedirectResponse('/staff/login', 303)
        raise
    detail = await request.app.state.ticket_repo.staff_detail(identity, ticket_id)
    ticket = detail['ticket']
    context = await _staff_context(request, identity)
    owner_grants = [g for g in identity['grants'] if g['role_code'] == ticket.get('owner_role') and g['matter_id'] in ('*', ticket['matter_id'])]
    permission = 'ticket.escalated' if ticket.get('owner_role') == 'director' else 'ticket.handle'
    can_handle = ticket.get('owner_id') == identity['user_id'] and any(permission in g.get('permissions', ROLE_PERMISSIONS.get(g['role_code'], set())) for g in owner_grants)
    transfer_permission = {'window': 'ticket.transfer', 'reviewer': 'ticket.dispatch'}.get(ticket.get('owner_role'))
    can_transfer = can_handle and transfer_permission and any(transfer_permission in g.get('permissions', ROLE_PERMISSIONS.get(g['role_code'], set())) for g in owner_grants)
    targets = await request.app.state.ticket_repo.assignees(identity, ticket['matter_id'], ticket_id) if can_transfer and ticket['status'] != 'closed' else []
    context.update(detail=detail, ticket=ticket, targets=targets, can_handle=can_handle, user_id=identity['user_id'])
    return request.app.state.templates.TemplateResponse(request=request, name='staff/tickets_detail.html', context=context)




