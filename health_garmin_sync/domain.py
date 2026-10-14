"""Measurement models, safe errors, and shared value normalization."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SUPPORTED_SCHEMAS = ("1", "2")

SUPPORTED_METRICS = ("weight", "body_fat", "bmi", "lbm")


class SafeError(Exception):
    """Error whose text contains neither login credentials nor tokens."""


class TokenPersistenceError(SafeError):
    """Renewed credentials were not saved; stop this client's API requests."""


def aware(text: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SafeError("时间必须为 ISO 8601，例如 2026-09-29T08:00:00.000+08:00。") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise SafeError("时间缺少时区：使用格式 yyyy-MM-dd'T'HH:mm:ss.SSSXXX。")
    return dt


def normalized_source(value: Any) -> str:
    return str(value).strip().casefold()


@dataclass(frozen=True)
class Sample:
    metric: str
    at: datetime
    value: float
    source: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.metric, self.at.astimezone(UTC).isoformat(), normalized_source(self.source)


@dataclass(frozen=True)
class Measurement:
    at: datetime
    weight: float
    fat: float | None
    source: str
    bmi: float | None = None
    lbm: float | None = None

    @property
    def key(self) -> str:
        text = f"{normalized_source(self.source)}|{self.at.astimezone(UTC).isoformat()}"
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    @property
    def core(self) -> str:
        # Optional BMI arriving later does not create a second remote record.
        fat = "none" if self.fat is None else f"{self.fat:.6f}"
        return f"{self.weight:.6f}|{fat}"

    def payload(self) -> dict[str, Any]:
        payload = {
            "at": self.at.isoformat(),
            "weight": self.weight,
            "fat": self.fat,
            "source": self.source,
            "bmi": self.bmi,
        }
        if self.lbm is not None:
            payload["lbm"] = self.lbm  # Local evidence only; never mapped to Garmin muscle_mass.
        return payload


def safe_exception(exc: Exception) -> str:
    name = type(exc).__name__
    match = re.search(
        r"(?:HTTP|Error|status)[ :]*(400|401|403|404|429|500|502|503|504)\b", str(exc), re.I
    )
    return name + (f" HTTP {match.group(1)}" if match else "")


def measurement_wire_time(r: Measurement) -> datetime:
    at = r.at.astimezone(UTC)
    # FIT encodes whole seconds; add_weigh_in receives a millisecond timestamp.
    microsecond = 0 if r.fat is not None or r.bmi is not None else at.microsecond // 1000 * 1000
    return at.replace(microsecond=microsecond)
