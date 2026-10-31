"""显式创建虚构示例账号；公共演示密码仅用于本地示例，不自动在生产启动时执行。"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zhengwu_precheck.core.staff_permissions import ROLES
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.settings import Settings

DEMO_PASSWORD = "123456"  # 用户指定的公开示例密码；禁止用于真实账号。
DEMO_ROLES = ("citizen", "window", "reviewer", "owner", "supervisor", "hotline", "engineer", "director")
MATTERS = ("residence-permit", "sole-trader-registration")


def save_manifest(path, accounts):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"teaching_only": True, "accounts": accounts}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def provision(settings):
    settings.validate()
    manifest_path = settings.runtime_dir / "demo-accounts.json"
    owned = json.loads(manifest_path.read_text(encoding="utf-8"))["accounts"] if manifest_path.exists() else {}
    repository = ChatRepository(settings.runtime_dir / "chat.sqlite3")
    staff = StaffRepository(repository.engine)
    try:
        await repository.initialize()
        await staff.initialize()
        # 先核对全部同名账号，避免覆盖用户或仅创建一半后才发现冲突。
        existing = {}
        for role in DEMO_ROLES:
            username = "demo_" + role
            row = await staff.user(username)
            if row and owned.get(username) != row["id"]:
                raise ValueError(f"已有账号 {username} 不属于本工具；保留其密码和权限，未创建示例账号。")
            existing[username] = row

        if settings.staff_jwt_secret is None and "ZHENGWU_STAFF_JWT_SECRET" not in os.environ:
            key_path = settings.runtime_dir / "staff-jwt.key"
            try:
                descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(secrets.token_urlsafe(48))
            settings.staff_jwt_secret = key_path.read_text(encoding="utf-8").strip()
        if not settings.staff_jwt_secret or len(settings.staff_jwt_secret.encode()) < 32:
            raise ValueError("工作人员登录密钥未配置或长度不足，请检查本机环境配置。")

        result = []
        for role in DEMO_ROLES:
            username = "demo_" + role
            row = existing[username]
            if not row:
                await repository.create_user(username, DEMO_PASSWORD)
                row = await staff.user(username)
                owned[username] = row["id"]
                save_manifest(manifest_path, owned)
            if role != "citizen":
                granted = {(grant["role_code"], grant["matter_id"]) for grant in await staff.grants(row["id"])}
                for matter in MATTERS:
                    if (role, matter) not in granted:
                        await staff.grant_role(username, role, matter, actor="maintenance:teaching-demo-setup")
            result.append({"username": username, "role": role, "user_id": row["id"]})
        return result
    finally:
        await repository.close()


def main():
    settings = Settings()
    try:
        accounts = asyncio.run(provision(settings))
    except (OSError, ValueError) as error:
        print(f"示例账号初始化失败：{error}", file=sys.stderr)
        return 1
    print("以下均为虚构示例账号；新建账号密码统一为 123456。重复执行保留原密码。")
    for account in accounts:
        print(f"{account['username']}: {ROLES.get(account['role'], '办事人')}")
    print(f"账号库：{settings.runtime_dir / 'chat.sqlite3'}")
    print("工作人员入口：/staff/login；办事人入口：/。若网页已经启动，请重新启动服务以加载本机密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
