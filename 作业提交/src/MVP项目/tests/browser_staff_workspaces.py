"""禁用脚本验证岗位首页、模块导航及手机布局；仅访问各岗位正常入口。"""
import json
import os
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".venv" / "browser-results"
URL = "http://127.0.0.1:" + os.getenv("BROWSER_TEST_PORT", "8765")
PROFILES = {
    "window": ("窗口受理工作台", "窗口工单受理", {"all", "queue", "mine", "pending"}),
    "reviewer": ("业务审核工作台", "专业审核与分派", {"all", "mine", "pending"}),
    "supervisor": ("监督审核工作台", None, None),
    "engineer": ("平台运维工作台", None, None),
}


def login(page, role):
    page.goto(URL + "/staff/login")
    page.get_by_label("工作人员账号").fill("staff-" + role)
    page.get_by_label("密码", exact=True).fill("browser-staff-test-password")
    page.get_by_role("button", name="登录后台", exact=True).click()
    expect(page.get_by_role("heading", name=PROFILES[role][0], exact=True)).to_be_visible()


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    errors = []
    passed = []
    for role, (title, ticket_name, scopes) in PROFILES.items():
        context = browser.new_context(java_script_enabled=False, viewport={"width": 1360, "height": 980})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        login(page, role)
        expect(page.get_by_label("本岗位功能入口")).to_be_visible()
        assert page.get_by_text("业务功能安排", exact=True).count() == 0
        assert page.get_by_role("link", name="办事人咨询入口", exact=True).count() == 0
        assert page.locator("body").bounding_box()["width"] <= 1360
        page.screenshot(path=str(OUTPUT / f"workspace-{role}.png"), full_page=True)
        nav = page.get_by_role("navigation", name="后台导航", exact=True)
        if ticket_name:
            assert nav.get_by_role("link", name="审核发布", exact=True).count() == 0
            nav.get_by_role("link", name=ticket_name, exact=True).click()
            expect(page.get_by_role("heading", name=ticket_name, exact=True)).to_be_visible()
            assert set(page.locator('select[name="scope"] option').evaluate_all(
                "options => options.map(option => option.value)")) == scopes
            page.screenshot(path=str(OUTPUT / f"workspace-{role}-tickets.png"), full_page=True)
        elif role == "supervisor":
            nav.get_by_role("link", name="审核发布", exact=True).click()
            expect(page.get_by_role("heading", name="审核发布", exact=True)).to_be_visible()
            assert nav.get_by_role("link", name="工单处理", exact=True).count() == 0
        else:
            assert nav.get_by_role("link").count() == 4
            nav.get_by_role("link", name="系统运行状态", exact=True).click()
            expect(page.get_by_role("heading", name="系统运行状态", exact=True)).to_be_visible()
            expect(page.get_by_text("正常", exact=True)).to_be_visible()
            nav.get_by_role("link", name="运行日志与错误查询", exact=True).click()
            expect(page.get_by_role("heading", name="运行日志与错误查询", exact=True)).to_be_visible()
            expect(page.locator("tbody")).to_contain_text("/staff/login")
            page.screenshot(path=str(OUTPUT / "engineer-logs.png"), full_page=True)
            # 独立测试服务使用虚构模型替身，验证流内超时后的跨页面定位。
            api = context.request
            csrf = api.get(URL + "/bootstrap").json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())}
            first = api.post(URL + "/chat", data={"question": "居住证需要什么材料？"}, headers=headers)
            packets = [json.loads(line[6:]) for line in first.text().splitlines() if line.startswith("data: ")]
            prompt = next(item for item in packets if "confirmation_token" in item)
            confirmed = api.post(URL + "/sessions/" + prompt["session_id"] + "/matter", data={
                "matter_id": "residence-permit", "scenario_id": "rental-demo",
                "confirmation_token": prompt["confirmation_token"]}, headers=headers)
            assert confirmed.status == 200
            failed = api.post(URL + "/chat", data={"session_id": prompt["session_id"], "question": "居住证模拟超时"},
                              headers={**headers, "Idempotency-Key": str(uuid4())})
            assert "MODEL_TIMEOUT" in failed.text(), (failed.status, failed.text())
            nav.get_by_role("link", name="失败任务记录", exact=True).click()
            page.get_by_label("任务来源", exact=True).select_option("chat")
            page.get_by_role("button", name="查询", exact=True).click()
            expect(page.locator("tbody")).to_contain_text("MODEL_TIMEOUT")
            page.screenshot(path=str(OUTPUT / "engineer-failures.png"), full_page=True)
            page.locator('tbody a[href^="/staff/logs?request_id="]').first.click()
            expect(page.locator("tbody")).to_contain_text("MODEL_TIMEOUT")
            page.get_by_label("日志级别", exact=True).select_option("error")
            page.get_by_label("错误码", exact=True).fill("MODEL_TIMEOUT")
            page.get_by_role("button", name="查询", exact=True).click()
            expect(page.locator("tbody tr")).to_have_count(1)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(OUTPUT / "engineer-logs-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1360, "height": 980})
        expect(page.locator(".staff-header")).to_contain_text(title)
        page.get_by_role("button", name="退出登录", exact=True).click()
        expect(page).to_have_url(URL + "/staff/login")
        passed.append(role + "：岗位首页、导航和模块入口")
        context.close()

    mobile = browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844}, is_mobile=True)
    page = mobile.new_page()
    login(page, "window")
    assert page.locator("body").bounding_box()["width"] <= 390
    for card in page.locator(".workspace-card").all():
        assert card.bounding_box()["width"] <= 390
    page.screenshot(path=str(OUTPUT / "workspace-window-mobile.png"), full_page=True)
    passed.append("窗口岗位手机布局")
    assert not errors, errors
    (OUTPUT / "staff-workspaces-result.json").write_text(json.dumps(
        {"javascript_enabled": False, "passed": passed, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
    mobile.close()
    browser.close()
    print(f"岗位工作台浏览器验证通过：{len(passed)}组，正常登录与模块入口均无需JavaScript。")
