"""在隔离运行库中用网页表单跑完两个预设案例，包括两次转派确认和用户结案。"""
import asyncio
import os
from pathlib import Path
from test_staff_workspaces import EXPECTED
import sys
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from create_demo_accounts import provision
from zhengwu_precheck.core.services.demo_cases import workflow_cases
from zhengwu_precheck.settings import Settings

runtime = Path(os.environ['ZHENGWU_RUNTIME_DIR']).resolve()
if not runtime.is_relative_to(ROOT / '.venv' / 'tmp'):
    raise RuntimeError('浏览器案例验证必须使用独立测试运行库')
asyncio.run(provision(Settings(runtime_dir=runtime, staff_jwt_secret=os.environ['BROWSER_STAFF_TEST_SECRET'])))
URL = 'http://127.0.0.1:' + os.environ['BROWSER_TEST_PORT']
OUTPUT = ROOT / '.venv' / 'browser-results'


def submit(form, name):
    with form.page.expect_navigation(wait_until='networkidle'):
        form.get_by_role('button', name=name, exact=True).click()


with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    contexts = []
    errors = []
    try:
        citizen_context = browser.new_context(viewport={'width': 1360, 'height': 980})
        contexts.append(citizen_context)
        citizen = citizen_context.new_page()
        citizen.on('pageerror', lambda error: errors.append(str(error)))
        citizen.goto(URL)
        citizen.get_by_role('button', name='登录', exact=True).click()
        citizen.get_by_label('账号', exact=True).fill('demo_citizen')
        citizen.get_by_label('密码', exact=True).fill('123456')
        citizen.locator('#login-form').get_by_role('button', name='登录', exact=True).click()
        expect(citizen.locator('#identity')).to_have_text('已登录：demo_citizen')

        staff_pages = {}
        for role in ('window', 'reviewer', 'director'):
            context = browser.new_context(java_script_enabled=False)
            contexts.append(context)
            page = context.new_page()
            page.goto(URL + '/staff/login')
            page.get_by_label('工作人员账号').fill('demo_' + role)
            page.get_by_label('密码', exact=True).fill('123456')
            page.get_by_role('button', name='登录后台', exact=True).click()
            expect(page.get_by_role('heading', name=EXPECTED[role][0], exact=True)).to_be_visible()
            staff_pages[role] = page

        for case in workflow_cases():
            citizen.goto(URL + '/my/tickets?example=' + case['id'])
            expect(citizen.get_by_role('heading', name='预设案例（示例）', exact=True)).to_be_visible()
            expect(citizen.locator('#ticket-title')).to_have_value(case['title'])
            expect(citizen.locator('#matter')).to_have_value(case['matter_id'])
            expect(citizen.locator('#scenario')).to_have_value(case['scenario_id'])
            citizen.screenshot(path=str(OUTPUT / (case['id'] + '-preset.png')), full_page=True)
            submit(citizen.locator('form[action="/tickets/submit"]'), '提交工单')
            tid = parse_qs(urlparse(citizen.url).query)['ticket_id'][0]
            citizen.locator('#supplement').fill(case['first_message'])
            submit(citizen.locator('form[action$="/messages"]'), '发送补充')

            window = staff_pages['window']
            window.goto(URL + '/staff/tickets')
            row = window.locator('tbody tr').filter(has_text=tid)
            submit(row.locator('form[action$="/accept"]'), '接手')
            window.goto(URL + '/staff/tickets/' + tid)
            form = window.locator('form[action$="/replies"]')
            expect(form.locator('[name="content"]')).to_have_value(case['window_request'])
            submit(form, '确认发送答复')

            citizen.reload(wait_until='networkidle')
            expect(citizen.locator('#supplement')).to_have_value(case['citizen_supplement'])
            submit(citizen.locator('form[action$="/messages"]'), '发送补充')

            for source_role, target_role, reason_key, reply_key in (
                ('window', 'reviewer', 'window_transfer', 'reviewer_reply'),
                ('reviewer', 'director', 'reviewer_transfer', 'director_reply'),
            ):
                source, target = staff_pages[source_role], staff_pages[target_role]
                source.goto(URL + '/staff/tickets/' + tid)
                transfer = source.locator('form[action$="/transfers"]')
                expect(transfer.locator('[name="reason"]')).to_have_value(case[reason_key])
                expect(transfer.locator('[name="target_role"]')).to_have_value(target_role)
                expect(transfer.locator('[name="target_role"] option:checked')).to_have_text(
                    {'reviewer':'业务审核员', 'director':'服务中心主任'}[target_role])
                submit(transfer, '发起转派，等待接收确认')
                target.goto(URL + '/staff/tickets/' + tid)
                submit(target.locator('form[action*="/transfers/"][action$="/accept"]'), '确认接收')
                reply = target.locator('form[action$="/replies"]')
                expect(reply.locator('[name="content"]')).to_have_value(case[reply_key])
                expect(reply.locator('[name="kind"]')).to_have_value('result')
                submit(reply, '确认发送答复')

            citizen.reload(wait_until='networkidle')
            expect(citizen.locator('body')).to_contain_text('确认问题已解决并结案')
            submit(citizen.locator('form[action$="/confirm-resolved"]'), '确认问题已解决')
            result = citizen_context.request.get(URL + '/tickets/' + tid).json()
            assert result['ticket']['status'] == 'closed'
            assert result['ticket']['close_reason'] == 'confirmed_resolved'
            assert [tr['target_role'] for tr in result['transfers']] == ['reviewer', 'director']
            assert all(tr['status'] == 'accepted' for tr in result['transfers'])
            assert len(result['messages']) == 5
            citizen.screenshot(path=str(OUTPUT / (case['id'] + '-closed.png')), full_page=True)
            print('PASS ' + case['title'] + '：用户发消息、窗口补正、两级转派审核、用户确认结案')

        working_count = citizen_context.request.get(URL + '/tickets').json()['total']
        for case in workflow_cases():
            citizen.goto(URL + '/examples/workflow?example=' + case['id'])
            expect(citizen.get_by_role('link', name='手动逐步办理', exact=True)).to_be_visible()
            expect(citizen.locator('#demo-next')).to_be_enabled()
            citizen.locator('#demo-next').click()
            expect(citizen.locator('#demo-count')).to_have_text('1 / 11 步')
            expect(citizen.locator('#demo-role')).to_have_text('办事人')
            run_id = parse_qs(urlparse(citizen.url).query)['run'][0]
            citizen.reload(wait_until='networkidle')
            expect(citizen.locator('#demo-count')).to_have_text('1 / 11 步')
            # 暂停后立即继续，旧的播放循环不能多发步骤或覆盖新播放状态。
            citizen.locator('#demo-speed').select_option('3000')
            citizen.locator('#demo-play').click()
            expect(citizen.locator('#demo-count')).to_have_text('2 / 11 步')
            expect(citizen.locator('#demo-role')).to_have_text('窗口工作人员')
            citizen.locator('#demo-pause').click()
            expect(citizen.locator('#demo-next')).to_be_enabled()
            citizen.locator('#demo-speed').select_option('200')
            citizen.locator('#demo-play').click()
            expect(citizen.locator('#demo-count')).to_have_text('11 / 11 步 · 已结案', timeout=20000)
            expect(citizen.locator('#demo-play')).to_be_disabled()
            expect(citizen.locator('#demo-next')).to_be_disabled()
            state = citizen_context.request.get(URL + '/examples/api/workflows/' + run_id).json()
            assert state['ticket']['status'] == 'closed'
            assert len(state['messages']) == 5
            assert [t['target_role'] for t in state['transfers']] == ['reviewer', 'director']
            assert citizen_context.request.get(URL + '/tickets').json()['total'] == working_count
            citizen.screenshot(path=str(OUTPUT / (case['id'] + '-automatic-closed.png')), full_page=True)
            citizen.locator('#demo-new').click()
            expect(citizen.locator('#demo-count')).to_have_text('0 / 11 步')
            previous = citizen_context.request.get(URL + '/examples/api/workflows/' + run_id).json()
            assert previous['ticket']['status'] == 'closed'
            history = citizen_context.request.get(URL + '/examples/api/workflows').json()
            assert any(row['id'] == run_id and row['status'] == 'closed' for row in history)
            citizen.goto(URL)
            expect(citizen.locator('#identity')).to_have_text('已登录：demo_citizen')
            print('PASS ' + case['title'] + '：自动视角切换、暂停立即继续、刷新续办、独立记录和新建保留历史')
        citizen.set_viewport_size({'width': 390, 'height': 844})
        citizen.goto(URL + '/examples/workflow?run=' + run_id)
        expect(citizen.locator('#demo-count')).to_have_text('11 / 11 步 · 已结案')
        assert citizen.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        citizen.screenshot(path=str(OUTPUT / 'automatic-workflow-mobile.png'), full_page=True)
        print('PASS 手机宽度下流程与记录可阅读，无横向溢出')
        assert not errors, errors
    finally:
        for context in contexts:
            context.close()
        browser.close()
