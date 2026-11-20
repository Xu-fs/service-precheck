"""维护端授权工具：必须具有本地数据库维护权限，网页不提供提权入口。

账号先通过create_user.py创建。授权操作记录维护人员标识，不输出密钥或口令。
"""
import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.core.staff_permissions import ROLES, MATTER_NAMES
from zhengwu_precheck.settings import Settings


async def execute(args):
    settings = Settings()
    settings.validate()
    base = ChatRepository(settings.runtime_dir / "chat.sqlite3")
    try:
        await base.initialize()
        repository = StaffRepository(base.engine)
        await repository.initialize()
        actor = "maintenance:" + args.operator
        if args.action in {"grant-role", "revoke-role"}:
            method = repository.grant_role if args.action == "grant-role" else repository.revoke_role
            await method(args.username, args.role, args.matter, actor=actor)
        elif args.action == "grant-record":
            await repository.grant_record(args.username, args.conversation_id, args.level, actor=actor)
        elif args.action == "revoke-record":
            await repository.revoke_record(args.username, args.conversation_id, actor=actor)
        else:
            await repository.disable(args.username, actor=actor)
        print("授权操作已完成并留痕；岗位变更后需重新登录。")
    finally:
        await base.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="工作人员岗位与记录授权维护工具")
    parser.add_argument("--operator", required=True, help="执行维护人员标识，用于留痕")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("grant-role", "revoke-role"):
        command = commands.add_parser(name)
        command.add_argument("username")
        command.add_argument("role", choices=ROLES)
        command.add_argument("--matter", required=True, choices=MATTER_NAMES)
    for name in ("grant-record", "revoke-record"):
        command = commands.add_parser(name)
        command.add_argument("username")
        command.add_argument("conversation_id")
        if name == "grant-record":
            command.add_argument("--level", required=True, choices=["summary", "body"])
    commands.add_parser("disable").add_argument("username")
    try:
        asyncio.run(execute(parser.parse_args()))
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
