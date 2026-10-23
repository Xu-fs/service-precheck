"""从源码目录调用入口；仅演示骨架运行。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zhengwu_precheck.main import main

if __name__ == "__main__":
    main()
