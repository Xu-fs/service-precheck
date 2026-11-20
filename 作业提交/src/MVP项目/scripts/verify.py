"""统一工程检查；默认基础检查，--full 额外运行本机浏览器回归。"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_steps(steps):
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(ROOT / "src"), environment.get("PYTHONPATH", "")])
    )
    for title, command in steps:
        print(f"\n正在检查：{title}", flush=True)
        try:
            result = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        except OSError as error:
            print(f"无法运行 {title}：{error}", file=sys.stderr)
            return 1
        if result.returncode:
            print(f"检查失败：{title}（退出码 {result.returncode}）", file=sys.stderr)
            return result.returncode
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full", action="store_true", help="基础检查后运行全部浏览器回归；需 Playwright 和 Chrome"
    )
    args = parser.parse_args(argv)
    python = [sys.executable, "-X", "utf8"]
    steps = [
        ("目录边界", [*python, "scripts/check_structure.py"]),
        (
            "Ruff 静态检查",
            [
                *python,
                "-m",
                "ruff",
                "check",
                "--config",
                "config/ruff.toml",
                "src",
                "scripts",
                "tests",
                "examples",
            ],
        ),
        (
            "检查工具格式",
            [
                *python,
                "-m",
                "ruff",
                "format",
                "--check",
                "--config",
                "config/ruff.toml",
                "scripts/verify.py",
                "tests/test_verify.py",
            ],
        ),
        ("依赖与入口", [*python, "examples/demo.py"]),
        ("Python 全量测试", [*python, "-m", "unittest", "discover", "-s", "tests", "-v"]),
        ("前端流解析", ["node", "--test", "tests/test_sse.mjs"]),
    ]
    if args.full:
        steps.append(("浏览器全量回归", [*python, "tests/run_browser_check.py", "--suite", "all"]))
    result = run_steps(steps)
    if not result:
        print("\n全部检查通过。" if args.full else "\n基础检查通过（未运行浏览器回归）。")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
