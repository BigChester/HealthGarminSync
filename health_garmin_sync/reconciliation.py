"""Build remote comparison plans and preserve unknown-request exclusions."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import domain as hg_domain
from . import file_io as hg_file_io
from . import garmin as hg_garmin
from . import remote as hg_remote
from . import runtime as hg_runtime
from . import sources as hg_sources
from . import state as hg_state


def source_alias_overlap(
    r: hg_domain.Measurement, key: str, at: datetime, wire_at: datetime | None = None
) -> bool:
    if key == r.key:
        return False
    current, previous = r.at.astimezone(UTC), at.astimezone(UTC)
    return (
        abs((current - previous).total_seconds()) <= 1
        or wire_at is not None
        and (
            abs((hg_domain.measurement_wire_time(r) - previous).total_seconds()) <= 1
            or abs((current - wire_at).total_seconds()) <= 1
        )
    )


def source_alias_detail(
    r: hg_domain.Measurement, context: list[tuple[str, datetime, str]]
) -> str | None:
    for key, at, detail in context:
        if source_alias_overlap(r, key, at):
            return detail
    return None


def unknown_source_alias(
    r: hg_domain.Measurement, context: list[tuple[str, datetime, datetime, str]]
) -> str | None:
    for key, at, wire_at, detail in context:
        if source_alias_overlap(r, key, at, wire_at):
            return detail + " 与当前测量时间重叠；来源或时间修订不能授权重复提交"
    return None


def missing_source_alias(
    r: hg_domain.Measurement, context: list[tuple[str, datetime, str]]
) -> str | None:
    if detail := source_alias_detail(r, context):
        return detail + " 与当前测量时间重叠且远端缺失；核对后用 resolve --absent 允许补传"
    return None


def comparison_plan(
    db: sqlite3.Connection,
    records: list[hg_domain.Measurement],
    obj: Any,
    start: date,
    end: date,
    *,
    selected_ids: set[str] | None = None,
) -> dict[str, Any]:
    index = hg_remote.RemoteIndex(obj)
    entries, claimed, owners = [], set(), {}
    active = hg_state.active_operations()
    unknown = hg_state.unknown_request_context(db)
    verified = hg_state.verified_source_context(db)
    for r in records:
        kind, candidates, reason = index.decide(r)
        # A unique exact row does not own unrelated measurements in its minute.
        # Ambiguous times or unknown values still protect the whole near range.
        claimed.update(index.nearby(r) if kind == "conflict" else candidates)
        previous = hg_state.state_row(db, r)
        status = previous["status"] if previous else "pending"
        action = kind
        if status == "ignored":
            action, reason = "ignored", "用户已明确忽略"
        elif kind != "matched" and (
            status in ("inflight", "uncertain", "repair_uncertain") or r.key in active
        ):
            action, reason = "uncertain", "之前的请求结果未确定，只读核对，不重复提交"
        elif kind != "matched" and (alias := unknown_source_alias(r, unknown)):
            action, reason = "uncertain", alias
        elif (
            kind == "missing"
            and not (previous and status == "pending")
            and (alias := missing_source_alias(r, verified))
        ):
            action, reason = "review_missing", alias
        elif kind == "missing" and status in (
            "verified",
            "verified_manual",
            "verified_conflict",
            "remote_missing",
        ):
            action, reason = (
                "review_missing",
                "以前已验证的记录远端缺失；核对后用 resolve --absent 允许补传",
            )
        entry = {
            "id": r.key,
            "action": action,
            "reason": reason,
            "local_status": status,
            "measurement": r.payload(),
            "remote": [index.rows[i] for i in candidates],
        }
        entries.append(entry)
        if len(candidates) == 1:
            owners.setdefault(candidates[0], []).append(entry)
    for group in owners.values():
        if len(group) > 1:
            for entry in group:
                if entry["action"] != "ignored":
                    entry.update(action="conflict", reason="多条 Apple 测量指向同一 Garmin 记录")
    local_times = sorted((r.at.timestamp(), i) for i, r in enumerate(records))
    for (stamp, i), (next_stamp, j) in zip(local_times, local_times[1:]):
        if next_stamp - stamp <= 1:
            for k in (i, j):
                if entries[k]["action"] != "ignored":
                    entries[k].update(
                        action="conflict", reason="多条 Apple 测量的时间重叠，不能唯一确定对应关系"
                    )
    for r, entry in zip(records, entries):
        if entry["action"] == "conflict":
            claimed.update(index.nearby(r))
    extras = []
    for i, row in enumerate(index.rows):
        calendar = row.get("calendarDate")
        try:
            day = (
                date.fromisoformat(calendar)
                if isinstance(calendar, str)
                else hg_remote.remote_time(row).astimezone().date()
            )
        except ValueError:
            raise hg_domain.SafeError("Garmin calendarDate 无法解析。")
        if i not in claimed and start <= day <= end:
            extras.append(row)
    if selected_ids is not None:
        # All current sources establish correspondence, even when completed
        # history is not selected for another remote check or status update.
        entries = [entry for entry in entries if entry["id"] in selected_ids]
    return {
        "created_at": hg_runtime.now().isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "entries": entries,
        "extra_remote": extras,
        "remote_snapshot": obj,
        "counts": dict(Counter(e["action"] for e in entries)),
    }


def print_comparison(plan: dict[str, Any]) -> None:
    print("Garmin 对账范围：", plan["start"], "至", plan["end"])
    for entry in plan["entries"]:
        r = entry["measurement"]
        fat = "—" if r["fat"] is None else f"{r['fat']:.3f}%"
        print(
            f"{entry['id']}  {entry['action']:15s}  {r['at']}  {r['weight']:.3f} kg  {fat}  {entry['reason']}"
        )
    print("分类统计：", json.dumps(plan["counts"], ensure_ascii=False))
    print(
        "Garmin 多余记录：", len(plan["extra_remote"]), "（仅限此次对账日期和来源范围，保留待核对）"
    )
    for row in plan["extra_remote"]:
        print(
            "EXTRA",
            row.get("samplePk"),
            row.get("calendarDate"),
            hg_remote.remote_time(row).isoformat(),
            row.get("weight"),
            row.get("bodyFat"),
        )


def save_comparison(plan: dict[str, Any], cfg: dict[str, Any], *, automatic: bool = False) -> Path:
    plan["config"] = {
        key: cfg.get(key)
        for key in (
            "inbox",
            "allowed_sources",
            "weight_bare_unit",
            "fat_bare_unit",
            "pair_window_seconds",
        )
    }
    name = (
        "sync-latest.json"
        if automatic
        else "compare-" + hg_runtime.now().strftime("%Y%m%dT%H%M%S%fZ") + ".json"
    )
    path = hg_runtime.STATE / "plans" / name
    hg_file_io.write_json(path, plan)
    hg_file_io.write_json(
        hg_runtime.STATE / "last_compare.json",
        {
            "created_at": plan["created_at"],
            "path": str(path),
            "counts": plan["counts"],
            "extra_remote": len(plan["extra_remote"]),
        },
    )
    return path


def compare_exports(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(cfg)
    if args.inbox:
        cfg["inbox"] = str(Path(args.inbox).expanduser().resolve())
    start = date.fromisoformat(args.since or cfg["since"])
    end = date.fromisoformat(args.until) if args.until else hg_runtime.now().astimezone().date()
    if start > end:
        raise hg_domain.SafeError("对账起始日期不能晚于结束日期。")
    cfg["since"] = start.isoformat()
    _, records, waiting, _ = hg_sources.load_measurements(cfg)
    records = [r for r in records if start <= r.at.date() <= end]
    if waiting:
        raise hg_domain.SafeError("有歧义样本尚未配对，请先处理 preview 中的 WAIT。")
    if not records:
        raise hg_domain.SafeError("选定范围没有可用 Apple 测量，不能据此生成清理清单。")
    api = hg_garmin.api_client()
    obj = hg_remote.fetch_remote_snapshot(api, start, end, targets=records)
    with contextlib.closing(hg_state.database()) as db, db:
        plan = comparison_plan(db, records, obj, start, end)
    print_comparison(plan)
    path = save_comparison(plan, cfg)
    print("只读对账完成，未修改 Garmin。清单：", path)
    return plan


def select_comparison_records(
    records: list[hg_domain.Measurement], db: sqlite3.Connection, full: bool = False
) -> list[hg_domain.Measurement]:
    # The first run checks all retained history. Later runs recheck recent data
    # and every pending, uncertain, changed or remotely missing older record.
    recent = hg_runtime.now().astimezone().date() - timedelta(days=30)
    selected = []
    first = full or not (hg_runtime.STATE / "last_sync_comparison.json").exists()
    for r in records:
        previous = hg_state.state_row(db, r)
        bmi_changed = (
            previous is not None
            and r.bmi is not None
            and json.loads(previous["payload"]).get("bmi") != r.bmi
        )
        if (
            first
            or r.at.astimezone().date() >= recent
            or previous is None
            or previous["status"] not in ("verified", "verified_manual", "ignored")
            or previous["core"] != r.core
            or bmi_changed
        ):
            selected.append(r)
        elif r.lbm is not None:
            payload = json.loads(previous["payload"])
            if payload.get("lbm") != r.lbm:
                # LBM is local-only; preserve the existing verification evidence.
                payload["lbm"] = r.lbm
                db.execute(
                    "UPDATE uploads SET payload=? WHERE record_id=?",
                    (json.dumps(payload, ensure_ascii=False), r.key),
                )
    return selected


def load_local_comparison(
    cfg: dict[str, Any],
    records: list[hg_domain.Measurement],
    api: Any,
    db: sqlite3.Connection,
    *,
    source_records: list[hg_domain.Measurement] | None = None,
) -> dict[str, Any]:
    if not records:
        return {"entries": [], "counts": {}, "extra_remote": []}
    start, end = min(r.at.date() for r in records), max(r.at.date() for r in records)
    obj = hg_remote.fetch_remote_snapshot(api, start, end, targets=records)
    plan = comparison_plan(
        db,
        source_records if source_records is not None else records,
        obj,
        start,
        end,
        selected_ids={r.key for r in records},
    )
    save_comparison(plan, cfg, automatic=True)
    return plan
