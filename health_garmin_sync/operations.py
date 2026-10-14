"""Review, verify, and resume individual repair and deletion operations."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from . import domain as hg_domain
from . import exports as hg_exports
from . import file_io as hg_file_io
from . import garmin as hg_garmin
from . import processing as hg_processing
from . import reconciliation as hg_reconciliation
from . import remote as hg_remote
from . import runtime as hg_runtime
from . import sources as hg_sources
from . import state as hg_state


def reviewed_replacement_snapshot(
    obj: Any, target: hg_domain.Measurement, targets: list[hg_domain.Measurement]
) -> dict[str, Any]:
    """Exclude only uniquely matched, distinguishable reviewed source neighbours."""
    index = hg_remote.RemoteIndex(obj)
    decisions = [index.decide(r) for r in targets]
    owners = Counter(
        indices[0] for kind, indices, _ in decisions if kind in ("matched", "repair", "replace")
    )
    overlapping = set()
    ordered = sorted((r.at.timestamp(), i) for i, r in enumerate(targets))
    for (stamp, i), (next_stamp, j) in zip(ordered, ordered[1:]):
        if next_stamp - stamp <= 2:  # FIT seconds plus matching tolerance.
            overlapping.update((i, j))
    accepted = {
        indices[0]
        for i, (r, (kind, indices, _)) in enumerate(zip(targets, decisions))
        if r.key != target.key
        and i not in overlapping
        and kind == "matched"
        and owners[indices[0]] == 1
    }
    return {**obj, "dateWeightList": [row for i, row in enumerate(index.rows) if i not in accepted]}


class ReviewedReplacementAPI:
    """Revalidate reviewed neighbours on every replacement read, including POST preflight."""

    def __init__(
        self, api: Any, target: hg_domain.Measurement, targets: list[hg_domain.Measurement]
    ):
        self.api, self.target, self.targets = api, target, targets

    def __getattr__(self, name):
        return getattr(self.api, name)

    def get_body_composition(self, start: str, end: str):
        return reviewed_replacement_snapshot(
            self.api.get_body_composition(start, end), self.target, self.targets
        )


def reviewed_source_targets(
    replacement: hg_domain.Measurement,
    records: list[hg_domain.Measurement],
    saved: dict[str, Any],
    *,
    measurement: dict[str, Any] | None = None,
) -> list[hg_domain.Measurement]:
    reviewed = {entry["id"]: entry["measurement"] for entry in saved["entries"]}
    # Changed or newly exported sources can make an otherwise reviewed neighbour
    # ambiguous; include every current source when checking distinguishability.
    overlapping = set()
    ordered = sorted((r.at.timestamp(), r.key) for r in records)
    for (stamp, key), (next_stamp, next_key) in zip(ordered, ordered[1:]):
        if next_stamp - stamp <= 2:
            overlapping.update((key, next_key))
    if measurement is not None and (
        replacement.key in overlapping
        or not any(r.key == replacement.key and r.payload() == measurement for r in records)
    ):
        raise hg_domain.SafeError("待恢复的 Apple 测量已变化或来源重叠，请重新对账；不恢复上传。")
    return [
        replacement,
        *(
            r
            for r in records
            if r.key != replacement.key
            and r.key not in overlapping
            and reviewed.get(r.key) == r.payload()
        ),
    ]


def replacement_preflight(
    obj: Any,
    original: dict[str, Any],
    replacement: hg_domain.Measurement,
    records: list[hg_domain.Measurement],
    saved: dict[str, Any],
) -> list[hg_domain.Measurement]:
    targets = reviewed_source_targets(replacement, records, saved)
    remaining = {
        **obj,
        "dateWeightList": [row for row in hg_remote.RemoteIndex(obj).rows if row != original],
    }
    scoped = reviewed_replacement_snapshot(remaining, replacement, targets)
    if hg_remote.classify_remote(replacement, scoped) != "absent":
        raise hg_domain.SafeError(
            "删除后目标测量附近仍有未能唯一匹配已审阅 Apple 数据的记录，未删除；请重新对账。"
        )
    return targets


def unfinished_operation_deletes(history: dict[str, Any], remote_row: dict[str, Any]) -> bool:
    """Match an issued deletion by canonical ID and its original calendar date."""
    pk = hg_remote.canonical_remote_id(remote_row.get("samplePk"))
    if history.get("type") != "full_replacement":
        original = history.get("original") or {}
        return (
            hg_remote.canonical_remote_id(original.get("samplePk")) == pk
            and original.get("calendarDate") == remote_row["calendarDate"]
        )
    issued = [
        event
        for event in history.get("events", [])
        if event.get("kind") == "delete_intent"
        and hg_remote.canonical_remote_id(event.get("id")) == pk
    ]
    if not issued:
        return False  # Unrelated batches do not require their backup to be readable.
    try:
        calendars = set()
        for event in issued:
            if "calendarDate" not in event:
                continue
            calendar = event["calendarDate"]
            if (
                not isinstance(calendar, str)
                or date.fromisoformat(calendar).isoformat() != calendar
            ):
                raise ValueError("invalid deletion date")
            calendars.add(calendar)
        if any("calendarDate" not in event for event in issued):
            before = json.loads(
                hg_file_io.read_regular_text(Path(history["backup"]) / "remote-before.json")
            )
            originals = [
                row
                for row in hg_remote.RemoteIndex(before).rows
                if hg_remote.canonical_remote_id(row.get("samplePk")) == pk
            ]
            if len(originals) != 1:
                raise hg_domain.SafeError("删除意图无法关联唯一原记录。")
            calendars.add(date.fromisoformat(originals[0]["calendarDate"]).isoformat())
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        hg_domain.SafeError,
    ) as exc:
        raise hg_domain.SafeError(
            "未完成批次的删除日期无法可靠核对，请先核对操作记录；不重复删除。"
        ) from exc
    return remote_row["calendarDate"] in calendars


def reviewed_operation(
    args: argparse.Namespace, cfg: dict[str, Any], delete_extra: bool = False
) -> None:
    """Apply only the selected, unchanged item in a previously saved comparison."""
    path = Path(args.plan).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise hg_domain.SafeError("请选择本工具生成的有效对账清单。")
    try:
        path.resolve().relative_to((hg_runtime.STATE / "plans").resolve())
    except ValueError:
        raise hg_domain.SafeError("清单必须位于本项目 state/plans 目录。")
    saved = json.loads(hg_file_io.read_regular_text(path))
    for key in ("allowed_sources", "weight_bare_unit", "fat_bare_unit", "pair_window_seconds"):
        if saved["config"].get(key) != cfg.get(key):
            raise hg_domain.SafeError("来源、单位或配对设置已改变，请重新对账。")
    start, end = date.fromisoformat(saved["start"]), date.fromisoformat(saved["end"])
    source_cfg = dict(cfg, inbox=saved["config"]["inbox"], since=start.isoformat())
    _, records, waiting, _ = hg_sources.load_measurements(source_cfg)
    records = [r for r in records if start <= r.at.date() <= end]
    if not records or waiting:
        raise hg_domain.SafeError("Apple 导出为空或存在歧义，请重新对账。")
    api = hg_garmin.api_client()
    obj = hg_remote.fetch_remote_snapshot(api, start, end, targets=records)
    with contextlib.closing(hg_state.database()) as db, db:
        fresh = hg_reconciliation.comparison_plan(db, records, obj, start, end)

        def selected_extra(rows, identifier):
            pk = hg_remote.canonical_remote_id(identifier)
            return [
                r
                for r in rows
                if (
                    hg_remote.canonical_remote_id(r.get("samplePk")) == pk
                    if pk is not None
                    else str(r.get("samplePk")) == identifier
                )
            ]

        if delete_extra:
            if not args.complete_export:
                raise hg_domain.SafeError(
                    "删除多余记录需要 --complete-export，确认已完整导出选定日期内需要保留的 Apple 数据。"
                )
            old = selected_extra(saved["extra_remote"], args.remote_id)
            current = selected_extra(fresh["extra_remote"], args.remote_id)
            if (
                len(old) != 1
                or len(current) != 1
                or hg_remote.canonical_remote_row(old[0])
                != hg_remote.canonical_remote_row(current[0])
            ):
                raise hg_domain.SafeError("多余记录已变化或不再多余，请重新对账。")
            remote_row = current[0]
            measurement = None
            operation_key = "extra-" + args.remote_id
        else:
            old = [e for e in saved["entries"] if e["id"] == args.record_id]
            current = [e for e in fresh["entries"] if e["id"] == args.record_id]
            if len(old) != 1 or len(current) != 1:
                raise hg_domain.SafeError("清单中找不到唯一的指定 Apple 测量。")
            entry = current[0]
            if entry["action"] == "matched":
                r = next(r for r in records if r.key == args.record_id)
                hg_state.set_status(db, r, "verified", "Reviewed repair already matched; no write")
                print("远端已匹配，无需修复。")
                return
            if entry["action"] not in ("repair", "replace") or old[0]["action"] not in (
                "repair",
                "replace",
            ):
                raise hg_domain.SafeError(
                    "只能修复唯一对应的缺字段或数值不同记录；不处理歧义或 uncertain。"
                )
            if old[0]["measurement"] != entry["measurement"] or [
                hg_remote.canonical_remote_row(r) for r in old[0]["remote"]
            ] != [hg_remote.canonical_remote_row(r) for r in entry["remote"]]:
                raise hg_domain.SafeError("Apple 或 Garmin 数据已变化，请重新对账。")
            remote_row = entry["remote"][0]
            measurement = next(r for r in records if r.key == args.record_id)
            operation_key = measurement.key
            unsupported = [
                key
                for key in (
                    "muscleMass",
                    "skeletalMuscleMass",
                    "boneMass",
                    "bodyWater",
                    "waterMass",
                    "fatMass",
                    "percentHydration",
                    "visceralFat",
                    "visceralFatMass",
                    "visceralFatRating",
                    "metabolicAge",
                    "basalMet",
                    "activeMet",
                    "physiqueRating",
                )
                if remote_row.get(key) is not None
            ]
            if unsupported:
                raise hg_domain.SafeError(
                    "原记录包含尚不支持保留的身体成分字段，未删除：" + ", ".join(unsupported)
                )
            fat = measurement.fat
            bmi = measurement.bmi
            if fat is None and remote_row.get("bodyFat") is not None:
                fat = hg_exports.parse_quantity(str(remote_row["bodyFat"]) + "%", "body_fat", cfg)
            if bmi is None and remote_row.get("bmi") is not None:
                bmi = hg_exports.parse_quantity(remote_row["bmi"], "bmi", cfg)
            lbm = measurement.lbm
            if lbm is None:
                previous = hg_state.state_row(db, measurement)
                if previous and previous["core"] == measurement.core:
                    lbm = json.loads(previous["payload"]).get("lbm")
            replacement = hg_domain.Measurement(
                measurement.at, measurement.weight, fat, measurement.source, bmi, lbm
            )
            local = hg_domain.Measurement(
                measurement.at,
                measurement.weight,
                measurement.fat,
                measurement.source,
                measurement.bmi,
                lbm,
            )
        pk = str(remote_row.get("samplePk", ""))
        calendar = remote_row.get("calendarDate")
        if not pk.isdigit() or int(pk) <= 0 or not isinstance(calendar, str):
            raise hg_domain.SafeError("缺少明确的 Garmin 记录 ID 或 calendarDate，不能删除。")
        date.fromisoformat(calendar)
        if measurement:
            replacement_preflight(obj, remote_row, replacement, records, saved)
        operation_dir = hg_runtime.STATE / "operations"
        for existing in operation_dir.glob("*.json"):
            history = json.loads(hg_file_io.read_regular_text(existing))
            if history.get("phase") in ("verified", "deleted_verified", "closed_partial"):
                continue
            if history.get("key") == operation_key or unfinished_operation_deletes(
                history, remote_row
            ):
                raise hg_domain.SafeError(
                    "此记录有未完成的修改，请先核对 state/operations 中的操作记录，不重复删除。"
                )
        print("选定原记录：", json.dumps(remote_row, ensure_ascii=False))
        if measurement:
            print("Apple 目标测量：", json.dumps(measurement.payload(), ensure_ascii=False))
        confirm = "DELETE " + pk if delete_extra else "REPLACE " + measurement.key
        print(
            "将备份后删除选定的一条 Garmin 记录"
            + ("，并按 Apple 数值重建。" if measurement else "。")
        )
        if input("确认请输入 " + confirm + "：").strip() != confirm:
            print("已取消，未修改 Garmin。")
            return
        # The operator may spend time reviewing the prompt. Recheck both sides
        # after confirmation so the deletion still refers to the reviewed data.
        _, latest_records, latest_waiting, _ = hg_sources.load_measurements(source_cfg)
        latest_records = [r for r in latest_records if start <= r.at.date() <= end]
        if not latest_records or latest_waiting:
            raise hg_domain.SafeError("确认期间 Apple 数据已变化，请重新对账。")
        obj = hg_remote.fetch_remote_snapshot(api, start, end, targets=latest_records)
        latest = hg_reconciliation.comparison_plan(db, latest_records, obj, start, end)
        if delete_extra:
            candidates = selected_extra(latest["extra_remote"], pk)
            unchanged = [hg_remote.canonical_remote_row(r) for r in candidates] == [
                hg_remote.canonical_remote_row(remote_row)
            ]
        else:
            candidates = [e for e in latest["entries"] if e["id"] == measurement.key]
            unchanged = (
                len(candidates) == 1
                and candidates[0]["action"] in ("repair", "replace")
                and candidates[0]["measurement"] == measurement.payload()
                and [hg_remote.canonical_remote_row(r) for r in candidates[0]["remote"]]
                == [hg_remote.canonical_remote_row(remote_row)]
            )
        if not unchanged:
            raise hg_domain.SafeError("确认期间目标数据已变化，未删除；请重新对账。")
        remote_row = candidates[0] if delete_extra else candidates[0]["remote"][0]
        pk, calendar = str(remote_row["samplePk"]), remote_row["calendarDate"]
        if measurement:
            reviewed_targets = replacement_preflight(
                obj, remote_row, replacement, latest_records, saved
            )
        fresh = latest
        stamp = hg_runtime.now().strftime("%Y%m%dT%H%M%S%fZ")
        backup = hg_runtime.STATE / "backups" / stamp
        hg_file_io.create_backup_dir(backup)
        hg_file_io.write_json(backup / "remote.json", obj)
        hg_file_io.write_json(backup / "plan.json", {**fresh, "config": source_cfg})
        hg_file_io.write_json(backup / "config.json", cfg)
        backup_db = backup / "sync.sqlite3"
        with contextlib.closing(sqlite3.connect(backup_db)) as target, target:
            db.backup(target)
        backup_db.chmod(0o600)
        journal = {
            "key": operation_key,
            "phase": "deleting",
            "created_at": hg_runtime.now().isoformat(),
            "original": remote_row,
            "measurement": measurement.payload() if measurement else None,
            "replacement": replacement.payload() if measurement else None,
            "backup": str(backup),
        }
        journal_path = operation_dir / (stamp + ".json")
        hg_file_io.write_json(
            journal_path, journal, exclusive=True
        )  # Durable before DELETE, including the original record.
        if measurement:
            hg_state.set_status(
                db,
                local,
                "repair_uncertain",
                "Reviewed replacement started; do not resend or delete again",
            )
        try:
            api.delete_weigh_in(pk, calendar)
            remaining = hg_remote.fetch_remote_snapshot(api, start, end, targets=latest_records)
            if not hg_remote.original_id_absent(remaining["dateWeightList"], pk, calendar=calendar):
                raise hg_domain.SafeError("删除请求后原记录仍可见，暂停后续操作。")
            journal["phase"] = "deleted_verified" if delete_extra else "deleted"
            hg_file_io.write_json(journal_path, journal)
            if measurement:
                # Replacement optional values were validated before any DELETE.
                if not hg_state.save_snapshot_status(
                    db,
                    local,
                    "pending",
                    "Original removal confirmed; reviewed replacement permitted",
                ):
                    raise hg_domain.SafeError("本地测量已修订或忽略，停止恢复旧操作；请重新对账。")
                journal["phase"] = "uploading"
                hg_file_io.write_json(journal_path, journal)
                scoped_api = ReviewedReplacementAPI(api, replacement, reviewed_targets)
                result = hg_processing.process_record(
                    scoped_api, db, replacement, reviewed=True, local_record=local
                )
                journal["phase"] = "verified" if result == "verified" else "uncertain"
                hg_file_io.write_json(journal_path, journal)
                if result != "verified":
                    raise hg_domain.SafeError(
                        "修复提交后尚未读回匹配，停止；只读核对，不重复提交。"
                    )
                hg_state.save_verified_snapshot(
                    db,
                    local,
                    "Reviewed replacement matched Apple values; optional remote fields preserved",
                )
            print("操作完成；备份：", backup)
        except Exception as exc:
            journal["phase"] = "uncertain"
            journal["error"] = hg_domain.safe_exception(exc)
            hg_file_io.write_json(journal_path, journal)
            raise hg_domain.SafeError(
                "操作结果未确定，已保存备份和操作记录；停止，不重复提交："
                + hg_domain.safe_exception(exc)
            ) from exc


def verify_operation(args: argparse.Namespace) -> bool:
    path = Path(args.operation).expanduser()
    journal = hg_state.read_operation(path)
    if journal.get("type") == "full_replacement":
        from . import history as hg_history

        return hg_history.verify(path)
    day = date.fromisoformat(journal["original"]["calendarDate"])
    payload = journal.get("replacement") or journal.get("measurement")
    r = (
        hg_domain.Measurement(**{**payload, "at": hg_domain.aware(payload["at"])})
        if payload
        else None
    )
    original_pk = str(journal["original"]["samplePk"]) if r is None else None
    try:
        obj = hg_remote.fetch_remote_snapshot(
            hg_garmin.api_client(), day, day, targets=[r] if r else ()
        )
        if r is not None:
            classification = hg_remote.classify_remote(r, obj)
            matched = classification == "matched"
            result = {"matched": "匹配", "absent": "目标缺失", "conflict": "目标冲突"}[
                classification
            ]
        else:
            matched = hg_remote.original_id_absent(
                obj["dateWeightList"], original_pk, calendar=journal["original"]["calendarDate"]
            )
            result = "原记录已不存在" if matched else "原记录仍存在"
    except Exception:
        hg_state.record_failed_check(path, journal)
        raise
    if r is not None:
        if matched:
            with contextlib.closing(hg_state.database()) as db, db:
                hg_state.save_operation_verified(
                    journal, r, db, "Delayed reviewed-operation verification matched"
                )
            journal["phase"] = "verified"
    else:
        if matched:
            journal["phase"] = "deleted_verified"
    journal["last_checked_at"] = hg_runtime.now().isoformat()
    journal["last_check_passed"] = matched
    hg_file_io.write_json(path, journal)
    print("本次只读核对：", result, "；操作历史阶段：", journal["phase"])
    return matched


class ResumedReplacementAPI:
    """Recheck raw identity and current reviewed sources before every resume read."""

    def __init__(self, api: Any, target: hg_domain.Measurement, journal: dict[str, Any]):
        self.api, self.target, self.journal = api, target, journal
        self.context_needed = False

    def __getattr__(self, name):
        return getattr(self.api, name)

    def get_body_composition(self, start: str, end: str):
        obj = self.api.get_body_composition(start, end)
        kind = hg_remote.classify_remote(self.target, obj)
        if kind == "matched":
            return obj  # Read-only completion does not require source access.
        if not hg_remote.original_id_absent(
            hg_remote.RemoteIndex(obj).rows,
            self.journal["original"]["samplePk"],
            calendar=self.journal["original"]["calendarDate"],
        ):
            raise hg_domain.SafeError("原 Garmin 记录仍存在，停止；不会重复删除。")
        with contextlib.closing(hg_state.database()) as db:
            local = hg_state.local_operation_record(self.journal, self.target, db)
            if hg_state.operation_state_changed(self.journal, local, db):
                raise hg_domain.SafeError("本地测量已修订或忽略，不能恢复旧操作；请重新对账。")
        if kind == "absent" and not self.context_needed:
            return obj
        backup = self.journal.get("backup")
        if not backup:
            raise hg_domain.SafeError("远端存在冲突，需重新核对；不恢复上传。")
        self.context_needed = True
        backup = Path(backup)
        saved = json.loads(hg_file_io.read_regular_text(backup / "plan.json"))
        cfg = saved.get("config")
        if cfg is None:
            cfg = json.loads(hg_file_io.read_regular_text(backup / "config.json"))
        source_cfg = dict(cfg, since=saved["start"])
        _, records, waiting, _ = hg_sources.load_measurements(source_cfg)
        if waiting:
            raise hg_domain.SafeError("Apple 导出存在配对歧义，请重新对账；不恢复上传。")
        targets = reviewed_source_targets(
            self.target, records, saved, measurement=self.journal["measurement"]
        )
        scoped = reviewed_replacement_snapshot(obj, self.target, targets)
        if hg_remote.classify_remote(self.target, scoped) != "absent":
            raise hg_domain.SafeError(
                "目标附近记录无法唯一匹配未变化的已审阅 Apple 数据，请重新对账；不恢复上传。"
            )
        return scoped


def resume_operation(args: argparse.Namespace) -> None:
    path = Path(args.operation).expanduser()
    journal = hg_state.read_operation(path)
    if journal.get("type") == "full_replacement":
        raise hg_domain.SafeError(
            "完整替换操作先用 verify-operation 只读核对；不自动重复删除或上传。"
        )
    payload = journal.get("replacement") or journal.get("measurement")
    if not payload:
        raise hg_domain.SafeError("删除操作仅用 verify-operation 核对，不重复删除。")
    r = hg_domain.Measurement(**{**payload, "at": hg_domain.aware(payload["at"])})
    api = ResumedReplacementAPI(hg_garmin.api_client(), r, journal)
    obj = hg_processing.remote_for(api, r)
    original_pk = str(journal["original"]["samplePk"])
    if hg_remote.classify_remote(r, obj) == "matched":
        with contextlib.closing(hg_state.database()) as db, db:
            hg_state.save_operation_verified(
                journal, r, db, "Reviewed operation already matched; no resume upload"
            )
        journal["phase"] = "verified"
        hg_file_io.write_json(path, journal)
        print("远端已匹配，无需继续上传。")
        return
    if not hg_remote.original_id_absent(
        hg_remote.RemoteIndex(obj).rows, original_pk, calendar=journal["original"]["calendarDate"]
    ):
        raise hg_domain.SafeError("原 Garmin 记录仍存在，停止；不会重复删除。")
    if hg_remote.classify_remote(r, obj) != "absent":
        raise hg_domain.SafeError("远端存在冲突，需重新核对；不恢复上传。")
    print("原记录已查不到；待恢复测量：", json.dumps(r.payload(), ensure_ascii=False))
    if (
        input("在 Garmin 确认目标测量确实不存在后，输入 RESUME " + r.key + "：").strip()
        != "RESUME " + r.key
    ):
        print("已取消。")
        return
    obj = hg_processing.remote_for(api, r)
    if hg_remote.classify_remote(r, obj) == "matched":
        with contextlib.closing(hg_state.database()) as db, db:
            hg_state.save_operation_verified(
                journal, r, db, "Reviewed operation matched during confirmation; no upload"
            )
        journal["phase"] = "verified"
        hg_file_io.write_json(path, journal)
        print("远端已匹配，无需继续上传。")
        return
    if (
        not hg_remote.original_id_absent(
            hg_remote.RemoteIndex(obj).rows,
            original_pk,
            calendar=journal["original"]["calendarDate"],
        )
        or hg_remote.classify_remote(r, obj) != "absent"
    ):
        raise hg_domain.SafeError("确认期间远端记录发生变化，不恢复上传。")
    journal["phase"] = "uploading"
    journal["resumed_at"] = hg_runtime.now().isoformat()
    hg_file_io.write_json(path, journal)
    with contextlib.closing(hg_state.database()) as db, db:
        local = hg_state.local_operation_record(journal, r, db)
        try:
            if not hg_state.save_snapshot_status(
                db,
                local,
                "pending",
                "User confirmed remote absence for reviewed operation resume",
                protect_revision=bool(journal.get("measurement")),
            ):
                raise hg_domain.SafeError("本地测量已修订或忽略，不能恢复旧操作；请重新对账。")
            result = hg_processing.process_record(api, db, r, reviewed=True, local_record=local)
        except Exception as exc:
            journal.update(phase="uncertain", error=hg_domain.safe_exception(exc))
            for save in (
                lambda: hg_state.save_snapshot_status(
                    db, local, "repair_uncertain", "Resume preflight failed; do not resend"
                ),
                lambda: hg_file_io.write_json(path, journal),
            ):
                try:
                    save()
                except Exception as secondary:
                    try:
                        print("恢复失败证据未保存：", type(secondary).__name__, file=sys.stderr)
                    except Exception:
                        pass
            raise
    journal["phase"] = "verified" if result == "verified" else "uncertain"
    hg_file_io.write_json(path, journal)
    print("恢复结果：", result, "；不会自动重发。")
    if result != "verified":
        raise hg_domain.SafeError("恢复请求尚未读回验证，请使用 verify-operation 继续只读核对。")


def resolve_record(args: argparse.Namespace) -> None:
    with contextlib.closing(hg_state.database()) as db, db:
        row = db.execute("SELECT * FROM uploads WHERE record_id=?", (args.record_id,)).fetchone()
        if not row:
            raise hg_domain.SafeError("没有这个 ID 的本地上传状态。")
        r = hg_domain.Measurement(
            **{
                **json.loads(row["payload"]),
                "at": hg_domain.aware(json.loads(row["payload"])["at"]),
            }
        )
        print(json.dumps(r.payload(), ensure_ascii=False, indent=2))
        print("本工具不会删除或覆盖任何 Garmin 记录。请先在 Garmin 核对日期、时间、体重、体脂率。")
        if args.present:
            if input("确认 Garmin 已存在这条正确记录，输入 PRESENT：").strip() == "PRESENT":
                hg_state.set_status(db, r, "verified_manual", "User verified in Garmin")
        elif args.absent:
            if r.key in hg_state.active_operations():
                raise hg_domain.SafeError(
                    "此测量有未完成的修复操作，请使用 verify-operation 或 resume-operation 保留备份字段。"
                )
            if alias := hg_reconciliation.unknown_source_alias(
                r, hg_state.unknown_request_context(db)
            ):
                raise hg_domain.SafeError(alias + "；请先核对旧测量或操作。")
            if row["status"] in ("verified", "verified_manual"):
                raise hg_domain.SafeError("已验证记录不开放自动重置，避免误重复。")
            if (
                input("确认 Garmin 未收到该记录，允许下一次尝试上传，输入 ABSENT：").strip()
                == "ABSENT"
            ):
                hg_state.set_status(db, r, "pending", "User explicitly confirmed absence")
        elif args.ignore:
            if input("仅忽略这条本地任务，输入 IGNORE：").strip() == "IGNORE":
                hg_state.set_status(db, r, "ignored", "User chose not to upload")
