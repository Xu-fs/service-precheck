"""新助手的真实页面初始化、规则确认、恢复和原生建单流程。"""
import json
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.venv/browser-results'
URL = 'http://127.0.0.1:' + os.getenv('BROWSER_TEST_PORT', '8765')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(URL)
        page.goto(URL + '/assistant')
        page.get_by_role('button', name='查看规则版本', exact=True).click()
        expect(page.locator('#assistant-confirm')).to_be_enabled()
        case_id = page.evaluate("() => localStorage.getItem('zw-assistant-case')")
        page.locator('#assistant-confirm').click()
        expect(page.locator('#assistant-status')).to_have_text('已确认事项及规则。')
        page.reload()
        expect(page.locator('#assistant-rule-info')).to_have_text('已恢复此前确认的规则。')
        assert page.evaluate("() => localStorage.getItem('zw-assistant-case')") == case_id
        page.goto(URL)
        page.get_by_role('button', name='登录', exact=True).click()
        page.get_by_label('账号', exact=True).fill('browser-tickets')
        page.get_by_label('密码', exact=True).fill('browser-only-test-password')
        page.locator('#login-form').get_by_role('button', name='登录', exact=True).click()
        expect(page.locator('#identity')).to_have_text('已登录：browser-tickets')
        page.goto(URL + '/assistant')
        page.get_by_role('button', name='查看规则版本', exact=True).click()
        expect(page.locator('#assistant-confirm')).to_be_enabled()
        page.locator('#assistant-confirm').click()
        expect(page.locator('#assistant-status')).to_have_text('已确认事项及规则。')
        page.locator('#material-file').set_input_files(str(ROOT/'data/materials/fictional-identity.png'))
        page.locator('#material-consent').check()
        page.get_by_role('button',name='上传材料',exact=True).click()
        material=page.locator('#material-list article').first
        expect(material).to_contain_text('fictional-identity.png')
        material.get_by_label('虚构姓名',exact=True).fill('示例甲')
        material.get_by_label('虚构地址',exact=True).fill('虚构路一号')
        material.get_by_label('日期 YYYY-MM-DD',exact=True).fill('2026-10-01')
        material.get_by_role('button',name='确认本人转录的字段',exact=True).click()
        expect(material.get_by_label('虚构姓名',exact=True)).to_have_value('示例甲')
        page.get_by_role('button',name='按已确认规则辅助预检',exact=True).click()
        expect(page.locator('#assistant-status')).to_contain_text('尚未审核发布')
        material.get_by_role('checkbox',name='同意在预览后向工单处理人员共享这份材料').check()
        page.locator('#assistant-question').fill('虚构示例咨询：请人工说明材料填写要求。')
        page.get_by_role('button', name='整理人工咨询草稿', exact=True).click()
        page.get_by_role('link', name='预览草稿并确认提交', exact=True).click()
        expect(page.get_by_role('heading', name='共享范围', exact=True)).to_be_visible()
        expect(page.get_by_role('link',name='下载已授权材料',exact=True)).to_be_visible()
        page.get_by_role('button', name='确认以上内容及共享范围，提交工单', exact=True).click()
        expect(page).to_have_url(re.compile(r'/my/tickets\?ticket_id=TK-'))
        submitted_url=page.url
        page.goto(URL+'/assistant')
        page.get_by_role('button',name='撤回授权并删除原件',exact=True).click()
        expect(page.locator('#material-list article')).to_have_count(0)
        page.goto(submitted_url)
        expect(page.locator('body')).to_contain_text('材料已变化或授权已撤回')
        assert not errors, errors
        page.screenshot(path=str(OUTPUT / 'assistant-submitted.png'), full_page=True)
        (OUTPUT / 'assistant-results.json').write_text(json.dumps({
            'passed':['首次初始化建立案例','规则确认与刷新恢复','登录后整理并确认人工草稿',
                      '私有上传与主动转录','未发布预检规则明确暂停','共享材料进入确认预览','撤回授权使历史材料不可下载'],
            'page_errors':errors}, ensure_ascii=False, indent=2), encoding='utf-8')
        print('助手浏览器：7项通过')
    finally:
        browser.close()
