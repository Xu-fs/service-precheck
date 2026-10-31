"""Windows可靠的测试服务生命周期：直接启动Python子进程，随机端口，退出时停止。

通用with_server辅助脚本使用shell启动，Windows下可能只结束shell而遗留子进程。
此专用运行器只管理自己启动的进程，不影响用户其他服务。
"""
import os
import argparse
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description="本机真实浏览器回归，模型使用测试替身")
parser.add_argument("--suite", choices=["citizen", "staff", "content", "tickets", "all"], default="all")
args = parser.parse_args()
temporary = ROOT / ".venv" / "tmp"
temporary.mkdir(parents=True, exist_ok=True)
output = ROOT / ".venv" / "browser-results"
output.mkdir(parents=True, exist_ok=True)
suites = {"citizen": ["browser_chat.py"], "staff": ["browser_staff.py"], "content": ["browser_content.py"],
          "tickets": ["browser_tickets.py"],
          "all": ["browser_chat.py", "browser_staff.py", "browser_tickets.py", "browser_content.py"]}

# 每套使用独立服务和运行库，不关闭业务限流，也不共享提示词/知识状态；content保持最后运行。
for script in suites[args.suite]:
    with tempfile.TemporaryDirectory(dir=temporary) as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = {**os.environ, "ZHENGWU_RUNTIME_DIR": directory, "BROWSER_TEST_PORT": str(port), "PYTHONIOENCODING": "utf-8",
                       "BROWSER_STAFF_TEST_SECRET": "browser-only-signing-key-" + "test9" * 10}
        with (output / "server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen([sys.executable, "-X", "utf8", str(ROOT / "tests" / "browser_server.py")],
                                      cwd=ROOT, env=environment, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("测试服务启动失败，请查看.venv/browser-results/server.log")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.5):
                            break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise RuntimeError("测试服务启动超时")
                        time.sleep(.1)
                result = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "tests" / script)], cwd=ROOT, env=environment)
            finally:
                # Windows虚拟环境解释器可能有启动器子进程，按本运行器PID清理整棵树。
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
    if result.returncode:
        raise SystemExit(result.returncode)
raise SystemExit(0)
