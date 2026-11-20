"""免登录网页验收：模拟公网HTTP，完成两个案例并检查窄屏。"""
from pathlib import Path
import socket
import os
import sys
import tempfile
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from test_chat import FakeQA
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings


def main():
    with tempfile.TemporaryDirectory() as directory:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        os.environ['ZHENGWU_ALLOWED_HOSTS'] = 'demo.test'
        app = create_app(Settings(runtime_dir=Path(directory)), qa=FakeQA())
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, '验收服务器未启动'
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='chrome', headless=True, args=[
                    '--host-resolver-rules=MAP demo.test 127.0.0.1', '--no-proxy-server'])
                try:
                    context = browser.new_context(viewport={'width': 1360, 'height': 900})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    base = f'http://demo.test:{port}'
                    for preset in ('residence-supplement', 'business-address'):
                        page.goto(base + '/examples/workflow?example=' + preset)
                        page.wait_for_load_state('networkidle')
                        assert not errors, errors
                        assert not page.evaluate('window.isSecureContext'), '必须覆盖公网HTTP环境'
                        expect(page.get_by_role('button', name='开始自动演示', exact=True)).to_be_enabled()
                        page.locator('#demo-speed').select_option('200')
                        page.get_by_role('button', name='开始自动演示', exact=True).click()
                        expect(page.locator('#demo-count')).to_have_text('11 / 11 步 · 已结案', timeout=30000)
                        assert not any(cookie['name'] == 'zw_auth' for cookie in context.cookies())
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.goto(base + '/examples/workflow')
                    expect(page.locator('#demo-preset')).to_be_visible()
                    expect(page.locator('#demo-play')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    assert not errors, errors
                    print('PASS: HTTP访客免登录，两个案例11步结案，无登录身份，无脚本错误，390px无横向溢出')
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=20)


if __name__ == '__main__':
    main()
