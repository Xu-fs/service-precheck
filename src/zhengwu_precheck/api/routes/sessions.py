"""会话读取、事项确认及最小独立账号登录。"""
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from zhengwu_precheck.api.dependencies import csrf, principal
from zhengwu_precheck.api.schemas import LoginInput, MatterInput
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.chat import MATTERS
from zhengwu_precheck.utils.security import digest

router = APIRouter()


def visitor_cookie(request, response):
    token = request.cookies.get("zw_anon", "")
    if not 32 <= len(token) <= 128:
        token = secrets.token_urlsafe(32)
        response.set_cookie("zw_anon", token, httponly=True, secure=request.app.state.settings.secure_cookies,
                            samesite="lax", max_age=30 * 86400, path="/")
    return token


@router.get("/bootstrap")
async def bootstrap(request: Request, response: Response):
    token = visitor_cookie(request, response)
    user = None
    if auth := request.cookies.get("zw_auth"):
        user = await request.app.state.repo.authenticate(auth)
        if not user:
            raise AppError(401, "AUTH_EXPIRED", "登录已失效，请退出后重新登录")
    return {"csrf_token": digest("csrf:" + token), "identity": "user" if user else "anonymous",
            "username": user["username"] if user else None, "matters": MATTERS}


@router.get("/sessions/{session_id}/messages")
async def messages(session_id: UUID, request: Request, who=Depends(principal),
                   before: UUID | None = None, limit: int = Query(40, ge=1, le=100)):
    return await request.app.state.repo.list_messages(str(session_id), who, str(before) if before else None, limit)


@router.post("/sessions/{session_id}/matter", dependencies=[Depends(csrf)])
async def confirm(session_id: UUID, body: MatterInput, request: Request, who=Depends(principal)):
    if not any(item["matter_id"] == body.matter_id and item["scenario_id"] == body.scenario_id for item in MATTERS):
        raise AppError(422, "INVALID_MATTER", "请选择有效的事项及教学情形")
    await request.app.state.repo.confirm(str(session_id), who, body.matter_id, body.scenario_id, body.confirmation_token)
    return {"matter_id": body.matter_id, "scenario_id": body.scenario_id, "confirmed": True}


@router.post("/auth/login", dependencies=[Depends(csrf)])
async def login(body: LoginInput, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    request.app.state.limiter.check("login:" + ip, 5, 60)
    token, username = await request.app.state.repo.login(body.username, body.password)
    response.set_cookie("zw_auth", token, httponly=True, secure=request.app.state.settings.secure_cookies,
                        samesite="lax", max_age=8 * 3600, path="/")
    return {"username": username, "identity": "user"}


@router.post("/auth/logout", dependencies=[Depends(csrf)])
async def logout(request: Request, response: Response):
    if token := request.cookies.get("zw_auth"):
        await request.app.state.repo.logout(token)
    response.delete_cookie("zw_auth", path="/")
    return {"identity": "anonymous"}
