"""监督员留存操作：真实后台身份、同一全事项岗位授权及CSRF。"""
import asyncio
from itertools import islice
import json
import os
from typing import Any

from fastapi import APIRouter,Request
from pydantic import BaseModel,ConfigDict,Field,ValidationError

from zhengwu_precheck.api.staff_dependencies import staff_identity,staff_csrf
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.retention_policy import dispose

router=APIRouter(prefix='/staff/api/retention')


class DisposalInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    signed_policy: dict[str,Any]
    approved: dict[str,str]=Field(max_length=1000)
    dry_run: bool=True
    quiesced: bool=False


@router.post('/dispose')
async def retention_dispose(request:Request):
    identity=await staff_identity(request)
    if not any(grant.get('role_code')=='supervisor' and grant.get('matter_id')=='*' for grant in identity['grants']):
        await request.app.state.repo.audit('retention_permission_denied',identity['user_id'],outcome='denied')
        raise AppError(403,'STAFF_PERMISSION_DENIED','仅具有全事项授权的数据与流程监督员可执行留存处置')
    await staff_csrf(request)
    await request.app.state.shared_limiter.check('retention:'+identity['user_id'],count=2,window=60)
    key=os.getenv('ZHENGWU_RETENTION_POLICY_KEY','').encode()
    if len(key)<32:
        raise AppError(503,'RETENTION_POLICY_UNAVAILABLE','留存策略签名配置尚未就绪，请联系维护人员')
    try:
        body=bytearray()
        async for block in request.stream():
            body.extend(block)
            if len(body)>512*1024:
                raise ValueError('body limit')
        data=DisposalInput.model_validate(json.loads(body))
        if any(not isinstance(identifier,str) or not 1<=len(identifier)<=128 or len(digest)!=64 or any(character not in '0123456789abcdef' for character in digest) for identifier,digest in data.approved.items()):
            raise ValueError('approval format')
        if not data.dry_run and not data.quiesced:
            raise ValueError('quiescence required')
    except (ValueError,TypeError,ValidationError,RecursionError):
        raise AppError(422,'INVALID_RETENTION_REQUEST','留存请求或批准清单无效；实际处置须先确认停止该目录写入') from None
    runtime=request.app.state.settings.runtime_dir
    directory=runtime/'retention-records'
    if not directory.is_dir():
        raise AppError(422,'INVALID_RETENTION_REQUEST','尚无独立留存记录目录，请由维护人员配置')
    if len(list(islice(directory.glob('*.json'),1001)))>1000:
        raise AppError(422,'INVALID_RETENTION_REQUEST','单次最多复核1000条独立留存记录，请分批处理')
    sid=identity['user_id']
    try:
        result=await asyncio.to_thread(dispose,directory,data.signed_policy,key,
            supervisor=sid,verify_supervisor=lambda actor:actor==sid,
            approved=data.approved,archive=runtime/'retention-archives',audit=runtime/'retention-audit.jsonl',
            dry_run=data.dry_run)
    except (ValueError,KeyError,TypeError,AttributeError,PermissionError,OSError,OverflowError,RecursionError):
        await request.app.state.repo.audit('retention_dispose',sid,outcome='rejected')
        raise AppError(422,'INVALID_RETENTION_POLICY','留存策略、批准记录或存储条件无效，未完成处置，请交监督员核对') from None
    await request.app.state.repo.audit('retention_dispose',sid,entity='records:'+str(len(result)),outcome='dry_run' if data.dry_run else 'executed')
    return {'dry_run':data.dry_run,'records':result,'policy_version':data.signed_policy['policy']['version']}
