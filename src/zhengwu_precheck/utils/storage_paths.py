"""将运行文件限制在指定私有目录中；调用方另行核对身份和对象授权。"""

from pathlib import Path, PureWindowsPath

from zhengwu_precheck.core.errors import AppError


def private_path(root: Path, key: str) -> Path:
    try:
        if (
            not isinstance(key, str)
            or not key
            or "\\" in key
            or PureWindowsPath(key).drive
            or Path(key).is_absolute()
            or ".." in key.split("/")
        ):
            raise ValueError
        directory = Path(root).resolve()
        target = (directory / key).resolve()
        if target == directory or not target.is_relative_to(directory):
            raise ValueError
        return target
    except (OSError, RuntimeError, TypeError, ValueError):
        raise AppError(503, "STORAGE_PATH_INVALID", "存储路径校验失败，请联系维护人员") from None
