"""Local Apple Health export -> Garmin CN bridge. JSON schema v1/v2. No HTTP server."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import __version__
from . import domain as hg_domain
from . import exports as hg_exports
from . import file_io as hg_file_io
from . import garmin as hg_garmin
from . import launchagent as hg_launchagent
from . import operations as hg_operations
from . import reconciliation as hg_reconciliation
from . import runtime as hg_runtime
from . import state as hg_state
from . import sync as hg_sync


def choose_folder() -> str:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'POSIX path of (choose folder with prompt "选择 iCloud Drive / 快捷指令 / Health / inbox 文件夹")',
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            pass
    return input("粘贴导出 inbox 的绝对路径（不带外层引号）：").strip().strip("\"'")


def init_config(args: argparse.Namespace) -> None:
    if hg_runtime.CONFIG.exists():
        raise hg_domain.SafeError("已有 config.json，不会覆盖。更改配置请用 settings。")
    inbox = Path(args.inbox or choose_folder()).expanduser().resolve()
    if not inbox.is_dir():
        raise hg_domain.SafeError("请选择已经存在且能读取的 inbox 文件夹。")
    default_since = datetime.now().astimezone().date().isoformat()
    since = (
        args.since
        or input(f"从哪一天开始上传？默认只从今天 [{default_since}]：").strip()
        or default_since
    )
    date.fromisoformat(since)
    source = input("PICOOC 在健康中显示的准确来源名称 [PICOOC]：").strip() or "PICOOC"
    cfg = {
        "is_cn": True,
        "inbox": str(inbox),
        "allowed_sources": [source],
        "since": since,
        "weight_bare_unit": "kg",
        "fat_bare_unit": "percent",
        "pair_window_seconds": 0,
        "settle_seconds": 120,
        "max_records_per_run": 10,
        "enabled": False,
        "archive_after_sync": True,
    }
    hg_file_io.write_json(hg_runtime.CONFIG, cfg)
    print("已初始化，默认只同步选定起始日之后的数据，自动写入关闭。")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def update_settings(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    changes = {
        "fat_bare_unit": args.fat_unit,
        "weight_bare_unit": args.weight_unit,
        "pair_window_seconds": args.pair_window,
        "since": args.since,
        "inbox": args.inbox,
    }
    archive_setting = getattr(args, "archive_after_sync", None)
    if archive_setting is not None:
        changes["archive_after_sync"] = archive_setting == "on"
    if args.source is not None:
        changes["allowed_sources"] = [args.source]
    if cfg.get("enabled") and any(v is not None for v in changes.values()):
        raise hg_domain.SafeError(
            "先运行 ./health-garmin-sync disable，再修改单位、来源、时间或目录，并重新预览。"
        )
    prospective = dict(cfg)
    for k, v in changes.items():
        if v is not None:
            prospective[k] = v
    hg_runtime.validate_config(prospective)
    if args.inbox is not None:
        p = Path(args.inbox).expanduser().resolve()
        if not p.is_dir():
            raise hg_domain.SafeError("inbox 不存在。")
        prospective["inbox"] = str(p)
    hg_file_io.write_json(hg_runtime.CONFIG, prospective)
    cfg.update(prospective)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def inspect_files(cfg: dict[str, Any]) -> None:
    inbox = Path(cfg["inbox"])
    files = sorted(inbox.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    print("目录：", inbox)
    print("JSON 文件数：", len(files))
    for p in files[:6]:
        if p.is_symlink():
            raise hg_domain.SafeError("导出目录含符号链接，已停止。")
        if not 0 < p.stat().st_size <= 4 * 1024 * 1024:
            raise hg_domain.SafeError(f"{p.name} 为空或超过 4 MB。")
        try:
            obj = json.loads(hg_file_io.read_regular_text(p, encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise hg_domain.SafeError(f"{p.name} 不是完整 UTF-8 JSON，或尚未下载完毕。") from exc
        print("\n文件：", p.name)
        try:
            groups = hg_exports.export_groups(obj)
        except hg_domain.SafeError as exc:
            print("格式检查未通过：", exc)
            continue
        print("schema_version=", obj.get("schema_version"), "exported_at=", obj.get("exported_at"))
        for metric, items in groups.items():
            print("metric=", metric, "样本数：", len(items))
            for item in items[-3:]:
                print(json.dumps(item, ensure_ascii=False))
    print("注意：这是原始数值显示；尚未转换单位，也不会上传。")


def show_status(cfg: dict[str, Any]) -> None:
    print("自动写入：", cfg.get("enabled"), "；地区：Garmin 中国区")
    print("导出目录：", cfg["inbox"], "；上传起始日期：", cfg["since"])
    print("运行解释器：", sys.executable)
    print("登录令牌目录：", hg_runtime.STATE / "tokens", "（不要共享）")
    with contextlib.closing(hg_state.database()) as db, db:
        for row in db.execute("SELECT status,COUNT(*) AS n FROM uploads GROUP BY status"):
            print(row["status"], row["n"])
        for row in db.execute(
            "SELECT record_id,status,updated_at,detail FROM uploads ORDER BY updated_at DESC LIMIT 10"
        ):
            print(dict(row))
    p = hg_runtime.STATE / "last_run.json"
    if p.exists():
        print(hg_file_io.read_regular_text(p))
    if sys.platform == "darwin":
        code = hg_launchagent.agent_service_command(
            "print", f"gui/{os.getuid()}/{hg_runtime.LABEL}", checked=False
        )
        if code != 0:
            raise hg_domain.SafeError(
                f"LaunchAgent 未载入或服务查询未成功（返回码 {code}）；运行 ./health-garmin-sync doctor 核查。"
            )
        print("LaunchAgent 已载入：", True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="health-garmin-sync", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("init")
    p.add_argument("--inbox")
    p.add_argument("--since")
    for name in (
        "inspect",
        "preview",
        "login",
        "run",
        "enable",
        "disable",
        "status",
        "install-agent",
        "uninstall-agent",
    ):
        subs.add_parser(name)
    p = subs.add_parser("upload-one")
    p.add_argument("record_id")
    p = subs.add_parser("compare", help="只读查询 Garmin，生成当前或历史差异清单")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--inbox")
    p = subs.add_parser("repair-one", help="按对账清单备份并修复指定的一条冲突测量")
    p.add_argument("record_id")
    p.add_argument("--plan", required=True)
    p = subs.add_parser("delete-extra", help="按对账清单删除指定的一条多余 Garmin 测量")
    p.add_argument("remote_id")
    p.add_argument("--plan", required=True)
    p.add_argument("--complete-export", action="store_true")
    p = subs.add_parser(
        "replace-all", help="以完整 Apple 导出为准，备份并替换选定日期范围的 Garmin 历史"
    )
    p.add_argument("--plan", required=True)
    p.add_argument("--complete-export", action="store_true")
    p = subs.add_parser("verify-operation", help="只读核对未完成的修复或删除操作")
    p.add_argument("operation")
    p = subs.add_parser("resume-operation", help="人工确认目标缺失后恢复已备份的修复测量")
    p.add_argument("operation")
    p = subs.add_parser("remote")
    p.add_argument("date")
    p = subs.add_parser("settings")
    p.add_argument("--fat-unit", choices=("percent", "fraction"))
    p.add_argument("--weight-unit", choices=("kg", "lb", "g"))
    p.add_argument("--pair-window", type=int)
    p.add_argument("--since")
    p.add_argument("--source")
    p.add_argument("--inbox")
    p.add_argument("--archive-after-sync", choices=("on", "off"))
    p = subs.add_parser("restore-exports", help="将当前 inbox 的本地原始 JSON 归档精确恢复到新目录")
    p.add_argument("--to", required=True)
    p = subs.add_parser("resolve")
    p.add_argument("record_id")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--present", action="store_true")
    group.add_argument("--absent", action="store_true")
    group.add_argument("--ignore", action="store_true")
    p = subs.add_parser("doctor", help="离线只读检查配置、导出和后台任务")
    p.add_argument("--json", action="store_true", help="输出 JSON 检查报告")
    args = parser.parse_args()
    if args.command == "doctor":
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            from . import diagnostics as hg_diagnostics

            return hg_diagnostics.doctor(json_output=args.json)
        finally:
            sys.dont_write_bytecode = previous_bytecode
    os.umask(0o077)

    def report_error(exc: BaseException, *, log: bool = False) -> int:
        if isinstance(exc, KeyboardInterrupt):
            try:
                print("已中断。上传中断的记录不会盲目重发。", file=sys.stderr)
            except Exception:
                pass
            return 130
        if isinstance(exc, hg_domain.SafeError):
            text = ("停止：", exc)
            message, values = "%s", (exc,)
        else:
            detail = hg_domain.safe_exception(exc)
            text = ("错误：", detail, "。请检查配置/网络；先核对 Garmin 再重试。")
            message, values = "Unhandled %s", (detail,)
        try:
            print(*text, file=sys.stderr)
        except Exception:
            pass
        if log:
            try:
                hg_runtime.LOG.error(message, *values)
            except Exception as failure:
                try:
                    print("错误日志未保存：", type(failure).__name__, file=sys.stderr)
                except Exception:
                    pass
        return 2

    primary_result = None
    try:
        hg_file_io.private_dir(hg_runtime.STATE)
        with hg_file_io.open_run_lock(hg_runtime.STATE / "run.lock") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if args.command == "run":
                    print("另一个进程正在处理，本次退出，避免重复上传。")
                    return 0
                return report_error(
                    hg_domain.SafeError(
                        f"另一个进程正在处理，{args.command} 未执行；请等待当前进程结束后重试。"
                    )
                )
            owned_handler = None
            try:
                owned_handler = hg_file_io.PrivateRotatingFileHandler(
                    hg_runtime.STATE / "sync.log",
                    maxBytes=1_000_000,
                    backupCount=3,
                    encoding="utf-8",
                )
                check_logging = hg_file_io.guard_command_logging(owned_handler)

                def finish_logging():
                    logging.getLogger().removeHandler(owned_handler)
                    owned_handler.close()
                    check_logging()

                logging.basicConfig(
                    level=logging.INFO,
                    handlers=[owned_handler],
                    format="%(asctime)s %(levelname)s %(message)s",
                )
                if args.command == "init":
                    init_config(args)
                    finish_logging()
                    return 0
                if args.command == "run":
                    hg_sync.run_with_attempt(check_logging=finish_logging)
                    return 0
                if args.command == "uninstall-agent":
                    hg_launchagent.manage_agent(False)
                    finish_logging()
                    return 0
                cfg = hg_runtime.config()
                if args.command == "inspect":
                    inspect_files(cfg)
                elif args.command == "preview":
                    hg_sync.show_preview(cfg)
                elif args.command == "login":
                    hg_garmin.api_client(interactive=True)
                    print("已登录 Garmin 中国区。令牌保存在本机 state/tokens；未上传健康数据。")
                elif args.command == "upload-one":
                    hg_sync.run_sync(cfg, args.record_id)
                elif args.command == "compare":
                    hg_reconciliation.compare_exports(args, cfg)
                elif args.command == "repair-one":
                    hg_operations.reviewed_operation(args, cfg)
                elif args.command == "delete-extra":
                    hg_operations.reviewed_operation(args, cfg, delete_extra=True)
                elif args.command == "replace-all":
                    from . import history as hg_history

                    hg_history.execute(args, cfg)
                elif args.command == "verify-operation":
                    if not hg_operations.verify_operation(args):
                        return 2
                elif args.command == "resume-operation":
                    hg_operations.resume_operation(args)
                elif args.command == "settings":
                    update_settings(args, cfg)
                elif args.command == "status":
                    show_status(cfg)
                elif args.command == "install-agent":
                    hg_launchagent.manage_agent(True, cfg)
                elif args.command == "disable":
                    cfg["enabled"] = False
                    hg_file_io.write_json(hg_runtime.CONFIG, cfg)
                    print(
                        "自动写入已关闭。要停掉定时检查，再运行 ./health-garmin-sync uninstall-agent。"
                    )
                elif args.command == "enable":
                    with contextlib.closing(hg_state.database()) as db, db:
                        n = db.execute(
                            "SELECT COUNT(*) FROM uploads WHERE status IN ('verified','verified_manual')"
                        ).fetchone()[0]
                    if not n:
                        raise hg_domain.SafeError(
                            "先完成至少一条真实记录试传及核验，再启用自动写入。"
                        )
                    hg_sync.show_preview(cfg)
                    if input("确认以后自动上传完整新测量，输入 ENABLE：").strip() == "ENABLE":
                        cfg["enabled"] = True
                        hg_file_io.write_json(hg_runtime.CONFIG, cfg)
                        print("自动写入已启用。")
                elif args.command == "remote":
                    d = date.fromisoformat(args.date).isoformat()
                    obj = hg_garmin.api_client().get_body_composition(d, d)
                    p = hg_runtime.STATE / f"remote-{d}.json"
                    hg_file_io.write_json(p, obj)
                    print("仅查询，响应保存到：", p)
                    print(json.dumps(obj, ensure_ascii=False, indent=2))
                elif args.command == "resolve":
                    hg_operations.resolve_record(args)
                elif args.command == "restore-exports":
                    from . import source_archive as hg_source_archive

                    hg_source_archive.restore_exports(cfg, args.to)
                finish_logging()
            except (Exception, KeyboardInterrupt) as exc:
                primary_result = report_error(exc, log=owned_handler is not None)
                return primary_result
            finally:
                if owned_handler is not None:
                    logging.getLogger().removeHandler(owned_handler)
                    try:
                        owned_handler.close()
                    except Exception as exc:
                        print("日志关闭失败：", type(exc).__name__, file=sys.stderr)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        result = report_error(exc)
        return primary_result if primary_result is not None else result
