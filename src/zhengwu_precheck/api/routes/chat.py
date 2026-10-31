"""POST流式聊天：头发送前做身份/配置检查，流内只发SSE错误。"""
import asyncio
import json
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
import httpx

from zhengwu_precheck.api.dependencies import csrf, principal
from zhengwu_precheck.api.schemas import ChatInput
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.services.chat import MATTERS, fallback, needs_confirmation
from zhengwu_precheck.utils.security import digest
from zhengwu_precheck.utils.sse import encode_event

router = APIRouter()


@router.post("/chat", dependencies=[Depends(csrf)])
async def chat(body: ChatInput, request: Request, who=Depends(principal)):
    repo, qa, settings = request.app.state.repo, request.app.state.qa, request.app.state.settings
    try:
        key = str(UUID(request.headers.get("Idempotency-Key", "")))
    except ValueError:
        raise AppError(400, "INVALID_IDEMPOTENCY_KEY", "请提供有效的请求标识") from None
    ip = request.client.host if request.client else "unknown"
    await request.app.state.shared_limiter.check("chat-ip:" + ip, 120, 600)
    await request.app.state.shared_limiter.check("chat-owner:" + who["key"])
    sid = str(body.session_id) if body.session_id else None
    conversation = await repo.conversation(sid, who) if sid else None
    existing = await repo.existing(who, key)
    replay, snapshot = None, None
    if existing:
        if existing["question_hash"] != digest(body.question) or (sid and sid != existing["conversation_id"]):
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "同一请求标识不能用于不同问题或会话")
        await repo.conversation(existing["conversation_id"], who)
        if existing["status"] == "complete":
            replay = json.loads(existing["result_json"])
            turn = {"session_id": existing["conversation_id"], "request_id": existing["request_id"],
                    "message_id": existing["id"], "user_message_id": existing["reply_to_message_id"]}
        else:
            raise AppError(409, "REQUEST_IN_PROGRESS" if existing["status"] == "pending" else "REQUEST_RETRY_REQUIRED",
                           "请求处理中，请查询消息状态" if existing["status"] == "pending" else "上次请求未完成，请使用新的请求标识重试", True)
    else:
        confirm = needs_confirmation(conversation, body.question)
        knowledge_repo = request.app.state.knowledge_repo
        if not confirm:
            # 流前仅检查当前就绪性；真正依据仍在接纳写事务中重新读取并冻结。
            async with repo.engine.connect() as conn:
                preview = await knowledge_repo.capture(conn, conversation["matter_id"], conversation["scenario_id"])
            if preview["managed"] or conversation["matter_id"] == "residence-permit":
                qa.ensure_ready(preview)
        turn = await repo.reserve(sid, body.question, key, who, expected_conversation=conversation,
                                  context_factory=None if confirm else knowledge_repo.capture)
        snapshot = turn.pop("_snapshot", None)  # 绝不经**turn把内部模板正文送往浏览器。

    async def stream():
        seq, task = 0, None
        def emit(name, data):
            nonlocal seq
            seq += 1
            return encode_event(name, {**turn, "seq": seq, **data})
        try:
            yield emit("session", {"replayed": replay is not None})
            if replay is None:
                queue = asyncio.Queue()
                async def progress(stage, text):
                    await queue.put(("status", {"stage": stage, "text": text}))
                async def produce():
                    from zhengwu_precheck.core.services.model_budget import model_principal
                    model_principal.set(who['key'])
                    try:
                        if confirm:
                            token = await repo.set_confirmation(turn)
                            result = {"outcome": "needs_matter_confirmation", "text": "请先确认你要咨询的事项和教学情形。",
                                      "options": MATTERS, "confirmation_token": token, "citations": []}
                        elif conversation["matter_id"] != "residence-permit" and not snapshot["managed"]:
                            result = fallback(settings.human_service)
                        else:
                            await progress("retrieving", "正在检索办事指南……")
                            history = await repo.history(turn["session_id"], conversation["matter_id"])
                            result = await asyncio.wait_for(qa.answer(body.question, history, conversation["matter_id"], progress=progress, snapshot=snapshot), settings.request_timeout)
                            if result["outcome"] == "insufficient_evidence":
                                reason = result.get("reason", "insufficient_evidence")
                                text = result["text"]
                                result = fallback(settings.human_service)
                                result.update(reason=reason, text=text)
                        if snapshot:
                            result.update(prompt_version_id=snapshot["prompt"]["id"], prompt_version=snapshot["prompt"]["version"],
                                          release_ids=snapshot["release_ids"], knowledge_version_ids=snapshot["version_ids"])
                        await queue.put(("result", result))
                    except Exception as exc:
                        await queue.put(("failure", exc))
                task = asyncio.create_task(produce())
                while True:
                    try:
                        kind, value = await asyncio.wait_for(queue.get(), settings.heartbeat_seconds)
                    except TimeoutError:
                        if not await repo.heartbeat(turn):
                            raise AppError(409, "REQUEST_INTERRUPTED", "请求已中断，请重新提问", True)
                        yield ": ping\n\n"
                        continue
                    if kind == "status":
                        yield emit("status", value)
                    elif kind == "failure":
                        raise value
                    else:
                        result = value
                        break
                # 完整结果与引用先落库；浏览器断线后仍可恢复已经校验的完整答案。
                await repo.finish(turn, result)
            else:
                result = replay
            if result["outcome"] == "needs_matter_confirmation":
                yield emit("matter_required", {k: result[k] for k in ("text", "options", "confirmation_token")})
            elif result["outcome"] == "insufficient_evidence":
                yield emit("fallback", {k: result[k] for k in ("text", "reason", "actions")})
            else:
                for offset in range(0, len(result["text"]), 18):
                    yield emit("answer_delta", {"text": result["text"][offset:offset + 18]})
                    await asyncio.sleep(0)
                yield emit("citations", {"items": result.get("citations", [])})
            yield emit("done", {"outcome": result["outcome"], "persisted": True})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error, (TimeoutError, httpx.TimeoutException)):
                error = AppError(504, "MODEL_TIMEOUT", "回答服务暂时超时，请稍后重试", True)
            elif not isinstance(error, AppError):
                error = AppError(500, "CHAT_FAILED", "回答暂时未能完成，请稍后重试", True)
            with anyio.CancelScope(shield=True):
                await repo.fail(turn, error.code)
            yield emit("error", {"code": error.code, "message": error.message + '；可进入人工咨询继续处理。', "retryable": error.retryable})
        finally:
            if task and not task.done():
                task.cancel()
            # 只更改pending消息，已经提交的完整答案不会因传输中断而被撤销。
            with anyio.CancelScope(shield=True):
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                await repo.fail(turn, "CLIENT_DISCONNECTED", cancelled=True)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
