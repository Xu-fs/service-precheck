"""真实浏览器验证（仅测试服务器注入模型替身）；产物写已忽略的.venv目录。"""
from pathlib import Path
import json
import os
from playwright.sync_api import sync_playwright, expect

OUTPUT = Path(__file__).resolve().parents[1] / ".venv" / "browser-results"
OUTPUT.mkdir(parents=True, exist_ok=True)
URL = "http://127.0.0.1:" + os.getenv("BROWSER_TEST_PORT", "8765")


with sync_playwright() as p:
    # 优先使用本机已有Chrome，避免为测试要求用户另装浏览器。
    browser = p.chromium.launch(channel="chrome", headless=True)
    errors = []
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(URL)
    page.wait_for_load_state("networkidle")
    page.screenshot(path=str(OUTPUT / "desktop-start.png"), full_page=True)
    page.get_by_role("button", name="示例申请表有哪些字段？", exact=True).click()
    page.get_by_role("button", name="居住证申领", exact=False).click()
    expect(page.get_by_text("正在检索办事指南……", exact=True).last).to_be_visible()
    expect(page.locator(".message-content").last).to_have_text("示例申请表包含姓名、联系电话和居住地址三个示例字段。")
    expect(page.locator("#send")).to_be_enabled()
    page.get_by_text("查看引用来源（1）").click()
    expect(page.locator(".citation")).to_contain_text("DEMO-2026-02")
    page.screenshot(path=str(OUTPUT / "desktop-answer.png"), full_page=True)
    session_id = page.evaluate("localStorage.getItem('zhengwu.chat.anonymous')")

    # 刷新后恢复完整消息与引用，不再次调用POST。
    page.reload(); page.wait_for_load_state("networkidle")
    expect(page.locator(".message-content").last).to_have_text("示例申请表包含姓名、联系电话和居住地址三个示例字段。")
    # 用户问题以纯文本展示，即使带HTML也不创建DOM标签。
    page.locator("#question").fill('<img src=x onerror="window.injected=true">')
    page.get_by_role("button", name="发送问题").click()
    expect(page.locator("#send")).to_be_enabled()
    assert page.locator(".messages img").count() == 0
    assert not page.evaluate("Boolean(window.injected)")
    page.locator("#question").fill("火星旅游")
    page.get_by_role("button", name="发送问题").click()
    expect(page.locator(".message-content").last).to_have_text("现有办事指南中未找到相关依据")
    expect(page.get_by_role("button", name="登录后提交人工工单")).to_be_enabled()

    # 中途取消不能永久锁住会话；已取消记录可刷新恢复。
    page.locator("#question").fill("慢回答")
    page.get_by_role("button", name="发送问题").click()
    expect(page.get_by_text("正在检索办事指南……", exact=True).last).to_be_visible()
    page.get_by_role("button", name="停止回答").click()
    expect(page.locator("#send")).to_be_enabled(timeout=10000)
    page.locator("#question").fill("示例字段")
    page.get_by_role("button", name="发送问题").click()
    expect(page.locator(".message-content").last).to_have_text("示例申请表包含姓名、联系电话和居住地址三个示例字段。")

    # 独立浏览器环境不能读取已知会话ID。
    other = browser.new_context()
    attacker = other.new_page(); attacker.goto(URL); attacker.wait_for_load_state("networkidle")
    denial = other.request.get(f"{URL}/sessions/{session_id}/messages")
    assert denial.status == 404
    other.close()

    # 页面登录切换身份，不能自动认领先前匿名会话。
    page.get_by_role("button", name="登录", exact=True).click()
    page.get_by_label("账号", exact=True).fill("browser-test")
    page.get_by_label("密码", exact=True).fill("browser-only-test-password")
    with page.expect_response(lambda response: response.url.endswith('/auth/login')) as login_response:
        page.locator("#login-form").get_by_role("button", name="登录", exact=True).click()
    assert login_response.value.ok, login_response.value.text()
    expect(page.locator("#identity")).to_have_text("已登录：browser-test")
    assert context.request.get(f"{URL}/sessions/{session_id}/messages").status == 404
    page.get_by_role("button", name="退出登录", exact=True).click()
    expect(page.locator("#identity")).to_have_text("公开咨询无需登录")

    # 手机与减少动态效果模式必须显示所有片段，而不只显示第一段。
    mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, reduced_motion="reduce")
    phone = mobile.new_page(); phone.on("pageerror", lambda error: errors.append(str(error)))
    phone.goto(URL); phone.wait_for_load_state("networkidle")
    phone.get_by_role("button", name="示例申请表有哪些字段？", exact=True).click()
    phone.get_by_role("button", name="居住证申领", exact=False).click()
    expect(phone.locator(".message-content").last).to_have_text("示例申请表包含姓名、联系电话和居住地址三个示例字段。")
    phone.get_by_text("查看引用来源（1）").click()
    assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth")
    phone.screenshot(path=str(OUTPUT / "mobile-answer.png"), full_page=True)
    phone.get_by_role("button", name="选择或切换事项").click()
    phone.get_by_role("button", name="个体工商户设立登记", exact=False).click()
    expect(phone.locator(".message-content").last).to_have_text("现有办事指南中未找到相关依据")
    assert not errors, errors
    (OUTPUT / "result.json").write_text(json.dumps({"passed": ["桌面问答", "事项确认", "检索状态", "引用展开", "刷新恢复", "HTML转义", "拒答入口", "取消恢复", "跨匿名身份隔离", "独立账号登录", "匿名历史不被自动认领", "手机无横向溢出", "减少动态效果完整回答", "跨事项无依据拒答"], "page_errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("浏览器验证通过：14项，控制台脚本错误0项。")
    mobile.close(); context.close(); browser.close()
