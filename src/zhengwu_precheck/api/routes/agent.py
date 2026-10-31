"""办事人直接确认接口；模型不可调用或伪造这些事件。"""
import json
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from zhengwu_precheck.api.dependencies import principal, csrf
from zhengwu_precheck.api.routes.tickets import _body, _citizen, _form
from zhengwu_precheck.core.services.chat import MATTERS
from zhengwu_precheck.core.services.agent_tools import dispatch
from zhengwu_precheck.core.errors import AppError

router = APIRouter()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Selection(Strict):
    matter_id: str = Field(min_length=1,max_length=100)
    scenario_id: str = Field(min_length=1,max_length=100)
    snapshot_id: str = Field(min_length=1,max_length=128)


class Confirmation(Strict):
    draft_version: int = Field(ge=1)


class Precheck(Strict):
    material_set_id: str = Field(min_length=1,max_length=128)
    rule_snapshot_id: str = Field(min_length=1,max_length=128)
    expected_case_version: int = Field(ge=1)


class Question(Strict):
    question: str = Field(min_length=1,max_length=2000)


class ExceptionReport(Strict):
    kind: str = Field(min_length=1,max_length=40)
    detail: str = Field(min_length=1,max_length=1000)


async def validate_payload(request, who, payload, *, existing_exception_ids=()):
    if not isinstance(payload,dict):
        raise AppError(422,'INVALID_DRAFT','草稿格式无效')
    item = (payload.get('matter_id'),payload.get('scenario_id'))
    if not any((m['matter_id'],m['scenario_id'])==item for m in MATTERS) and item != ('unassigned','manual-triage'):
        raise AppError(422,'INVALID_MATTER','请选择有效事项，或选择人工咨询分流')
    if payload.get('case_id'):
        async with request.app.state.repo.engine.connect() as c:
            await request.app.state.agent_repo.case(c,payload['case_id'],who)
    if payload.get('exception_ids'):
        ids=payload['exception_ids']
        if not isinstance(ids,list) or any(not isinstance(eid,str) or eid not in existing_exception_ids for eid in ids):
            raise AppError(422,'INVALID_LINK','请通过当前咨询的受控操作关联异常')
        async with request.app.state.repo.engine.connect() as c:
            for eid in ids:
                found=(await c.execute(text('SELECT id FROM agent_exceptions WHERE id=:id AND case_id=:case AND principal_key=:who'),
                    {'id':eid,'case':payload.get('case_id'),'who':who['key']})).scalar()
                if not found:
                    raise AppError(404,'EXCEPTION_NOT_FOUND','异常不存在或无权访问')
    shared=payload.get('shared_material_ids',[])
    if not isinstance(shared,list) or len(shared)>10 or any(not isinstance(v,str) for v in shared):
        raise AppError(422,'INVALID_DRAFT','共享材料标识无效')
    frozen=[]
    for mid in shared:
        item=await request.app.state.material_repo.get(who['user_id'],mid)
        if item['case_id']!=payload.get('case_id'):
            raise AppError(404,'MATERIAL_NOT_FOUND','材料不属于当前咨询')
        frozen.append({k:item[k] for k in ('id','version','sha256','filename')})
    payload['shared_material_versions']=frozen
    if payload.get('precheck_id'):
        checked=await request.app.state.material_repo.get_precheck(who['user_id'],payload['precheck_id'])
        if checked['case_id']!=payload.get('case_id'):
            raise AppError(404,'PRECHECK_NOT_FOUND','预检不属于当前咨询')
        await request.app.state.agent_service.validate_precheck(who['user_id'],checked)
    return payload


@router.post('/agent/cases',dependencies=[Depends(csrf)])
async def create_case(request:Request,who=Depends(principal)):
    await request.app.state.shared_limiter.check('case-create:'+who['key'],20,60)
    return await request.app.state.agent_service.create_case(who)


@router.get('/agent/cases/{case_id}/rules')
async def rules(case_id:str, request:Request, matter_id:str, scenario_id:str,who=Depends(principal)):
    async with request.app.state.repo.engine.connect() as c:
        await request.app.state.agent_repo.case(c,case_id,who)
    if not any(m['matter_id']==matter_id and m['scenario_id']==scenario_id for m in MATTERS):
        raise AppError(422,'INVALID_MATTER','事项情形无效')
    snap=await request.app.state.agent_service.snapshot(matter_id,scenario_id)
    sid=request.app.state.agent_repo.snapshot_key(snap)[0]
    return {'snapshot_id':sid,'matter_id':matter_id,'scenario_id':scenario_id,'conflict':snap['conflict'],
            'version_ids':snap['version_ids'],'local_signature':snap.get('local_signature'),
            'teaching_rule_version':snap['teaching_rule_version'],'precheck_rules_approved':snap['precheck_rules_approved'],'guides':snap['guides'],
            'notice':'仅供教学；确认后规则变化须重新确认，不代表申请或审批。'}


@router.get('/agent/cases/{case_id}')
async def get_case(case_id:str,request:Request,who=Depends(principal)):
    async with request.app.state.repo.engine.connect() as c:
        case=await request.app.state.agent_repo.case(c,case_id,who)
        selected=(await c.execute(text('SELECT version,snapshot_id FROM agent_rule_selections WHERE case_id=:id AND principal_key=:who'),
            {'id':case_id,'who':who['key']})).mappings().first()
    return {'case_id':case_id,'matter_id':case['matter_id'],'scenario_id':case['scenario_id'],
            'selection':dict(selected) if selected else None}


@router.post('/agent/cases/{case_id}/selection',dependencies=[Depends(csrf)])
async def choose(case_id:str,body:Selection,request:Request,who=Depends(principal)):
    return await request.app.state.agent_service.choose(case_id,who,body.matter_id,body.scenario_id,body.snapshot_id)


@router.post('/agent/cases/{case_id}/ask',dependencies=[Depends(csrf)])
async def ask(case_id:str,body:Question,request:Request,who=Depends(principal)):
    return await request.app.state.agent_service.run(body.question,case_id,who)


@router.post('/agent/cases/{case_id}/exceptions',dependencies=[Depends(csrf)])
async def report_exception(case_id:str,body:ExceptionReport,request:Request,who=Depends(principal)):
    eid=await request.app.state.agent_repo.record_exception(case_id,who,body.kind,'办事人报告，待人工核实：'+body.detail)
    return {'id':eid,'status':'requires_manual','text':'已记录待人工核实的异常，请预览人工咨询草稿后确认提交。'}


@router.post('/agent/cases/{case_id}/exception-draft',dependencies=[Depends(csrf)])
async def exception_draft(case_id:str,request:Request):
    who=await _citizen(request)
    body=await request.json()
    if not isinstance(body,dict) or set(body)!={'exception_id','summary','expected_case_version'}:
        raise AppError(422,'INVALID_EXCEPTION','异常草稿格式无效')
    handlers=await request.app.state.agent_service.handlers(case_id,who)
    return await dispatch('prepare_ticket_draft',{'case_id':case_id,'purpose':'exception_review',
        'summary':body['summary'],'exception_ids':[body['exception_id']],
        'expected_case_version':body['expected_case_version']},who,handlers=handlers)


@router.post('/agent/cases/{case_id}/precheck',dependencies=[Depends(csrf)])
async def precheck(case_id:str,body:Precheck,request:Request,who=Depends(principal)):
    async with request.app.state.repo.engine.connect() as c:
        case=await request.app.state.agent_repo.case(c,case_id,who)
    snap=await request.app.state.agent_service.snapshot(case['matter_id'],case['scenario_id'])
    selected=await request.app.state.agent_repo.selection(case_id,who,snap,body.expected_case_version)
    if body.rule_snapshot_id!=selected['snapshot_id']:
        raise AppError(409,'SELECTION_CHANGED','规则已变化，请重新确认')
    handlers=await request.app.state.agent_service.handlers(case_id,who)
    result=await dispatch('check_materials',{'case_id':case_id,'selection_confirmation_id':selected['snapshot_id'],
        'rule_snapshot_id':body.rule_snapshot_id,'material_set_id':body.material_set_id,
        'expected_case_version':body.expected_case_version},who,handlers=handlers)
    return result


@router.post('/agent/drafts',dependencies=[Depends(csrf)])
async def create_draft(request:Request):
    who=await _citizen(request)
    await request.app.state.shared_limiter.check('draft-create:'+who['key'],20,60)
    try:
        payload=await request.json()
    except ValueError:
        raise AppError(400,'INVALID_JSON','请求内容不是有效JSON') from None
    await validate_payload(request,who,payload)
    return await request.app.state.agent_repo.create_draft(who['user_id'],payload)


@router.put('/agent/drafts/{draft_id}',dependencies=[Depends(csrf)])
async def update_draft(draft_id:str,request:Request):
    who=await _citizen(request)
    body=await request.json()
    if not isinstance(body,dict) or set(body)!={'expected_version','data'} or type(body['expected_version']) is not int:
        raise AppError(422,'INVALID_DRAFT','请提供草稿版本和完整正文')
    current=await request.app.state.agent_repo.get_draft(who['user_id'],draft_id)
    await validate_payload(request,who,body['data'],existing_exception_ids=current['data'].get('exception_ids',[]))
    return await request.app.state.agent_repo.update_draft(who['user_id'],draft_id,body['expected_version'],body['data'])


@router.get('/agent/drafts/{draft_id}')
async def get_draft(draft_id:str,request:Request):
    who=await _citizen(request)
    return await request.app.state.agent_repo.get_draft(who['user_id'],draft_id)


@router.post('/agent/drafts/{draft_id}/confirm',dependencies=[Depends(csrf)])
async def confirm(draft_id:str,body:Confirmation,request:Request):
    who=await _citizen(request)
    return await request.app.state.agent_repo.confirm_draft(who['user_id'],draft_id,body.draft_version)


@router.post('/tickets/drafts')
async def preview_form(request:Request):
    data=await _body(request,citizen=True)
    who=await _citizen(request)
    data.pop('idempotency_key',None)
    data={k:v for k,v in data.items() if v!=''}
    data['shared_material_ids']=[]
    await validate_payload(request,who,data)
    draft=await request.app.state.agent_repo.create_draft(who['user_id'],data)
    return RedirectResponse('/my/ticket-drafts/'+draft['id'],303)


@router.get('/my/ticket-drafts/{draft_id}')
async def preview(draft_id:str,request:Request):
    from zhengwu_precheck.utils.security import digest
    who=await _citizen(request)
    draft=await request.app.state.agent_repo.get_draft(who['user_id'],draft_id)
    return request.app.state.templates.TemplateResponse(request=request,name='ticket_draft.html',context={
        'draft':draft,'shared_context':await request.app.state.ticket_repo.draft_context(who['user_id'],draft_id),
        'csrf_token':digest('csrf:'+request.cookies.get('zw_anon',''))})


@router.post('/tickets/drafts/{draft_id}/submit')
async def submit_form(draft_id:str,request:Request):
    data=await _body(request,citizen=True)
    who=await _citizen(request)
    try:
        version=int(data.get('draft_version',''))
    except (ValueError,TypeError):
        raise AppError(422,'INVALID_DRAFT','草稿版本无效') from None
    draft=await request.app.state.agent_repo.get_draft(who['user_id'],draft_id)
    if draft['version']!=version or draft['content_hash']!=data.get('draft_hash'):
        raise AppError(409,'DRAFT_CHANGED','草稿已变化，请重新预览确认')
    confirmation=await request.app.state.agent_repo.confirm_draft(who['user_id'],draft_id,version)
    ticket=await request.app.state.ticket_repo.create_confirmed(who['user_id'],draft_id,version,confirmation['id'],data['idempotency_key'])
    return RedirectResponse('/my/tickets?ticket_id='+ticket['id'],303)


@router.post('/tickets/drafts/{draft_id}/edit')
async def edit_form(draft_id:str,request:Request):
    data=await _body(request,citizen=True)
    who=await _citizen(request)
    draft=await request.app.state.agent_repo.get_draft(who['user_id'],draft_id)
    try:
        version=int(data.get('draft_version',''))
    except (ValueError,TypeError):
        raise AppError(422,'INVALID_DRAFT','草稿版本无效') from None
    payload={**draft['data'],'title':data.get('title'),'content':data.get('content')}
    if data.get('remove_materials')=='yes':
        payload['shared_material_ids']=[]
    if data.get('remove_precheck')=='yes':
        payload.pop('precheck_id',None)
    if data.get('remove_exceptions')=='yes':
        payload['exception_ids']=[]
    await validate_payload(request,who,payload,existing_exception_ids=draft['data'].get('exception_ids',[]))
    await request.app.state.agent_repo.update_draft(who['user_id'],draft_id,version,payload)
    return RedirectResponse('/my/ticket-drafts/'+draft_id,303)


@router.get('/assistant')
async def assistant_page(request:Request):
    # 复用首页签发的匿名凭证；首次访问先建立凭证后再回到本页。
    if not request.cookies.get('zw_anon'):
        return RedirectResponse('/',303)
    return request.app.state.templates.TemplateResponse(request=request,name='assistant.html',context={'matters':MATTERS})
