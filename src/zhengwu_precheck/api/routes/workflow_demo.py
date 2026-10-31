"""已登录办事人的独立示例演示入口；不签发或替换任何工作人员登录身份。"""
import secrets

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from zhengwu_precheck.api.dependencies import csrf, principal
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.demo_cases import workflow_cases
from zhengwu_precheck.utils.security import digest

router = APIRouter()


class StartDemo(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preset_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=128)


class NextStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_step: StrictInt = Field(ge=0, le=11)


async def viewer(request):
    who = await principal(request)
    if not who['user_id']:
        raise AppError(401, 'LOGIN_REQUIRED', '请先使用办事人账号登录，再开始演示')
    return who


@router.get('/examples/workflow')
async def page(request: Request):
    token = request.cookies.get('zw_anon', '')
    rotate = not 32 <= len(token) <= 128
    if rotate:
        token = secrets.token_urlsafe(32)
    try:
        who = await viewer(request)
    except AppError as error:
        if error.status != 401:
            raise
        who = None
    cases = workflow_cases()
    preset = request.query_params.get('example', cases[0]['id'])
    response = request.app.state.templates.TemplateResponse(request=request, name='workflow_demo.html',
        context={'who': who, 'csrf_token': digest('csrf:' + token), 'example_cases': cases,
                 'preset_id': preset, 'run_id': request.query_params.get('run', '')})
    if rotate:
        response.set_cookie('zw_anon', token, httponly=True, secure=request.app.state.settings.secure_cookies,
                            samesite='lax', max_age=30 * 86400, path='/')
    return response


@router.post('/examples/api/workflows')
async def start(request: Request, body: StartDemo):
    csrf(request)
    who = await viewer(request)
    await request.app.state.shared_limiter.check('workflow-demo:' + who['user_id'], 6, 60)
    return await request.app.state.workflow_demo.create(who['user_id'], body.preset_id, body.idempotency_key)


@router.get('/examples/api/workflows/{run_id}')
async def detail(run_id: str, request: Request):
    who = await viewer(request)
    return await request.app.state.workflow_demo.detail(who['user_id'], run_id)


@router.get('/examples/api/workflows')
async def history(request: Request):
    who = await viewer(request)
    return await request.app.state.workflow_demo.history(who['user_id'])


@router.post('/examples/api/workflows/{run_id}/next')
async def advance(run_id: str, request: Request, body: NextStep):
    csrf(request)
    who = await viewer(request)
    return await request.app.state.workflow_demo.advance(who['user_id'], run_id, body.expected_step)
