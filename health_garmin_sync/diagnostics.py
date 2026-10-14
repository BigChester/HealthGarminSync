"""Offline diagnostics: read setup evidence without initializing local state."""

import json
import os
import plistlib
import re
import stat
import subprocess
import sys
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import domain as hg_domain
from . import file_io as hg_file_io
from . import launchagent as hg_launchagent
from . import runtime as hg_runtime
from . import sources as hg_sources

SCOPE = "仅检查当前进程访问与本地证据；后台执行和 macOS 权限仍需设备验收。"
RESULTS = frozenset(
    (
        "pending",
        "verified",
        "verified_manual",
        "verified_conflict",
        "remote_missing",
        "repair_uncertain",
        "uncertain",
        "inflight",
        "ignored",
        "repair",
        "replace",
        "conflict",
        "deferred",
    )
)
UNCERTAIN_RESULTS = frozenset(("inflight", "uncertain", "repair_uncertain"))
REVIEW_RESULTS = frozenset(("repair", "replace", "conflict", "verified_conflict", "remote_missing"))
PHASES = frozenset(
    (
        "verified",
        "deleted_verified",
        "closed_partial",
        "uncertain",
        "deleting",
        "deleted",
        "uploading",
        "replacing",
    )
)
TERMINAL_PHASES = frozenset(("verified", "deleted_verified", "closed_partial"))
STALE_SECONDS = 7200


def _json(path, reject_symlink=True):
    if reject_symlink and path.is_symlink():
        raise ValueError("symlink")
    try:
        return json.loads(
            hg_file_io.read_regular_text(path, encoding="utf-8", reject_symlink=reject_symlink)
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting too deep") from exc


class FutureEvidenceError(ValueError):
    pass


class ClockOrderEvidenceError(ValueError):
    pass


def evidence_time(text, reference_time):
    stamp = hg_domain.aware(text)
    if (stamp - reference_time).total_seconds() > 600:
        raise FutureEvidenceError("future evidence time")
    return stamp


def historical_replacement_summary(value):
    """Recognize the completed replacement summary written by older installations."""
    if not isinstance(value, dict) or value.get("kind") != "full_replacement":
        return None
    fields = ("source_measurements", "remote_measurements", "matched", "extra")
    if (
        value.get("phase") != "verified"
        or not isinstance(value.get("finished_at"), str)
        or any(type(value.get(key)) is not int or value[key] < 0 for key in fields)
        or value["matched"] != value["source_measurements"]
        or value["extra"] != 0
        or value["remote_measurements"] != value["matched"] + value["extra"]
    ):
        raise ValueError("invalid historical replacement summary")
    return {key: value[key] for key in fields}


def historical_verification_passed(value):
    """Old verification kept its phase even after a failed read-back."""
    fields = {"matched", "target_count", "remote_count", "extra"}
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or any(type(value[key]) is not int or value[key] < 0 for key in fields)
    ):
        raise ValueError("invalid historical verification")
    return (
        value["matched"] == value["target_count"] == value["remote_count"] and value["extra"] == 0
    )


def attempt_summary(
    value,
    *,
    allowed_stages=("config", "sync"),
    allowed_outcomes=("running", "completed", "preview", "failed", "interrupted"),
    allow_http=True,
    reference_time=None,
):
    if reference_time is None:
        reference_time = hg_runtime.now()
    fields = {"outcome", "started_at", "finished_at", "stage", "error_type"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid attempt fields")
    outcome, stage = value["outcome"], value["stage"]
    if outcome not in allowed_outcomes or stage not in allowed_stages:
        raise ValueError("invalid attempt values")
    if not isinstance(value["started_at"], str):
        raise ValueError("invalid attempt start")
    started = evidence_time(value["started_at"], reference_time)
    finished = value["finished_at"]
    error = value["error_type"]
    if outcome == "running":
        if finished is not None or error is not None:
            raise ValueError("unfinished attempt has terminal fields")
    else:
        if not isinstance(finished, str):
            raise ValueError("invalid attempt finish")
        finished = evidence_time(finished, reference_time)
        if outcome in ("completed", "preview"):
            if stage != allowed_stages[-1] or error is not None:
                raise ValueError("invalid completed attempt")
        elif not isinstance(error, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*"
            + (r"(?: HTTP (?:400|401|403|404|429|500|502|503|504))?" if allow_http else ""),
            error,
        ):
            raise ValueError("invalid attempt error category")
        if finished < started:
            raise ClockOrderEvidenceError("attempt ended before it started")
    # Never forward exception categories or arbitrary fields to user-facing output.
    return {
        "outcome": outcome,
        "stage": stage,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat() if finished is not None else None,
    }


def collect():
    checks = []
    reference_time = hg_runtime.now()

    def add(identifier, status, message, hint=None, data=None):
        item = {"id": identifier, "status": status, "message": message}
        if hint:
            item["hint"] = hint
        if data is not None:
            item["data"] = data
        checks.append(item)

    if sys.version_info >= (3, 12):
        add("python", "ok", "Python 版本符合要求。")
    else:
        add("python", "error", "需要 Python 3.12+。", "运行 bash scripts/setup.sh。")
    interpreter = hg_runtime.BASE / ".venv/bin/python"
    if interpreter.is_file() and os.access(interpreter, os.X_OK):
        add("interpreter", "ok", "项目虚拟环境解释器可执行。")
    else:
        add(
            "interpreter",
            "error",
            "项目虚拟环境解释器缺失或不可执行。",
            "运行 bash scripts/setup.sh。",
        )
    entry = hg_runtime.BASE / "health_garmin_sync" / "background.py"
    if entry.is_file() and os.access(entry, os.R_OK):
        add("agent_entry", "ok", "后台入口存在且当前进程可读取。")
    else:
        add("agent_entry", "error", "后台入口缺失或当前进程无法读取。", "检查项目文件是否完整。")
    try:
        installed = version("garminconnect")
    except PackageNotFoundError:
        add("dependency", "error", "未安装 garminconnect。", "运行 bash scripts/setup.sh。")
    except (OSError, ValueError):
        add(
            "dependency",
            "error",
            "无法读取 garminconnect 版本信息。",
            "运行 bash scripts/setup.sh。",
        )
    else:
        if installed == hg_runtime.REQUIRED_GARMIN_VERSION:
            add("dependency", "ok", "garminconnect 版本符合固定依赖。")
        else:
            add(
                "dependency",
                "error",
                "garminconnect 版本与固定依赖不符。",
                "运行 bash scripts/setup.sh。",
            )

    cfg = None
    source_summary = None
    try:
        cfg = hg_runtime.validate_config(_json(hg_runtime.CONFIG, reject_symlink=False))
    except FileNotFoundError:
        add("config", "error", "尚未初始化配置。", "运行 ./health-garmin-sync init。")
    except hg_domain.SafeError as exc:
        add("config", "error", str(exc), "检查 config.json。")
    except (OSError, UnicodeError, ValueError):
        add("config", "error", "配置无法读取或不是完整 JSON。", "检查 config.json。")
    else:
        add("config", "ok", "配置校验通过。")
        add(
            "enabled",
            "info" if cfg["enabled"] or cfg.get("health_export_enabled", False) else "warn",
            "自动写入已启用。" if cfg["enabled"] else "自动写入已关闭。",
        )
        if cfg.get("health_outbox") is not None or cfg.get("health_export_enabled", False):
            add(
                "health_export",
                "info",
                "定时血压导出已开启。"
                if cfg.get("health_export_enabled")
                else "定时血压导出已关闭。",
                data={
                    "enabled": cfg.get("health_export_enabled", False),
                    "outbox": cfg.get("health_outbox"),
                },
            )

    if cfg is not None:
        try:
            inbox = Path(cfg["inbox"]).expanduser()
            files = [path for path in inbox.iterdir() if path.suffix == ".json"]
            if not files:
                archive = hg_runtime.STATE / "source_archive.sqlite3"
                if archive.exists() and not archive.is_symlink() and archive.is_file():
                    add(
                        "inbox",
                        "info",
                        "inbox 为空；检测到本地原始导出归档。",
                        "运行 ./health-garmin-sync preview 检查当前目录对应的归档；doctor 不打开 SQLite。",
                    )
                else:
                    add(
                        "inbox",
                        "warn",
                        "当前进程可读取 inbox，但没有 JSON 导出。",
                        "在 iPhone 手动导出一次。",
                    )
            else:
                samples, records, waiting, exports = hg_sources.load_measurements(
                    cfg, include_archive=False
                )
                data = {
                    "files": len(files),
                    "samples": len(samples),
                    "measurements": len(records),
                    "waiting_pairs": len(waiting),
                }
                if waiting:
                    add(
                        "inbox",
                        "error",
                        "导出存在配对歧义。",
                        "运行 ./health-garmin-sync preview 核对配对。",
                        data,
                    )
                elif not records:
                    add(
                        "inbox",
                        "warn",
                        "导出有效，但当前范围与来源没有可用测量。",
                        "运行 ./health-garmin-sync preview 核对来源和起始日期。",
                        data,
                    )
                else:
                    add("inbox", "ok", "当前进程可读取导出，解析和配对通过。", data=data)
                    if exports:
                        source_summary = (
                            max(hg_domain.aware(stamp) for stamp in exports.values()),
                            len(records),
                        )
        except (OSError, ValueError, RuntimeError, hg_domain.SafeError):
            add(
                "inbox",
                "error",
                "当前进程无法读取或校验 inbox 导出。",
                "检查目录与下载状态，运行 ./health-garmin-sync preview 查看具体原因。",
            )

    state_available = False
    try:
        if hg_runtime.STATE.is_symlink():
            add("state", "error", "state 不能是符号链接。")
        elif not hg_runtime.STATE.exists():
            add("state", "warn", "state 尚不存在；本次不会创建。")
        elif not hg_runtime.STATE.is_dir():
            add("state", "error", "state 不是目录。")
        elif not os.access(hg_runtime.STATE, os.R_OK | os.W_OK | os.X_OK):
            add("state", "error", "当前进程无法读写 state。", "检查本地目录权限。")
        else:
            children = list(hg_runtime.STATE.iterdir())
            state_available = True
            invalid_file = None
            recoverable_plans = unchecked_plans = False
            runtime_names = (
                "run.lock",
                "agent-log.lock",
                "sync.log",
                "sync.sqlite3",
                "last_attempt.json.tmp",
                "last_agent_attempt.json.tmp",
                "last_run.json.tmp",
                "plans/sync-latest.json.tmp",
                "last_compare.json.tmp",
                "last_sync_comparison.json.tmp",
            )
            replace_targets = (
                "plans/sync-latest.json",
                "last_compare.json",
                "last_sync_comparison.json",
            )
            directory_obstacles = (
                "sync.log.1",
                "sync.log.2",
                "sync.log.3",
                "sync.sqlite3-journal",
                "sync.sqlite3-wal",
            )
            log_names = ["agent.out.log", "agent.err.log"]
            readwrite_logs = {
                base + suffix for base in log_names for suffix in ("", ".1", ".2", ".3")
            }
            log_names.extend(
                child.name
                for child in children
                if re.fullmatch(r"agent\.(?:out|err)\.log\.[0-9]+", child.name)
            )
            for name in (
                "plans",
                *runtime_names,
                *replace_targets,
                *log_names,
                *directory_obstacles,
            ):
                path = hg_runtime.STATE / name
                try:
                    metadata = path.lstat()
                    mode = metadata.st_mode
                    if name == "plans" or name in replace_targets:
                        invalid = (
                            not stat.S_ISDIR(mode)
                            if name == "plans"
                            else stat.S_ISDIR(mode) or stat.S_ISLNK(mode)
                        )
                        if invalid:
                            invalid_file = name
                            break
                        if name == "plans":
                            recoverable_plans = metadata.st_uid == os.geteuid() and not os.access(
                                path, os.X_OK
                            )
                        continue
                    if name in directory_obstacles:
                        if stat.S_ISDIR(mode):
                            invalid_file = name
                            break
                        continue
                    access = os.W_OK if name in runtime_names else 0
                    if name in readwrite_logs or name == "sync.sqlite3":
                        access = os.R_OK | os.W_OK
                    if not stat.S_ISREG(mode) or (access and not os.access(path, access)):
                        invalid_file = name
                        break
                except FileNotFoundError:
                    continue
                except PermissionError:
                    if name.startswith("plans/") and recoverable_plans:
                        unchecked_plans = True
                        continue
                    invalid_file = name
                    break
                except OSError:
                    invalid_file = name
                    break
            if invalid_file:
                add(
                    "state",
                    "error",
                    "本地运行文件无效或当前进程缺少所需权限。",
                    f"检查 state/{invalid_file} 的文件类型和权限。",
                )
            elif unchecked_plans:
                add(
                    "state",
                    "warn",
                    "当前进程无法检查 plans 内的运行文件。",
                    "下次运行会恢复 state/plans 权限；之后重新运行 ./health-garmin-sync doctor。",
                )
            else:
                add("state", "info", "当前进程可访问 state。")
    except OSError:
        add("state", "error", "当前进程无法访问 state。", "检查本地目录权限。")

    if state_available:
        try:
            operations = hg_runtime.STATE / "operations"
            pending, total, failed_checks, historical_checks = 0, 0, 0, 0
            if operations.is_symlink():
                raise ValueError("symlink")
            if operations.exists():
                for path in operations.iterdir():
                    if path.suffix != ".json":
                        continue
                    job = _json(path)
                    if (
                        not isinstance(job, dict)
                        or job.get("phase") not in PHASES
                        or not isinstance(job.get("key"), str)
                        or not job["key"]
                    ):
                        raise ValueError("invalid journal")
                    ids = job.get("record_ids", [])
                    if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
                        raise ValueError("invalid journal")
                    if "last_checked_at" in job or "last_check_passed" in job:
                        historical = (
                            job.get("type") == "full_replacement"
                            and job["phase"] in ("verified", "closed_partial")
                            and "last_check_passed" not in job
                        )
                        if (
                            not isinstance(job.get("last_checked_at"), str)
                            or not historical
                            and type(job.get("last_check_passed")) is not bool
                        ):
                            raise ValueError("invalid journal verification")
                        evidence_time(job["last_checked_at"], reference_time)
                        if historical:
                            historical_checks += 1
                            failed_checks += not historical_verification_passed(
                                job.get("verification")
                            )
                        else:
                            failed_checks += not job["last_check_passed"]
                    pending += job["phase"] not in TERMINAL_PHASES
                    total += 1
            add(
                "operations",
                "error" if failed_checks else "warn" if pending else "ok",
                f"未完成操作 {pending} 项；最近一次核对未通过 {failed_checks} 项。",
                "运行 ./health-garmin-sync verify-operation 核对对应操作记录。"
                if pending or failed_checks
                else None,
                {"total": total, "unfinished": pending, "failed_checks": failed_checks},
            )
            if historical_checks:
                add(
                    "historical_operations",
                    "info",
                    "历史已关闭替换操作使用旧版计数核对结果；不代表当前数据已核验。",
                    data={"count": historical_checks},
                )
        except FutureEvidenceError:
            add(
                "operations",
                "error",
                "操作记录最近核对时间晚于当前 Mac 时间超过十分钟。",
                "核对 Mac 时间和 state/operations 中的最近核对证据。",
            )
        except (OSError, UnicodeError, ValueError, TypeError, hg_domain.SafeError):
            add(
                "operations",
                "error",
                "操作记录无法读取或结构无效。",
                "检查 state/operations 中的本地证据。",
            )
        try:
            last = _json(hg_runtime.STATE / "last_run.json")
            history = historical_replacement_summary(last)
            if history is not None:
                last = {"run_at": last["finished_at"], "results": {}}
            if (
                not isinstance(last, dict)
                or not isinstance(last.get("run_at"), str)
                or not isinstance(last.get("results"), dict)
                or any(
                    not isinstance(result, str) or result not in RESULTS
                    for result in last["results"].values()
                )
            ):
                raise ValueError("invalid last run")
            ran_at = evidence_time(last["run_at"], reference_time)
            counts = dict(Counter(last["results"].values()))
            local_counts = last.get("local_status_counts", {})
            if not isinstance(local_counts, dict) or any(
                key not in RESULTS or type(count) is not int or count < 0
                for key, count in local_counts.items()
            ):
                raise ValueError("invalid local status counts")
            unknown_count = last.get("unknown_request_count", 0)
            if type(unknown_count) is not int or unknown_count < 0:
                raise ValueError("invalid unknown request count")
            archive_counts = last.get("source_archive")
            if "source_archive" in last:
                if (
                    not isinstance(archive_counts, dict)
                    or set(archive_counts)
                    != {"archived", "removed", "changed", "failed", "missing"}
                    or any(type(count) is not int or count < 0 for count in archive_counts.values())
                ):
                    raise ValueError("invalid source archive counts")
            statuses = set(counts) | {key for key, count in local_counts.items() if count > 0}
            stale = (
                cfg is not None
                and cfg["enabled"]
                and (reference_time - ran_at).total_seconds() > STALE_SECONDS
            )
            if unknown_count or UNCERTAIN_RESULTS.intersection(statuses):
                status = "error"
                message = "最近运行摘要包含请求结果未确定的记录。"
                hint = (
                    "先用 ./health-garmin-sync compare 查询并人工核对；结果未确定时不要重复提交。"
                )
                if "repair_uncertain" in statuses:
                    hint += " 有修复操作日志时用 ./health-garmin-sync verify-operation 核对。"
            elif REVIEW_RESULTS.intersection(statuses):
                status = "warn"
                message = "最近运行摘要包含需要人工处理的差异。"
                hint = "运行 ./health-garmin-sync compare 并人工核对；修复或冲突按对账清单处理。"
                if "remote_missing" in statuses:
                    hint += " 已确认缺失的记录按 resolve --absent 流程处理。"
            else:
                status = "info"
                message = "最近运行摘要可读取；所列结果不代表当前记录已核验。"
                hint = None
            if stale:
                if status == "info":
                    status = "warn"
                hint = (
                    hint + " " if hint else ""
                ) + "最近运行摘要距今超过两小时；请核对任务与设备状态。"
            data = {"run_at": ran_at.isoformat(), "results": counts}
            if history is not None:
                message = "历史完整替换已核验；不代表当前导出已核验。"
                data["historical_replacement"] = history
            if "unknown_request_count" in last:
                data["unknown_request_count"] = unknown_count
            if "local_status_counts" in last:
                data["local_status_counts"] = local_counts
                if status in ("error", "warn") and any(local_counts.values()):
                    hint = (
                        (hint + " " if hint else "")
                        + "本地状态为最近同步时的快照；运行 ./health-garmin-sync status 核对当前状态。"
                    )
            add("last_run", status, message, hint, data=data)
            if archive_counts is not None:
                failed_cleanup = archive_counts["failed"] > 0
                add(
                    "source_archive",
                    "warn" if failed_cleanup else "info",
                    "原始 JSON 已归档，但部分 inbox 文件未能清理。"
                    if failed_cleanup
                    else "最近同步的原始 JSON 归档计数可读取。",
                    "检查 inbox 与归档隔离目录权限；下次 ./health-garmin-sync run 会继续安全清理。"
                    if failed_cleanup
                    else None,
                    data=archive_counts,
                )
            if source_summary is not None and source_summary[0] > ran_at:
                latest_export, source_count = source_summary
                freshness = {
                    "latest_export_at": latest_export.isoformat(),
                    "last_run_at": ran_at.isoformat(),
                    "source_measurements": source_count,
                }
                if history is not None:
                    freshness["historically_verified_measurements"] = history["matched"]
                    freshness["measurement_count_difference"] = (
                        source_count - history["source_measurements"]
                    )
                add(
                    "source_freshness",
                    "warn",
                    "当前源导出比最近完成的运行证据更新；最新数据尚未由该证据核验。",
                    "导出时间与测量数量差不表示 Garmin 缺失；需要时运行 ./health-garmin-sync compare 只读核对。",
                    freshness,
                )
        except FileNotFoundError:
            add("last_run", "warn", "尚无最近运行摘要。")
        except FutureEvidenceError:
            add(
                "last_run",
                "error",
                "最近运行摘要时间晚于当前 Mac 时间超过十分钟。",
                "核对 Mac 时间和 state/last_run.json 中的时间证据。",
            )
        except (OSError, UnicodeError, ValueError, hg_domain.SafeError):
            add(
                "last_run",
                "error",
                "最近运行摘要无法读取或结构无效。",
                "检查 state/last_run.json。",
            )
        try:
            attempt = attempt_summary(
                _json(hg_runtime.STATE / "last_attempt.json"), reference_time=reference_time
            )
            outcome = attempt["outcome"]
            status = (
                "error"
                if outcome in ("failed", "interrupted")
                else "warn"
                if outcome == "running"
                else "info"
            )
            messages = {
                "running": "最近运行尚无结束证据；可能仍在运行或曾被中断。",
                "completed": "最近运行命令已结束；不代表全部记录已验证。",
                "preview": "最近运行只完成关闭自动写入时的本地预览。",
                "failed": "最近运行失败；请查看本地日志并先核对 Garmin。",
                "interrupted": "最近运行已中断；上传结果需核对，不能直接重发。",
            }
            add("last_attempt", status, messages[outcome], data=attempt)
        except FileNotFoundError:
            add("last_attempt", "info", "尚无运行尝试摘要；兼容已有安装。")
        except ClockOrderEvidenceError:
            add(
                "last_attempt",
                "error",
                "运行尝试摘要的结束时间早于开始时间。",
                "核对 Mac 时间和 state/last_attempt.json 中的时间证据。",
            )
        except FutureEvidenceError:
            add(
                "last_attempt",
                "error",
                "运行尝试摘要时间晚于当前 Mac 时间超过十分钟。",
                "核对 Mac 时间和 state/last_attempt.json 中的时间证据。",
            )
        except (OSError, UnicodeError, ValueError, hg_domain.SafeError):
            add(
                "last_attempt",
                "error",
                "运行尝试摘要无法读取或结构无效。",
                "检查 state/last_attempt.json。",
            )
        try:
            attempt = attempt_summary(
                _json(hg_runtime.STATE / "last_agent_attempt.json"),
                allowed_stages=("setup", "run"),
                allowed_outcomes=("running", "completed", "failed", "interrupted"),
                allow_http=False,
                reference_time=reference_time,
            )
            outcome = attempt["outcome"]
            status = (
                "error"
                if outcome in ("failed", "interrupted")
                else "warn"
                if outcome == "running"
                else "info"
            )
            messages = {
                "running": "最近后台入口尚无结束证据；可能仍在运行或曾被中断。",
                "completed": "最近后台入口阶段已结束；不代表同步完成或全部记录已验证。",
                "failed": "最近后台入口失败；请查看本地日志并核对运行摘要。",
                "interrupted": "最近后台入口已中断；先核对运行摘要与 Garmin，不要直接重发。",
            }
            latest = hg_domain.aware(attempt["finished_at"] or attempt["started_at"])
            stale = (
                cfg is not None
                and (cfg["enabled"] or cfg.get("health_export_enabled", False))
                and (reference_time - latest).total_seconds() > STALE_SECONDS
            )
            hint = None
            if stale:
                if status == "info":
                    status = "warn"
                hint = "后台入口摘要距今超过两小时；请核对定时任务、设备状态与本地日志。"
            add("last_agent_attempt", status, messages[outcome], hint, data=attempt)
        except FileNotFoundError:
            enabled = cfg is not None and (
                cfg["enabled"] or cfg.get("health_export_enabled", False)
            )
            add(
                "last_agent_attempt",
                "warn" if enabled else "info",
                "尚无后台入口摘要；兼容已有安装，不能据此确认后台执行。",
            )
        except ClockOrderEvidenceError:
            add(
                "last_agent_attempt",
                "error",
                "后台入口摘要的结束时间早于开始时间。",
                "核对 Mac 时间和 state/last_agent_attempt.json 中的时间证据。",
            )
        except FutureEvidenceError:
            add(
                "last_agent_attempt",
                "error",
                "后台入口摘要时间晚于当前 Mac 时间超过十分钟。",
                "核对 Mac 时间和 state/last_agent_attempt.json 中的时间证据。",
            )
        except (OSError, UnicodeError, ValueError, hg_domain.SafeError):
            add(
                "last_agent_attempt",
                "error",
                "后台入口摘要无法读取或结构无效。",
                "检查 state/last_agent_attempt.json。",
            )

    enabled = cfg is not None and (cfg["enabled"] or cfg.get("health_export_enabled", False))
    try:
        tokens = hg_runtime.STATE / "tokens"
        if tokens.is_dir() and not tokens.is_symlink():
            present = any(path.is_file() for path in tokens.iterdir())
            add(
                "tokens",
                "info" if present else "error" if enabled else "warn",
                "登录令牌文件存在；未读取或验证登录。" if present else "登录令牌目录为空。",
                None if present else "需要同步时运行 ./health-garmin-sync login。",
            )
        elif tokens.exists() or tokens.is_symlink():
            add("tokens", "error", "登录令牌路径不是有效本地目录。", "检查 state/tokens。")
        else:
            add(
                "tokens",
                "error" if enabled else "warn",
                "登录令牌目录尚不存在。",
                "需要同步时运行 ./health-garmin-sync login。",
            )
    except OSError:
        add("tokens", "error", "无法检查登录令牌文件是否存在。", "检查 state/tokens 的本地权限。")

    if sys.platform != "darwin":
        add("agent", "info", "当前系统非 macOS，跳过 LaunchAgent 检查。")
    else:
        plist = Path.home() / "Library/LaunchAgents" / f"{hg_runtime.LABEL}.plist"
        try:
            if plist.is_symlink():
                raise ValueError("symlink")
            with hg_file_io.open_regular_file(plist, "rb") as stream:
                actual = plistlib.load(stream)
            expected = hg_launchagent.agent_plist()
            if not isinstance(actual, dict):
                raise ValueError("invalid plist")
            mismatches = [key for key, value in expected.items() if actual.get(key) != value]
            if "Program" in actual and actual["Program"] != expected["ProgramArguments"][0]:
                mismatches.append("Program")
            if mismatches:
                add(
                    "agent_plist",
                    "error",
                    "任务配置不匹配：" + "、".join(mismatches) + "。",
                    "运行 ./health-garmin-sync install-agent 更新任务。",
                )
            else:
                add("agent_plist", "ok", "LaunchAgent 配置与当前项目匹配。")
        except FileNotFoundError:
            add(
                "agent_plist",
                "error" if enabled else "warn",
                "尚未安装 LaunchAgent。",
                "运行 ./health-garmin-sync install-agent。" if enabled else None,
            )
        except (
            OSError,
            ValueError,
            RecursionError,
            hg_domain.SafeError,
            plistlib.InvalidFileException,
            ExpatError,
        ):
            add(
                "agent_plist",
                "error",
                "LaunchAgent 配置无法读取或不是有效 plist。",
                "运行 ./health-garmin-sync install-agent 更新任务。",
            )
        try:
            service = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/{hg_runtime.LABEL}"],
                capture_output=True,
                timeout=5,
            )
            if service.returncode == 0:
                add("agent_service", "ok", "LaunchAgent 已载入；尚未证明后台执行成功。")
            else:
                add(
                    "agent_service",
                    "error" if enabled else "warn",
                    "LaunchAgent 未载入或服务查询未成功。",
                    "运行 ./health-garmin-sync install-agent，随后核对 ./health-garmin-sync doctor。"
                    if enabled
                    else None,
                )
        except subprocess.TimeoutExpired:
            add(
                "agent_service",
                "error",
                "LaunchAgent 服务查询超时。",
                "稍后重试 ./health-garmin-sync doctor。",
            )
        except OSError:
            add(
                "agent_service",
                "error",
                "launchctl 不可用或无法执行。",
                "检查本机 launchctl 和当前用户会话。",
            )

    counts = dict(Counter(check["status"] for check in checks))
    status = "error" if counts.get("error") else "warning" if counts.get("warn") else "ok"
    return {
        "status": status,
        "summary": {key: counts.get(key, 0) for key in ("ok", "info", "warn", "error")},
        "scope": SCOPE,
        "checks": checks,
    }


def doctor(json_output=False):
    report = collect()
    if json_output:
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    else:
        labels = {"ok": "通过", "info": "信息", "warn": "注意", "error": "错误"}
        overall = {"ok": "通过", "warning": "有提醒", "error": "有错误"}
        print("离线诊断：", overall[report["status"]])
        for check in report["checks"]:
            text = f"[{labels[check['status']]}] {check['message']}"
            if check.get("data"):
                text += " " + json.dumps(check["data"], ensure_ascii=False)
            if check.get("hint"):
                text += " " + check["hint"]
            print(text)
        print(SCOPE)
    return 2 if report["summary"]["error"] else 0
