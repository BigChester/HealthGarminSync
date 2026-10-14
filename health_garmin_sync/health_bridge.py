"""Coordinate the optional Garmin blood-pressure export to the iPhone bridge."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import domain as hg_domain
from . import file_io as hg_file_io
from . import garmin as hg_garmin
from . import health_outbox as hg_health_outbox
from . import runtime as hg_runtime


def china_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def validate_outbox(value: str, cfg: dict[str, Any]) -> Path:
    path = Path(value).expanduser().absolute()
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise hg_domain.SafeError("血压输出路径必须是普通目录。")
    resolved = path.resolve()
    for protected in (Path(cfg["inbox"]).expanduser().resolve(), hg_runtime.STATE.resolve()):
        if resolved == protected or resolved in protected.parents or protected in resolved.parents:
            raise hg_domain.SafeError("血压输出目录必须独立于称重 inbox 和本地 state。")
    return path


def export(
    cfg: dict[str, Any],
    *,
    outbox: str,
    since: str | None = None,
    until: str | None = None,
    batch_size: int = 100,
    automatic: bool = False,
) -> dict[str, int]:
    directory = validate_outbox(outbox, cfg)
    try:
        start = date.fromisoformat(since or cfg["since"])
        end = date.fromisoformat(until) if until else china_today()
    except (TypeError, ValueError) as exc:
        raise hg_domain.SafeError("血压导出日期必须为 YYYY-MM-DD。") from exc
    if start > end or end > china_today():
        raise hg_domain.SafeError("血压导出日期必须从早到晚，且结束日期不能晚于中国区今天。")
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise hg_domain.SafeError("血压单批数量必须为 1..100 的整数。")
    if automatic:
        start = hg_health_outbox.recommended_since(start, end)
    result = hg_health_outbox.export_blood_pressure(
        hg_garmin.api_client(), outbox=directory, since=start, until=end, batch_size=batch_size
    )
    hg_file_io.write_json(
        hg_runtime.STATE / "last_health_export.json",
        {
            "run_at": hg_runtime.now().isoformat(),
            "since": start.isoformat(),
            "until": end.isoformat(),
            "results": result,
        },
    )
    print("Garmin 血压导出：", result)
    return result


def run_configured(cfg: dict[str, Any]) -> dict[str, int] | None:
    if not cfg.get("health_export_enabled", False):
        return None
    return export(cfg, outbox=cfg["health_outbox"], automatic=True)
