"""Preview local measurements and coordinate bounded synchronization attempts."""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any

from . import domain as hg_domain
from . import file_io as hg_file_io
from . import garmin as hg_garmin
from . import processing as hg_processing
from . import reconciliation as hg_reconciliation
from . import runtime as hg_runtime
from . import sources as hg_sources
from . import state as hg_state


def show_preview(cfg: dict[str, Any]) -> list[hg_domain.Measurement]:
    samples, records, waiting, exports = hg_sources.load_measurements(cfg)
    print("本地预览：不登录 Garmin，不上传。")
    print("最近导出：", json.dumps(exports, ensure_ascii=False))
    print(f"唯一原始样本 {len(samples)}；完整测量 {len(records)}；等待配对 {len(waiting)}")
    print(
        f"字段覆盖：体重 {len(records)} 条；含体脂 {sum(r.fat is not None for r in records)} 条；"
        f"含 BMI {sum(r.bmi is not None for r in records)} 条；"
        f"含瘦体重 {sum(r.lbm is not None for r in records)} 条（仅本地保留）。缺失指标不会自动推算。"
    )
    with contextlib.closing(hg_state.database()) as db, db:
        for r in records:
            row = hg_state.state_row(db, r)
            s = row["status"] if row else "pending"
            if row and row["core"] != r.core:
                s = "changed"
            elif row and r.bmi is not None and json.loads(row["payload"]).get("bmi") != r.bmi:
                s = "needs_compare"
            bmi = "—" if r.bmi is None else f"{r.bmi:.2f}"
            fat = "—" if r.fat is None else f"{r.fat:.3f}%"
            lbm = "—" if r.lbm is None else f"{r.lbm:.3f} kg（仅本地）"
            print(
                f"{r.key}  {s:15s}  {r.at.isoformat()}  {r.weight:.3f} kg  {fat}  BMI={bmi}  LBM={lbm}"
            )
    for msg in waiting:
        print("WAIT", msg)
    return records


def finish_run_attempt(attempt: dict[str, Any], preserve_error: bool = False) -> None:
    attempt["finished_at"] = hg_runtime.now().isoformat()
    try:
        hg_file_io.write_json(hg_runtime.STATE / "last_attempt.json", attempt)
    except BaseException as exc:
        if not preserve_error:
            raise
        detail = type(exc).__name__
        try:
            print("运行结束证据未保存：", detail, file=sys.stderr)
            hg_runtime.LOG.error("Run attempt evidence not saved: %s", detail)
        except Exception:
            pass


def run_with_attempt(*, check_logging=None) -> None:
    """Record only a CLI run whose caller already holds the exclusive run lock."""
    attempt = {
        "outcome": "running",
        "started_at": hg_runtime.now().isoformat(),
        "finished_at": None,
        "stage": "config",
        "error_type": None,
    }
    hg_file_io.write_json(hg_runtime.STATE / "last_attempt.json", attempt)
    try:
        cfg = hg_runtime.config()
        attempt["stage"] = "sync"
        hg_file_io.write_json(hg_runtime.STATE / "last_attempt.json", attempt)
        if cfg["enabled"] or not cfg.get("health_export_enabled", False):
            run_sync(cfg)
        if cfg.get("health_export_enabled", False):
            from . import health_bridge

            health_bridge.run_configured(cfg)
        if check_logging is not None:
            check_logging()
    except KeyboardInterrupt as exc:
        attempt.update(outcome="interrupted", error_type=hg_domain.safe_exception(exc))
        finish_run_attempt(attempt, preserve_error=True)
        raise
    except Exception as exc:
        attempt.update(outcome="failed", error_type=hg_domain.safe_exception(exc))
        finish_run_attempt(attempt, preserve_error=True)
        raise
    else:
        attempt["outcome"] = (
            "completed" if cfg["enabled"] or cfg.get("health_export_enabled", False) else "preview"
        )
        finish_run_attempt(attempt)


def run_sync(cfg: dict[str, Any], record_id: str | None = None) -> None:
    if record_id is None and not cfg.get("enabled", False):
        print("自动写入未启用；本次只预览。启用前先完成单条试传。")
        show_preview(cfg)
        return
    source_snapshot = []
    samples, records, waiting, exports = hg_sources.load_measurements(
        cfg, source_snapshot=source_snapshot
    )
    source_records = records
    if record_id:
        records = [r for r in records if r.key == record_id]
        if len(records) != 1:
            raise hg_domain.SafeError(
                "ID 不对应唯一完整记录，请从 ./health-garmin-sync preview 复制。"
            )
        r = records[0]
        print(json.dumps(r.payload(), ensure_ascii=False, indent=2))
        if input("这会向 Garmin 中国区写入真实数据。确认请输入 UPLOAD：").strip() != "UPLOAD":
            print("已取消。")
            return
    summary = {
        "run_at": hg_runtime.now().isoformat(),
        "exports": exports,
        "complete_measurements": len(records),
        "waiting_pairs": len(waiting),
        "results": {},
        "local_status_counts": {},
        "unknown_request_count": 0,
    }
    if not records:
        dbpath = hg_runtime.STATE / "sync.sqlite3"
        if dbpath.exists() or dbpath.is_symlink():
            with contextlib.closing(hg_state.database()) as db, db:
                summary["local_status_counts"] = hg_state.local_status_counts(db)
                summary["unknown_request_count"] = hg_state.unknown_request_count(db)
        print("没有可用测量，无须访问 Garmin。")
        hg_file_io.write_json(hg_runtime.STATE / "last_run.json", summary)
        return
    with contextlib.closing(hg_state.database()) as db, db:
        selected = hg_reconciliation.select_comparison_records(
            records, db, full=record_id is not None
        )
        api = hg_garmin.api_client() if selected else None
        plan = hg_reconciliation.load_local_comparison(
            cfg, selected, api, db, source_records=source_records
        )
        summary["comparison"] = plan["counts"]
        hg_reconciliation.print_comparison(plan) if plan["entries"] else print(
            "近期没有需要复核的测量。"
        )
        by_id = {r.key: r for r in records}
        writes = 0
        recent = [hg_domain.aware(v) for k, v in exports.items() if k in ("weight", "body_fat")]
        settle_seconds = int(cfg["settle_seconds"])
        settling = (
            not record_id
            and settle_seconds > 0
            and recent
            and (hg_runtime.now() - max(recent)).total_seconds() < settle_seconds
        )
        for entry in plan["entries"]:
            r, action = by_id[entry["id"]], entry["action"]
            if action == "matched":
                hg_state.set_status(
                    db, r, "verified", "Remote comparison matched time and provided values"
                )
                result = "verified"
            elif action == "review_missing":
                hg_state.set_status(db, r, "remote_missing", entry["reason"])
                result = "remote_missing"
            elif action in ("repair", "replace", "conflict"):
                previous = entry["local_status"]
                # Source overlap can override an uncertain comparison action.
                # Resolving that overlap must never authorize retrying a request
                # whose outcome is still unknown.
                if previous in ("inflight", "uncertain", "repair_uncertain"):
                    blocked = previous
                    result = blocked
                else:
                    blocked = (
                        "verified_conflict"
                        if previous
                        in ("verified", "verified_manual", "verified_conflict", "remote_missing")
                        else "conflict"
                    )
                    result = action
                hg_state.set_status(db, r, blocked, entry["reason"])
            elif action == "uncertain":
                result = hg_processing.process_record(api, db, r)
            elif action == "missing" and not settling and writes < int(cfg["max_records_per_run"]):
                result = hg_processing.process_record(api, db, r)
                writes += 1
            else:
                result = "ignored" if action == "ignored" else "deferred"
            summary["results"][r.key] = result
            print(r.key, result)
            hg_runtime.LOG.info("record=%s status=%s", r.key, result)
        summary["local_status_counts"] = hg_state.local_status_counts(db)
        summary["unknown_request_count"] = hg_state.unknown_request_count(db)
        hg_file_io.write_json(
            hg_runtime.STATE / "last_sync_comparison.json",
            {"checked_at": hg_runtime.now().isoformat()},
        )
        if settling:
            summary["deferred"] = "settling"
            print("手机刚导出，新增测量留待下一轮处理。")
        elif cfg.get("archive_after_sync", False):
            from . import source_archive

            summary["source_archive"] = source_archive.archive_completed(
                cfg, source_snapshot, samples, source_records, db
            )
            print("原始 JSON 本地归档：", summary["source_archive"])
    hg_file_io.write_json(hg_runtime.STATE / "last_run.json", summary)
