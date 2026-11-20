"""运行设置；凭证从环境变量或仓库外运行目录读取。"""
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
    agent_max_calls: int = field(default_factory=lambda:int(os.getenv('ZHENGWU_AGENT_MAX_CALLS','6')))
    trusted_teaching_materials: bool = field(default_factory=lambda:os.getenv('ZHENGWU_TRUSTED_TEACHING_MATERIALS','false').lower()=='true')
    model_daily_requests: int = field(default_factory=lambda:int(os.getenv('ZHENGWU_MODEL_DAILY_REQUESTS','1000')))
    model_daily_tokens: int = field(default_factory=lambda:int(os.getenv('ZHENGWU_MODEL_DAILY_TOKENS','2000000')))
    model_concurrent: int = field(default_factory=lambda:int(os.getenv('ZHENGWU_MODEL_CONCURRENT','4')))
    human_service: str = field(default_factory=lambda: os.getenv("ZHENGWU_HUMAN_SERVICE", "请向实际受理窗口咨询；当前示例环境未配置真实窗口联系方式。"))
    guide_path: Path = field(default_factory=lambda: Path(os.getenv("ZHENGWU_GUIDE", str(ROOT / "data" / "居住证申领_示例指南.md"))))

    def __post_init__(self):
        # 显式环境设置优先；示例初始化保存的本机密钥可供普通入口重启复用。
        if self.staff_jwt_secret is None and "ZHENGWU_STAFF_JWT_SECRET" not in os.environ:
            key_file = self.runtime_dir / "staff-jwt.key"
            if key_file.is_file():
                self.staff_jwt_secret = key_file.read_text(encoding="utf-8").strip()

    def validate(self):
        if not 1 <= self.agent_max_calls <= 12 or any(type(v) is not int or v < 1 for v in (self.model_daily_requests,self.model_daily_tokens,self.model_concurrent)):
            raise ValueError('工具次数和模型额度必须为有效正整数；每轮工具最多12次')
        # 默认仓库外；仅允许既有.venv中的忽略目录作为测试运行位置。
        path = self.runtime_dir.resolve()
        if path.is_relative_to(ROOT) and not path.is_relative_to(ROOT / ".venv"):
            raise ValueError("运行数据必须位于仓库外或已忽略的.venv运行目录")
        self.runtime_dir = path
        path.mkdir(parents=True, exist_ok=True)
