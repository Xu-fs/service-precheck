"""禁用JS验证真正的SSR后台；账号与密钥只由测试服务提供。"""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".venv" / "browser-results"
URL = "http://127.0.0.1:" + os.getenv("BROWSER_TEST_PORT", "8765")


def login(page, role):
    page.goto(URL + "/staff/login")
    page.get_by_label("工作人员账号").fill("staff-" + role)
    page.get_by_label("密码", exact=True).fill("browser-staff-test-password")
    page.get_by_role("button", name="登录后台", exact=True).click()
    expect(page.get_by_role("heading", name="我的工作台", exact=True)).to_be_visible()


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    context = browser.new_context(java_script_enabled=False, viewport={"width": 1360, "height": 980})
    page = context.new_page()
    page.goto(URL + "/staff")
    expect(page).to_have_url(URL + "/staff/login")
    page.screenshot(path=str(OUTPUT / "staff-login.png"), full_page=True)
    login(page, "window")
    expect(page.locator("tbody")).to_contain_text("窗口工作人员")
    expect(page.locator("tbody")).to_contain_text("居住证申领")
    assert page.get_by_role("navigation").get_by_role("link", name="提示词模板").count() == 0
    assert page.get_by_role("navigation").get_by_role("link", name="审核发布").count() == 0
    assert context.request.get(URL + "/staff/reviews").status == 403
    page.get_by_role("navigation").get_by_role("link", name="办事知识库").click()
    expect(page.get_by_text("暂无授权资料。", exact=False)).to_be_visible()
    assert page.get_by_role("link", name="上传教学资料").count() == 0
    access = next(cookie for cookie in context.cookies() if cookie["name"] == "zw_staff_access")
    assert access["httpOnly"] and access["value"].count(".") == 2
    assert context.request.get(URL + "/bootstrap").json()["identity"] == "anonymous"
    page.get_by_role("button", name="续期登录", exact=True).click()
    renewed = next(cookie for cookie in context.cookies() if cookie["name"] == "zw_staff_access")
    assert access["value"] != renewed["value"]
    assert context.request.get(URL + "/staff/prompts").status == 403
    page.get_by_role("button", name="退出登录", exact=True).click()
    assert context.request.get(URL + "/staff/api/me").status == 401

    login(page, "supervisor")
    expect(page.get_by_role("navigation").get_by_role("link", name="提示词模板")).to_be_visible()
    expect(page.get_by_role("navigation").get_by_role("link", name="审核发布")).to_be_visible()
    page.screenshot(path=str(OUTPUT / "staff-workspace.png"), full_page=True)
    page.get_by_role("navigation").get_by_role("link", name="提示词模板").click()
    expect(page.get_by_role("button", name="保存并生效新版本", exact=True)).to_be_visible()
    page.get_by_label("系统提示词（最多2000字）").fill("浏览器教学模板：根据引用回答，不提供审批结论。")
    page.get_by_label("修改说明", exact=True).fill("浏览器教学版本更新")
    page.get_by_role("button", name="保存并生效新版本", exact=True).click()
    expect(page.get_by_role("heading", name="居住证申领 · 当前生效版本 v2", exact=True)).to_be_visible()
    page.locator("details").last.locator("summary").click()
    page.get_by_label("恢复说明", exact=True).fill("浏览器教学恢复初始内容")
    page.get_by_role("button", name="恢复此内容为新版本", exact=True).click()
    expect(page.get_by_role("heading", name="居住证申领 · 当前生效版本 v3", exact=True)).to_be_visible()
    page.get_by_role("navigation").get_by_role("link", name="审核发布").click()
    expect(page.get_by_text("暂无版本包。", exact=True)).to_be_visible()
    page.get_by_role("navigation").get_by_role("link", name="对话与预检审计").click()
    expect(page.get_by_role("heading", name="对话与预检审计", exact=True).first).to_be_visible()
    page.get_by_role("button", name="退出登录", exact=True).click()

    login(page, "engineer")
    expect(page.get_by_role("link", name="查看技术运行状态")).to_be_visible()
    assert page.get_by_role("navigation").get_by_role("link", name="办事知识库").count() == 0
    assert context.request.get(URL + "/staff/api/system/status").json()["database"] == "connected"
    assert context.request.get(URL + "/staff/knowledge").status == 403
    page.get_by_role("button", name="退出登录", exact=True).click()

    login(page, "hotline")
    me = context.request.get(URL + "/staff/api/me").json()
    assert "conversation.read" not in me["permissions"] and "material.view" not in me["permissions"]
    page.get_by_role("button", name="退出登录", exact=True).click()
    mobile = browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844}, is_mobile=True)
    phone = mobile.new_page(); login(phone, "window")
    assert phone.locator("body").bounding_box()["width"] <= 390
    phone.screenshot(path=str(OUTPUT / "staff-mobile.png"), full_page=True)
    result = {"javascript_enabled": False, "passed": ["未登录跳转", "原生表单登录", "JWT HttpOnly Cookie", "事项范围展示", "岗位菜单差异", "窗口不能管理提示词", "续期轮换", "退出撤销", "监督员审核入口", "提示词原生表单保存与恢复", "知识列表真实空状态", "窗口无审核菜单且入口拒绝", "审核发布真实空状态", "审计页面可访问", "工程师技术状态", "工程师不能查看业务模块", "热线无材料正文权限", "办事人凭证隔离", "手机布局"]}
    (OUTPUT / "staff-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"工作人员浏览器验证通过：{len(result['passed'])}项，禁用JS仍可完成登录、续期、退出和提示词保存恢复。")
    mobile.close(); context.close(); browser.close()
