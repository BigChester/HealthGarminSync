"""Load live and archived exports into normalized local measurements."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import domain as hg_domain
from . import exports as hg_exports


def load_exports(
    cfg: dict[str, Any], *, source_snapshot=None, include_archive: bool = True
) -> tuple[list[hg_domain.Sample], dict[str, str]]:
    from . import source_archive

    inbox = Path(cfg["inbox"]).expanduser()
    if not inbox.is_dir():
        raise hg_domain.SafeError(
            "找不到导出目录，请确认 iCloud 下载状态和 config.json 中的真实路径。"
        )
    canonical = source_archive.canonical_inbox(cfg)
    snapshots = [
        source_archive.capture(path, canonical) for path in source_archive.live_paths(inbox)
    ]
    if include_archive:
        snapshots.extend(source_archive.read_archive(cfg))
    if not snapshots:
        raise hg_domain.SafeError("导出目录没有 .json 文件；先在 iPhone 手动运行导出快捷指令。")
    all_samples: list[hg_domain.Sample] = []
    exports: dict[str, str] = {}
    allowed = {hg_domain.normalized_source(source) for source in cfg["allowed_sources"]}
    for snapshot in snapshots:
        try:
            obj = json.loads(snapshot.raw.decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise hg_domain.SafeError(
                f"{snapshot.name} 不是完整 UTF-8 JSON，或尚未下载完毕。"
            ) from exc
        try:
            new, exported = hg_exports.decode_export(obj, cfg)
        except hg_domain.SafeError as exc:
            raise hg_domain.SafeError(f"{snapshot.name}: {exc}") from exc
        snapshot.samples = new
        snapshot.exported_at = exported.isoformat()
        all_samples.extend(new)
        for metric, items in hg_exports.export_groups(obj).items():
            if items and not any(
                hg_domain.normalized_source(item["source"]) in allowed for item in items
            ):
                continue
            if metric not in exports or exported > hg_domain.aware(exports[metric]):
                exports[metric] = exported.isoformat()
    samples = hg_exports.deduplicate(all_samples)
    if source_snapshot is not None:
        source_snapshot.extend(snapshots)
    return samples, exports


def load_measurements(cfg: dict[str, Any], *, source_snapshot=None, include_archive=True):
    samples, exports = load_exports(
        cfg, source_snapshot=source_snapshot, include_archive=include_archive
    )
    records, waiting = hg_exports.pair_samples(
        samples, int(cfg["pair_window_seconds"]), allow_weight_only=True
    )
    return samples, records, waiting, exports
