"""Describe and manage the current project's macOS background service."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import domain as hg_domain
from . import runtime as hg_runtime


def agent_plist() -> dict[str, Any]:
    return {
        "Label": hg_runtime.LABEL,
        "ProgramArguments": [
            str(hg_runtime.BASE / ".venv/bin/python"),
            "-m",
            "health_garmin_sync.background",
        ],
        "WorkingDirectory": str(hg_runtime.BASE),
        "StartInterval": 1800,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "HOME": str(Path.home())},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def agent_service_command(action: str, *arguments: str, checked: bool = True) -> int:
    try:
        result = subprocess.run(
            ["/bin/launchctl", action, *arguments], capture_output=True, timeout=5
        )
    except subprocess.TimeoutExpired as exc:
        raise hg_domain.SafeError(f"任务 {action} 超时（5 秒）；未确认操作完成。") from exc
    except OSError as exc:
        raise hg_domain.SafeError(f"任务 {action} 无法执行：{type(exc).__name__}。") from exc
    if checked and result.returncode != 0:
        raise hg_domain.SafeError(
            f"任务 {action} 失败（返回码 {result.returncode}）；未确认操作完成。"
        )
    return result.returncode


def manage_agent(install: bool, cfg: dict[str, Any] | None = None) -> None:
    if sys.platform != "darwin":
        raise hg_domain.SafeError("LaunchAgent 仅能在 macOS 安装。")
    path = Path.home() / "Library/LaunchAgents" / f"{hg_runtime.LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if install and (
        cfg is None or not (cfg.get("enabled") or cfg.get("health_export_enabled", False))
    ):
        raise hg_domain.SafeError(
            "先完成试传并运行 ./health-garmin-sync enable，或运行 enable-health，再安装定时任务。"
        )
    # Only this tool's known service label is affected; no system-wide changes.
    if not install:
        agent_service_command("bootout", f"{domain}/{hg_runtime.LABEL}")
        path.unlink(missing_ok=True)
        print("已卸载本工具的定时任务；本地状态、导出和 Garmin 数据均保留。")
        return
    document = plistlib.dumps(agent_plist())
    if path.is_symlink():
        raise hg_domain.SafeError("拒绝覆盖符号链接 plist。")
    if path.exists() and not path.is_file():
        raise hg_domain.SafeError("拒绝覆盖非普通文件 plist。")
    path.parent.mkdir(parents=True, exist_ok=True)
    draft = None
    try:
        stream = tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{hg_runtime.LABEL}.", suffix=".tmp", delete=False
        )
        try:
            draft = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(document)
            stream.flush()
        finally:
            preserve_error = sys.exc_info()[0] is not None
            try:
                stream.close()
            except BaseException as exc:
                if not preserve_error:
                    raise
                print("临时任务配置关闭失败：", type(exc).__name__, file=sys.stderr)
        if agent_service_command("print", f"{domain}/{hg_runtime.LABEL}", checked=False) == 0:
            agent_service_command("bootout", f"{domain}/{hg_runtime.LABEL}")
        draft.replace(path)
        draft = None
        agent_service_command("bootstrap", domain, str(path))
    finally:
        if draft is not None:
            preserve_error = sys.exc_info()[0] is not None
            try:
                draft.unlink(missing_ok=True)
            except BaseException as exc:
                if not preserve_error:
                    raise
                print("临时任务配置清理失败：", type(exc).__name__, file=sys.stderr)
    print("定时任务已注册：当前用户登录时，每 30 分钟尝试处理。")
    print("首次检查可能与本命令的锁重叠。稍后可手动运行 ./health-garmin-sync run 验证。")
