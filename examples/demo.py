"""检查问答网页依赖；不启动常驻服务器，适合CI运行。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zhengwu_precheck.main import main

if __name__ == "__main__":
    main(["--check"])
