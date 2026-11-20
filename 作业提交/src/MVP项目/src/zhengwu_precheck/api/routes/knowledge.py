"""知识与发布的SSR/JSON接口；所有写操作复用岗位、事项和CSRF校验。"""
import json
import math
import re
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from zhengwu_precheck.api.staff_dependencies import staff_identity, staff_csrf
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import MATTER_NAMES, allows, workspace_context
from zhengwu_precheck.infrastructure.knowledge_repository import KINDS, SCENARIOS
from zhengwu_precheck.utils.security import digest

router = APIRouter(prefix="/staff")
STATUS_NAMES = {"queued": "等待处理", "processing": "正在处理", "needs_text": "待OCR或人工补充", "ready": "索引就绪", "failed": "处理失败",
                "draft": "草稿", "submitted": "待审核", "approved": "审核通过", "rejected": "已驳回", "published": "已发布", "retired": "已停用"}


def form_request(request):
    return "form" in request.headers.get("content-type", "")


async def payload(request):
    if form_request(request):
        return dict(await request.form(max_fields=20, max_files=1, max_part_size=200000))
    try:
        data = await request.json()
    except ValueError:
        raise AppError(400, "INVALID_JSON", "请求不是有效JSON") from None
    if not isinstance(data, dict):
        raise AppError(422, "INVALID_REQUEST", "请求字段格式无效")
    return data


def string(data, key, default=""):
    value = data.get(key, default)
    if not isinstance(value, str):
        raise AppError(422, "INVALID_REQUEST", "请求文本字段格式无效")
    return value


def teaching(data):
    value = data.get("is_teaching", False)
    if type(value) is bool:
        return value
    if isinstance(value, str) and value in {"true", "on", "1", "false", "0", ""}:
        return value in {"true", "on", "1"}
    raise AppError(422, "INVALID_REQUEST", "示例资料确认字段格式无效")


def integer(value, required=False):
    if value is None or value == "":
        if not required:
            return None
    elif type(value) is int and 0 < value <= 9223372036854775807:
        return value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value) and int(value) > 0:
        return int(value)
    raise AppError(422, "INVALID_REQUEST", "请提供有效的正整数版本号或页码")


def timestamp(value, default=None):
    if value is None or value == "":
        return default
    try:
        if type(value) in (int, float):
            result = float(value)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            result = parsed.timestamp()
        else:
            raise ValueError()
        if not math.isfinite(result):
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError, OSError):
        raise AppError(422, "INVALID_REQUEST", "生效时间格式无效") from None


def context(request, identity, name, **extra):
    return {**workspace_context(identity),
            "module_name": name, "csrf_token": digest("staff-csrf:" + request.cookies.get("zw_staff_csrf", "")), "status_names": STATUS_NAMES,
            "matters": {m: n for m, n in MATTER_NAMES.items() if m != "*" and allows(identity["grants"], "knowledge.read", m)},
            "kinds": KINDS, "scenarios": SCENARIOS, "allows": lambda permission, matter=None: allows(identity["grants"], permission, matter), **extra}


def render(request, template, identity, name, **extra):
    return request.app.state.templates.TemplateResponse(request=request, name="staff/" + template, context=context(request, identity, name, **extra))


def result(request, data, location):
    return RedirectResponse(location, status_code=303) if form_request(request) else data


@router.get("/knowledge")
@router.get("/rules")
async def knowledge_list(request: Request):
    identity = await staff_identity(request)
    filters = dict(request.query_params)
    if request.url.path.endswith("/rules") and "kind" not in filters:
        filters["kind"] = "rule"
    versions = await request.app.state.knowledge_repo.list_versions(identity, filters)
    return render(request, "knowledge_list.html", identity, "规则与清单" if request.url.path.endswith("/rules") else "办事知识库", versions=versions, filters=filters)


@router.get("/knowledge/new")
async def new_upload_page(request: Request):
    identity = await staff_identity(request)
    if not allows(identity["grants"], "knowledge.manage"):
        raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位无权上传或维护知识")
    return render(request, "knowledge_upload.html", identity, "办事知识库")


@router.get("/knowledge/versions/{version_id}")
async def version_page(version_id: str, request: Request):
    identity = await staff_identity(request)
    repo = request.app.state.knowledge_repo
    version = await repo.get_version(identity, version_id)
    async with repo.engine.connect() as conn:
        chunks = (await conn.execute(text("SELECT * FROM knowledge_chunks WHERE version_id=:id ORDER BY chunk_no"), {"id": version_id})).mappings().all()
        jobs = (await conn.execute(text("SELECT * FROM knowledge_jobs WHERE version_id=:id ORDER BY created_at DESC"), {"id": version_id})).mappings().all()
        latest = (await conn.execute(text("SELECT MAX(version_no) FROM knowledge_versions WHERE document_id=:id"), {"id": version["document_id"]})).scalar()
    return render(request, "knowledge_detail.html", identity, "办事知识库", version=version, chunks=chunks, jobs=jobs,
                  pages=json.loads(version["pages_json"] or "[]"), latest=latest)


@router.get("/api/knowledge")
async def knowledge_api(request: Request):
    return {"items": await request.app.state.knowledge_repo.list_versions(await staff_identity(request), dict(request.query_params))}


@router.post("/api/knowledge/uploads")
async def upload(request: Request):
    identity = await staff_identity(request)
    if not allows(identity["grants"], "knowledge.manage"):
        raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位无权上传知识")
    await staff_csrf(request)
    data = await payload(request)
    file = data.get("file")
    if not file or not hasattr(file, "read"):
        raise AppError(422, "INVALID_DOCUMENT", "请选择Markdown或PDF文件")
    try:
        raw = await file.read(20 * 1024 * 1024 + 1)
    finally:
        await file.close()
    repo = request.app.state.knowledge_repo
    uploaded = await repo.upload(identity, title=string(data, "title"), matter_id=string(data, "matter_id"),
        scenario_id=string(data, "scenario_id"), kind=string(data, "kind", "guide"), filename=file.filename or "",
        source_uri=string(data, "source_uri"), rule_version=string(data, "rule_version"), raw=raw,
        visibility=string(data, "visibility") or None, is_teaching=teaching(data))
    request.app.state.knowledge_service.wakeup.set()
    return result(request, uploaded, "/staff/knowledge/versions/" + uploaded["version_id"])


@router.post("/api/rules")
async def create_rule(request: Request):
    identity = await staff_identity(request)
    await staff_csrf(request)
    data = await payload(request)
    matter = string(data, "matter_id")
    request.app.state.knowledge_repo.authorize(identity, "rule.edit", matter)
    content, kind = string(data, "content"), string(data, "kind")
    if not 1 <= len(content.strip()) <= 50000 or kind not in {"rule", "checklist"}:
        raise AppError(422, "INVALID_KNOWLEDGE", "请填写示例规则或清单正文，最多50000字符")
    uploaded = await request.app.state.knowledge_repo.upload(identity, title=string(data, "title"), matter_id=matter,
        scenario_id=string(data, "scenario_id"), kind=kind, filename="示例规则.md", source_uri=string(data, "source_uri"),
        rule_version=string(data, "rule_version"), raw=content.encode("utf-8"), is_teaching=teaching(data))
    request.app.state.knowledge_service.wakeup.set()
    return result(request, uploaded, "/staff/knowledge/versions/" + uploaded["version_id"])


@router.post("/api/knowledge/{document_id}/versions")
@router.post("/api/knowledge/{document_id}/versions/{version_id}/text")
async def save_version(document_id: str, request: Request, version_id: str | None = None):
    identity = await staff_identity(request)
    await staff_csrf(request)
    data = await payload(request)
    expected_latest = integer(data.get("expected_latest"), required=True)
    source_id = version_id or string(data, "base_version_id")
    source = await request.app.state.knowledge_repo.get_version(identity, source_id, "knowledge.manage")
    if source["document_id"] != document_id:
        raise AppError(404, "KNOWLEDGE_NOT_FOUND", "文档与版本不匹配")
    saved = await request.app.state.knowledge_repo.new_version(identity, source_id, content=string(data, "content"),
        rule_version=string(data, "rule_version"), page=integer(data.get("page")), expected_latest=expected_latest)
    request.app.state.knowledge_service.wakeup.set()
    return result(request, saved, "/staff/knowledge/versions/" + saved["version_id"])


@router.post("/api/knowledge/{document_id}/versions/{version_id}/reprocess")
async def reprocess(document_id: str, version_id: str, request: Request):
    identity = await staff_identity(request); await staff_csrf(request)
    repo = request.app.state.knowledge_repo
    source = await repo.get_version(identity, version_id, "knowledge.manage")
    if source["document_id"] != document_id:
        raise AppError(404, "KNOWLEDGE_NOT_FOUND", "文档与版本不匹配")
    data = await repo.retry(identity, version_id)
    request.app.state.knowledge_service.wakeup.set()
    return result(request, data, "/staff/knowledge/versions/" + version_id)


@router.post("/api/knowledge/{document_id}/archive")
async def archive(document_id: str, request: Request):
    identity = await staff_identity(request); await staff_csrf(request)
    return result(request, await request.app.state.knowledge_repo.archive(identity, document_id), "/staff/knowledge")


@router.get("/api/jobs/{job_id}")
async def job(job_id: str, request: Request):
    data = await request.app.state.knowledge_repo.job(await staff_identity(request), job_id)
    data.pop("lease_token", None)
    return data


@router.get("/reviews")
async def release_list(request: Request):
    identity = await staff_identity(request)
    if not allows(identity["grants"], "rule.release.read"):
        raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位无权查看审核发布")
    repo = request.app.state.knowledge_repo
    review_identity = {**identity, "grants": [grant for grant in identity["grants"] if allows([grant], "rule.release.read")]}
    return render(request, "releases.html", identity, "审核发布", releases=await repo.list_releases(review_identity), versions=await repo.list_versions(review_identity))


@router.get("/reviews/{release_id}")
async def release_detail(release_id: str, request: Request):
    identity = await staff_identity(request)
    if not allows(identity["grants"], "rule.release.read"):
        raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位无权查看审核发布")
    repo = request.app.state.knowledge_repo
    release = await repo.release(identity, release_id, "rule.release.read")
    async with repo.engine.connect() as conn:
        actions = (await conn.execute(text("SELECT * FROM knowledge_actions WHERE release_id=:id ORDER BY created_at"), {"id": release_id})).mappings().all()
    return render(request, "release_detail.html", identity, "审核发布", release=release, actions=actions)


@router.post("/api/releases")
async def create_release(request: Request):
    identity = await staff_identity(request); await staff_csrf(request)
    data = await payload(request)
    versions = data.get("version_ids", [])
    if form_request(request):
        versions = (await request.form()).getlist("version_ids")
    if not isinstance(versions, list) or any(not isinstance(value, str) or not value.strip() for value in versions):
        raise AppError(422, "INVALID_RELEASE", "版本ID必须为文本列表")
    saved = await request.app.state.knowledge_repo.create_release(identity, title=string(data, "title"), version_ids=versions,
        effective_from=timestamp(data.get("effective_from"), time.time()), effective_to=timestamp(data.get("effective_to")))
    return result(request, saved, "/staff/reviews/" + saved["id"])


@router.post("/api/releases/{release_id}/{action}")
async def release_action(release_id: str, action: str, request: Request):
    identity = await staff_identity(request); await staff_csrf(request)
    data = await payload(request)
    saved = await request.app.state.knowledge_repo.release_action(identity, release_id, action, string(data, "comment"), integer(data.get("expected_revision"), required=True))
    return result(request, saved, "/staff/reviews/" + release_id)


@router.get("/feedback")
async def feedback_page(request: Request):
    identity = await staff_identity(request)
    repo = request.app.state.knowledge_repo
    versions = await repo.list_versions(identity)
    scopes = [g["matter_id"] for g in identity["grants"] if allows([g], "knowledge.feedback") or allows([g], "feedback.manage")]
    if not scopes:
        raise AppError(403, "STAFF_PERMISSION_DENIED", "无规则反馈权限")
    from sqlalchemy import bindparam
    query = text("SELECT * FROM knowledge_feedback WHERE (:all=1 OR matter_id IN :scopes) ORDER BY created_at DESC LIMIT 100").bindparams(bindparam("scopes", expanding=True))
    async with repo.engine.connect() as conn:
        items = (await conn.execute(query, {"all": int("*" in scopes), "scopes": scopes})).mappings().all()
    return render(request, "feedback.html", identity, "规则问题反馈", items=items, versions=versions)


@router.post("/api/feedback")
async def feedback_create(request: Request):
    identity = await staff_identity(request); await staff_csrf(request)
    data = await payload(request)
    repo = request.app.state.knowledge_repo
    matter, content = string(data, "matter_id"), string(data, "content")
    repo.authorize(identity, "knowledge.feedback", matter)
    if matter not in SCENARIOS or not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000:
        raise AppError(422, "INVALID_REQUEST", "请填写事项和1至2000字的问题描述")
    version_id = string(data, "version_id") or None
    if version_id:
        version = await repo.get_version(identity, version_id)
        if version["matter_id"] != matter:
            raise AppError(422, "INVALID_REQUEST", "反馈事项与版本不匹配")
    identifier = str(uuid4())
    async with repo.engine.begin() as conn:
        await conn.execute(text("INSERT INTO knowledge_feedback(id,matter_id,version_id,content,created_by,created_at) VALUES(:id,:matter,:version,:content,:actor,:now)"),
            {"id": identifier, "matter": matter, "version": version_id, "content": content.strip(), "actor": identity["user_id"], "now": time.time()})
        await repo._log(conn, identity, "knowledge_feedback_created", identifier)
    return result(request, {"id": identifier}, "/staff/feedback")


@router.post("/api/feedback/{feedback_id}/resolve")
async def feedback_resolve(feedback_id: str, request: Request):
    identity = await staff_identity(request); await staff_csrf(request)
    data = await payload(request)
    repo = request.app.state.knowledge_repo
    comment = data.get("resolution", "")
    if not isinstance(comment, str) or not 1 <= len(comment.strip()) <= 2000:
        raise AppError(422, "INVALID_REQUEST", "请填写处理说明")
    async with repo.engine.begin() as conn:
        row = (await conn.execute(text("SELECT * FROM knowledge_feedback WHERE id=:id"), {"id": feedback_id})).mappings().first()
        if not row or not allows(identity["grants"], "feedback.manage", row["matter_id"]):
            raise AppError(404, "FEEDBACK_NOT_FOUND", "反馈不存在或无处理权限")
        changed = await conn.execute(text("UPDATE knowledge_feedback SET resolution=:resolution,resolved_by=:actor,resolved_at=:now WHERE id=:id AND resolved_at IS NULL"),
            {"resolution": comment.strip(), "actor": identity["user_id"], "now": time.time(), "id": feedback_id})
        if changed.rowcount != 1:
            raise AppError(409, "FEEDBACK_CONFLICT", "该反馈已处理")
        await repo._log(conn, identity, "knowledge_feedback_resolved", feedback_id)
    return result(request, {"resolved": True}, "/staff/feedback")
