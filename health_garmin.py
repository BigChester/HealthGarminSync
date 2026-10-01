#!/usr/bin/env python3
"""Local Apple Health export -> Garmin CN bridge. JSON schema v1/v2. No HTTP server.

The only network library is imported by explicit Garmin commands. Offline preview
never authenticates. Source files are never modified. Run with Python >= 3.12.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
import contextlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import fcntl
import getpass
import hashlib
from importlib.metadata import version
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any

BASE = Path(__file__).resolve().parent
STATE = BASE / "state"
CONFIG = BASE / "config.json"
LABEL = "local.health-garmin-sync"
REQUIRED_GARMIN_VERSION = "0.3.16"
SUPPORTED_SCHEMAS = ("1", "2")
SUPPORTED_METRICS = ("weight", "body_fat", "bmi", "lbm")
LOG = logging.getLogger("health_garmin")


class SafeError(Exception):
    """Error whose text contains neither login credentials nor tokens."""


def now() -> datetime:
    return datetime.now(UTC)


def aware(text: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SafeError("时间必须为 ISO 8601，例如 2026-09-29T08:00:00.000+08:00。") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise SafeError("时间缺少时区：使用格式 yyyy-MM-dd'T'HH:mm:ss.SSSXXX。")
    return dt


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise SafeError("本地私密目录不能是符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_json(path: Path, data: Any) -> None:
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.is_symlink() or path.is_symlink():
        raise SafeError("拒绝写入符号链接。")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def config() -> dict[str, Any]:
    if not CONFIG.exists():
        raise SafeError("尚未初始化，请先运行 ./hg init。")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if cfg.get("is_cn") is not True:
        raise SafeError("本工具仅配置为 Garmin 中国区；is_cn 必须为 true。")
    date.fromisoformat(cfg["since"])
    if not 0 <= int(cfg["pair_window_seconds"]) <= 120:
        raise SafeError("配对窗口必须在 0..120 秒之间。")
    return cfg


def normalized_source(value: Any) -> str:
    return str(value).strip().casefold()


def parse_quantity(raw: Any, metric: str, cfg: dict[str, Any]) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise SafeError("value 必须为数值或带单位的文本，不能是健康样本对象。")
    txt = str(raw).strip().replace("\u00a0", " ").replace("\u202f", " ").replace("％", "%")
    m = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([^0-9]*)", txt)
    if not m:
        raise SafeError("无法解析数值；保留一个数字及可选单位，不使用千分位或逗号小数。")
    try:
        n = float(Decimal(m.group(1)))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise SafeError("无效数值。") from exc
    unit = m.group(2).strip().casefold()
    if metric in ("weight", "lbm"):
        if not unit:
            unit = cfg["weight_bare_unit"]
        factors = {"kg": 1, "公斤": 1, "千克": 1, "kilograms": 1,
                   "g": 0.001, "克": 0.001, "lb": 0.45359237,
                   "lbs": 0.45359237, "磅": 0.45359237}
        if unit not in factors:
            raise SafeError("体重/瘦体重单位未知；支持 kg、g、lb，不会猜测裸数值单位。")
        n *= factors[unit]
        valid = 0 < n < 600
    elif metric == "body_fat":
        if unit in ("%", "percent", "百分比"):
            pass
        elif unit == "":
            declared = cfg["fat_bare_unit"]
            if declared == "fraction":
                if not 0 < n < 1:
                    raise SafeError("已设置 fraction，但裸体脂数值不在 0..1。请检查单位。")
                n *= 100
            elif declared == "percent":
                if 0 < n <= 1:
                    raise SafeError("裸体脂数值 <=1：可能是比例值。核对后用 ./hg settings --fat-unit fraction。")
            else:
                raise SafeError("请明确设置 fat_bare_unit 为 percent 或 fraction。")
        else:
            raise SafeError("体脂单位未知；不会自动猜测或乘以 100。")
        valid = 0 < n < 100
    elif metric == "bmi":
        if unit not in ("", "bmi", "kg/m²", "kg/m2"):
            raise SafeError("BMI 单位未知。")
        valid = 0 < n < 100
    else:
        raise SafeError("metric 必须为 weight、body_fat、bmi 或 lbm。")
    if not math.isfinite(n) or not valid:
        raise SafeError("数值超出本工具的数据校验范围，请检查单位和原始记录。")
    return round(n, 6)


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
        payload = {"at": self.at.isoformat(), "weight": self.weight,
                   "fat": self.fat, "source": self.source, "bmi": self.bmi}
        if self.lbm is not None:
            payload["lbm"] = self.lbm  # Local evidence only; never mapped to Garmin muscle_mass.
        return payload


def export_groups(obj: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize schema v1 (one metric) and v2 (a metrics dictionary).

    Preserve the v1 Sample/Measurement identity. A one-item dictionary is
    accepted for Shortcuts interoperability; JSON-in-a-string is not accepted.
    This function checks structure only; values and sources are checked later.
    """
    if not isinstance(obj, dict):
        raise SafeError("JSON 顶层必须为词典，不是数组或带引号的 JSON 文本。")
    schema = str(obj.get("schema_version"))
    if schema not in SUPPORTED_SCHEMAS:
        raise SafeError("文件必须使用 schema_version=1 或 2；字段名不是 version。")
    if obj.get("demo"):
        raise SafeError("示例文件禁止上传。请从自己的 Apple 健康导出真实记录。")
    if schema == "1":
        if obj.get("metric") not in SUPPORTED_METRICS:
            raise SafeError("文件 metric 必须为 weight、body_fat、bmi 或 lbm。")
        if "metrics" in obj:
            raise SafeError("schema_version=1 不能混入 metrics；合并文件请使用版本 2。")
        raw_groups = {obj["metric"]: obj.get("samples")}
    else:
        if "metric" in obj or "samples" in obj:
            raise SafeError("版本 2 顶层使用 metrics，不能保留单指标 metric/samples。")
        raw_groups = obj.get("metrics")
        if not isinstance(raw_groups, dict) or not raw_groups:
            raise SafeError("版本 2 的 metrics 必须为非空词典，不能是 JSON 文本或数组。")
        if any(key not in SUPPORTED_METRICS for key in raw_groups):
            if "bodt_fat" in raw_groups:
                raise SafeError("bodt_fat 拼写错误，请在 iPhone 快捷指令中改为 body_fat 后重新导出。")
            raise SafeError("metrics 中仅支持 weight、body_fat、bmi、lbm；请检查键名。")

    groups: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for metric, items in raw_groups.items():
        if isinstance(items, dict):
            # Reject a whole child payload accidentally inserted instead of samples.
            if not all(field in items for field in ("at", "value", "source")):
                raise SafeError(f"{metric} 应放 samples 列表，不是整个子快捷指令返回词典。")
            items = [items]
        if not isinstance(items, list):
            raise SafeError(f"{metric} 样本必须为数组或单个样本词典，不能是带引号的 JSON 文本。")
        total += len(items)
        if total > 20000:
            raise SafeError("单文件样本合计过多，请缩短快捷指令导出范围。")
        for item in items:
            if not isinstance(item, dict):
                raise SafeError(f"{metric} 中必须是一条条样本词典，不能是嵌套数组或文本。")
            if not all(field in item for field in ("at", "value", "source")):
                raise SafeError(f"{metric} 样本必须有 at、value、source。")
            if item.get("demo"):
                raise SafeError("示例样本禁止上传。请从自己的 Apple 健康导出真实记录。")
        groups[metric] = items
    return groups


def decode_export(obj: Any, cfg: dict[str, Any]) -> tuple[list[Sample], datetime]:
    groups = export_groups(obj)
    exported = aware(obj.get("exported_at", ""))
    allowed = {normalized_source(s) for s in cfg["allowed_sources"]}
    output: list[Sample] = []
    for metric, samples in groups.items():
        for item in samples:
            source = item.get("source")
            if not isinstance(source, str) or not source.strip():
                raise SafeError("样本缺少 source；请导出真实来源或使用已核对的来源过滤。")
            if normalized_source(source) not in allowed:
                continue
            dt = aware(item.get("at", ""))
            if dt.date() < date.fromisoformat(cfg["since"]):
                continue
            if dt > now() + timedelta(minutes=10):
                raise SafeError("发现未来测量时间，请检查日期格式、时区和手机时间。")
            value = parse_quantity(item.get("value"), metric, cfg)
            output.append(Sample(metric, dt, value, source.strip()))
    return output, exported


def deduplicate(samples: list[Sample]) -> list[Sample]:
    unique: dict[tuple[str, str, str], Sample] = {}
    for s in samples:
        previous = unique.get(s.key)
        if previous and abs(previous.value - s.value) > 0.000001:
            raise SafeError(f"同来源、同时间的 {s.metric} 出现不同值：{s.at.isoformat()}。"
                            "请检查旧导出/修订，不会静默覆盖。")
        unique[s.key] = s
    return sorted(unique.values(), key=lambda s: (s.at, s.metric))


def load_exports(cfg: dict[str, Any]) -> tuple[list[Sample], dict[str, str]]:
    inbox = Path(cfg["inbox"]).expanduser()
    if not inbox.is_dir():
        raise SafeError("找不到导出目录，请确认 iCloud 下载状态和 config.json 中的真实路径。")
    files = sorted(inbox.glob("*.json"))
    if not files:
        raise SafeError("导出目录没有 .json 文件；先在 iPhone 手动运行导出快捷指令。")
    all_samples: list[Sample] = []
    exports: dict[str, str] = {}
    for p in files:
        if p.is_symlink():
            raise SafeError("导出目录含符号链接，已停止。")
        before = p.stat()
        if before.st_size == 0 or before.st_size > 4 * 1024 * 1024:
            raise SafeError(f"{p.name} 为空或超过 4 MB，请检查 iCloud 文件下载。")
        try:
            obj = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SafeError(f"{p.name} 不是完整 UTF-8 JSON，或尚未下载完毕。") from exc
        after = p.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SafeError("文件正在更新，本次暂停；下一轮会重新读取。")
        try:
            new, exported = decode_export(obj, cfg)
        except SafeError as exc:
            raise SafeError(f"{p.name}: {exc}") from exc
        all_samples.extend(new)
        for metric in export_groups(obj):
            if metric not in exports or exported > aware(exports[metric]):
                exports[metric] = exported.isoformat()
    return deduplicate(all_samples), exports


def pair_samples(samples: list[Sample], window: int = 0,
                 allow_weight_only: bool = False) -> tuple[list[Measurement], list[str]]:
    weights = [s for s in samples if s.metric == "weight"]
    fats = [s for s in samples if s.metric == "body_fat"]
    bmis = [s for s in samples if s.metric == "bmi"]
    lbms = [s for s in samples if s.metric == "lbm"]
    paired: list[Measurement] = []
    waiting = []

    def candidates(anchor: Sample, pool: list[Sample]) -> list[Sample]:
        same_source = [s for s in pool if normalized_source(s.source) == normalized_source(anchor.source)]
        exact = [s for s in same_source if s.at == anchor.at]
        return exact or [s for s in same_source if abs((s.at - anchor.at).total_seconds()) <= window]

    for w in weights:
        fs = candidates(w, fats)
        if len(fs) > 1 or (not fs and not allow_weight_only):
            waiting.append(f"{w.at.isoformat()}：体脂候选数={len(fs)}，等待或人工核对。")
            continue
        f = fs[0] if fs else None
        reverse = candidates(f, weights) if f else []
        if f and (len(reverse) != 1 or reverse[0] != w):
            waiting.append(f"{w.at.isoformat()}：体脂可能对应多次称重，不配对。")
            continue
        bs = candidates(w, bmis)
        if len(bs) > 1:
            waiting.append(f"{w.at.isoformat()}：BMI 候选不唯一，不配对。")
            continue
        if bs:
            reverse_bmi = candidates(bs[0], weights)
            if len(reverse_bmi) != 1 or reverse_bmi[0] != w:
                waiting.append(f"{w.at.isoformat()}：BMI 可能对应多次称重，不配对。")
                continue
        bmi = bs[0].value if bs else None
        ls = candidates(w, lbms)
        if len(ls) > 1 or (ls and candidates(ls[0], weights) != [w]):
            waiting.append(f"{w.at.isoformat()}：瘦体重候选有歧义，不配对。")
            continue
        lbm = ls[0].value if ls else None
        if lbm is not None and lbm > w.value:
            raise SafeError(f"{w.at.isoformat()}：瘦体重大于体重，请核对单位和测量。")
        paired.append(Measurement(w.at, w.value, f.value if f else None, w.source, bmi, lbm))
    return sorted(paired, key=lambda r: r.at), waiting


def database() -> sqlite3.Connection:
    dbpath = STATE / "sync.sqlite3"
    if dbpath.is_symlink():
        raise SafeError("状态数据库不能是符号链接。")
    db = sqlite3.connect(dbpath, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS uploads (
       record_id TEXT PRIMARY KEY, core TEXT NOT NULL, payload TEXT NOT NULL,
       status TEXT NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL)""")
    db.commit()
    dbpath.chmod(0o600)
    return db


def set_status(db: sqlite3.Connection, r: Measurement, status: str, detail: str = "") -> None:
    db.execute("""INSERT INTO uploads VALUES(?,?,?,?,?,?)
        ON CONFLICT(record_id) DO UPDATE SET core=excluded.core,
        payload=excluded.payload,status=excluded.status,
        updated_at=excluded.updated_at,detail=excluded.detail""",
        (r.key, r.core, json.dumps(r.payload(), ensure_ascii=False), status, now().isoformat(), detail))
    db.commit()  # In-flight marker is durable BEFORE submitting a network write.


def get_status(db: sqlite3.Connection, r: Measurement) -> str:
    row = db.execute("SELECT * FROM uploads WHERE record_id=?", (r.key,)).fetchone()
    if not row:
        return "pending"
    if row["core"] != r.core:
        raise SafeError(f"记录 {r.key} 的体重/体脂已修订，必须人工核对已有上传。")
    return row["status"]


@contextlib.contextmanager
def utc_fit_clock():
    """0.3.16 FIT uses mktime(t.timetuple()), ignoring a datetime's offset.
    Use UTC only in this single-threaded process and pass UTC wall time.
    Does NOT modify macOS time-zone settings. Also correct for newer timestamp().
    """
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def api_client(interactive: bool = False):
    try:
        installed = version("garminconnect")
        if installed != REQUIRED_GARMIN_VERSION:
            raise SafeError(f"需要 garminconnect=={REQUIRED_GARMIN_VERSION}，当前为 {installed}。")
        from garminconnect import Garmin
    except ImportError as exc:
        raise SafeError("缺少依赖；运行 bash setup.sh。") from exc
    token_dir = STATE / "tokens"
    private_dir(token_dir)
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    if interactive:
        email = input("Garmin 中国区登录邮箱：").strip()
        password = getpass.getpass("密码（不会显示，也不写入文件）：")
        api = Garmin(email=email, password=password, is_cn=True,
                     prompt_mfa=lambda: getpass.getpass("验证码：").strip())
        password = None
    else:
        api = Garmin(is_cn=True)
    try:
        api.login(str(token_dir))
    except Exception as exc:
        # Do not print raw exception text: upstream URLs can contain tickets.
        code = safe_exception(exc)
        raise SafeError(f"Garmin 登录/令牌恢复失败：{code}。网络正常后运行 ./hg login。") from exc
    return GarminMeasurementAPI(api)


class GarminMeasurementAPI:
    """Use full weigh-in details; dateRange only exposes the latest each day."""

    def __init__(self, api):
        self.api = api

    def __getattr__(self, name):
        return getattr(self.api, name)

    def get_body_composition(self, start: str, end: str | None = None):
        end = end or start
        if start == end:
            obj = self.api.get_daily_weigh_ins(start)
            RemoteIndex(obj)
            return obj
        obj = self.api.get_weigh_ins(start, end)
        if not isinstance(obj, dict) or not isinstance(obj.get("dailyWeightSummaries"), list):
            raise SafeError("Garmin 完整称重明细结构未知，停止，不使用每日最后值替代。")
        rows = []
        for summary in obj["dailyWeightSummaries"]:
            if not isinstance(summary, dict) or not isinstance(summary.get("allWeightMetrics"), list):
                raise SafeError("Garmin 每日称重明细不完整，停止。")
            items, count = summary["allWeightMetrics"], summary.get("numOfWeightEntries")
            if isinstance(count, bool) or not isinstance(count, int) or count != len(items):
                raise SafeError("Garmin 每日称重条数与明细不一致，停止。")
            day = summary.get("summaryDate")
            if not isinstance(day, str) or not start <= day <= end:
                raise SafeError("Garmin 明细日期超出请求范围，停止。")
            date.fromisoformat(day)
            if any(not isinstance(row, dict) or row.get("calendarDate") != day for row in items):
                raise SafeError("Garmin 每日明细的测量日期不一致，停止。")
            rows.extend(items)
        result = {"dateWeightList": rows}
        RemoteIndex(result)
        return result


def safe_exception(exc: Exception) -> str:
    name = type(exc).__name__
    match = re.search(r"(?:HTTP|Error|status)[ :]*(400|401|403|404|429|500|502|503|504)\b", str(exc), re.I)
    return name + (f" HTTP {match.group(1)}" if match else "")


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


def classify_remote(r: Measurement, obj: Any) -> str:
    try:
        kind, _, _ = RemoteIndex(obj).decide(r)
    except SafeError:
        if isinstance(obj, dict) and isinstance(obj.get("dateWeightList"), list):
            return "conflict"
        raise
    return {"matched": "matched", "missing": "absent"}.get(kind, "conflict")


class RemoteIndex:
    """Index a fully validated snapshot by absolute measurement time."""

    def __init__(self, obj: Any):
        if not isinstance(obj, dict) or not isinstance(obj.get("dateWeightList"), list):
            raise SafeError("Garmin 返回结构未知，已停止写入。")
        self.rows = obj["dateWeightList"]
        dated = []
        for i, row in enumerate(self.rows):
            if not isinstance(row, dict) or remote_time(row) is None:
                raise SafeError("Garmin 测量时间无法可靠解析，已停止本轮对账。")
            dated.append((remote_time(row).timestamp(), i))
        dated.sort()
        self.times = [stamp for stamp, _ in dated]
        self.indices = [i for _, i in dated]

    def nearby(self, r: Measurement) -> list[int]:
        stamp = r.at.timestamp()
        return self.indices[bisect_left(self.times, stamp - 60):bisect_right(self.times, stamp + 60)]

    def decide(self, r: Measurement) -> tuple[str, list[int], str]:
        near = self.nearby(r)
        exact = [i for i in near if abs((remote_time(self.rows[i]) - r.at).total_seconds()) <= 1]
        if not near:
            return "missing", [], "Garmin 中没有对应测量"
        if len(exact) != 1:
            return "conflict", near, "时间附近存在多条记录或时间偏差，需人工核对"
        i = exact[0]
        row = self.rows[i]
        try:
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


def state_row(db: sqlite3.Connection, r: Measurement) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM uploads WHERE record_id=?", (r.key,)).fetchone()


def active_operations() -> set[str]:
    active = set()
    for path in (STATE / "operations").glob("*.json"):
        if path.is_symlink():
            raise SafeError("操作记录不能是符号链接。")
        journal = json.loads(path.read_text())
        if journal.get("phase") not in ("verified", "deleted_verified", "closed_partial"):
            active.add(journal["key"])
            active.update(journal.get("record_ids", []))
    return active


def fetch_remote_snapshot(api: Any, start: date, end: date) -> dict[str, Any]:
    """Fetch all chunks before any write; pad dates for UTC/local midnight boundaries."""
    cursor, final = start - timedelta(days=1), end + timedelta(days=1)
    rows, seen = [], {}
    while cursor <= final:
        stop = min(cursor + timedelta(days=30), final)
        obj = api.get_body_composition(cursor.isoformat(), stop.isoformat())
        index = RemoteIndex(obj)
        for row in index.rows:
            identity = (str(row.get("samplePk")), str(row.get("calendarDate")))
            if row.get("samplePk") is None:
                identity = (json.dumps(row, sort_keys=True, allow_nan=False), "")
            if identity in seen:
                if seen[identity] != row:
                    raise SafeError("分批查询中的同一 Garmin 记录发生变化，请重新对账。")
                continue
            seen[identity] = row
            rows.append(row)
        cursor = stop + timedelta(days=1)
    return {"dateWeightList": rows}


def comparison_plan(db: sqlite3.Connection, records: list[Measurement], obj: Any,
                    start: date, end: date) -> dict[str, Any]:
    index = RemoteIndex(obj)
    entries, claimed, owners = [], set(), {}
    active = active_operations()
    for r in records:
        kind, candidates, reason = index.decide(r)
        claimed.update(index.nearby(r))
        previous = state_row(db, r)
        status = previous["status"] if previous else "pending"
        action = kind
        if status == "ignored":
            action, reason = "ignored", "用户已明确忽略"
        elif kind != "matched" and (status in ("inflight", "uncertain", "repair_uncertain") or r.key in active):
            action, reason = "uncertain", "之前的请求结果未确定，只读核对，不重复提交"
        elif kind == "missing" and status in ("verified", "verified_manual", "verified_conflict", "remote_missing"):
            action, reason = "review_missing", "以前已验证的记录远端缺失；核对后用 resolve --absent 允许补传"
        entry = {"id": r.key, "action": action, "reason": reason, "local_status": status,
                 "measurement": r.payload(), "remote": [index.rows[i] for i in candidates]}
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
                    entries[k].update(action="conflict", reason="多条 Apple 测量的时间重叠，不能唯一确定对应关系")
    extras = []
    for i, row in enumerate(index.rows):
        calendar = row.get("calendarDate")
        try:
            day = date.fromisoformat(calendar) if isinstance(calendar, str) else remote_time(row).astimezone().date()
        except ValueError:
            raise SafeError("Garmin calendarDate 无法解析。")
        if i not in claimed and start <= day <= end:
            extras.append(row)
    return {"created_at": now().isoformat(), "start": start.isoformat(), "end": end.isoformat(),
            "entries": entries, "extra_remote": extras, "remote_snapshot": obj,
            "counts": dict(Counter(e["action"] for e in entries))}


def print_comparison(plan: dict[str, Any]) -> None:
    print("Garmin 对账范围：", plan["start"], "至", plan["end"])
    for entry in plan["entries"]:
        r = entry["measurement"]
        fat = "—" if r["fat"] is None else f'{r["fat"]:.3f}%'
        print(f'{entry["id"]}  {entry["action"]:15s}  {r["at"]}  {r["weight"]:.3f} kg  {fat}  {entry["reason"]}')
    print("分类统计：", json.dumps(plan["counts"], ensure_ascii=False))
    print("Garmin 多余记录：", len(plan["extra_remote"]), "（仅限此次对账日期和来源范围，保留待核对）")
    for row in plan["extra_remote"]:
        print("EXTRA", row.get("samplePk"), row.get("calendarDate"),
              remote_time(row).isoformat(), row.get("weight"), row.get("bodyFat"))


def save_comparison(plan: dict[str, Any], cfg: dict[str, Any]) -> Path:
    plan["config"] = {key: cfg.get(key) for key in
                      ("inbox", "allowed_sources", "weight_bare_unit", "fat_bare_unit", "pair_window_seconds")}
    path = STATE / "plans" / ("compare-" + now().strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    write_json(path, plan)
    write_json(STATE / "last_compare.json", {"created_at": plan["created_at"], "path": str(path),
                                             "counts": plan["counts"], "extra_remote": len(plan["extra_remote"])})
    return path


def compare_exports(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(cfg)
    if args.inbox:
        cfg["inbox"] = str(Path(args.inbox).expanduser().resolve())
    start = date.fromisoformat(args.since or cfg["since"])
    end = date.fromisoformat(args.until) if args.until else now().astimezone().date()
    if start > end:
        raise SafeError("对账起始日期不能晚于结束日期。")
    cfg["since"] = start.isoformat()
    _, records, waiting, _ = load_measurements(cfg)
    records = [r for r in records if start <= r.at.date() <= end]
    if waiting:
        raise SafeError("有歧义样本尚未配对，请先处理 preview 中的 WAIT。")
    if not records:
        raise SafeError("选定范围没有可用 Apple 测量，不能据此生成清理清单。")
    api = api_client()
    obj = fetch_remote_snapshot(api, start, end)
    with database() as db:
        plan = comparison_plan(db, records, obj, start, end)
    print_comparison(plan)
    path = save_comparison(plan, cfg)
    print("只读对账完成，未修改 Garmin。清单：", path)
    return plan


def reviewed_operation(args: argparse.Namespace, cfg: dict[str, Any], delete_extra: bool = False) -> None:
    """Apply only the selected, unchanged item in a previously saved comparison."""
    path = Path(args.plan).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise SafeError("请选择本工具生成的有效对账清单。")
    try:
        path.resolve().relative_to((STATE / "plans").resolve())
    except ValueError:
        raise SafeError("清单必须位于本项目 state/plans 目录。")
    saved = json.loads(path.read_text())
    for key in ("allowed_sources", "weight_bare_unit", "fat_bare_unit", "pair_window_seconds"):
        if saved["config"].get(key) != cfg.get(key):
            raise SafeError("来源、单位或配对设置已改变，请重新对账。")
    start, end = date.fromisoformat(saved["start"]), date.fromisoformat(saved["end"])
    source_cfg = dict(cfg, inbox=saved["config"]["inbox"], since=start.isoformat())
    _, records, waiting, _ = load_measurements(source_cfg)
    records = [r for r in records if start <= r.at.date() <= end]
    if not records or waiting:
        raise SafeError("Apple 导出为空或存在歧义，请重新对账。")
    api = api_client()
    obj = fetch_remote_snapshot(api, start, end)
    with database() as db:
        fresh = comparison_plan(db, records, obj, start, end)
        if delete_extra:
            if not args.complete_export:
                raise SafeError("删除多余记录需要 --complete-export，确认已完整导出选定日期内需要保留的 Apple 数据。")
            old = [r for r in saved["extra_remote"] if str(r.get("samplePk")) == args.remote_id]
            current = [r for r in fresh["extra_remote"] if str(r.get("samplePk")) == args.remote_id]
            if len(old) != 1 or len(current) != 1 or old != current:
                raise SafeError("多余记录已变化或不再多余，请重新对账。")
            remote_row = current[0]
            measurement = None
            operation_key = "extra-" + args.remote_id
        else:
            old = [e for e in saved["entries"] if e["id"] == args.record_id]
            current = [e for e in fresh["entries"] if e["id"] == args.record_id]
            if len(old) != 1 or len(current) != 1:
                raise SafeError("清单中找不到唯一的指定 Apple 测量。")
            entry = current[0]
            if entry["action"] == "matched":
                r = next(r for r in records if r.key == args.record_id)
                set_status(db, r, "verified", "Reviewed repair already matched; no write")
                print("远端已匹配，无需修复。")
                return
            if entry["action"] not in ("repair", "replace") or old[0]["action"] not in ("repair", "replace"):
                raise SafeError("只能修复唯一对应的缺字段或数值不同记录；不处理歧义或 uncertain。")
            if old[0]["measurement"] != entry["measurement"] or old[0]["remote"] != entry["remote"]:
                raise SafeError("Apple 或 Garmin 数据已变化，请重新对账。")
            remote_row = entry["remote"][0]
            measurement = next(r for r in records if r.key == args.record_id)
            operation_key = measurement.key
            unsupported = [key for key in ("muscleMass", "skeletalMuscleMass", "boneMass", "bodyWater", "waterMass", "fatMass", "percentHydration", "visceralFat", "visceralFatMass", "visceralFatRating", "metabolicAge", "basalMet", "activeMet", "physiqueRating") if remote_row.get(key) is not None]
            if unsupported:
                raise SafeError("原记录包含尚不支持保留的身体成分字段，未删除：" + ", ".join(unsupported))
            fat = measurement.fat
            bmi = measurement.bmi
            if fat is None and remote_row.get("bodyFat") is not None:
                fat = parse_quantity(str(remote_row["bodyFat"]) + "%", "body_fat", cfg)
            if bmi is None and remote_row.get("bmi") is not None:
                bmi = parse_quantity(remote_row["bmi"], "bmi", cfg)
            replacement = Measurement(measurement.at, measurement.weight, fat, measurement.source, bmi, measurement.lbm)
        pk = str(remote_row.get("samplePk", ""))
        calendar = remote_row.get("calendarDate")
        if not pk.isdigit() or int(pk) <= 0 or not isinstance(calendar, str):
            raise SafeError("缺少明确的 Garmin 记录 ID 或 calendarDate，不能删除。")
        date.fromisoformat(calendar)
        operation_dir = STATE / "operations"
        for existing in operation_dir.glob("*.json"):
            history = json.loads(existing.read_text())
            if history.get("key") == operation_key and history.get("phase") not in ("verified", "deleted_verified"):
                raise SafeError("此记录有未完成的修改，请先核对 state/operations 中的操作记录，不重复删除。")
        print("选定原记录：", json.dumps(remote_row, ensure_ascii=False))
        if measurement:
            print("Apple 目标测量：", json.dumps(measurement.payload(), ensure_ascii=False))
        confirm = "DELETE " + pk if delete_extra else "REPLACE " + measurement.key
        print("将备份后删除选定的一条 Garmin 记录" + ("，并按 Apple 数值重建。" if measurement else "。"))
        if input("确认请输入 " + confirm + "：").strip() != confirm:
            print("已取消，未修改 Garmin。")
            return
        # The operator may spend time reviewing the prompt. Recheck both sides
        # after confirmation so the deletion still refers to the reviewed data.
        _, latest_records, latest_waiting, _ = load_measurements(source_cfg)
        latest_records = [r for r in latest_records if start <= r.at.date() <= end]
        if not latest_records or latest_waiting:
            raise SafeError("确认期间 Apple 数据已变化，请重新对账。")
        obj = fetch_remote_snapshot(api, start, end)
        latest = comparison_plan(db, latest_records, obj, start, end)
        if delete_extra:
            candidates = [r for r in latest["extra_remote"] if str(r.get("samplePk")) == pk]
            unchanged = candidates == [remote_row]
        else:
            candidates = [e for e in latest["entries"] if e["id"] == measurement.key]
            unchanged = (len(candidates) == 1 and candidates[0]["action"] in ("repair", "replace")
                         and candidates[0]["measurement"] == measurement.payload()
                         and candidates[0]["remote"] == [remote_row])
        if not unchanged:
            raise SafeError("确认期间目标数据已变化，未删除；请重新对账。")
        fresh = latest
        stamp = now().strftime("%Y%m%dT%H%M%S%fZ")
        backup = STATE / "backups" / stamp
        private_dir(backup)
        write_json(backup / "remote.json", obj)
        write_json(backup / "plan.json", fresh)
        write_json(backup / "config.json", cfg)
        backup_db = backup / "sync.sqlite3"
        with sqlite3.connect(backup_db) as target:
            db.backup(target)
        backup_db.chmod(0o600)
        journal = {"key": operation_key, "phase": "deleting", "created_at": now().isoformat(),
                   "original": remote_row, "measurement": measurement.payload() if measurement else None,
                   "replacement": replacement.payload() if measurement else None,
                   "backup": str(backup)}
        journal_path = operation_dir / (stamp + ".json")
        write_json(journal_path, journal)  # Durable before DELETE, including the original record.
        if measurement:
            set_status(db, measurement, "repair_uncertain", "Reviewed replacement started; do not resend or delete again")
        try:
            api.delete_weigh_in(pk, calendar)
            remaining = fetch_remote_snapshot(api, start, end)
            if any(str(row.get("samplePk")) == pk for row in remaining["dateWeightList"]):
                raise SafeError("删除请求后原记录仍可见，暂停后续操作。")
            journal["phase"] = "deleted_verified" if delete_extra else "deleted"
            write_json(journal_path, journal)
            if measurement:
                # Replacement optional values were validated before any DELETE.
                set_status(db, replacement, "pending", "Original removal confirmed; reviewed replacement permitted")
                journal["phase"] = "uploading"
                write_json(journal_path, journal)
                result = process_record(api, db, replacement, reviewed=True)
                journal["phase"] = "verified" if result == "verified" else "uncertain"
                write_json(journal_path, journal)
                if result != "verified":
                    raise SafeError("修复提交后尚未读回匹配，停止；只读核对，不重复提交。")
                set_status(db, measurement, "verified", "Reviewed replacement matched Apple values; optional remote fields preserved")
            print("操作完成；备份：", backup)
        except Exception as exc:
            journal["phase"] = "uncertain"
            journal["error"] = safe_exception(exc)
            write_json(journal_path, journal)
            raise SafeError("操作结果未确定，已保存备份和操作记录；停止，不重复提交：" + safe_exception(exc)) from exc


def read_operation(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SafeError("请选择本工具生成的有效操作记录。")
    try:
        path.resolve().relative_to((STATE / "operations").resolve())
    except ValueError:
        raise SafeError("操作记录必须位于本项目 state/operations 目录。")
    return json.loads(path.read_text())


def verify_operation(args: argparse.Namespace) -> None:
    path = Path(args.operation).expanduser()
    journal = read_operation(path)
    if journal.get("type") == "full_replacement":
        from replace_history import verify
        verify(path)
        return
    day = date.fromisoformat(journal["original"]["calendarDate"])
    obj = fetch_remote_snapshot(api_client(), day, day)
    payload = journal.get("replacement") or journal.get("measurement")
    if payload:
        r = Measurement(**{**payload, "at": aware(payload["at"])})
        matched = classify_remote(r, obj) == "matched"
        if matched:
            with database() as db:
                set_status(db, r, "verified", "Delayed reviewed-operation verification matched")
            journal["phase"] = "verified"
    else:
        matched = not any(str(row.get("samplePk")) == str(journal["original"]["samplePk"])
                          for row in obj["dateWeightList"])
        if matched:
            journal["phase"] = "deleted_verified"
    journal["last_checked_at"] = now().isoformat()
    write_json(path, journal)
    print("只读核对：", journal["phase"], "；未重复删除或上传。")


def resume_operation(args: argparse.Namespace) -> None:
    path = Path(args.operation).expanduser()
    journal = read_operation(path)
    if journal.get("type") == "full_replacement":
        raise SafeError("完整替换操作先用 verify-operation 只读核对；不自动重复删除或上传。")
    payload = journal.get("replacement") or journal.get("measurement")
    if not payload:
        raise SafeError("删除操作仅用 verify-operation 核对，不重复删除。")
    r = Measurement(**{**payload, "at": aware(payload["at"])})
    api = api_client()
    obj = remote_for(api, r)
    original_pk = str(journal["original"]["samplePk"])
    if classify_remote(r, obj) == "matched":
        with database() as db:
            set_status(db, r, "verified", "Reviewed operation already matched; no resume upload")
        journal["phase"] = "verified"
        write_json(path, journal)
        print("远端已匹配，无需继续上传。")
        return
    if any(str(row.get("samplePk")) == original_pk for row in RemoteIndex(obj).rows):
        raise SafeError("原 Garmin 记录仍存在，停止；不会重复删除。")
    if classify_remote(r, obj) != "absent":
        raise SafeError("远端存在冲突，需重新核对；不恢复上传。")
    print("原记录已查不到；待恢复测量：", json.dumps(r.payload(), ensure_ascii=False))
    if input("在 Garmin 确认目标测量确实不存在后，输入 RESUME " + r.key + "：").strip() != "RESUME " + r.key:
        print("已取消。")
        return
    obj = remote_for(api, r)
    if classify_remote(r, obj) != "matched":
        if any(str(row.get("samplePk")) == original_pk for row in RemoteIndex(obj).rows) or classify_remote(r, obj) != "absent":
            raise SafeError("确认期间远端记录发生变化，不恢复上传。")
    journal["phase"] = "uploading"
    journal["resumed_at"] = now().isoformat()
    write_json(path, journal)
    with database() as db:
        set_status(db, r, "pending", "User confirmed remote absence for reviewed operation resume")
        result = process_record(api, db, r, reviewed=True)
    journal["phase"] = "verified" if result == "verified" else "uncertain"
    write_json(path, journal)
    print("恢复结果：", result, "；不会自动重发。")
    if result != "verified":
        raise SafeError("恢复请求尚未读回验证，请使用 verify-operation 继续只读核对。")


def load_local_comparison(cfg: dict[str, Any], records: list[Measurement], api: Any,
                          db: sqlite3.Connection, full: bool = False) -> dict[str, Any]:
    # The first run checks all retained history. Later runs recheck recent data
    # and every pending, uncertain, changed or remotely missing older record.
    recent = now().astimezone().date() - timedelta(days=30)
    selected = []
    first = full or not (STATE / "last_sync_comparison.json").exists()
    for r in records:
        previous = state_row(db, r)
        bmi_changed = previous is not None and r.bmi is not None and json.loads(previous["payload"]).get("bmi") != r.bmi
        if first or r.at.date() >= recent or previous is None or previous["status"] not in ("verified", "verified_manual", "ignored") or previous["core"] != r.core or bmi_changed:
            selected.append(r)
    if not selected:
        return {"entries": [], "counts": {}, "extra_remote": []}
    start, end = min(r.at.date() for r in selected), max(r.at.date() for r in selected)
    obj = fetch_remote_snapshot(api, start, end)
    plan = comparison_plan(db, selected, obj, start, end)
    save_comparison(plan, cfg)
    return plan


def remote_for(api: Any, r: Measurement) -> Any:
    d = r.at.astimezone(UTC).date()
    return api.get_body_composition((d - timedelta(days=1)).isoformat(),
                                    (d + timedelta(days=1)).isoformat())


def process_record(api: Any, db: sqlite3.Connection, r: Measurement, sleep=time.sleep,
                   reviewed: bool = False) -> str:
    row = state_row(db, r)
    previous = row["status"] if row else "pending"
    if previous == "ignored":
        return previous
    # Reading errors happen before the write and are safe to try on the next run.
    classification = classify_remote(r, remote_for(api, r))
    if classification == "matched":
        set_status(db, r, "verified", "Read-back matched time, weight, fat and available BMI")
        return "verified"
    if not reviewed and r.key in active_operations():
        set_status(db, r, "repair_uncertain", "Unfinished reviewed operation; verify or explicitly resume its journal")
        return "repair_uncertain"
    if previous in ("inflight", "uncertain", "repair_uncertain"):
        # Never blindly re-POST an uncertain write, even when an immediate read is empty.
        set_status(db, r, "uncertain", "Awaiting remote visibility or explicit manual resolution")
        return "uncertain"
    if classification == "absent" and previous in ("verified", "verified_manual", "verified_conflict", "remote_missing"):
        set_status(db, r, "remote_missing", "Previously verified record is missing; explicit absence confirmation required")
        return "remote_missing"
    if classification == "conflict":
        result = "verified_conflict" if previous in ("verified", "verified_manual", "verified_conflict", "remote_missing") else "conflict"
        set_status(db, r, result, "Existing remote values differ or timestamp correspondence is ambiguous")
        return result
    set_status(db, r, "inflight", "Network write may occur; do not blindly retry")
    try:
        if r.fat is None and r.bmi is None:
            response = api.add_weigh_in(weight=r.weight, unitKey="kg",
                                        timestamp=r.at.isoformat(timespec="milliseconds"))
        else:
            with utc_fit_clock():
                # UTC input fixes 0.3.16's mktime handling; the SDK encodes FIT seconds.
                response = api.add_body_composition(
                    timestamp=r.at.astimezone(UTC).isoformat(timespec="seconds"),
                    weight=r.weight, percent_fat=r.fat, bmi=r.bmi)
        if isinstance(response, dict):
            result = response.get("detailedImportResult", {})
            if isinstance(result, dict) and result.get("failures"):
                set_status(db, r, "uncertain", "Garmin import response reported failures; inspect remotely")
                return "uncertain"
        for delay in (0, 3, 7):
            if delay:
                sleep(delay)
            if classify_remote(r, remote_for(api, r)) == "matched":
                set_status(db, r, "verified", "Read-back matched after upload")
                return "verified"
        set_status(db, r, "uncertain", "Submitted but not verified by remote read-back")
    except Exception as exc:
        set_status(db, r, "uncertain", safe_exception(exc))
    return "uncertain"


def load_measurements(cfg: dict[str, Any]):
    samples, exports = load_exports(cfg)
    records, waiting = pair_samples(samples, int(cfg["pair_window_seconds"]), allow_weight_only=True)
    return samples, records, waiting, exports


def show_preview(cfg: dict[str, Any]) -> list[Measurement]:
    samples, records, waiting, exports = load_measurements(cfg)
    print("本地预览：不登录 Garmin，不上传。")
    print("最近导出：", json.dumps(exports, ensure_ascii=False))
    print(f"唯一原始样本 {len(samples)}；完整测量 {len(records)}；等待配对 {len(waiting)}")
    print(f"字段覆盖：体重 {len(records)} 条；含体脂 {sum(r.fat is not None for r in records)} 条；"
          f"含 BMI {sum(r.bmi is not None for r in records)} 条；"
          f"含瘦体重 {sum(r.lbm is not None for r in records)} 条（仅本地保留）。缺失指标不会自动推算。")
    with database() as db:
        for r in records:
            row = state_row(db, r)
            s = row["status"] if row else "pending"
            if row and row["core"] != r.core:
                s = "changed"
            elif row and r.bmi is not None and json.loads(row["payload"]).get("bmi") != r.bmi:
                s = "needs_compare"
            bmi = "—" if r.bmi is None else f"{r.bmi:.2f}"
            fat = "—" if r.fat is None else f"{r.fat:.3f}%"
            lbm = "—" if r.lbm is None else f"{r.lbm:.3f} kg（仅本地）"
            print(f"{r.key}  {s:15s}  {r.at.isoformat()}  {r.weight:.3f} kg  {fat}  BMI={bmi}  LBM={lbm}")
    for msg in waiting:
        print("WAIT", msg)
    return records


def run_sync(cfg: dict[str, Any], record_id: str | None = None) -> None:
    if record_id is None and not cfg.get("enabled", False):
        print("自动写入未启用；本次只预览。启用前先完成单条试传。")
        show_preview(cfg)
        return
    samples, records, waiting, exports = load_measurements(cfg)
    if record_id:
        records = [r for r in records if r.key == record_id]
        if len(records) != 1:
            raise SafeError("ID 不对应唯一完整记录，请从 ./hg preview 复制。")
        r = records[0]
        print(json.dumps(r.payload(), ensure_ascii=False, indent=2))
        if input("这会向 Garmin 中国区写入真实数据。确认请输入 UPLOAD：").strip() != "UPLOAD":
            print("已取消。")
            return
    summary = {"run_at": now().isoformat(), "exports": exports,
               "complete_measurements": len(records), "waiting_pairs": len(waiting), "results": {}}
    if not records:
        print("没有可用测量，无须访问 Garmin。")
        write_json(STATE / "last_run.json", summary)
        return
    with database() as db:
        api = api_client()
        plan = load_local_comparison(cfg, records, api, db, full=record_id is not None)
        summary["comparison"] = plan["counts"]
        print_comparison(plan) if plan["entries"] else print("近期没有需要复核的测量。")
        by_id = {r.key: r for r in records}
        writes = 0
        recent = [aware(v) for k, v in exports.items() if k in ("weight", "body_fat")]
        settling = not record_id and recent and (now() - max(recent)).total_seconds() < int(cfg["settle_seconds"])
        for entry in plan["entries"]:
            r, action = by_id[entry["id"]], entry["action"]
            if action == "matched":
                set_status(db, r, "verified", "Remote comparison matched time and provided values")
                result = "verified"
            elif action == "review_missing":
                set_status(db, r, "remote_missing", entry["reason"])
                result = "remote_missing"
            elif action in ("repair", "replace", "conflict"):
                blocked = "verified_conflict" if entry["local_status"] in ("verified", "verified_manual", "verified_conflict", "remote_missing") else "conflict"
                set_status(db, r, blocked, entry["reason"])
                result = action
            elif action == "uncertain":
                result = process_record(api, db, r)
            elif action == "missing" and not settling and writes < int(cfg["max_records_per_run"]):
                result = process_record(api, db, r)
                writes += 1
            else:
                result = "ignored" if action == "ignored" else "deferred"
            summary["results"][r.key] = result
            print(r.key, result)
            LOG.info("record=%s status=%s", r.key, result)
        write_json(STATE / "last_sync_comparison.json", {"checked_at": now().isoformat()})
        if settling:
            summary["deferred"] = "settling"
            print("手机刚导出，新增测量留待下一轮处理。")
    write_json(STATE / "last_run.json", summary)


def choose_folder() -> str:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["/usr/bin/osascript", "-e",
                'POSIX path of (choose folder with prompt "选择 iCloud Drive / 快捷指令 / Health / inbox 文件夹")'],
                check=True, text=True, capture_output=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            pass
    return input("粘贴导出 inbox 的绝对路径（不带外层引号）：").strip().strip("\"'")


def init_config(args: argparse.Namespace) -> None:
    if CONFIG.exists():
        raise SafeError("已有 config.json，不会覆盖。更改配置请用 settings。")
    inbox = Path(args.inbox or choose_folder()).expanduser().resolve()
    if not inbox.is_dir():
        raise SafeError("请选择已经存在且能读取的 inbox 文件夹。")
    default_since = datetime.now().astimezone().date().isoformat()
    since = args.since or input(f"从哪一天开始上传？默认只从今天 [{default_since}]：").strip() or default_since
    date.fromisoformat(since)
    source = input("PICOOC 在健康中显示的准确来源名称 [PICOOC]：").strip() or "PICOOC"
    cfg = {"is_cn": True, "inbox": str(inbox), "allowed_sources": [source],
           "since": since, "weight_bare_unit": "kg", "fat_bare_unit": "percent",
           "pair_window_seconds": 0, "settle_seconds": 120,
           "max_records_per_run": 10, "enabled": False}
    write_json(CONFIG, cfg)
    print("已初始化，默认只同步选定起始日之后的数据，自动写入关闭。")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def update_settings(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    changes = {"fat_bare_unit": args.fat_unit, "weight_bare_unit": args.weight_unit,
               "pair_window_seconds": args.pair_window, "since": args.since,
               "inbox": args.inbox}
    if args.source:
        changes["allowed_sources"] = [args.source]
    if cfg.get("enabled") and any(v is not None for v in changes.values()):
        raise SafeError("先运行 ./hg disable，再修改单位、来源、时间或目录，并重新预览。")
    for k, v in changes.items():
        if v is not None:
            cfg[k] = v
    if args.inbox:
        p = Path(args.inbox).expanduser().resolve()
        if not p.is_dir():
            raise SafeError("inbox 不存在。")
        cfg["inbox"] = str(p)
    date.fromisoformat(cfg["since"])
    if not 0 <= int(cfg["pair_window_seconds"]) <= 120:
        raise SafeError("配对窗口只能在 0..120 秒，建议默认 0。")
    write_json(CONFIG, cfg)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def inspect_files(cfg: dict[str, Any]) -> None:
    inbox = Path(cfg["inbox"])
    files = sorted(inbox.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    print("目录：", inbox)
    print("JSON 文件数：", len(files))
    for p in files[:6]:
        if p.is_symlink():
            raise SafeError("导出目录含符号链接，已停止。")
        if not 0 < p.stat().st_size <= 4 * 1024 * 1024:
            raise SafeError(f"{p.name} 为空或超过 4 MB。")
        try:
            obj = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SafeError(f"{p.name} 不是完整 UTF-8 JSON，或尚未下载完毕。") from exc
        print("\n文件：", p.name)
        try:
            groups = export_groups(obj)
        except SafeError as exc:
            print("格式检查未通过：", exc)
            continue
        print("schema_version=", obj.get("schema_version"), "exported_at=", obj.get("exported_at"))
        for metric, items in groups.items():
            print("metric=", metric, "样本数：", len(items))
            for item in items[-3:]:
                print(json.dumps(item, ensure_ascii=False))
    print("注意：这是原始数值显示；尚未转换单位，也不会上传。")


def agent_plist() -> dict[str, Any]:
    return {"Label": LABEL,
            "ProgramArguments": [str(BASE / ".venv/bin/python"), str(BASE / "health_garmin.py"), "run"],
            "WorkingDirectory": str(BASE), "StartInterval": 1800, "RunAtLoad": True,
            "ProcessType": "Background",
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "HOME": str(Path.home())},
            "StandardOutPath": str(STATE / "agent.out.log"),
            "StandardErrorPath": str(STATE / "agent.err.log")}


def manage_agent(install: bool, cfg: dict[str, Any]) -> None:
    if sys.platform != "darwin":
        raise SafeError("LaunchAgent 仅能在 macOS 安装。")
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if install and not cfg.get("enabled"):
        raise SafeError("先完成试传并运行 ./hg enable，然后安装定时任务。")
    # Only this tool's known service label is affected; no system-wide changes.
    subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    if not install:
        path.unlink(missing_ok=True)
        print("已卸载本工具的定时任务；本地状态、导出和 Garmin 数据均保留。")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SafeError("拒绝覆盖符号链接 plist。")
    path.write_bytes(plistlib.dumps(agent_plist()))
    path.chmod(0o600)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)], check=True)
    print("定时任务已注册：当前用户登录时，每 30 分钟尝试处理。")
    print("首次检查可能与本命令的锁重叠。稍后可手动运行 ./hg run 验证。")


def show_status(cfg: dict[str, Any]) -> None:
    print("自动写入：", cfg.get("enabled"), "；地区：Garmin 中国区")
    print("导出目录：", cfg["inbox"], "；上传起始日期：", cfg["since"])
    print("运行解释器：", sys.executable)
    print("登录令牌目录：", STATE / "tokens", "（不要共享）")
    with database() as db:
        for row in db.execute("SELECT status,COUNT(*) AS n FROM uploads GROUP BY status"):
            print(row["status"], row["n"])
        for row in db.execute("SELECT record_id,status,updated_at,detail FROM uploads ORDER BY updated_at DESC LIMIT 10"):
            print(dict(row))
    p = STATE / "last_run.json"
    if p.exists():
        print(p.read_text(encoding="utf-8"))
    if sys.platform == "darwin":
        result = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
        print("LaunchAgent 已载入：", result.returncode == 0)


def resolve_record(args: argparse.Namespace) -> None:
    with database() as db:
        row = db.execute("SELECT * FROM uploads WHERE record_id=?", (args.record_id,)).fetchone()
        if not row:
            raise SafeError("没有这个 ID 的本地上传状态。")
        r = Measurement(**{**json.loads(row["payload"]), "at": aware(json.loads(row["payload"])["at"])})
        print(json.dumps(r.payload(), ensure_ascii=False, indent=2))
        print("本工具不会删除或覆盖任何 Garmin 记录。请先在 Garmin 核对日期、时间、体重、体脂率。")
        if args.present:
            if input("确认 Garmin 已存在这条正确记录，输入 PRESENT：").strip() == "PRESENT":
                set_status(db, r, "verified_manual", "User verified in Garmin")
        elif args.absent:
            if r.key in active_operations():
                raise SafeError("此测量有未完成的修复操作，请使用 verify-operation 或 resume-operation 保留备份字段。")
            if row["status"] in ("verified", "verified_manual"):
                raise SafeError("已验证记录不开放自动重置，避免误重复。")
            if input("确认 Garmin 未收到该记录，允许下一次尝试上传，输入 ABSENT：").strip() == "ABSENT":
                set_status(db, r, "pending", "User explicitly confirmed absence")
        elif args.ignore:
            if input("仅忽略这条本地任务，输入 IGNORE：").strip() == "IGNORE":
                set_status(db, r, "ignored", "User chose not to upload")


def main() -> int:
    os.umask(0o077)
    private_dir(STATE)
    logging.basicConfig(level=logging.INFO, handlers=[
        RotatingFileHandler(STATE / "sync.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")],
        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("init"); p.add_argument("--inbox"); p.add_argument("--since")
    for name in ("inspect", "preview", "login", "run", "enable", "disable", "status", "install-agent", "uninstall-agent"):
        subs.add_parser(name)
    p = subs.add_parser("upload-one"); p.add_argument("record_id")
    p = subs.add_parser("compare", help="只读查询 Garmin，生成当前或历史差异清单")
    p.add_argument("--since"); p.add_argument("--until"); p.add_argument("--inbox")
    p = subs.add_parser("repair-one", help="按对账清单备份并修复指定的一条冲突测量")
    p.add_argument("record_id"); p.add_argument("--plan", required=True)
    p = subs.add_parser("delete-extra", help="按对账清单删除指定的一条多余 Garmin 测量")
    p.add_argument("remote_id"); p.add_argument("--plan", required=True)
    p.add_argument("--complete-export", action="store_true")
    p = subs.add_parser("replace-all", help="以完整 Apple 导出为准，备份并替换选定日期范围的 Garmin 历史")
    p.add_argument("--plan", required=True)
    p.add_argument("--complete-export", action="store_true")
    p = subs.add_parser("verify-operation", help="只读核对未完成的修复或删除操作")
    p.add_argument("operation")
    p = subs.add_parser("resume-operation", help="人工确认目标缺失后恢复已备份的修复测量")
    p.add_argument("operation")
    p = subs.add_parser("remote"); p.add_argument("date")
    p = subs.add_parser("settings")
    p.add_argument("--fat-unit", choices=("percent", "fraction"))
    p.add_argument("--weight-unit", choices=("kg", "lb", "g"))
    p.add_argument("--pair-window", type=int); p.add_argument("--since")
    p.add_argument("--source"); p.add_argument("--inbox")
    p = subs.add_parser("resolve"); p.add_argument("record_id")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--present", action="store_true")
    group.add_argument("--absent", action="store_true")
    group.add_argument("--ignore", action="store_true")
    args = parser.parse_args()
    try:
        with (STATE / "run.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("另一个进程正在处理，本次退出，避免重复上传。")
                return 0
            if args.command == "init":
                init_config(args)
                return 0
            cfg = config()
            if args.command == "inspect": inspect_files(cfg)
            elif args.command == "preview": show_preview(cfg)
            elif args.command == "login":
                api_client(interactive=True)
                print("已登录 Garmin 中国区。令牌保存在本机 state/tokens；未上传健康数据。")
            elif args.command == "run": run_sync(cfg)
            elif args.command == "upload-one": run_sync(cfg, args.record_id)
            elif args.command == "compare": compare_exports(args, cfg)
            elif args.command == "repair-one": reviewed_operation(args, cfg)
            elif args.command == "delete-extra": reviewed_operation(args, cfg, delete_extra=True)
            elif args.command == "replace-all":
                from replace_history import execute
                execute(args, cfg)
            elif args.command == "verify-operation": verify_operation(args)
            elif args.command == "resume-operation": resume_operation(args)
            elif args.command == "settings": update_settings(args, cfg)
            elif args.command == "status": show_status(cfg)
            elif args.command in ("install-agent", "uninstall-agent"):
                manage_agent(args.command == "install-agent", cfg)
            elif args.command == "disable":
                cfg["enabled"] = False; write_json(CONFIG, cfg)
                print("自动写入已关闭。要停掉定时检查，再运行 ./hg uninstall-agent。")
            elif args.command == "enable":
                with database() as db:
                    n = db.execute("SELECT COUNT(*) FROM uploads WHERE status IN ('verified','verified_manual')").fetchone()[0]
                if not n:
                    raise SafeError("先完成至少一条真实记录试传及核验，再启用自动写入。")
                show_preview(cfg)
                if input("确认以后自动上传完整新测量，输入 ENABLE：").strip() == "ENABLE":
                    cfg["enabled"] = True; write_json(CONFIG, cfg)
                    print("自动写入已启用。")
            elif args.command == "remote":
                d = date.fromisoformat(args.date).isoformat()
                obj = api_client().get_body_composition(d, d)
                p = STATE / f"remote-{d}.json"; write_json(p, obj)
                print("仅查询，响应保存到：", p)
                print(json.dumps(obj, ensure_ascii=False, indent=2))
            elif args.command == "resolve": resolve_record(args)
        return 0
    except SafeError as exc:
        print("停止：", exc, file=sys.stderr); LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        print("已中断。上传中断的记录不会盲目重发。", file=sys.stderr)
        return 130
    except Exception as exc:
        # Raw tracebacks can contain tokens. Report type only by default.
        detail = safe_exception(exc)
        print("错误：", detail, "。请检查配置/网络；先核对 Garmin 再重试。", file=sys.stderr)
        LOG.error("Unhandled %s", detail)
        return 2


if __name__ == "__main__":
    # Helpers must share this module's SafeError class and state, rather than
    # importing a second health_garmin instance under another module name.
    sys.modules.setdefault("health_garmin", sys.modules[__name__])
    raise SystemExit(main())
