"""Private regular-file I/O, atomic JSON writes, locks, and command logging."""

from __future__ import annotations

import json
import os
import stat
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import domain as hg_domain


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise hg_domain.SafeError("本地私密目录不能是符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def create_backup_dir(path: Path) -> None:
    private_dir(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise hg_domain.SafeError(
            "备份路径已存在，未执行修改；请保留原有恢复证据后重新操作。"
        ) from exc
    path.chmod(0o700)


def open_regular_file(path, mode, *, encoding=None, errors=None, reject_symlink=True):
    """Open a regular file without blocking or truncating an unchecked path."""
    flags = {
        "r": os.O_RDONLY,
        "rb": os.O_RDONLY,
        "w": os.O_WRONLY | os.O_CREAT,
        "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
    }[mode] | os.O_NONBLOCK
    if reject_symlink:
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise hg_domain.SafeError("本地文件必须是普通文件。")
        if mode == "w":
            os.ftruncate(fd, 0)
        return os.fdopen(
            fd,
            mode,
            encoding=None if mode == "rb" else encoding,
            errors=None if mode == "rb" else errors,
        )
    except BaseException:
        try:
            os.close(fd)
        except Exception as exc:
            try:
                print("本地运行文件关闭失败：", type(exc).__name__, file=sys.stderr)
            except Exception:
                pass
        raise


def open_regular_text(path, mode, *, encoding=None, errors=None, reject_symlink=True):
    return open_regular_file(
        path, mode, encoding=encoding, errors=errors, reject_symlink=reject_symlink
    )


def open_regular_append(path, *, encoding=None, errors=None):
    return open_regular_text(path, "a", encoding=encoding, errors=errors)


def read_regular_text(path, *, encoding="utf-8", reject_symlink=True):
    with open_regular_text(path, "r", encoding=encoding, reject_symlink=reject_symlink) as stream:
        return stream.read()


def open_run_lock(path: Path):
    return open_regular_append(path)


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Use the same safe append opening for initialization and every rollover."""

    def _open(self):
        return open_regular_append(self.baseFilename, encoding=self.encoding, errors=self.errors)


def write_json(path: Path, data: Any, *, exclusive: bool = False) -> None:
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.is_symlink() or path.is_symlink():
        raise hg_domain.SafeError("拒绝写入符号链接。")
    f = open_regular_text(tmp, "w", encoding="utf-8")
    try:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    except BaseException:
        try:
            f.close()
        except Exception as exc:
            try:
                print("JSON 临时文件关闭失败：", type(exc).__name__, file=sys.stderr)
            except Exception:
                pass
        raise
    else:
        f.close()
    tmp.chmod(0o600)
    if exclusive:
        try:
            os.link(tmp, path)
        except FileExistsError as exc:
            raise hg_domain.SafeError(
                "操作记录已存在，未执行修改；请保留原有恢复证据后重新操作。"
            ) from exc
        finally:
            tmp.unlink()
    else:
        tmp.replace(path)


def guard_command_logging(handler):
    """Defer this owned handler's errors without exposing raw logging diagnostics."""
    failure = None
    closed = False
    original_emit, original_close = handler.emit, handler.close

    def failed(record=None):
        nonlocal failure
        if failure is None:
            failure = type(sys.exc_info()[1]).__name__
            try:
                print(
                    "同步日志未保存：",
                    failure,
                    "；请检查 state/sync.log 和轮换文件。",
                    file=sys.stderr,
                )
            except Exception:
                pass

    def emit(record):
        if not closed and failure is None:
            try:
                original_emit(record)
            except Exception:
                failed()

    def close():
        nonlocal closed
        if not closed:
            closed = True
            try:
                original_close()
            except Exception:
                failed()

    def check():
        if failure is not None:
            raise hg_domain.SafeError(
                f"同步日志保存失败：{failure}；请检查 state/sync.log 和轮换文件。"
            )

    handler.handleError, handler.emit, handler.close = failed, emit, close
    return check
