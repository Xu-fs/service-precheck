"""聊天页右侧站内消息：真实虚构工单、阅读操作、跳转及窄屏布局。"""
from pathlib import Path
import os
import re
from playwright.sync_api import sync_playwright, expect

URL = 'http://127.0.0.1:' + os.getenv('BROWSER_TEST_PORT', '8765')
OUTPUT = Path(__file__).resolve().parents[1] / '.venv' / 'browser-results'

with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    context = browser.new_context(viewport={'width':1440, 'height':960})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    panel = page.locator('#notification-panel')
    expect(panel).to_be_visible()
    expect(panel).to_contain_text('登录后查看工单消息')
    panel.get_by_role('button', name='登录查看').click()
    page.get_by_label('账号', exact=True).fill('browser-tickets')
    page.get_by_label('密码', exact=True).fill('browser-only-test-password')
    page.locator('#login-form').get_by_role('button', name='登录', exact=True).click()
    expect(panel).to_contain_text('暂无消息')
    token = context.request.get(URL + '/bootstrap').json()['csrf_token']
    response = context.request.post(URL + '/tickets/submit', headers={'X-CSRF-Token':token}, data={
        'matter_id':'residence-permit', 'scenario_id':'rental-demo',
        'title':'聊天右侧消息（虚构示例）', 'content':'请核对虚构示例中的申请材料。',
        'idempotency_key':'browser-notification-create'})
    assert response.ok, response.text()
    ticket = response.json()
    panel.get_by_role('button', name='刷新消息').click()
    expect(panel.locator('.notification-item')).to_have_count(1)
    expect(panel).to_contain_text('办理中')
    expect(page.locator('#notification-count')).to_have_text('1')
    chat_box = page.locator('.chat-panel').bounding_box()
    panel_box = panel.bounding_box()
    assert panel_box['x'] >= chat_box['x'] + chat_box['width'] - 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#question').fill('保留的咨询草稿')
    panel.get_by_role('button', name='标记已读').click()
    expect(panel.get_by_role('button', name='标记已读')).to_have_count(0)
    expect(page.locator('#notification-count')).to_be_hidden()
    expect(page.locator('#question')).to_have_value('保留的咨询草稿')
    note = context.request.get(URL + '/notifications').json()[0]
    assert note['read_at'] and note['ticket_status'] == 'queued'
    page.screenshot(path=str(OUTPUT / 'notifications-desktop.png'), full_page=True)
    panel.get_by_role('link', name='查看工单').click()
    expect(page.get_by_role('heading', name='工单详情', exact=True)).to_be_visible()
    expect(page.locator('#ticket-detail')).to_contain_text(ticket['id'])
    page.goto(URL + '/my/tickets')
    expect(page.get_by_role('heading', name='站内消息', exact=True)).to_have_count(0)
    staff = browser.new_context()
    login_page = staff.request.get(URL + '/staff/login')
    staff_token = re.search('name="csrf_token" value="([^"]+)"', login_page.text())[1]
    logged_in = staff.request.post(URL + '/staff/api/auth/login', headers={'X-CSRF-Token':staff_token},
        data={'username':'staff-window', 'password':'browser-staff-test-password'})
    assert logged_in.ok, logged_in.text()
    staff_headers = {'X-CSRF-Token':logged_in.json()['csrf_token']}
    for action, extra in (('accept', {}), ('replies', {'kind':'result', 'content':'虚构示例答复，材料说明已核对。'})):
        response = staff.request.post(URL + '/staff/api/tickets/' + ticket['id'] + '/' + action,
            headers=staff_headers, data={'expected_version':ticket['version'],
                'idempotency_key':'browser-notification-' + action, **extra})
        assert response.ok, response.text()
        ticket = response.json()
    confirmed = context.request.post(URL + '/tickets/' + ticket['id'] + '/confirm-resolved',
        headers={'X-CSRF-Token':token}, data={'expected_version':ticket['version'],
            'idempotency_key':'browser-notification-confirm'})
    assert confirmed.ok, confirmed.text()
    staff.close()
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    expect(panel.locator('.notification-state.closed')).to_have_count(2)
    expect(panel).not_to_contain_text('办理中')
    page.set_viewport_size({'width':900, 'height':900})
    expect(panel).not_to_have_attribute('open', '')
    panel.locator('summary').click()
    expect(panel.get_by_role('link', name='查看工单').first).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.set_viewport_size({'width':390, 'height':844})
    page.keyboard.press('Escape')
    expect(panel).not_to_have_attribute('open', '')
    expect(page.locator('#question')).to_be_visible()
    panel.locator('summary').click()
    expect(panel.get_by_role('link', name='查看工单').first).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(OUTPUT / 'notifications-mobile.png'), full_page=True)
    page.keyboard.press('Escape')
    expect(panel).not_to_have_attribute('open', '')
    page.get_by_role('button', name='退出登录', exact=True).click()
    panel.locator('summary').click()
    expect(panel).to_contain_text('登录后查看工单消息')
    expect(panel.locator('.notification-item')).to_have_count(0)
    assert not errors, errors
    print('站内消息浏览器验证通过：右侧布局、登录/退出、刷新、已读、工单跳转、草稿保留、手机折叠及无横向溢出。')
    context.close()
    browser.close()
