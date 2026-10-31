"""智能体工具边界；尚未接入的操作明确返回不可用。"""
import asyncio
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from zhengwu_precheck.core.errors import AppError


class StrictInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SearchInput(StrictInput):
    query: str = Field(min_length=1, max_length=2000)
    jurisdiction_code: str = Field(min_length=1, max_length=100)
    matter_id: str | None = Field(default=None, max_length=100)
    scenario_id: str | None = Field(default=None, max_length=100)


class RecommendInput(StrictInput):
    case_id: str = Field(min_length=1, max_length=128)
    intent: str = Field(min_length=1, max_length=2000)
    search_result_id: str = Field(min_length=1, max_length=128)


class MaterialsInput(StrictInput):
    case_id: str = Field(min_length=1, max_length=128)
    selection_confirmation_id: str = Field(min_length=1, max_length=128)
    rule_snapshot_id: str = Field(min_length=1, max_length=128)
    material_set_id: str = Field(min_length=1, max_length=128)
    expected_case_version: int = Field(ge=0)


class DraftInput(StrictInput):
    case_id: str = Field(min_length=1, max_length=128)
    purpose: Literal['ordinary_consultation', 'exception_review']
    summary: str = Field(min_length=1, max_length=4000)
    expected_case_version: int = Field(ge=0)
    precheck_id: str | None = Field(default=None, max_length=128)
    exception_ids: list[str] | None = Field(default=None, max_length=20)


class CreateInput(StrictInput):
    draft_id: str = Field(min_length=1, max_length=128)
    draft_version: int = Field(ge=0)
    submission_confirmation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


class StatusInput(StrictInput):
    ticket_id: str = Field(min_length=1, max_length=128)


TOOL_INPUTS = {
    'search_guidelines': SearchInput,
    'recommend_matter': RecommendInput,
    'check_materials': MaterialsInput,
    'prepare_ticket_draft': DraftInput,
    'create_manual_ticket': CreateInput,
    'get_ticket_status': StatusInput,
}


def ticket_status_handler(repository):
    """复用本人权限校验，仅返回办事人可见的状态信息。"""
    async def get_status(args, principal):
        user_id = principal.get('user_id') if isinstance(principal, dict) else None
        if not user_id:
            raise AppError(401, 'LOGIN_REQUIRED', '请先登录后查询工单')
        detail = await repository.citizen_detail(user_id, args.ticket_id)
        ticket = detail['ticket']
        return {'ticket_id': ticket['id'], 'status': ticket['status'],
                'version': ticket['version'], 'created_at': ticket['created_at'],
                'messages': [{'kind': message['kind'], 'content': message['content'],
                              'created_at': message['created_at']}
                             for message in detail['messages']]}
    return get_status


def _result(status, code, message, retryable=False):
    return {'status': status, 'data': None,
            'error': {'code': code, 'message': message, 'retryable': retryable},
            'next_actions': [], 'trace_id': str(uuid4())}


async def dispatch(name, arguments, principal, *, handlers=None, timeout=10):
    """验证模型参数；不接受模型提供的身份或权限。"""
    schema = TOOL_INPUTS.get(name) if isinstance(name, str) else None
    if schema is None:
        return _result('blocked', 'UNKNOWN_TOOL', '工具不存在')
    if not isinstance(arguments, dict):
        return _result('blocked', 'INVALID_ARGUMENTS', '工具参数格式不正确')
    try:
        validated = schema.model_validate(arguments)
    except ValidationError:
        return _result('blocked', 'INVALID_ARGUMENTS', '工具参数格式不正确')
    handler = (handlers or {}).get(name)
    if handler is None:
        return _result('temporarily_unavailable', 'TOOL_NOT_CONNECTED', '此工具暂未接入', True)
    try:
        data = await asyncio.wait_for(handler(validated, principal), timeout=timeout)
    except TimeoutError:
        return _result('temporarily_unavailable', 'TOOL_TIMEOUT', '工具响应超时，请核对状态后重试', True)
    except AppError as error:
        return _result('blocked', error.code, error.message, error.retryable)
    except Exception:
        return _result('temporarily_unavailable', 'TOOL_FAILED', '工具暂时不可用', True)
    return {'status': 'ok', 'data': data, 'error': None, 'next_actions': [], 'trace_id': str(uuid4())}
