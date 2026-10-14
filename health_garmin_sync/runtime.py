"""Repository paths, clock, logger, and configuration validation."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import domain as hg_domain
from . import file_io as hg_file_io

BASE = Path(__file__).resolve().parents[1]

STATE = BASE / "state"

CONFIG = BASE / "config.json"

LABEL = "local.health-garmin-sync"

REQUIRED_GARMIN_VERSION = "0.3.16"

LOG = logging.getLogger("health_garmin")


def now() -> datetime:
    return datetime.now(UTC)


def config() -> dict[str, Any]:
    if not CONFIG.exists():
        raise hg_domain.SafeError("尚未初始化，请先运行 ./health-garmin-sync init。")
    try:
        cfg = json.loads(hg_file_io.read_regular_text(CONFIG, reject_symlink=False))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise hg_domain.SafeError(
            "config.json 无法读取为完整 UTF-8 JSON，请检查本地配置。"
        ) from exc
    return validate_config(cfg)


def validate_config(cfg: Any) -> dict[str, Any]:
    """Validate read and prospective settings without changing their values."""
    if not isinstance(cfg, dict):
        raise hg_domain.SafeError("config.json 必须是 JSON 对象。")
    if cfg.get("is_cn") is not True:
        raise hg_domain.SafeError("本工具仅配置为 Garmin 中国区；is_cn 必须为 true。")
    if type(cfg.get("enabled")) is not bool:
        raise hg_domain.SafeError(
            "config.json 的 enabled 必须为布尔值 true 或 false，不能是文本或数字。"
        )
    if "archive_after_sync" in cfg and type(cfg["archive_after_sync"]) is not bool:
        raise hg_domain.SafeError("config.json 的 archive_after_sync 必须为布尔值 true 或 false。")
    if "health_export_enabled" in cfg and type(cfg["health_export_enabled"]) is not bool:
        raise hg_domain.SafeError("config.json 的 health_export_enabled 必须为布尔值。")
    health_outbox = cfg.get("health_outbox")
    if health_outbox is not None and (
        not isinstance(health_outbox, str)
        or not health_outbox.strip()
        or not Path(health_outbox).is_absolute()
    ):
        raise hg_domain.SafeError("config.json 的 health_outbox 必须为绝对目录路径或 null。")
    if cfg.get("health_export_enabled", False) and health_outbox is None:
        raise hg_domain.SafeError("启用血压导出前须设置独立的 health_outbox 目录。")
    for key in ("inbox", "since"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise hg_domain.SafeError(f"config.json 的 {key} 必须为非空文本。")
    try:
        date.fromisoformat(cfg["since"])
    except ValueError as exc:
        raise hg_domain.SafeError("config.json 的 since 必须为有效日期，例如 2026-09-29。") from exc
    sources = cfg.get("allowed_sources")
    if (
        not isinstance(sources, list)
        or not sources
        or any(not isinstance(s, str) or not s.strip() for s in sources)
    ):
        raise hg_domain.SafeError("config.json 的 allowed_sources 必须为包含非空来源名称的列表。")
    for key, choices in (
        ("weight_bare_unit", ("kg", "lb", "g")),
        ("fat_bare_unit", ("percent", "fraction")),
    ):
        if cfg.get(key) not in choices:
            raise hg_domain.SafeError(f"config.json 的 {key} 必须为 " + "、".join(choices) + "。")
    for key, minimum, maximum in (
        ("pair_window_seconds", 0, 120),
        ("settle_seconds", 0, None),
        ("max_records_per_run", 1, None),
    ):
        value = cfg.get(key)
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            bounds = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
            raise hg_domain.SafeError(f"config.json 的 {key} 必须为 {bounds} 的整数。")
    return cfg
