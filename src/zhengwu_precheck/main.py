"""FastAPI装配入口；默认仅绑定本机，不预置账号和模型密钥。"""
import argparse
from contextlib import asynccontextmanager
import os
import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from zhengwu_precheck.api.dependencies import RateLimiter
from zhengwu_precheck.api.routes import chat, sessions, staff, knowledge, prompts, audits, tickets
from zhengwu_precheck.api.routes import agent
from zhengwu_precheck.api.routes import materials
from zhengwu_precheck.api.routes import operations
from zhengwu_precheck.infrastructure.request_limiter import SharedRateLimiter
from zhengwu_precheck.infrastructure.agent_repository import AgentRepository
from zhengwu_precheck.core.services.agent import AgentService
from zhengwu_precheck.infrastructure.material_repository import MaterialRepository
from zhengwu_precheck.infrastructure.quota_repository import QuotaRepository, QuotaLimits
from zhengwu_precheck.core.services.model_budget import ModelBudget
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.guide_qa import GuideQA
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.core.services.staff_auth import StaffAuth
from zhengwu_precheck.infrastructure.prompt_repository import PromptRepository
from zhengwu_precheck.infrastructure.audit_repository import AuditRepository
from zhengwu_precheck.infrastructure.knowledge_repository import KnowledgeRepository
from zhengwu_precheck.core.services.knowledge import KnowledgeService
from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository
from zhengwu_precheck.utils.body_limit import BodyLimitMiddleware, body_limit
from zhengwu_precheck.settings import Settings
from zhengwu_precheck.utils.security import digest

PACKAGE = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None, *, qa=None) -> FastAPI:
    settings = settings or Settings()
    settings.validate()
    repo = ChatRepository(settings.runtime_dir / "chat.sqlite3", settings.lease_seconds)
    service = qa if qa is not None else GuideQA(settings)
    staff_repo = StaffRepository(repo.engine)
    prompt_repo = PromptRepository(repo.engine)
    audit_repo = AuditRepository(repo.engine)
    knowledge_repo = KnowledgeRepository(repo.engine, settings.runtime_dir)
    knowledge_service = KnowledgeService(knowledge_repo, service, settings)
    ticket_repo = TicketRepository(repo.engine)
    agent_repo = AgentRepository(repo.engine)
    material_repo = MaterialRepository(repo.engine,settings.runtime_dir/'private-materials',
        processor={'trusted_teaching':True} if settings.trusted_teaching_materials else None)
    quota_repo = QuotaRepository(repo.engine)
    shared_limiter = SharedRateLimiter(repo.engine)
    service.budget = ModelBudget(quota_repo,QuotaLimits(settings.model_daily_requests,settings.model_daily_tokens,
        settings.model_concurrent,lease_seconds=max(180,int(settings.request_timeout)+30)))
    service.knowledge_repo = knowledge_repo
    service.knowledge_service = knowledge_service

    @asynccontextmanager
    async def lifespan(app):
        await repo.initialize()
        await staff_repo.initialize()
        await prompt_repo.initialize()
        await knowledge_repo.initialize()
        await audit_repo.initialize()
        await ticket_repo.initialize()
        await agent_repo.initialize()
        await material_repo.initialize()
        await quota_repo.initialize()
        await shared_limiter.initialize()
        import asyncio
        worker = asyncio.create_task(knowledge_service.run())
        ticket_worker = asyncio.create_task(ticket_repo.run())
        try:
            yield
        finally:
            worker.cancel()
            ticket_repo.stop()
            await asyncio.gather(worker, ticket_worker, return_exceptions=True)
            await service.close()
            await repo.close()

    app = FastAPI(title="政务易办教学问答", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.repo, app.state.qa, app.state.settings = repo, service, settings
    app.state.staff_repo, app.state.staff_auth = staff_repo, StaffAuth(staff_repo, settings.staff_jwt_secret)
    app.state.prompt_repo, app.state.audit_repo = prompt_repo, audit_repo
    app.state.knowledge_repo, app.state.knowledge_service = knowledge_repo, knowledge_service
    app.state.ticket_repo = ticket_repo
    app.state.agent_repo = agent_repo
    app.state.agent_service = AgentService(app)
    ticket_repo.validate_current_precheck = app.state.agent_service.validate_precheck
    app.state.material_repo = material_repo
    app.state.quota_repo = quota_repo
    app.state.shared_limiter = shared_limiter
    app.add_middleware(BodyLimitMiddleware)
    app.state.limiter = RateLimiter()
    hosts = os.getenv("ZHENGWU_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",")
    allowed_hosts = {host.strip().strip("[]").lower() for host in hosts}

    def error_response(request, error):
        return JSONResponse(status_code=error.status, content={"error": {"code": error.code, "message": error.message,
            "request_id": getattr(request.state, "request_id", str(uuid4())), "retryable": error.retryable}})

    @app.middleware("http")
    async def response_policy(request, call_next):
        request.state.request_id = str(uuid4())
        if (request.url.hostname or "").lower() not in allowed_hosts:
            response = error_response(request, AppError(400, "INVALID_HOST", "访问地址未获允许，请使用配置的服务地址"))
        elif request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > body_limit(request.url.path):
            response = error_response(request, AppError(413, "REQUEST_TOO_LARGE", "请求内容过长，请缩短问题"))
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(request, error):
        return error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        malformed = any(item["type"] == "json_invalid" for item in error.errors())
        is_chat = request.url.path == "/chat"
        return error_response(request, AppError(400 if malformed else 422, "INVALID_JSON" if malformed else ("INVALID_QUESTION" if is_chat else "INVALID_REQUEST"),
                                              "请求内容不是有效JSON" if malformed else ("请求字段格式不正确，问题应为1至2000个字符" if is_chat else "请求字段格式不正确，请检查提交内容")))

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return error_response(request, AppError(error.status_code, "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR",
                                              "该功能不存在或暂未开放" if error.status_code == 404 else "请求方式或内容不正确"))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return error_response(request, AppError(503, "STORAGE_UNAVAILABLE", "记录服务暂时不可用，请稍后重试", True))

    templates = Jinja2Templates(directory=str(PACKAGE / "templates"))
    app.state.templates = templates
    @app.get('/health')
    async def health():
        from sqlalchemy import text
        async with repo.engine.connect() as connection:
            await connection.execute(text('SELECT 1'))
        return {'status':'ok','scope':'local_application','teaching_only':True}
    @app.get("/")
    async def home(request: Request):
        token = request.cookies.get("zw_anon", "")
        rotate = not 32 <= len(token) <= 128
        if rotate:
            token = secrets.token_urlsafe(32)
        # 失效登录时仍可使用页面内令牌安全退出；不把访问凭证本身交给JS。
        response = templates.TemplateResponse(request=request, name="chat.html", context={"csrf_token": digest("csrf:" + token)})
        if rotate:
            response.set_cookie("zw_anon", token, httponly=True, secure=settings.secure_cookies,
                                samesite="lax", max_age=30 * 86400, path="/")
        return response

    app.mount("/static", StaticFiles(directory=str(PACKAGE / "static"), check_dir=False), name="static")
    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(knowledge.router)
    app.include_router(prompts.router)
    app.include_router(audits.router)
    app.include_router(tickets.router)
    app.include_router(agent.router)
    app.include_router(materials.router)
    app.include_router(operations.router)
    app.include_router(staff.router)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="政务易办教学问答网页")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="只检查依赖导入，不启动服务")
    args = parser.parse_args(argv)
    if args.check:
        print("问答网页依赖已就绪；模型配置及真实效果需另行验证。")
        return
    import uvicorn
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
