"""真实Chrome闭环；验证后台脚本增强与禁JS表单，办事人正常JS。"""
import json
import os
from pathlib import Path
from test_staff_workspaces import EXPECTED
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".venv" / "browser-results"
URL = "http://127.0.0.1:" + os.getenv("BROWSER_TEST_PORT", "8765")
TITLE = "浏览器虚构示例工单：地址字段疑问"
QUESTION = "虚构示例信息：申请表地址字段与示例回执不同，请人工核对填写说明。"
SUPPLEMENT = "办事人补充虚构示例说明：回执地址是旧地址，现填写示例新地址。"
FIRST_REPLY = "示例人工核对：请补充说明两个地址的对应关系。"
SECOND_REPLY = "示例人工答复：已收到地址说明，请依示例清单继续准备；不代表申请受理或批准。"


def citizen_login(page, username):
    page.goto(URL)
    page.get_by_role("button", name="登录", exact=True).click()
    page.get_by_label("账号", exact=True).fill(username)
    page.get_by_label("密码", exact=True).fill("browser-only-test-password")
    page.locator("#login-form").get_by_role("button", name="登录", exact=True).click()
    expect(page.locator("#identity")).to_have_text("已登录：" + username)
    page.goto(URL + "/my/tickets")
    page.wait_for_load_state("networkidle")


def staff_login(page, role):
    page.goto(URL + "/staff/login")
    page.get_by_label("工作人员账号").fill("staff-" + role)
    page.get_by_label("密码", exact=True).fill("browser-staff-test-password")
    page.get_by_role("button", name="登录后台", exact=True).click()
    expect(page.get_by_role("heading", name=EXPECTED[role][0], exact=True)).to_be_visible()


def detail(context, ticket_id):
    response = context.request.get(URL + "/tickets/" + ticket_id)
    assert response.status == 200, response.text()
    return response.json()


def reply(page, content, kind):
    form = page.locator('form[action$="/replies"]')
    form.locator('[name="kind"]').select_option(kind)
    form.locator('[name="content"]').fill(content)
    form.get_by_role("button", name="确认发送答复", exact=True).click()
    expect(page.locator("body")).to_contain_text(content)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    contexts, passed, errors = [], [], []

    def new_page(*, staff=False, javascript=None):
        context = browser.new_context(java_script_enabled=not staff if javascript is None else javascript, viewport={"width": 1360, "height": 980})
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        return context, page

    try:
        anonymous_context, anonymous = new_page()
        anonymous.goto(URL + "/my/tickets")
        expect(anonymous.get_by_role("link", name="返回首页登录", exact=True)).to_be_visible()
        assert anonymous.locator('form[action="/tickets/submit"]').count() == 0
        assert anonymous_context.request.get(URL + "/tickets").status == 401
        passed.append("匿名只有登录提示，不能查看个人工单列表")

        citizen_context, citizen = new_page()
        citizen_login(citizen, "browser-tickets")
        form = citizen.locator('form[action="/tickets/submit"]')
        form.locator('[name="matter_id"]').select_option("residence-permit")
        form.locator('[name="scenario_id"]').select_option("rental-demo")
        form.locator('[name="title"]').fill(TITLE)
        form.locator('[name="content"]').fill(QUESTION)
        with citizen.expect_navigation(wait_until="networkidle"):
            form.get_by_role("button", name="提交工单", exact=True).click()
        ticket_id = parse_qs(urlparse(citizen.url).query)["ticket_id"][0]
        assert ticket_id.startswith("TK-")
        current = detail(citizen_context, ticket_id)["ticket"]
        assert current["status"] == "queued" and current["owner_id"] is None
        assert current["precheck_id"] is None and current["formal_case_id"] is None
        first_reply_due = current["first_reply_due"]
        assert first_reply_due - current["created_at"] == 48 * 3600
        expect(citizen.locator("#ticket-detail")).to_be_visible()
        citizen.screenshot(path=str(OUTPUT / "tickets-created.png"), full_page=True)
        passed.append("办事人用真实表单提交，进入窗口队列且无虚构预检或办件编号")
        passed.append("首次答复期限从提交起固定连续48小时")

        other_context, other = new_page()
        citizen_login(other, "browser-tickets-other")
        assert TITLE not in other.locator("body").inner_text()
        assert other_context.request.get(URL + "/tickets/" + ticket_id).status == 404
        csrf = other.locator('meta[name="csrf-token"]').get_attribute("content")
        for suffix, content in [("messages", "越权示例补充"), ("confirm-resolved", "")]:
            response = other_context.request.post(URL + "/tickets/" + ticket_id + "/" + suffix,
                data={"content": content, "expected_version": current["version"], "idempotency_key": str(uuid4())},
                headers={"X-CSRF-Token": csrf, "Origin": URL})
            assert response.status == 404, response.text()
        assert detail(citizen_context, ticket_id)["ticket"]["version"] == current["version"]
        passed.append("其他办事人列表无泄露，不能读取、补充或确认他人工单")

        window_context, window = new_page(staff=True)
        staff_login(window, "window")
        window.goto(URL + "/staff/tickets?scope=queue")
        row = window.get_by_role("row").filter(has_text=TITLE)
        expect(row).to_contain_text("窗口待接手")
        row.get_by_role("button", name="接手", exact=True).click()
        expect(window).to_have_url(URL + "/staff/tickets/" + ticket_id)
        owner = window_context.request.get(URL + "/staff/api/me").json()["user_id"]
        current = detail(citizen_context, ticket_id)["ticket"]
        assert current["owner_id"] == owner and current["owner_role"] == "window" and current["status"] == "processing"
        passed.append("窗口禁JS页面通过原生表单接手并成为实际责任人")

        enhanced_context, enhanced = new_page(staff=True, javascript=True)
        enhanced_context.add_cookies(window_context.cookies())
        enhanced.goto(URL + "/staff/tickets/" + ticket_id, wait_until="networkidle")
        enhanced_reply = enhanced.locator('form[action$="/replies"]')
        enhanced_reply.locator('[name="content"]').fill("过期页面上的虚构答复，不应发出")
        stale = window_context.new_page()
        stale.goto(URL + "/staff/tickets/" + ticket_id)
        stale.locator('[name="content"]').fill("禁JS过期页面的虚构答复，不应发出")

        transfer = window.locator('form[action$="/transfers"]')
        assert transfer.locator('[name="target_role"] option').filter(has_text="业务审核员").count() == 1
        assert transfer.locator('[name="target_role"] option').filter(has_text="服务中心主任").count() == 1
        transfer.locator('[name="target_role"]').select_option('reviewer')
        transfer.locator('[name="reason"]').fill("虚构示例专业异常，请业务审核员核对地址说明。")
        transfer.get_by_role("button", name="发起转派，等待接收确认", exact=True).click()
        expect(window.locator("body")).to_contain_text("待确认")
        pending = detail(citizen_context, ticket_id)
        assert pending["ticket"]["owner_id"] == owner and pending["transfers"][-1]["status"] == "pending"
        mine = window_context.request.get(URL + "/staff/api/tickets?scope=mine").json()
        assert ticket_id in [item["id"] for item in mine["items"]]
        assert pending["ticket"]["first_reply_due"] == first_reply_due
        expect(window.get_by_role("status", name="转派状态")).to_contain_text("转派待确认 · 业务审核员")
        expect(window.get_by_role("button", name="确认发送答复", exact=True)).to_be_disabled()
        expect(window.get_by_role("button", name="发起转派，等待接收确认", exact=True)).to_be_disabled()
        expect(window.locator('[name="content"]')).to_be_disabled()
        expect(window.locator('[name="target_role"]')).to_be_disabled()
        expect(window.get_by_role("button", name="撤回转派", exact=True)).to_be_enabled()
        window.screenshot(path=str(OUTPUT / "tickets-transfer-pending.png"), full_page=True)
        passed.append("专业审核转派待确认时窗口保留责任和待办，首次答复期限不变")
        passed.append("转派候选包含授权审核员及手动升级主任")

        enhanced_reply.get_by_role("button", name="确认发送答复", exact=True).click()
        dialog = enhanced.get_by_role("dialog", name="操作未完成")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("工单状态已变化")
        expect(enhanced).to_have_url(URL + "/staff/tickets/" + ticket_id)
        assert detail(citizen_context, ticket_id) == pending
        enhanced.screenshot(path=str(OUTPUT / "tickets-action-error-dialog.png"), full_page=True)
        dialog.get_by_role("button", name="知道了").click()
        expect(enhanced.get_by_role("button", name="确认发送答复", exact=True)).to_be_disabled()
        expect(enhanced.get_by_role("button", name="发起转派，等待接收确认", exact=True)).to_be_disabled()
        passed.append("过期页面操作失败在当前工单弹窗提示，关闭后刷新为锁定状态且未发送答复")

        stale.get_by_role("button", name="确认发送答复", exact=True).click()
        fallback_dialog = stale.get_by_role("dialog", name="操作未完成")
        expect(fallback_dialog).to_be_visible()
        expect(stale.get_by_role("link", name="返回工单列表")).to_be_visible()
        expect(stale.get_by_role("button", name="确认发送答复", exact=True)).to_be_disabled()
        fallback_dialog.get_by_role("button", name="知道了").click()
        expect(fallback_dialog).not_to_be_visible()
        stale.close()
        passed.append("禁JS表单失败也返回工单页和可关闭提示，保持待转派锁定")

        cancel = enhanced.locator('form[action$="/cancel"]')
        cancel.locator('[name="reason"]').fill("   ")
        cancel.get_by_role("button", name="撤回转派", exact=True).click()
        expect(dialog).to_be_visible()
        expect(cancel.locator('[name="reason"]')).to_have_value("   ")
        expect(enhanced.get_by_role("button", name="确认发送答复", exact=True)).to_be_disabled()
        enhanced.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(cancel.get_by_role("button", name="撤回转派", exact=True)).to_be_focused()
        cancel.locator('[name="reason"]').fill("虚构示例：撤回后调整说明")
        cancel.get_by_role("button", name="撤回转派", exact=True).click()
        expect(enhanced.get_by_role("button", name="确认发送答复", exact=True)).to_be_enabled()
        expect(enhanced.get_by_role("button", name="发起转派，等待接收确认", exact=True)).to_be_enabled()
        expect(enhanced.get_by_role("status", name="转派状态")).to_have_count(0)
        transfer = enhanced.locator('form[action$="/transfers"]')
        transfer.locator('[name="target_role"]').select_option('reviewer')
        transfer.locator('[name="reason"]').fill("虚构示例：调整后重新交业务审核岗位")
        transfer.get_by_role("button", name="发起转派，等待接收确认", exact=True).click()
        expect(enhanced.get_by_role("status", name="转派状态")).to_contain_text("转派待确认")
        expect(enhanced.get_by_role("button", name="确认发送答复", exact=True)).to_be_disabled()
        expect(enhanced.get_by_role("button", name="发起转派，等待接收确认", exact=True)).to_be_disabled()
        passed.append("脚本开启时撤回恢复操作，再转派立即显示状态并锁定按钮；错误弹窗支持Escape和焦点恢复")

        reviewer_context, reviewer = new_page(staff=True)
        staff_login(reviewer, "reviewer")
        reviewer.goto(URL + "/staff/tickets?scope=pending")
        reviewer.get_by_role("row").filter(has_text=TITLE).get_by_role("link", name="查看详情").click()
        reviewer.get_by_role("button", name="确认接收", exact=True).click()
        reviewer_id = reviewer_context.request.get(URL + "/staff/api/me").json()["user_id"]
        current = detail(citizen_context, ticket_id)["ticket"]
        assert current["owner_id"] == reviewer_id and current["owner_role"] == "reviewer"
        assert current["first_reply_due"] == first_reply_due
        passed.append("审核员禁JS确认接收后才切换责任人，原始48小时时限保留")

        reply(reviewer, FIRST_REPLY, "supplement")
        current = detail(citizen_context, ticket_id)["ticket"]
        assert current["status"] == "waiting_citizen" and current["round_no"] == 1 and current["close_due"]
        assert current["first_reply_at"] is not None
        passed.append("请求补充信息开启第1轮等待并记录真实首次答复")
        citizen.reload(wait_until="networkidle")
        expect(citizen.locator("body")).to_contain_text(FIRST_REPLY)
        notices = citizen_context.request.get(URL + "/notifications").json()
        notice = next(item for item in notices if item["ticket_id"] == ticket_id and not item["read_at"])
        citizen.goto(URL, wait_until="networkidle")
        notice_card = citizen.locator('[data-notification-id="' + notice["id"] + '"]')
        expect(notice_card.locator('.notification-ticket-title')).to_have_text(TITLE)
        expect(notice_card.get_by_role("button", name="删除消息", exact=True)).to_be_visible()
        citizen.locator('#notification-panel').screenshot(path=str(OUTPUT / "tickets-notification-title-delete.png"))
        citizen.set_viewport_size({'width':390, 'height':844})
        expect(citizen.locator('#notification-panel')).not_to_have_attribute('open', '')
        citizen.locator('#notification-panel summary').click()
        expect(notice_card.locator('.notification-ticket-title')).to_be_visible()
        expect(notice_card.get_by_role('button', name='删除消息', exact=True)).to_be_visible()
        assert citizen.evaluate('document.documentElement.scrollWidth <= innerWidth')
        citizen.screenshot(path=str(OUTPUT / 'tickets-notification-mobile.png'), full_page=True)
        citizen.set_viewport_size({'width':1360, 'height':980})
        expect(citizen.locator('#notification-panel')).to_have_attribute('open', '')
        notice_card.get_by_role("button", name="标记已读", exact=True).click()
        expect(notice_card.get_by_role("button", name="标记已读", exact=True)).to_have_count(0)
        read = next(item for item in citizen_context.request.get(URL + "/notifications").json() if item["id"] == notice["id"])
        assert read["read_at"] is not None
        before_delete = detail(citizen_context, ticket_id)
        assert other_context.request.get(URL + "/notifications").json() == []
        response = other_context.request.post(URL + "/notifications/" + notice["id"] + "/read",
            data={"expected_version": 0, "idempotency_key": str(uuid4())},
            headers={"X-CSRF-Token": csrf, "Origin": URL})
        assert response.status == 404, response.text()
        citizen.locator('#question').fill("正在输入的示例问题应当保留")
        deletion_path = URL + '/notifications/' + notice['id'] + '/delete'
        citizen.route(deletion_path, lambda route: route.fulfill(status=503,
            content_type='application/json', body=json.dumps({'error':{'message':'示例：删除暂时失败'}})))
        notice_card.get_by_role("button", name="删除消息", exact=True).click()
        expect(citizen.locator('#notification-status')).to_have_text('示例：删除暂时失败')
        expect(notice_card).to_be_visible()
        expect(notice_card.get_by_role("button", name="删除消息", exact=True)).to_be_enabled()
        citizen.unroute(deletion_path)
        notice_card.get_by_role("button", name="删除消息", exact=True).click()
        expect(notice_card).to_have_count(0)
        expect(citizen.locator('#question')).to_have_value("正在输入的示例问题应当保留")
        # 已读、未读通知均可删除，未读计数随之更新。
        remaining = citizen_context.request.get(URL + '/notifications').json()
        for item in remaining:
            citizen.locator('[data-notification-id="' + item['id'] + '"]').get_by_role(
                'button', name='删除消息', exact=True).click()
            expect(citizen.locator('[data-notification-id="' + item['id'] + '"]')).to_have_count(0)
        expect(citizen.locator('#notification-count')).to_be_hidden()
        expect(citizen.locator('#notification-status')).to_contain_text('暂无消息')
        assert citizen_context.request.get(URL + '/notifications').json() == []
        assert detail(citizen_context, ticket_id) == before_delete
        citizen.reload(wait_until='networkidle')
        expect(citizen.locator('#notification-items article')).to_have_count(0)
        passed.append("通知显示对应工单标题，右下角叉号可删除已读和未读消息，刷新后仍保持删除且工单不变")
        passed.append("删除失败保留消息并可重试；删除过程不影响聊天输入，未读计数和空状态同步更新")
        citizen.goto(URL + "/my/tickets?ticket_id=" + ticket_id, wait_until="networkidle")
        expect(citizen.get_by_role("button", name="发送补充", exact=True)).to_be_enabled()
        assert detail(citizen_context, ticket_id)["ticket"]["status"] == "waiting_citizen"
        passed.append("站内答复消息实际生成并可标记已读，已读不会关闭工单")
        passed.append("其他办事人看不到通知且不能标记他人通知已读")

        citizen.get_by_label("继续补充问题", exact=True).fill(SUPPLEMENT)
        with citizen.expect_navigation(wait_until="networkidle"):
            citizen.get_by_role("button", name="发送补充", exact=True).click()
        current = detail(citizen_context, ticket_id)["ticket"]
        assert current["status"] == "processing" and current["close_due"] is None and current["round_no"] == 1
        passed.append("办事人补充后停止本轮等待并退回处理中")
        reviewer.reload()
        expect(reviewer.locator("body")).to_contain_text(SUPPLEMENT)
        reply(reviewer, SECOND_REPLY, "result")
        current = detail(citizen_context, ticket_id)["ticket"]
        assert current["status"] == "waiting_citizen" and current["round_no"] == 2 and current["close_due"]
        assert current["first_reply_due"] == first_reply_due
        reviewer.screenshot(path=str(OUTPUT / "tickets-reviewer-replied.png"), full_page=True)
        passed.append("再次处理结果答复新开第2轮等待，历史对话保留")

        engineer_context, engineer = new_page(staff=True)
        staff_login(engineer, "engineer")
        denied = engineer_context.request.get(URL + "/staff/api/tickets/" + ticket_id)
        assert denied.status in (403, 404) and QUESTION not in denied.text()
        engineer_list = engineer_context.request.get(URL + "/staff/api/tickets")
        assert engineer_list.status == 403 and QUESTION not in engineer_list.text()
        response = engineer.goto(URL + "/staff/tickets/" + ticket_id)
        assert response.status in (403, 404) and QUESTION not in engineer.locator("body").inner_text()
        passed.append("工程师直接API和页面均无法读取工单正文")

        citizen.reload(wait_until="networkidle")
        expect(citizen.locator("body")).to_contain_text(SECOND_REPLY)
        with citizen.expect_navigation(wait_until="networkidle"):
            citizen.get_by_role("button", name="确认问题已解决", exact=True).click()
        final = detail(citizen_context, ticket_id)
        assert final["ticket"]["status"] == "closed" and final["ticket"]["close_reason"] == "confirmed_resolved"
        assert final["ticket"]["close_due"] is None
        for text in (FIRST_REPLY, SUPPLEMENT, SECOND_REPLY):
            assert text in [message["content"] for message in final["messages"]]
        expect(citizen.locator("body")).to_contain_text("办事人确认完成")
        assert citizen.get_by_role("button", name="发送补充", exact=True).count() == 0
        citizen.screenshot(path=str(OUTPUT / "tickets-citizen-closed.png"), full_page=True)
        passed.append("本人确认关闭记录为办事人确认完成，并保留全部往来消息")
        reviewer.reload()
        expect(reviewer.locator("body")).to_contain_text("办事人确认完成")
        assert reviewer.locator('form[action$="/replies"]').count() == 0
        assert len(final["transitions"]) >= 8
        reviewer.screenshot(path=str(OUTPUT / "tickets-closed-timeline.png"), full_page=True)
        passed.append("后台显示最终完成方式和完整流转记录，关闭后无回复入口")
        assert not errors, errors
        result = {"staff_javascript_modes": [False, True], "citizen_javascript_enabled": True,
            "ticket_id": ticket_id, "passed": passed, "page_errors": errors,
            "scope": "真实Chrome/SQLite/SSR工单闭环；外部模型仍为测试服务明确替身；不等待真实7/14天"}
        (OUTPUT / "tickets-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("工单浏览器验证通过：" + str(len(passed)) + "项，后台启用和禁用JS均已验证，脚本错误0项。")
    except Exception as error:
        (OUTPUT / "tickets-result.json").write_text(json.dumps({"passed": passed, "page_errors": errors,
            "failure": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
        for index, context in enumerate(contexts):
            if context.pages:
                context.pages[0].screenshot(path=str(OUTPUT / ("tickets-failure-" + str(index) + ".png")), full_page=True)
        raise
    finally:
        for context in contexts:
            context.close()
        browser.close()
