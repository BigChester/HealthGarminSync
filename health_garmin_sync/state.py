"""SQLite upload evidence and durable operation journal helpers."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import domain as hg_domain
from . import file_io as hg_file_io
from . import runtime as hg_runtime


def database() -> sqlite3.Connection:
    dbpath = hg_runtime.STATE / "sync.sqlite3"
    if dbpath.is_symlink():
        raise hg_domain.SafeError("状态数据库不能是符号链接。")
    db = sqlite3.connect(dbpath, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS uploads (
           record_id TEXT PRIMARY KEY, core TEXT NOT NULL, payload TEXT NOT NULL,
           status TEXT NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS request_times (
           record_id TEXT PRIMARY KEY, wire_at TEXT NOT NULL)""")
        db.commit()
        dbpath.chmod(0o600)
    except BaseException:
        db.close()
        raise
    return db


def set_status(
    db: sqlite3.Connection,
    r: hg_domain.Measurement,
    status: str,
    detail: str = "",
    *,
    request: hg_domain.Measurement | None = None,
) -> None:
    previous = state_row(db, r)
    if (
        previous
        and previous["status"] in ("inflight", "uncertain", "repair_uncertain")
        and status in ("inflight", "uncertain", "repair_uncertain", "ignored")
    ):
        # Capture the old-format fallback before a source revision changes it.
        payload = json.loads(previous["payload"])
        original = hg_domain.Measurement(**{**payload, "at": hg_domain.aware(payload["at"])})
        db.execute(
            "INSERT OR IGNORE INTO request_times VALUES (?,?)",
            (r.key, hg_domain.measurement_wire_time(original).isoformat()),
        )
    elif status == "inflight":
        db.execute(
            "INSERT OR REPLACE INTO request_times VALUES (?,?)",
            (r.key, hg_domain.measurement_wire_time(request or r).isoformat()),
        )
    elif status not in ("uncertain", "repair_uncertain") and not (
        status == "ignored" and previous and previous["status"] == "ignored"
    ):
        db.execute("DELETE FROM request_times WHERE record_id=?", (r.key,))
    payload = r.payload()
    if r.lbm is None:
        if previous and previous["core"] == r.core:
            retained = json.loads(previous["payload"])
            if "lbm" in retained:
                payload["lbm"] = retained["lbm"]
    db.execute(
        """INSERT INTO uploads VALUES(?,?,?,?,?,?)
        ON CONFLICT(record_id) DO UPDATE SET core=excluded.core,
        payload=excluded.payload,status=excluded.status,
        updated_at=excluded.updated_at,detail=excluded.detail""",
        (
            r.key,
            r.core,
            json.dumps(payload, ensure_ascii=False),
            status,
            hg_runtime.now().isoformat(),
            detail,
        ),
    )
    db.commit()  # In-flight marker is durable BEFORE submitting a network write.


def get_status(db: sqlite3.Connection, r: hg_domain.Measurement) -> str:
    row = db.execute("SELECT * FROM uploads WHERE record_id=?", (r.key,)).fetchone()
    if not row:
        return "pending"
    if row["core"] != r.core:
        raise hg_domain.SafeError(f"记录 {r.key} 的体重/体脂已修订，必须人工核对已有上传。")
    return row["status"]


def state_row(db: sqlite3.Connection, r: hg_domain.Measurement) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM uploads WHERE record_id=?", (r.key,)).fetchone()


def local_status_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        row["status"]: row["n"]
        for row in db.execute("SELECT status,COUNT(*) AS n FROM uploads GROUP BY status")
    }


def unknown_request_count(db: sqlite3.Connection) -> int:
    return db.execute("""SELECT COUNT(*) FROM uploads
        WHERE status IN ('inflight','uncertain','repair_uncertain')
           OR (status='ignored' AND EXISTS
               (SELECT 1 FROM request_times WHERE request_times.record_id=uploads.record_id))""").fetchone()[
        0
    ]


def active_operations() -> set[str]:
    active = set()
    for path in (hg_runtime.STATE / "operations").glob("*.json"):
        if path.is_symlink():
            raise hg_domain.SafeError("操作记录不能是符号链接。")
        journal = json.loads(hg_file_io.read_regular_text(path))
        if journal.get("phase") not in ("verified", "deleted_verified", "closed_partial"):
            active.add(journal["key"])
            active.update(journal.get("record_ids", []))
    return active


def unknown_request_context(db: sqlite3.Connection) -> list[tuple[str, datetime, datetime, str]]:
    """Keep unknown writes associated with their time when an export renames a source."""
    context = []
    for row in db.execute("""SELECT uploads.record_id,payload,wire_at FROM uploads
                           LEFT JOIN request_times USING(record_id)
                           WHERE status IN ('inflight','uncertain','repair_uncertain')
                              OR (status='ignored' AND wire_at IS NOT NULL)"""):
        payload = json.loads(row["payload"])
        record = hg_domain.Measurement(**{**payload, "at": hg_domain.aware(payload["at"])})
        wire_at = (
            hg_domain.aware(row["wire_at"])
            if row["wire_at"] is not None
            else hg_domain.measurement_wire_time(record)
        )
        context.append((row["record_id"], record.at, wire_at, "未确定测量 " + row["record_id"]))
    return context + unknown_operation_context()


def unknown_operation_context() -> list[tuple[str, datetime, datetime, str]]:
    context = []
    for path in (hg_runtime.STATE / "operations").glob("*.json"):
        if path.is_symlink():
            raise hg_domain.SafeError("操作记录不能是符号链接。")
        journal = json.loads(hg_file_io.read_regular_text(path))
        if journal.get("phase") in ("verified", "deleted_verified", "closed_partial"):
            continue
        payloads = (
            journal.get("targets", [])
            if journal.get("type") == "full_replacement"
            else [journal.get("measurement") or journal.get("replacement")]
        )
        for payload in payloads:
            if payload:
                record = hg_domain.Measurement(**{**payload, "at": hg_domain.aware(payload["at"])})
                sent = (
                    journal.get("replacement")
                    if journal.get("type") != "full_replacement"
                    else None
                )
                request = (
                    hg_domain.Measurement(**{**sent, "at": hg_domain.aware(sent["at"])})
                    if sent
                    else record
                )
                context.append(
                    (
                        record.key,
                        record.at,
                        hg_domain.measurement_wire_time(request),
                        "未完成操作 " + path.name + "；测量 " + record.key,
                    )
                )
    return context


def verified_source_context(db: sqlite3.Connection) -> list[tuple[str, datetime, str]]:
    return [
        (
            row["record_id"],
            hg_domain.aware(json.loads(row["payload"])["at"]),
            "以前已验证的测量 " + row["record_id"],
        )
        for row in db.execute(
            "SELECT record_id,payload FROM uploads WHERE status IN ('verified','verified_manual','verified_conflict','remote_missing')"
        )
    ]


def read_operation(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise hg_domain.SafeError("请选择本工具生成的有效操作记录。")
    try:
        path.resolve().relative_to((hg_runtime.STATE / "operations").resolve())
    except ValueError:
        raise hg_domain.SafeError("操作记录必须位于本项目 state/operations 目录。")
    return json.loads(hg_file_io.read_regular_text(path))


def local_operation_record(
    journal: dict[str, Any], r: hg_domain.Measurement, db: sqlite3.Connection
) -> hg_domain.Measurement:
    """Keep Apple identity and the latest local LBM separate from replacement values."""
    payload = journal.get("measurement")
    if not payload:
        return r  # Legacy operations may contain only a replacement.
    local = hg_domain.Measurement(**{**payload, "at": hg_domain.aware(payload["at"])})
    if local.key != r.key:
        raise hg_domain.SafeError("操作记录中的 Apple 测量与替换目标不一致，不能保存本地状态。")
    previous = state_row(db, local)
    current_lbm = (
        json.loads(previous["payload"]).get("lbm")
        if previous and previous["core"] == local.core
        else None
    )
    lbm = local.lbm if local.lbm is not None else r.lbm
    if current_lbm is not None:
        lbm = current_lbm
    return hg_domain.Measurement(local.at, local.weight, local.fat, local.source, local.bmi, lbm)


def snapshot_state_changed(
    db: sqlite3.Connection, local: hg_domain.Measurement, *, protect_revision: bool = True
) -> bool:
    previous = state_row(db, local)
    if previous is None:
        return False
    if previous["status"] == "ignored":
        return True
    return protect_revision and (
        previous["core"] != local.core or json.loads(previous["payload"]).get("bmi") != local.bmi
    )


def operation_state_changed(
    journal: dict[str, Any], local: hg_domain.Measurement, db: sqlite3.Connection
) -> bool:
    return snapshot_state_changed(db, local, protect_revision=bool(journal.get("measurement")))


def save_snapshot_status(
    db: sqlite3.Connection,
    local: hg_domain.Measurement,
    status: str,
    detail: str,
    *,
    protect_revision: bool = True,
    request: hg_domain.Measurement | None = None,
) -> bool:
    """Save an operation snapshot without overwriting a revised or ignored task."""
    if snapshot_state_changed(db, local, protect_revision=protect_revision):
        print("本地测量已修订或忽略，保留现有上传状态。")
        return False
    previous = state_row(db, local)
    current_lbm = (
        json.loads(previous["payload"]).get("lbm")
        if previous and previous["core"] == local.core
        else None
    )
    if current_lbm is not None:
        local = hg_domain.Measurement(
            local.at, local.weight, local.fat, local.source, local.bmi, current_lbm
        )
    set_status(db, local, status, detail, request=request)
    return True


def save_verified_snapshot(
    db: sqlite3.Connection,
    local: hg_domain.Measurement,
    detail: str,
    *,
    protect_revision: bool = True,
) -> bool:
    return save_snapshot_status(db, local, "verified", detail, protect_revision=protect_revision)


def save_operation_verified(
    journal: dict[str, Any], r: hg_domain.Measurement, db: sqlite3.Connection, detail: str
) -> None:
    save_verified_snapshot(
        db,
        local_operation_record(journal, r, db),
        detail,
        protect_revision=bool(journal.get("measurement")),
    )


def record_failed_check(path: Path, journal: dict[str, Any]) -> None:
    """Record an unsuccessful query without replacing its original exception."""
    try:
        hg_file_io.write_json(
            path,
            {
                **journal,
                "last_checked_at": hg_runtime.now().isoformat(),
                "last_check_passed": False,
            },
        )
    except Exception as exc:
        detail = type(exc).__name__
        try:
            print("本次核对失败证据未保存：", detail, file=sys.stderr)
            hg_runtime.LOG.error("Operation check failure evidence not saved: %s", detail)
        except Exception:
            pass
