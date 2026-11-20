"""预设之外的问题通过真实聊天、登录和普通工单表单完成提交。"""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.venv' / 'browser-results'
URL = 'http://127.0.0.1:' + os.getenv('BROWSER_TEST_PORT', '8765')


def ask(page, question):
    page.locator('#question').fill(question)
    page.get_by_role('button', name='发送问题', exact=True).click()
    reply = page.locator('.message.assistant').last
    expect(reply).to_contain_text('提交工单')
    expect(reply.get_by_role('button', name='提交工单', exact=True)).to_be_enabled()
    expect(page.locator('#send')).to_be_enabled()
    return reply


with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    context = browser.new_context(viewport={'width':1360, 'height':960}, reduced_motion='reduce')
    try:
        errors = []
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(URL, wait_until='networkidle')
        ask(page, '111')
        ask(page, '路灯坏了该找谁（虚构示例）？')
        expect(page.get_by_role('button', name='居住证申领', exact=False)).to_have_count(0)
        page.screenshot(path=str(OUTPUT / 'ticket-fallback-desktop.png'), full_page=True)

        # 较早回答的按钮必须保留它自己的问题，登录后仍能带到新页面。
        page.locator('.message.assistant').first.get_by_role('button', name='提交工单', exact=True).click()
        expect(page.locator('#login-dialog')).to_be_visible()
        page.get_by_label('账号', exact=True).fill('browser-tickets')
        page.get_by_label('密码', exact=True).fill('browser-only-test-password')
        page.locator('#login-form').get_by_role('button', name='登录', exact=True).click()
        expect(page).to_have_url(URL + '/my/tickets#create-title')
        expect(page.locator('#ticket-content')).to_have_value('111')
        expect(page.locator('#ticket-title')).to_have_value('111')
        expect(page.locator('#matter')).to_have_value('unassigned')
        expect(page.locator('#scenario')).to_have_value('manual-triage')
        assert context.request.get(URL + '/tickets').json()['total'] == 0
        page.get_by_role('button', name='提交工单', exact=True).click()
        expect(page.locator('#ticket-detail')).to_be_visible()
        ticket = context.request.get(URL + '/tickets').json()['items'][0]
        assert ticket['matter_id'] == 'unassigned'
        detail = context.request.get(URL + '/tickets/' + ticket['id']).json()['ticket']
        assert detail['content'] == '111' and detail['status'] == 'queued'

        # 已登录直接跳转；长问题完整保留，标题满足表单限制。
        page.goto(URL, wait_until='networkidle')
        long_question = '虚构道路照明问题，' * 30
        ask(page, long_question).get_by_role('button', name='提交工单', exact=True).click()
        expect(page.locator('#ticket-content')).to_have_value(long_question)
        assert len(page.locator('#ticket-title').input_value()) <= 120
        page.goto(URL + '/my/tickets', wait_until='networkidle')
        expect(page.locator('#ticket-content')).to_have_value('')

        page.goto(URL, wait_until='networkidle')
        page.set_viewport_size({'width':390, 'height':844})
        ask(page, '111')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(OUTPUT / 'ticket-fallback-mobile.png'), full_page=True)
        assert not errors, errors
        print('工单兜底验证通过：未知问题提示、逐条问题保留、匿名登录接续、已登录跳转、人工分流提交、长问题和手机布局。')
    finally:
        context.close()
        browser.close()
