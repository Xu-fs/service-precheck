"""第二三批真实浏览器回归：后台禁JS原生表单，办事人正常JS，HTTP模型显式替身。"""
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import time

from playwright.sync_api import sync_playwright, expect
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".venv" / "browser-results"
URL = "http://127.0.0.1:" + os.getenv("BROWSER_TEST_PORT", "8765")
QUOTE = "教学蓝色回执包含示例编号和提交月份两个字段。"
PDF_QUOTE = "教学人工补充页要求填写示例颜色和示例数量。"
NO_EVIDENCE = "现有办事指南中未找到相关依据"
FILENAME = "浏览器教学指南.md"
VERSION = "BROWSER-DEMO-2026-01"
MARKER_A = "浏览器模板标记甲"
MARKER_B = "浏览器模板标记乙"


def staff_login(page, role):
    page.goto(URL + "/staff/login")
    page.get_by_label("工作人员账号").fill("staff-" + role)
    page.get_by_label("密码", exact=True).fill("browser-staff-test-password")
    page.get_by_role("button", name="登录后台", exact=True).click()
    expect(page.get_by_role("heading", name="我的工作台", exact=True)).to_be_visible()


def citizen_login(page, username="browser-content"):
    page.goto(URL)
    page.get_by_role("button", name="登录", exact=True).click()
    page.get_by_label("账号", exact=True).fill(username)
    page.get_by_label("密码", exact=True).fill("browser-only-test-password")
    page.locator("#login-form").get_by_role("button", name="登录", exact=True).click()
    expect(page.locator("#identity")).to_have_text("已登录：" + username)


def upload(page, *, title, version, filename, buffer, matter="residence-permit", mime="text/markdown"):
    page.goto(URL + "/staff/knowledge/new")
    form = page.locator('form[action="/staff/api/knowledge/uploads"]')
    form.locator('[name="title"]').fill(title)
    form.locator('[name="matter_id"]').select_option(matter)
    form.locator('[name="scenario_id"]').select_option("rental-demo" if matter == "residence-permit" else "standard-demo")
    form.locator('[name="rule_version"]').fill(version)
    form.locator('[name="source_uri"]').fill("浏览器回归自编虚构教学资料")
    form.locator('[name="is_teaching"]').check()
    form.locator('[name="file"]').set_input_files({"name": filename, "mimeType": mime, "buffer": buffer})
    form.get_by_role("button", name="上传并处理").click()
    expect(page).to_have_url(URL + "/staff/knowledge/versions/" + page.url.rsplit("/", 1)[-1])
    assert "/staff/knowledge/versions/" in page.url
    return page.url.rsplit("/", 1)[-1]


def processing(page, label):
    # 在禁JS页面使用真正的刷新链接观察数据库任务；不把API替身当作处理结果。
    deadline = time.monotonic() + 30
    status = page.locator(".panel").first.locator("strong")
    while time.monotonic() < deadline:
        if status.inner_text() == label:
            return
        assert status.inner_text() != "处理失败", page.locator("body").inner_text()
        page.get_by_role("link", name="刷新处理状态", exact=True).click()
    raise AssertionError("知识处理未达到预期状态：" + label)


def draft_text(page, version, content, page_number=None):
    form = page.locator('form[action$="/text"]')
    form.locator('[name="rule_version"]').fill(version)
    form.locator('[name="content"]').fill(content)
    if page_number is not None:
        form.locator('[name="page"]').fill(str(page_number))
    form.get_by_role("button", name="保存为新草稿并处理").click()
    return page.url.rsplit("/", 1)[-1]


def release_action(page, action, comment):
    form = page.locator('form[action$="/' + action + '"]')
    form.locator('[name="comment"]').fill(comment)
    form.get_by_role("button").click()


def ask(page, question, *, expected=QUOTE, confirm=False, matter_name="居住证申领"):
    page.locator("#question").fill(question)
    page.get_by_role("button", name="发送问题").click()
    if confirm:
        page.get_by_role("button", name=matter_name, exact=False).click()
    expect(page.locator(".message-content").last).to_have_text(expected, timeout=15000)
    expect(page.locator("#send")).to_be_enabled(timeout=15000)


def save_prompt(page, content, comment):
    page.goto(URL + "/staff/prompts?matter_id=residence-permit")
    page.get_by_label("系统提示词（最多2000字）").fill(content)
    page.get_by_label("修改说明", exact=True).fill(comment)
    page.get_by_role("button", name="保存并生效新版本", exact=True).click()
    expect(page.get_by_label("系统提示词（最多2000字）")).to_have_value(content)


def captured_prompt(question):
    requests = [json.loads(line) for line in (OUTPUT / "content-model-requests.jsonl").read_text(encoding="utf-8").splitlines()]
    matches = [body for body in requests if json.loads(body["messages"][1]["content"])["question"] == question]
    assert len(matches) == 1, (question, len(matches))
    return matches[0]["messages"][0]["content"]


def csrf(page):
    return page.locator('input[name="csrf_token"]').first.get_attribute("value")


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    contexts = []
    errors = []
    passed = []
    try:
        owner_context = browser.new_context(java_script_enabled=False, viewport={"width": 1360, "height": 980})
        supervisor_context = browser.new_context(java_script_enabled=False, viewport={"width": 1360, "height": 980})
        citizen_context = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
        contexts.extend([owner_context, supervisor_context, citizen_context])
        owner = owner_context.new_page(); staff_login(owner, "owner")
        supervisor = supervisor_context.new_page(); staff_login(supervisor, "supervisor")
        citizen = citizen_context.new_page(); citizen.on("pageerror", lambda error: errors.append(str(error)))
        citizen_login(citizen)

        version_id = upload(owner, title="浏览器蓝色回执教学指南", version=VERSION, filename=FILENAME,
                            buffer=("# 仅供浏览器回归的虚构教学资料\n\n" + QUOTE).encode("utf-8"))
        processing(owner, "索引就绪")
        owner.screenshot(path=str(OUTPUT / "content-upload-ready.png"), full_page=True)
        passed.append("负责人禁JS上传Markdown并真实建立FAISS索引")
        other_version = upload(owner, title="第二事项独立教学资料", version="BROWSER-SOLE-01", filename="第二事项教学.md",
                               buffer="第二事项教学清单仅要求示例经营类别。".encode("utf-8"), matter="sole-trader-registration")
        processing(owner, "索引就绪")
        ask(citizen, "第二事项教学清单要求什么？", expected=NO_EVIDENCE, confirm=True, matter_name="个体工商户设立登记")
        passed.append("第二事项索引ready但未发布的内容不能用于公开问答")
        owner.goto(URL + "/staff/reviews")
        form = owner.locator('form[action="/staff/api/releases"]')
        form.locator('[name="title"]').fill("浏览器蓝色回执正式教学版本包")
        form.locator('[name="version_ids"][value="' + version_id + '"]').check()
        form.get_by_role("button", name="创建待提交版本包").click()
        release_url = owner.url
        release_id = release_url.rsplit("/", 1)[-1]
        release_action(owner, "submit", "负责人提交虚构教学知识")
        expect(owner.locator(".panel").first).to_contain_text("待审核")
        supervisor.goto(release_url)
        release_action(supervisor, "approve", "监督员独立核对原文与教学范围")
        expect(supervisor.locator(".panel").first).to_contain_text("审核通过")
        release_action(supervisor, "publish", "发布供浏览器回归使用的教学版本")
        expect(supervisor.locator(".panel").first).to_contain_text("已发布")
        supervisor.screenshot(path=str(OUTPUT / "content-release-published.png"), full_page=True)
        stale = supervisor_context.request.post(URL + "/staff/api/releases/" + release_id + "/retire",
            headers={"X-CSRF-Token": csrf(supervisor)}, data={"comment": "过期页面提交", "expected_revision": 1})
        assert stale.status == 409, stale.text()
        passed.extend(["负责人提交与监督员独立approve/publish原生表单", "发布状态过期revision拒绝409"])

        owner.goto(URL + "/staff/reviews")
        future_form = owner.locator('form[action="/staff/api/releases"]')
        future_form.locator('[name="title"]').fill("第二事项未来生效教学版本包")
        future_form.locator('[name="effective_from"]').fill((datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))
        future_form.locator('[name="version_ids"][value="' + other_version + '"]').check()
        future_form.get_by_role("button", name="创建待提交版本包").click()
        future_url = owner.url
        release_action(owner, "submit", "提交未来生效教学版本")
        supervisor.goto(future_url)
        release_action(supervisor, "approve", "复核第二事项未来教学版本")
        release_action(supervisor, "publish", "发布但在明日生效")
        expect(supervisor.locator(".panel").first).to_contain_text("已发布")
        ask(citizen, "未来包发布后第二事项教学清单要求什么？", expected=NO_EVIDENCE)
        passed.append("第二事项未来包SSR审核发布但未生效不进入检索")
        citizen.get_by_role("button", name="新建咨询", exact=True).click()
        ask(citizen, "发布后教学蓝色回执有哪些字段？", confirm=True)
        citizen.get_by_text("查看引用来源（1）").last.click()
        expect(citizen.locator(".citation").last).to_contain_text(FILENAME)
        expect(citizen.locator(".citation").last).to_contain_text(VERSION)
        citizen.screenshot(path=str(OUTPUT / "content-citizen-citation.png"), full_page=True)
        session_id = citizen.evaluate("localStorage.getItem('zhengwu.chat.user:browser-content')")
        messages = citizen_context.request.get(URL + "/sessions/" + session_id + "/messages").json()["items"]
        original_answer = next(item for item in reversed(messages) if item["outcome"] == "answered")
        record_id = "message:" + original_answer["id"]
        assert original_answer["citations"][0]["filename"] == FILENAME
        assert original_answer["citations"][0]["rule_version"] == VERSION
        passed.append("办事人真实检索与引用校验返回上传文件及发布版本")

        owner.goto(URL + "/staff/knowledge/versions/" + version_id)
        edited_id = draft_text(owner, "BROWSER-DEMO-2026-02", "教学蓝色回执的新草稿只包含示例日期。")
        processing(owner, "索引就绪")
        assert edited_id != version_id
        owner.goto(URL + "/staff/knowledge/versions/" + version_id)
        owner.get_by_text("Markdown正文", exact=True).click()
        expect(owner.locator("pre").first).to_contain_text(QUOTE)
        ask(citizen, "草稿编辑后教学蓝色回执有哪些字段？")
        citizen.reload(); citizen.wait_for_load_state("networkidle")
        expect(citizen.locator(".message-content").last).to_have_text(QUOTE)
        persisted = citizen_context.request.get(URL + "/sessions/" + session_id + "/messages").json()["items"]
        unchanged = next(item for item in persisted if item["id"] == original_answer["id"])
        assert unchanged["citations"] == original_answer["citations"]
        passed.append("新草稿不覆盖已发布正文或历史引用且刷新可恢复")

        # 空白PDF用内存生成，逐页人工补充；不进行OCR或向外部发送材料。
        writer = PdfWriter(); writer.add_blank_page(width=300, height=300); writer.add_blank_page(width=300, height=300)
        pdf = io.BytesIO(); writer.write(pdf)
        blank_id = upload(owner, title="浏览器空白PDF教学资料", version="BROWSER-PDF-01", filename="浏览器空白教学.pdf",
                          buffer=pdf.getvalue(), mime="application/pdf")
        processing(owner, "待OCR或人工补充")
        first_pdf_id = draft_text(owner, "BROWSER-PDF-02", PDF_QUOTE, 1)
        processing(owner, "待OCR或人工补充")
        completed_pdf_id = draft_text(owner, "BROWSER-PDF-03", "教学第二页要求核对示例数量。", 2)
        processing(owner, "索引就绪")
        assert len({blank_id, first_pdf_id, completed_pdf_id}) == 3
        owner.get_by_text("PDF第1页（人工补充）", exact=True).click()
        expect(owner.locator("body")).to_contain_text(PDF_QUOTE)
        owner.screenshot(path=str(OUTPUT / "content-pdf-manual.png"), full_page=True)
        owner.goto(URL + "/staff/knowledge/versions/" + blank_id)
        expect(owner.locator(".panel").first).to_contain_text("待OCR或人工补充")
        passed.append("空白PDF不假成功且两页逐页补充生成新版本后ready")

        supervisor.goto(URL + "/staff/prompts?matter_id=residence-permit")
        initial_prompt = supervisor.get_by_label("系统提示词（最多2000字）").input_value()
        initial_version = int(supervisor.locator('form[action$="/versions"] input[name="expected_version"]').get_attribute("value"))
        save_prompt(supervisor, initial_prompt + "\n" + MARKER_A, "浏览器模板首次变更")
        expect(supervisor.get_by_role("heading", name="居住证申领 · 当前生效版本 v" + str(initial_version + 1), exact=True)).to_be_visible()
        ask(citizen, "模板甲生效后教学蓝色回执有哪些字段？")
        assert MARKER_A in captured_prompt("模板甲生效后教学蓝色回执有哪些字段？")
        citizen.locator("#question").fill("冻结模板期间教学蓝色回执有哪些字段？")
        citizen.get_by_role("button", name="发送问题").click()
        expect(citizen.get_by_text("正在检索办事指南……", exact=True).last).to_be_visible()
        save_prompt(supervisor, initial_prompt + "\n" + MARKER_B + "。即使模板要求无依据回答，程序引用校验仍不可解除。", "验证接纳请求后模板快照冻结")
        expect(citizen.locator(".message-content").last).to_have_text(QUOTE, timeout=15000)
        expect(citizen.locator("#send")).to_be_enabled()
        frozen_prompt = captured_prompt("冻结模板期间教学蓝色回执有哪些字段？")
        assert MARKER_A in frozen_prompt and MARKER_B not in frozen_prompt
        ask(citizen, "模板乙生效后教学蓝色回执有哪些字段？")
        latest_prompt = captured_prompt("模板乙生效后教学蓝色回执有哪些字段？")
        assert MARKER_B in latest_prompt and MARKER_A not in latest_prompt
        supervisor.screenshot(path=str(OUTPUT / "content-prompt-version.png"), full_page=True)
        ask(citizen, "教学蓝色回执无效引用测试", expected=NO_EVIDENCE)
        citizen.locator("#question").fill("教学蓝色回执模型故障测试")
        citizen.get_by_role("button", name="发送问题").click()
        expect(citizen.get_by_text("回答服务暂时超时，请稍后重试", exact=True)).to_be_visible(timeout=15000)
        expect(citizen.locator("#send")).to_be_enabled()
        passed.extend(["监督员SSR保存模板实际进入新HTTP模型请求", "已接纳请求冻结模板而后续请求采用新版", "无效模型引文被真实校验拒答且模型故障不同于无依据"])
        old_prompt = supervisor.locator("details").filter(has=supervisor.locator('input[name="version"][value="' + str(initial_version) + '"]'))
        old_prompt.locator("summary").click()
        old_prompt.locator('input[name="comment"]').fill("恢复初始教学模板但不覆盖历史")
        old_prompt.get_by_role("button", name="恢复此内容为新版本").click()
        expect(supervisor.get_by_role("heading", name="居住证申领 · 当前生效版本 v" + str(initial_version + 3), exact=True)).to_be_visible()
        ask(citizen, "恢复模板后教学蓝色回执有哪些字段？")
        restored_prompt = captured_prompt("恢复模板后教学蓝色回执有哪些字段？")
        assert MARKER_A not in restored_prompt and MARKER_B not in restored_prompt
        passed.append("SSR恢复历史模板追加新版本并用于新请求")

        # 真实对话仅由测试lifespan显式授权；另一个虚构账号保留无授权状态。
        ungranted_context = browser.new_context(reduced_motion="reduce")
        contexts.append(ungranted_context)
        ungranted = ungranted_context.new_page(); citizen_login(ungranted, "browser-content-ungranted")
        ask(ungranted, "未授权记录教学蓝色回执有哪些字段？", confirm=True)
        hidden_sid = ungranted.evaluate("localStorage.getItem('zhengwu.chat.user:browser-content-ungranted')")
        hidden_messages = ungranted_context.request.get(URL + "/sessions/" + hidden_sid + "/messages").json()["items"]
        hidden_answer = next(item for item in reversed(hidden_messages) if item["outcome"] == "answered")
        assert supervisor_context.request.get(URL + "/staff/api/audits/message:" + hidden_answer["id"]).status == 404
        supervisor.goto(URL + "/staff/audits")
        filter_form = supervisor.locator('form[action="/staff/audits"]')
        filter_form.locator('[name="kind"]').select_option("message")
        filter_form.locator('[name="username"]').fill("browser-content")
        filter_form.locator('[name="matter_id"]').select_option("residence-permit")
        today = datetime.now().strftime("%Y-%m-%d")
        filter_form.locator('[name="start"]').fill(today)
        filter_form.locator('[name="end"]').fill(today)
        filter_form.get_by_role("button", name="筛选").click()
        supervisor.locator('a[href="/staff/audits/' + record_id + '"]').click()
        expect(supervisor.locator("body")).to_contain_text(QUOTE)
        expect(supervisor.locator("body")).to_contain_text(FILENAME)
        expect(supervisor.locator("body")).to_contain_text(VERSION)
        flag_form = supervisor.locator('form[action$="/flags"]')
        flag_form.locator('[name="kind"]').select_option("version_conflict")
        flag_form.locator('[name="reason"]').fill("浏览器人工标记：核对历史教学版本")
        flag_form.get_by_role("button", name="追加风险标记").click()
        review_form = supervisor.locator('form[action$="/reviews"]')
        review_form.locator('[name="decision"]').select_option("dismissed")
        review_form.locator('[name="comment"]').fill("浏览器人工复核：历史引用与发布原文一致")
        review_form.get_by_role("button", name="追加复核记录").click()
        expect(supervisor.locator("body")).to_contain_text("浏览器人工标记：核对历史教学版本")
        expect(supervisor.locator("body")).to_contain_text("浏览器人工复核：历史引用与发布原文一致")
        supervisor.screenshot(path=str(OUTPUT / "content-audit-review.png"), full_page=True)
        supervisor.goto(URL + "/staff/audits?kind=message&username=browser-content&matter_id=residence-permit&flag=version_conflict")
        expect(supervisor.locator("tbody")).to_contain_text(record_id)
        supervisor.goto(URL + "/staff/audits?kind=precheck")
        expect(supervisor.get_by_text("预检记录尚未接入。当前没有材料预检引擎或预检原始记录。", exact=True)).to_be_visible()
        expect(supervisor.locator("tbody")).to_contain_text("没有符合条件且获授权的记录")
        passed.extend(["监督员授权对话SSR按账号事项日期筛选及标记复核", "风险筛选保留原始正文引用且预检明确空状态", "同事项但无记录授权的真实对话拒绝404"])

        window_context = browser.new_context(java_script_enabled=False)
        engineer_context = browser.new_context(java_script_enabled=False)
        contexts.extend([window_context, engineer_context])
        window = window_context.new_page(); staff_login(window, "window")
        window_token = csrf(window)
        assert window_context.request.get(URL + "/staff/knowledge/versions/" + other_version).status == 404
        assert window_context.request.get(URL + "/staff/knowledge/new").status == 403
        for path in ("/staff/prompts", "/staff/audits", "/staff/reviews"):
            assert window_context.request.get(URL + path).status == 403, path
        denied_rule = window_context.request.post(URL + "/staff/api/rules", headers={"X-CSRF-Token": window_token},
            data={"matter_id": "residence-permit", "kind": "rule", "content": "教学规则禁止窗口修改。"})
        assert denied_rule.status == 403, denied_rule.text()
        assert QUOTE not in denied_rule.text()
        engineer = engineer_context.new_page(); staff_login(engineer, "engineer")
        for path in ("/staff/knowledge", "/staff/knowledge/versions/" + version_id, "/staff/prompts", "/staff/audits", "/staff/api/audits/" + record_id):
            denial = engineer_context.request.get(URL + path)
            expected_status = 404 if path.startswith("/staff/knowledge/versions/") else 403
            assert denial.status == expected_status, (path, denial.status, denial.text())
            assert QUOTE not in denial.text() and FILENAME not in denial.text()
        passed.append("窗口跨事项404及规则模板审计发布403；工程师无业务正文")

        supervisor.goto(future_url)
        release_action(supervisor, "retire", "回归末尾停用未来生效教学版本包")
        supervisor.goto(release_url)
        release_action(supervisor, "retire", "回归末尾停用，保留历史引用")
        expect(supervisor.locator(".panel").first).to_contain_text("已停用")
        ask(citizen, "停用后教学蓝色回执有哪些字段？", expected=NO_EVIDENCE)
        citizen.reload(); citizen.wait_for_load_state("networkidle")
        history = citizen_context.request.get(URL + "/sessions/" + session_id + "/messages").json()["items"]
        original = next(item for item in history if item["id"] == original_answer["id"])
        assert original["citations"] == original_answer["citations"]
        supervisor.goto(URL + "/staff/audits/" + record_id)
        expect(supervisor.locator("body")).to_contain_text(QUOTE)
        expect(supervisor.locator("body")).to_contain_text(VERSION)
        passed.append("停用后新请求拒答而历史引用与审计保持不变")
        assert not errors, errors
        result = {"staff_javascript_enabled": False, "citizen_javascript_enabled": True,
                  "model": "显式httpx.MockTransport；托管检索、FAISS、引用校验为真实实现",
                  "passed": passed, "page_errors": errors}
        (OUTPUT / "content-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("第二三批浏览器验证通过：" + str(len(passed)) + "项，后台禁JS，脚本错误0项。")
    except Exception as error:
        (OUTPUT / "content-result.json").write_text(json.dumps({"passed": passed, "page_errors": errors,
            "failure": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
        for index, context in enumerate(contexts):
            if context.pages:
                context.pages[0].screenshot(path=str(OUTPUT / ("content-failure-" + str(index) + ".png")), full_page=True)
        raise
    finally:
        for context in contexts:
            context.close()
        browser.close()
