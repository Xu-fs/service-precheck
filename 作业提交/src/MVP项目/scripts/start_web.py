"""Windows 双击入口的辅助脚本：复用本机服务，或启动后打开浏览器。"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser


ROOT = Path(__file__).resolve().parents[1]


def service_ready(url):
    # 本机探活直接连接，不依赖系统代理配置。
    try:
        with build_opener(ProxyHandler({})).open(url + "health", timeout=1) as response:
            health = json.load(response)
        return isinstance(health, dict) and health.get("status") == "ok" and (
            health.get("scope") == "local_application" and health.get("teaching_only") is True
        )
    except (OSError, URLError, ValueError):
        return False


def open_browser(url):
    try:
        opened = webbrowser.open(url)
    except (webbrowser.Error, OSError):
        opened = False
    if not opened:
        print(f"未能自动打开浏览器，请手动访问：{url}", flush=True)


def open_when_ready(url):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if service_ready(url):
            print(f"网页已就绪：{url}", flush=True)
            open_browser(url)
            return
        time.sleep(0.5)
    print(f"暂未检测到网页就绪，请查看启动日志；启动成功后可访问：{url}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="政务易办一键启动")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="启动但不打开浏览器")
    parser.add_argument("--check", action="store_true", help="只检查依赖，不启动服务")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "src"))
    if args.check:
        from zhengwu_precheck.main import main as run_app
        run_app(["--check"])
        return 0

    url = f"http://127.0.0.1:{args.port}/"
    if service_ready(url):
        print(f"本机服务已经启动：{url}", flush=True)
        if not args.no_browser:
            open_browser(url)
        return 0

    from zhengwu_precheck.main import main as run_app
    print(f"正在启动政务易办：{url}\n请保持此窗口打开，按 Ctrl+C 停止服务。", flush=True)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    run_app(["--host", "127.0.0.1", "--port", str(args.port)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
