"""Parse Garmin blood-pressure responses without network or health-store writes."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import domain as hg_domain

_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?"
)


@dataclass(frozen=True)
class BloodPressureMeasurement:
    source_id: str
    at: datetime
    local_timestamp: str
    source_type: str
    systolic: float
    diastolic: float
    pulse: float | None
    multi_measurement: bool

    def payload(self) -> dict[str, Any]:
        """Return only normalized measurement fields, preserving time precision."""
        at = self.at.astimezone(UTC)
        precision = "microseconds" if at.microsecond % 1000 else "milliseconds"
        return {
            "source_id": self.source_id,
            "at": at.isoformat(timespec=precision).replace("+00:00", "Z"),
            "local_timestamp": self.local_timestamp,
            "source_type": self.source_type,
            "systolic": self.systolic,
            "diastolic": self.diastolic,
            "pulse": self.pulse,
            "multi_measurement": self.multi_measurement,
        }


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise hg_domain.SafeError(f"血压记录的 {field} 必须为完整 ISO 8601 时间。")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise hg_domain.SafeError(f"血压记录的 {field} 不是有效时间。") from exc


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise hg_domain.SafeError(f"血压记录的 {field} 必须为正有限数值。")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise hg_domain.SafeError(f"血压记录的 {field} 必须为正有限数值。") from exc
    if not math.isfinite(result) or result <= 0:
        raise hg_domain.SafeError(f"血压记录的 {field} 必须为正有限数值。")
    return result


def _source_id(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return value
    raise hg_domain.SafeError("血压记录的 version 必须为正整数或规范十进制字符串。")


def _measurement(row: Any) -> BloodPressureMeasurement:
    if not isinstance(row, dict):
        raise hg_domain.SafeError("血压 measurements 中必须为记录词典。")
    source_id = _source_id(row.get("version"))
    at = _timestamp(row.get("measurementTimestampGMT"), "measurementTimestampGMT")
    # Garmin's GMT field explicitly identifies UTC, even when it lacks an offset.
    try:
        at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise hg_domain.SafeError("血压记录的 measurementTimestampGMT 无法转换为 UTC。") from exc
    local_timestamp = row.get("measurementTimestampLocal")
    _timestamp(local_timestamp, "measurementTimestampLocal")
    source_type = row.get("sourceType")
    if not isinstance(source_type, str) or not source_type.strip():
        raise hg_domain.SafeError("血压记录的 sourceType 必须为非空文本。")
    pulse = row.get("pulse")
    multi_measurement = row.get("multiMeasurement", False)
    if not isinstance(multi_measurement, bool):
        raise hg_domain.SafeError("血压记录的 multiMeasurement 必须为布尔值。")
    return BloodPressureMeasurement(
        source_id=source_id,
        at=at,
        local_timestamp=local_timestamp,
        source_type=source_type,
        systolic=_positive_number(row.get("systolic"), "systolic"),
        diastolic=_positive_number(row.get("diastolic"), "diastolic"),
        pulse=None if pulse is None else _positive_number(pulse, "pulse"),
        multi_measurement=multi_measurement,
    )


def parse_blood_pressure(response: Any) -> list[BloodPressureMeasurement]:
    """Read the measurementSummaries envelope; reject incomplete or ambiguous data.

    Count each summary before deduplication. Measurements stay individual records,
    with their original multiMeasurement flag; no averaging or clinical range filter
    is applied. Missing and null pulse values both mean no accompanying pulse.
    """
    if not isinstance(response, dict):
        raise hg_domain.SafeError("血压响应必须为包含 measurementSummaries 的词典。")
    summaries = response.get("measurementSummaries")
    if not isinstance(summaries, list):
        raise hg_domain.SafeError("血压响应的 measurementSummaries 必须为数组。")
    records: dict[str, BloodPressureMeasurement] = {}
    for summary in summaries:
        if not isinstance(summary, dict):
            raise hg_domain.SafeError("血压 measurementSummaries 中必须为汇总词典。")
        rows = summary.get("measurements")
        if not isinstance(rows, list):
            raise hg_domain.SafeError("血压汇总的 measurements 必须为数组。")
        if "numOfMeasurements" in summary:
            count = summary["numOfMeasurements"]
            if isinstance(count, bool) or not isinstance(count, int) or count != len(rows):
                raise hg_domain.SafeError("血压汇总的 numOfMeasurements 与记录数量不一致。")
        for row in rows:
            record = _measurement(row)
            previous = records.get(record.source_id)
            if previous is not None and previous != record:
                raise hg_domain.SafeError("同一血压 version 对应不同内容或时间，记录存在歧义。")
            records[record.source_id] = record
    return sorted(records.values(), key=lambda record: (record.at, record.source_id))
