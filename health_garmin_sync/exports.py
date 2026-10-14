"""Validate and pair Apple Health export values without loading source files."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from . import domain as hg_domain
from . import runtime as hg_runtime


def parse_quantity(raw: Any, metric: str, cfg: dict[str, Any]) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise hg_domain.SafeError("value 必须为数值或带单位的文本，不能是健康样本对象。")
    txt = str(raw).strip().replace("\u00a0", " ").replace("\u202f", " ").replace("％", "%")
    m = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([^0-9]*)", txt)
    if not m:
        raise hg_domain.SafeError("无法解析数值；保留一个数字及可选单位，不使用千分位或逗号小数。")
    try:
        n = float(Decimal(m.group(1)))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise hg_domain.SafeError("无效数值。") from exc
    unit = m.group(2).strip().casefold()
    if metric in ("weight", "lbm"):
        if not unit:
            unit = cfg["weight_bare_unit"]
        factors = {
            "kg": 1,
            "公斤": 1,
            "千克": 1,
            "kilograms": 1,
            "g": 0.001,
            "克": 0.001,
            "lb": 0.45359237,
            "lbs": 0.45359237,
            "磅": 0.45359237,
        }
        if unit not in factors:
            raise hg_domain.SafeError("体重/瘦体重单位未知；支持 kg、g、lb，不会猜测裸数值单位。")
        n *= factors[unit]
        valid = 0 < n < 600
    elif metric == "body_fat":
        if unit in ("%", "percent", "百分比"):
            pass
        elif unit == "":
            declared = cfg["fat_bare_unit"]
            if declared == "fraction":
                if not 0 < n < 1:
                    raise hg_domain.SafeError(
                        "已设置 fraction，但裸体脂数值不在 0..1。请检查单位。"
                    )
                n *= 100
            elif declared == "percent":
                if 0 < n <= 1:
                    raise hg_domain.SafeError(
                        "裸体脂数值 <=1：可能是比例值。核对后用 ./health-garmin-sync settings --fat-unit fraction。"
                    )
            else:
                raise hg_domain.SafeError("请明确设置 fat_bare_unit 为 percent 或 fraction。")
        else:
            raise hg_domain.SafeError("体脂单位未知；不会自动猜测或乘以 100。")
        valid = 0 < n < 100
    elif metric == "bmi":
        if unit not in ("", "bmi", "kg/m²", "kg/m2"):
            raise hg_domain.SafeError("BMI 单位未知。")
        valid = 0 < n < 100
    else:
        raise hg_domain.SafeError("metric 必须为 weight、body_fat、bmi 或 lbm。")
    if not math.isfinite(n) or not valid:
        raise hg_domain.SafeError("数值超出本工具的数据校验范围，请检查单位和原始记录。")
    return round(n, 6)


def export_groups(obj: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize schema v1 (one metric) and v2 (a metrics dictionary).

    Preserve the v1 Sample/Measurement identity. A one-item dictionary is
    accepted for Shortcuts interoperability; JSON-in-a-string is not accepted.
    This function checks structure only; values and sources are checked later.
    """
    if not isinstance(obj, dict):
        raise hg_domain.SafeError("JSON 顶层必须为词典，不是数组或带引号的 JSON 文本。")
    schema = str(obj.get("schema_version"))
    if schema not in hg_domain.SUPPORTED_SCHEMAS:
        raise hg_domain.SafeError("文件必须使用 schema_version=1 或 2；字段名不是 version。")
    if obj.get("demo"):
        raise hg_domain.SafeError("示例文件禁止上传。请从自己的 Apple 健康导出真实记录。")
    if schema == "1":
        if obj.get("metric") not in hg_domain.SUPPORTED_METRICS:
            raise hg_domain.SafeError("文件 metric 必须为 weight、body_fat、bmi 或 lbm。")
        if "metrics" in obj:
            raise hg_domain.SafeError("schema_version=1 不能混入 metrics；合并文件请使用版本 2。")
        raw_groups = {obj["metric"]: obj.get("samples")}
    else:
        if "metric" in obj or "samples" in obj:
            raise hg_domain.SafeError("版本 2 顶层使用 metrics，不能保留单指标 metric/samples。")
        raw_groups = obj.get("metrics")
        if not isinstance(raw_groups, dict) or not raw_groups:
            raise hg_domain.SafeError("版本 2 的 metrics 必须为非空词典，不能是 JSON 文本或数组。")
        if any(key not in hg_domain.SUPPORTED_METRICS for key in raw_groups):
            if "bodt_fat" in raw_groups:
                raise hg_domain.SafeError(
                    "bodt_fat 拼写错误，请在 iPhone 快捷指令中改为 body_fat 后重新导出。"
                )
            raise hg_domain.SafeError("metrics 中仅支持 weight、body_fat、bmi、lbm；请检查键名。")

    groups: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for metric, items in raw_groups.items():
        if isinstance(items, dict):
            # Reject a whole child payload accidentally inserted instead of samples.
            if not all(field in items for field in ("at", "value", "source")):
                raise hg_domain.SafeError(
                    f"{metric} 应放 samples 列表，不是整个子快捷指令返回词典。"
                )
            items = [items]
        if not isinstance(items, list):
            raise hg_domain.SafeError(
                f"{metric} 样本必须为数组或单个样本词典，不能是带引号的 JSON 文本。"
            )
        total += len(items)
        if total > 20000:
            raise hg_domain.SafeError("单文件样本合计过多，请缩短快捷指令导出范围。")
        for item in items:
            if not isinstance(item, dict):
                raise hg_domain.SafeError(
                    f"{metric} 中必须是一条条样本词典，不能是嵌套数组或文本。"
                )
            if not all(field in item for field in ("at", "value", "source")):
                raise hg_domain.SafeError(f"{metric} 样本必须有 at、value、source。")
            if item.get("demo"):
                raise hg_domain.SafeError("示例样本禁止上传。请从自己的 Apple 健康导出真实记录。")
        groups[metric] = items
    return groups


def decode_export(obj: Any, cfg: dict[str, Any]) -> tuple[list[hg_domain.Sample], datetime]:
    groups = export_groups(obj)
    exported = hg_domain.aware(obj.get("exported_at", ""))
    future_limit = hg_runtime.now() + timedelta(minutes=10)
    if exported > future_limit:
        raise hg_domain.SafeError("发现未来导出时间，请检查 exported_at、时区和手机时间。")
    allowed = {hg_domain.normalized_source(s) for s in cfg["allowed_sources"]}
    output: list[hg_domain.Sample] = []
    for metric, samples in groups.items():
        for item in samples:
            source = item.get("source")
            if not isinstance(source, str) or not source.strip():
                raise hg_domain.SafeError("样本缺少 source；请导出真实来源或使用已核对的来源过滤。")
            if hg_domain.normalized_source(source) not in allowed:
                continue
            dt = hg_domain.aware(item.get("at", ""))
            if dt.date() < date.fromisoformat(cfg["since"]):
                continue
            if dt > future_limit:
                raise hg_domain.SafeError("发现未来测量时间，请检查日期格式、时区和手机时间。")
            value = parse_quantity(item.get("value"), metric, cfg)
            output.append(hg_domain.Sample(metric, dt, value, source.strip()))
    return output, exported


def deduplicate(samples: list[hg_domain.Sample]) -> list[hg_domain.Sample]:
    unique: dict[tuple[str, str, str], hg_domain.Sample] = {}
    for s in samples:
        previous = unique.get(s.key)
        if previous and abs(previous.value - s.value) > 0.000001:
            raise hg_domain.SafeError(
                f"同来源、同时间的 {s.metric} 出现不同值：{s.at.isoformat()}。"
                "请检查旧导出/修订，不会静默覆盖。"
            )
        unique[s.key] = s
    return sorted(unique.values(), key=lambda s: (s.at, s.metric))


def pair_samples(
    samples: list[hg_domain.Sample], window: int = 0, allow_weight_only: bool = False
) -> tuple[list[hg_domain.Measurement], list[str]]:
    weights = [s for s in samples if s.metric == "weight"]
    fats = [s for s in samples if s.metric == "body_fat"]
    bmis = [s for s in samples if s.metric == "bmi"]
    lbms = [s for s in samples if s.metric == "lbm"]
    paired: list[hg_domain.Measurement] = []
    waiting = []

    def candidates(
        anchor: hg_domain.Sample, pool: list[hg_domain.Sample]
    ) -> list[hg_domain.Sample]:
        same_source = [
            s
            for s in pool
            if hg_domain.normalized_source(s.source) == hg_domain.normalized_source(anchor.source)
        ]
        exact = [s for s in same_source if s.at == anchor.at]
        return exact or [
            s for s in same_source if abs((s.at - anchor.at).total_seconds()) <= window
        ]

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
            raise hg_domain.SafeError(f"{w.at.isoformat()}：瘦体重大于体重，请核对单位和测量。")
        paired.append(
            hg_domain.Measurement(w.at, w.value, f.value if f else None, w.source, bmi, lbm)
        )
    return sorted(paired, key=lambda r: r.at), waiting
