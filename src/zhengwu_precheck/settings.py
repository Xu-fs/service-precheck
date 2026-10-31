"""非机密运行设置；凭证始终从环境变量读取。"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    staff_jwt_secret: str | None = field(default_factory=lambda: os.getenv("ZHENGWU_STAFF_JWT_SECRET"))
    runtime_dir: Path = field(default_factory=lambda: Path(os.getenv("ZHENGWU_RUNTIME_DIR", str(Path(tempfile.gettempdir()) / "zhengwu-web"))))
    secure_cookies: bool = field(default_factory=lambda: os.getenv("ZHENGWU_SECURE_COOKIES", "false").lower() == "true")
    heartbeat_seconds: float = 15
    request_timeout: float = 120
    lease_seconds: int = 180
    human_service: str = field(default_factory=lambda: os.getenv("ZHENGWU_HUMAN_SERVICE", "请向实际受理窗口咨询；当前教学环境未配置真实窗口联系方式。"))
    guide_path: Path = field(default_factory=lambda: Path(os.getenv("ZHENGWU_GUIDE", str(ROOT / "data" / "居住证申领_教学指南.md"))))

    def validate(self):
        # 默认仓库外；仅允许既有.venv中的忽略目录作为测试运行位置。
        path = self.runtime_dir.resolve()
        if path.is_relative_to(ROOT) and not path.is_relative_to(ROOT / ".venv"):
            raise ValueError("运行数据必须位于仓库外或已忽略的.venv运行目录")
        self.runtime_dir = path
        path.mkdir(parents=True, exist_ok=True)
