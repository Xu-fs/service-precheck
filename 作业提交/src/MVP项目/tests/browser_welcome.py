"""欢迎页与重新开始咨询；使用隔离运行库中的虚构账号和既有预设案例。"""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.venv' / 'browser-results'
URL = 'http://127.0.0.1:' + os.getenv('BROWSER_TEST_PORT', '8765')
CASES = json.loads((ROOT / 'data' / 'workflow-cases.json').read_text(encoding='utf-8'))


def welcome(page):
    expect(page.get_by_role('heading', name='欢迎来到政务易办', exact=True)).to_be_visible()
    expect(page.locator('#welcome')).to_contain_text('您可以描述您的问题，或提交工单。')
    expect(page.locator('#welcome .suggestions a')).to_have_count(2)
    expect(page.locator('#messages .message')).to_have_count(0)
    expect(page.locator('#question')).to_have_value('')
    expect(page.locator('#matter-label')).to_have_text('尚未确认事项')
    expect(page.locator('#load-older')).to_be_hidden()


def ask(page, question='身份证丢了怎么补办？'):
    page.locator('#question').fill(question)
    page.get_by_role('button', name='发送问题').click()
    expect(page.locator('#welcome')).to_be_hidden()
    expect(page.locator('.message.assistant').last.locator('.action-list a')).to_be_visible()
    expect(page.locator('#send')).to_be_enabled()


with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    context = browser.new_context(viewport={'width':1440, 'height':960}, reduced_motion='reduce')
    try:
        page = context.new_page()
        errors, questions, history_reads = [], [], []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def requested(request):
            if request.method == 'POST' and request.url == URL + '/chat':
                questions.append(request.post_data_json)
            if request.method == 'GET' and '/sessions/' in request.url and '/messages' in request.url:
                history_reads.append(request.url)

        page.on('request', requested)
        page.goto(URL)
        page.wait_for_load_state('networkidle')
        welcome(page)
        page.screenshot(path=str(OUTPUT / 'welcome-desktop.png'), full_page=True)

        # 同一页面可以连续咨询；刷新以后不读回此前保存的会话。
        ask(page)
        first_session = page.evaluate("localStorage.getItem('zhengwu.chat.anonymous')")
        ask(page, '便民场馆什么时候开放？')
        assert questions[-1]['session_id'] == first_session
        page.locator('#question').fill('上一次咨询的未发送草稿')
        page.reload(wait_until='networkidle')
        welcome(page)
        assert not history_reads, history_reads
        ask(page)
        assert questions[-1]['session_id'] is None
        assert page.evaluate("localStorage.getItem('zhengwu.chat.anonymous')") != first_session

        # 同一个浏览器新开页面以及“新建咨询”都回到开场状态。
        reopened = context.new_page()
        reopened.goto(URL)
        reopened.wait_for_load_state('networkidle')
        welcome(reopened)
        reopened.close()
        page.locator('#question').fill('准备清空的草稿')
        page.get_by_role('button', name='新建咨询', exact=True).click()
        welcome(page)

        page.get_by_role('button', name='登录', exact=True).click()
        page.get_by_label('账号', exact=True).fill('browser-tickets')
        page.get_by_label('密码', exact=True).fill('browser-only-test-password')
        page.locator('#login-form').get_by_role('button', name='登录', exact=True).click()
        expect(page.locator('#identity')).to_have_text('已登录：browser-tickets')
        welcome(page)
        ask(page)
        page.reload(wait_until='networkidle')
        welcome(page)
        expect(page.locator('#identity')).to_have_text('已登录：browser-tickets')
        assert not history_reads, history_reads

        # 两个入口沿用案例预填；点击入口不提交工单。
        before = context.request.get(URL + '/tickets').json()['total']
        for case in CASES:
            page.locator('#welcome').get_by_role('link', name=case['title'], exact=True).click()
            expect(page.locator('#ticket-title')).to_have_value(case['title'])
            expect(page.locator('#ticket-content')).to_have_value(case['content'])
            expect(page.locator('#matter')).to_have_value(case['matter_id'])
            page.goto(URL)
            page.wait_for_load_state('networkidle')
            welcome(page)
        assert context.request.get(URL + '/tickets').json()['total'] == before

        for width in (390, 320):
            page.set_viewport_size({'width':width, 'height':844})
            welcome(page)
            scroll_box = page.locator('#scroll-area').bounding_box()
            for case in CASES:
                button = page.locator('#welcome').get_by_role('link', name=case['title'], exact=True)
                expect(button).to_be_visible()
                box = button.bounding_box()
                assert box['y'] + box['height'] <= scroll_box['y'] + scroll_box['height'], '手机首屏未完整显示案例按钮'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(OUTPUT / f'welcome-mobile-{width}.png'), full_page=True)
        assert not errors, errors
        print('欢迎页验证通过：两个工单案例入口、连续咨询、匿名及登录刷新重置、新页面、新建咨询、手机布局；无脚本错误。')
    finally:
        context.close()
        browser.close()
