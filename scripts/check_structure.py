"""检查根目录边界及骨架必需文件，不代表业务功能验证。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {"README.md", "LICENSE", ".gitignore", "CHANGELOG.md",
              "CONTRIBUTING.md", "requirements.txt", "AGENTS.md"}
ROOT_DIRS = {"src", "docs", "tests", "examples", "scripts", "config", "data", ".github"}
LOCAL_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache",
              ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "htmlcov", "build", "dist"}
LOCAL_FILES = {".env", ".coverage", ".DS_Store", "Thumbs.db"}
REQUIRED = ROOT_FILES | {
    "src/zhengwu_precheck/__init__.py", "src/zhengwu_precheck/main.py",
    "src/zhengwu_precheck/core/__init__.py", "src/zhengwu_precheck/utils/__init__.py",
    "docs/design.md", "docs/api.md", "tests/test_core.py", "examples/demo.py",
    "scripts/build.sh", "scripts/deploy.sh", "scripts/check_structure.py",
    "config/default.yaml", "data/README.md", ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug_report.md", ".github/PULL_REQUEST_TEMPLATE.md",
}


def main():
    errors = []
    for item in ROOT.iterdir():
        allowed = ROOT_DIRS | LOCAL_DIRS if item.is_dir() else ROOT_FILES | LOCAL_FILES
        local_file = item.is_file() and (item.name.startswith(".env.") or item.suffix == ".log")
        if item.name not in allowed and not local_file:
            errors.append(f"超出根目录框架：{item.name}")
    for name in sorted(REQUIRED):
        if not (ROOT / name).is_file():
            errors.append(f"缺少文件：{name}")
    for name in sorted(ROOT_DIRS):
        if not (ROOT / name).is_dir():
            errors.append(f"缺少目录：{name}")
    source = ROOT / "src"
    if source.is_dir():
        for item in source.iterdir():
            if item.name not in {"zhengwu_precheck", "__pycache__"}:
                errors.append(f"源码应归入 src/zhengwu_precheck/：{item.name}")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"结构检查通过：{len(ROOT_DIRS)} 个项目目录，{len(REQUIRED)} 个必需文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
