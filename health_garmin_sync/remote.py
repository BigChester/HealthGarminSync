"""Validate and compare complete Garmin weigh-in snapshots."""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import domain as hg_domain


def remote_time(row: dict[str, Any]) -> datetime | None:
    for field in ("timestampGMT", "gmtTimestamp", "date", "dateTimestamp", "timestamp"):
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            n = float(value)
            if 1e12 <= n < 1e14:
                return datetime.fromtimestamp(n / 1000, UTC)
            if 1e9 <= n < 1e11:
                return datetime.fromtimestamp(n, UTC)
        if isinstance(value, str):
            if re.fullmatch(r"\d{10}|\d{13}", value):
                return remote_time({"date": int(value)})
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is not None:
                return dt
            if field in ("timestampGMT", "gmtTimestamp") and "T" in value:
                return dt.replace(tzinfo=UTC)
    return None


def canonical_remote_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not text.isdigit():
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number > 0 else None


def canonical_remote_row(row: dict[str, Any]) -> dict[str, Any]:
    """Compare known ID representations without changing saved remote evidence."""
    result = dict(row)
    pk = canonical_remote_id(row.get("samplePk"))
    if pk is not None:
        result["samplePk"] = str(pk)
    return result


def original_id_absent(rows: list[dict[str, Any]], original_pk: Any, *, calendar: str) -> bool:
    """Confirm absence of the date + ID addressed by the SDK's DELETE endpoint."""

    def record_day(value):
        if not isinstance(value, str):
            return None
        value = value.strip()  # The pinned SDK strips before validating YYYY-MM-DD.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    target, original_day = canonical_remote_id(original_pk), record_day(calendar)
    if target is None or original_day is None:
        raise hg_domain.SafeError("原记录 ID 或日期无法可靠解析，不能确认删除。")
    unidentified = False
    for row in rows:
        candidate, day = (
            canonical_remote_id(row.get("samplePk")),
            record_day(row.get("calendarDate")),
        )
        if candidate == target and day == original_day:
            return False
        unidentified |= candidate is None or day is None
    if unidentified:
        raise hg_domain.SafeError(
            "Garmin 明细包含无法识别的记录 ID 或日期，不能确认原记录已不存在。"
        )
    return True


def classify_remote(r: hg_domain.Measurement, obj: Any) -> str:
    try:
        kind, _, _ = RemoteIndex(obj).decide(r)
    except hg_domain.SafeError:
        if isinstance(obj, dict) and isinstance(obj.get("dateWeightList"), list):
            return "conflict"
        raise
    return {"matched": "matched", "missing": "absent"}.get(kind, "conflict")


class RemoteIndex:
    """Index a fully validated snapshot by absolute measurement time."""

    def __init__(self, obj: Any):
        if not isinstance(obj, dict) or not isinstance(obj.get("dateWeightList"), list):
            raise hg_domain.SafeError("Garmin 返回结构未知，已停止写入。")
        self.rows = obj["dateWeightList"]
        dated = []
        for i, row in enumerate(self.rows):
            if not isinstance(row, dict) or remote_time(row) is None:
                raise hg_domain.SafeError("Garmin 测量时间无法可靠解析，已停止本轮对账。")
            dated.append((remote_time(row).timestamp(), i))
        dated.sort()
        self.times = [stamp for stamp, _ in dated]
        self.indices = [i for _, i in dated]

    def nearby(self, r: hg_domain.Measurement) -> list[int]:
        stamp = r.at.timestamp()
        return self.indices[
            bisect_left(self.times, stamp - 60) : bisect_right(self.times, stamp + 60)
        ]

    def decide(self, r: hg_domain.Measurement) -> tuple[str, list[int], str]:
        near = self.nearby(r)
        exact = [i for i in near if abs((remote_time(self.rows[i]) - r.at).total_seconds()) <= 1]
        if not near:
            return "missing", [], "Garmin 中没有对应测量"
        if len(exact) != 1:
            return "conflict", near, "时间附近存在多条记录或时间偏差，需人工核对"
        i = exact[0]
        row = self.rows[i]
        try:
            if isinstance(row["weight"], bool):
                raise ValueError()
            weight = float(row["weight"]) / 1000
            if not math.isfinite(weight) or not 0 < weight < 600:
                raise ValueError()
            desired = [(r.fat, row.get("bodyFat"), 0.021), (r.bmi, row.get("bmi"), 0.11)]
            missing = False
            different = abs(weight - r.weight) > 0.021
            for local, remote, tolerance in desired:
                if local is None:
                    continue  # Apple has no value: never erase Garmin's existing optional value.
                if remote is None:
                    missing = True
                else:
                    if isinstance(remote, bool):
                        raise ValueError()
                    value = float(remote)
                    if not math.isfinite(value) or not 0 < value < 100:
                        raise ValueError()
                    different |= abs(value - local) > tolerance
        except (KeyError, TypeError, ValueError, OverflowError):
            return "conflict", [i], "远端数值无法可靠解析"
        if different:
            return "replace", [i], "同次测量数值不同"
        if missing:
            return "repair", [i], "同次测量缺少 Apple 中已有的体脂或 BMI"
        return "matched", [i], "时间和 Apple 提供的数值一致"


def fetch_remote_snapshot(api: Any, start: date, end: date, *, targets=()) -> dict[str, Any]:
    """Fetch all chunks before writes, covering job dates and target UTC dates."""
    days = [start, end, *(r.at.astimezone(UTC).date() for r in targets)]
    cursor, final = min(days) - timedelta(days=1), max(days) + timedelta(days=1)
    rows, seen = [], {}
    while cursor <= final:
        stop = min(cursor + timedelta(days=30), final)
        obj = api.get_body_composition(cursor.isoformat(), stop.isoformat())
        index = RemoteIndex(obj)
        for row in index.rows:
            if row.get("samplePk") is None:
                rows.append(row)
                continue
            identity = (str(row["samplePk"]), str(row.get("calendarDate")))
            if identity in seen:
                if seen[identity] != row:
                    raise hg_domain.SafeError("分批查询中的同一 Garmin 记录发生变化，请重新对账。")
                continue
            seen[identity] = row
            rows.append(row)
        cursor = stop + timedelta(days=1)
    return {"dateWeightList": rows}
