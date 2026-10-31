"""仅供浏览器自动验证：显式替换外部模型，不作为产品运行入口。"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import sys

import httpx
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zhengwu_precheck.core.guide_rag import ApiConfig
from zhengwu_precheck.core.services.guide_qa import GuideQA
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings
from test_chat import FakeQA

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".venv" / "browser-results"
REQUESTS = OUTPUT / "content-model-requests.jsonl"
FIELD_QUOTE = "教学蓝色回执包含示例编号和提交月份两个字段。"
PDF_QUOTE = "教学人工补充页要求填写示例颜色和示例数量。"


class BrowserQA(GuideQA):
    """旧回归委派原替身；托管知识走真实检索与引用校验，仅HTTP为替身。"""
    def __init__(self, settings):
        super().__init__(settings)
        self.legacy = FakeQA()

    def config(self, prefix):
        return ApiConfig("openai-compatible", "browser-fixture-" + prefix.lower(),
                         "http://127.0.0.1:9999/v1", "BROWSER_UNUSED_FIXTURE_KEY", 45)

    def ensure_ready(self, snapshot=None):
        if snapshot is None or not snapshot.get("managed"):
            self.legacy.ensure_ready()
        else:
            super().ensure_ready(snapshot=snapshot)

    async def answer(self, question, history, matter_id, progress=None, snapshot=None):
        if (snapshot is None or not snapshot.get("managed")) and matter_id != "residence-permit":
            return await super().answer(question, history, matter_id, progress=progress, snapshot=snapshot)
        if snapshot is None or not snapshot.get("managed"):
            # 留出观察检索状态的时间，保持取消、超时与旧火星拒答回归。
            await asyncio.sleep(0.5 if "慢回答" not in question else 30)
            if "模拟超时" in question:
                raise TimeoutError()
            if "火星" in question:
                return {"outcome": "insufficient_evidence", "text": "现有办事指南中未找到相关依据", "citations": []}
            return await self.legacy.answer(question, history, matter_id, progress)
        await asyncio.sleep(6 if "冻结模板" in question else 0.5)
        return await super().answer(question, history, matter_id, progress=progress, snapshot=snapshot)


def model_fixture(request):
    """只接纳明确测试地址与协议，禁止回退外网或机器上的业务配置。"""
    assert request.url.host == "127.0.0.1" and request.url.port == 9999
    body = json.loads(request.content)
    assert body["model"].startswith("browser-fixture-")
    if request.url.path == "/v1/embeddings":
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, 0.1, 0.01]}
                                                  for i in range(len(body["input"]))]})
    assert request.url.path == "/v1/chat/completions"
    context = json.loads(body["messages"][1]["content"])
    with REQUESTS.open("a", encoding="utf-8") as log:
        log.write(json.dumps(body, ensure_ascii=False) + "\n")
    question = context["question"]
    if "模型故障" in question:
        raise httpx.ReadTimeout("仅测试的模型超时", request=request)
    sources = context["teaching_sources"]
    preferred = PDF_QUOTE if "人工补充" in question else FIELD_QUOTE
    source = next((item for item in sources if preferred in item["text"]), sources[0])
    quote = preferred if preferred in source["text"] else next(iter(re.findall(r"[^。！？\n]+[。！？]", source["text"])), source["text"])
    if "无效引用" in question:
        quote = "这一句从未出现在教学原文中。"
    raw = json.dumps({"supported": True, "citations": [{"chunk_id": source["chunk_id"], "quote": quote}]}, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})


settings = Settings(staff_jwt_secret=os.getenv("BROWSER_STAFF_TEST_SECRET"),
                    guide_path=ROOT / "data" / "居住证申领_教学指南.md")
app = create_app(settings, qa=BrowserQA(settings))
original_lifespan = app.router.lifespan_context


async def grant_content_records(application):
    """仅测试lifespan为独立虚构办事人的新会话配置显式记录授权。"""
    granted = set()
    while True:
        async with application.state.repo.engine.connect() as conn:
            records = (await conn.execute(text("SELECT c.id FROM conversations c JOIN users u ON u.id=c.user_id WHERE u.username='browser-content'"))).scalars().all()
        for identifier in records:
            if identifier not in granted:
                await application.state.staff_repo.grant_record("staff-supervisor", identifier, "body", actor="browser-test-fixture")
                granted.add(identifier)
        await asyncio.sleep(0.1)


@asynccontextmanager
async def test_lifespan(application):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    REQUESTS.write_text("", encoding="utf-8")
    await application.state.qa.client.aclose()
    application.state.qa.client = httpx.AsyncClient(transport=httpx.MockTransport(model_fixture), trust_env=False)
    async with original_lifespan(application):
        for username in ("browser-test", "browser-content", "browser-content-ungranted", "browser-tickets", "browser-tickets-other"):
            await application.state.repo.create_user(username, "browser-only-test-password")
        for role in ("window", "supervisor", "engineer", "hotline", "owner", "reviewer", "director"):
            username = "staff-" + role
            await application.state.repo.create_user(username, "browser-staff-test-password")
            scopes = ("residence-permit", "sole-trader-registration") if role in {"owner", "supervisor"} else ("*",) if role == "engineer" else ("residence-permit",)
            for scope in scopes:
                await application.state.staff_repo.grant_role(username, role, scope, actor="browser-test-fixture")
        records = asyncio.create_task(grant_content_records(application))
        try:
            yield
        finally:
            records.cancel()
            await asyncio.gather(records, return_exceptions=True)


app.router.lifespan_context = test_lifespan

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("BROWSER_TEST_PORT", "8765")))
