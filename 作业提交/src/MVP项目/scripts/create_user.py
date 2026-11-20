"""人工创建独立账号；不预置密码，密码从交互输入读取。"""
import argparse
import asyncio
from getpass import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.settings import Settings


async def create(username, password):
    settings = Settings()
    settings.validate()
    repository = ChatRepository(settings.runtime_dir / "chat.sqlite3")
    try:
        await repository.initialize()
        await repository.create_user(username, password)
        print("账号已创建。")
    finally:
        await repository.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="创建本地独立账号")
    parser.add_argument("username")
    args = parser.parse_args()
    if not 1 <= len(args.username) <= 80:
        parser.error("账号长度应为1至80个字符")
    password = getpass("密码（至少12个字符）：")
    if len(password) < 12 or len(password) > 256 or password != getpass("再次输入密码："):
        parser.error("两次密码不一致或长度不符合要求")
    try:
        asyncio.run(create(args.username, password))
    except Exception:
        print("账号创建失败，请检查账号是否重复、数据库是否可写。", file=sys.stderr)
        raise SystemExit(1)
