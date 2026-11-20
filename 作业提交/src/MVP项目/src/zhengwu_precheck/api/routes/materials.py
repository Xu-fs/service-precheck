"""私有示例材料入口；读取前验证登录、归属及逐份授权。"""
from fastapi import APIRouter,Request,Depends
from fastapi.responses import Response
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import text
from zhengwu_precheck.api.dependencies import csrf
from zhengwu_precheck.api.routes.tickets import _citizen
from zhengwu_precheck.api.staff_dependencies import staff_identity
from zhengwu_precheck.core.staff_permissions import allows
from zhengwu_precheck.core.errors import AppError

router=APIRouter()


class Correction(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_version:int=Field(ge=1)
    kind:str=Field(max_length=40)
    fields:dict[str,str]


class MaterialSet(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    material_ids:list[str]=Field(max_length=10)


@router.post('/agent/cases/{case_id}/materials',dependencies=[Depends(csrf)])
async def upload(case_id:str,request:Request):
    who=await _citizen(request)
    await request.app.state.shared_limiter.check('material-upload:'+who['key'],10,60)
    async with request.app.state.repo.engine.connect() as c:
        await request.app.state.agent_repo.case(c,case_id,who)
    form=await request.form(max_files=1,max_fields=2,max_part_size=11*1024*1024)
    file=form.get('file')
    if not file or not hasattr(file,'read'):
        raise AppError(422,'INVALID_MATERIAL','请选择示例图片或PDF')
    try:
        content=await file.read(10*1024*1024+1)
        return await request.app.state.material_repo.store(who['user_id'],case_id,file.filename,content,form.get('consent')=='true')
    finally:
        await file.close()


@router.get('/agent/cases/{case_id}/materials')
async def list_materials(case_id:str,request:Request):
    who=await _citizen(request)
    return await request.app.state.material_repo.list(who['user_id'],case_id)


@router.get('/agent/materials/{material_id}')
async def material(material_id:str,request:Request):
    who=await _citizen(request)
    return await request.app.state.material_repo.get(who['user_id'],material_id)


@router.get('/agent/materials/{material_id}/download')
async def download(material_id:str,request:Request):
    who=await _citizen(request)
    content=await request.app.state.material_repo.read_bytes(who['user_id'],material_id)
    return Response(content,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="teaching-material.bin"'})


@router.post('/agent/materials/{material_id}/process',dependencies=[Depends(csrf)])
async def process(material_id:str,request:Request):
    import asyncio
    from zhengwu_precheck.infrastructure.quota_repository import QuotaLimits,QuotaExceeded
    who=await _citizen(request)
    await request.app.state.material_repo.get(who['user_id'],material_id)
    try:
        reservation=await request.app.state.quota_repo.reserve('local-ocr',0,QuotaLimits(1000,1,1,lease_seconds=90))
    except QuotaExceeded:
        raise AppError(429,'OCR_BUSY','材料处理繁忙，请稍后重试或先转录示例字段。',True) from None
    try:
        return await request.app.state.material_repo.process(who['user_id'],material_id)
    finally:
        task=asyncio.create_task(request.app.state.quota_repo.settle(reservation.id,0))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise


@router.put('/agent/materials/{material_id}/fields',dependencies=[Depends(csrf)])
async def fields(material_id:str,body:Correction,request:Request):
    who=await _citizen(request)
    return await request.app.state.material_repo.correct_fields(who['user_id'],material_id,body.expected_version,body.kind,body.fields)


@router.delete('/agent/materials/{material_id}',dependencies=[Depends(csrf)])
async def revoke(material_id:str,request:Request):
    who=await _citizen(request)
    return await request.app.state.material_repo.revoke(who['user_id'],material_id)


@router.post('/agent/cases/{case_id}/material-sets',dependencies=[Depends(csrf)])
async def material_set(case_id:str,body:MaterialSet,request:Request):
    who=await _citizen(request)
    return await request.app.state.material_repo.create_set(who['user_id'],case_id,body.material_ids)


@router.get('/agent/prechecks/{precheck_id}')
async def precheck(precheck_id:str,request:Request):
    who=await _citizen(request)
    result=await request.app.state.material_repo.get_precheck(who['user_id'],precheck_id)
    snap=await request.app.state.agent_service.snapshot(result['matter_id'],result['scenario_id'])
    selected=await request.app.state.agent_repo.selection(result['case_id'],who,snap,result['case_version'])
    if selected['snapshot_id']!=result['selection_snapshot_id']:
        raise AppError(409,'SELECTION_CHANGED','规则已变化，历史结果不能作为当前结论')
    return result


@router.get('/staff/api/tickets/{ticket_id}/materials/{material_id}')
async def staff_material(ticket_id:str,material_id:str,request:Request):
    identity=await staff_identity(request)
    detail=await request.app.state.ticket_repo.staff_detail(identity,ticket_id)
    t=detail['ticket']
    # 热线摘要缺少内部字段；不能叠加无处理关系岗位绕过正文授权。
    if 'conversation_id' not in t or not allows(identity['grants'],'material.view',t['matter_id']):
        raise AppError(404,'MATERIAL_NOT_FOUND','材料不存在或无权访问')
    async with request.app.state.repo.engine.connect() as c:
        row=(await c.execute(text('SELECT d.payload FROM agent_submissions s JOIN agent_drafts d ON d.id=s.draft_id WHERE s.ticket_id=:id'),{'id':ticket_id})).scalar()
    data=__import__('json').loads(row) if row else {}
    frozen=next((m for m in data.get('shared_material_versions',[]) if m['id']==material_id),None)
    if frozen is None:
        raise AppError(404,'MATERIAL_NOT_FOUND','材料不存在或未共享')
    current=await request.app.state.material_repo.get(t['user_id'],material_id)
    if current['version']!=frozen['version'] or current['sha256']!=frozen['sha256']:
        raise AppError(409,'MATERIAL_CHANGED','材料已变化或撤回，请办事人重新授权')
    content=await request.app.state.material_repo.read_bytes(t['user_id'],material_id)
    return Response(content,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="teaching-material.bin"'})
