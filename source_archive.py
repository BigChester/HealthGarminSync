"""Durable, exact source exports and conservative inbox cleanup."""
from __future__ import annotations

import contextlib
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import unicodedata

import health_garmin as h


@dataclass
class SourceSnapshot:
    inbox: str
    name: str
    raw: bytes
    exported_at: str = ""
    path: Path | None = None
    identity: tuple | None = None
    samples: list = field(default_factory=list)

    @property
    def sha256(self):
        return hashlib.sha256(self.raw).hexdigest()


def archive_path():
    return h.STATE / "source_archive.sqlite3"


def canonical_inbox(cfg):
    return str(Path(cfg["inbox"]).expanduser().resolve())


def file_identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def live_paths(inbox):
    """Recover JSONs moved before a crash; empty private directories are inert."""
    files = list(inbox.glob("*.json"))
    for directory in inbox.glob(".hg-archive-*"):
        if not re.fullmatch(r"\.hg-archive-[a-z0-9_]{8}", directory.name):
            continue
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise h.SafeError("归档隔离路径必须是私密普通目录，不能是符号链接。")
        files.extend(directory.glob("*.json"))
    return sorted(files)


def capture(path, inbox):
    """Read only a stable regular file, including its original encoding bytes."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise h.SafeError("导出文件必须是普通文件，不能是符号链接。")
    if not 0 < before.st_size <= 4 * 1024 * 1024:
        raise h.SafeError(f"{path.name} 为空或超过 4 MB，请检查 iCloud 文件下载。")
    with h.open_regular_file(path, "rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(4 * 1024 * 1024 + 1)
        after_read = os.fstat(stream.fileno())
    after = path.lstat()
    identity = file_identity(before)
    if (identity != file_identity(opened) or identity != file_identity(after_read)
            or identity != file_identity(after) or len(raw) != before.st_size):
        raise h.SafeError("文件正在更新，本次暂停；下一轮会重新读取。")
    return SourceSnapshot(inbox, path.name, raw, path=path, identity=identity)


_COLUMNS = {"inbox", "filename", "sha256", "raw", "exported_at", "archived_at"}


def _regular_database(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        raise h.SafeError("原始导出归档数据库必须是普通文件，不能是符号链接。")
    return True


def _schema(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(source_exports)")}
    if columns != _COLUMNS:
        raise h.SafeError("原始导出归档数据库结构无效；保留 inbox，先检查本地备份。")


def _validated(row):
    inbox, name, digest, raw, exported, archived = row
    if (not isinstance(inbox, str) or not Path(inbox).is_absolute()
            or not isinstance(name, str) or not name.endswith(".json")
            or name in (".", "..") or Path(name).name != name or "/" in name
            or "\x00" in name
            or not isinstance(raw, bytes) or not 0 < len(raw) <= 4 * 1024 * 1024
            or not isinstance(digest, str) or hashlib.sha256(raw).hexdigest() != digest):
        raise h.SafeError("原始导出归档内容或校验和损坏；不会清理 inbox。")
    h.aware(exported)
    h.aware(archived)
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
        h.export_groups(obj)
        if h.aware(obj.get("exported_at", "")).isoformat() != exported:
            raise h.SafeError("原始导出归档时间与内容不一致。")
    except (UnicodeError, ValueError, TypeError) as exc:
        raise h.SafeError("原始导出归档不是完整 UTF-8 JSON。") from exc
    return SourceSnapshot(inbox, name, raw, exported)


def read_archive(cfg):
    path = archive_path()
    if not _regular_database(path):
        return []
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            _schema(db)
            rows = db.execute("SELECT inbox,filename,sha256,raw,exported_at,archived_at "
                              "FROM source_exports WHERE inbox=? ORDER BY filename,sha256",
                              (canonical_inbox(cfg),)).fetchall()
            return [_validated(row) for row in rows]
    except sqlite3.Error as exc:
        raise h.SafeError("原始导出归档数据库无法读取；保留 inbox，先检查本地备份。") from exc


def persist(snapshots):
    """Commit all candidates before opening a separate read-only connection."""
    path = archive_path()
    existed = _regular_database(path)
    h.private_dir(path.parent)
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=10)) as db:
            db.execute("PRAGMA synchronous=FULL")
            if existed:
                _schema(db)
            else:
                db.execute("""CREATE TABLE source_exports (
                    inbox TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
                    raw BLOB NOT NULL, exported_at TEXT NOT NULL, archived_at TEXT NOT NULL,
                    PRIMARY KEY(inbox,filename,sha256))""")
            with db:
                for snapshot in snapshots:
                    db.execute("INSERT OR IGNORE INTO source_exports VALUES (?,?,?,?,?,?)",
                               (snapshot.inbox, snapshot.name, snapshot.sha256, snapshot.raw,
                                snapshot.exported_at, h.now().isoformat()))
            path.chmod(0o600)
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
            _schema(reader)
            for snapshot in snapshots:
                rows = reader.execute("SELECT inbox,filename,sha256,raw,exported_at,archived_at "
                                      "FROM source_exports WHERE inbox=? AND filename=? AND sha256=?",
                                      (snapshot.inbox, snapshot.name, snapshot.sha256)).fetchall()
                if len(rows) != 1 or _validated(rows[0]).raw != snapshot.raw:
                    raise h.SafeError("原始导出归档持久校验失败；inbox 保留。")
    except sqlite3.Error as exc:
        raise h.SafeError("原始导出归档保存或持久校验失败；inbox 保留。") from exc


def _verified_owners(samples, records, db, cfg):
    """Index sample ownership once; each file then needs only dictionary lookups."""
    window = int(cfg["pair_window_seconds"])
    fields = {"weight": "weight", "body_fat": "fat", "bmi": "bmi", "lbm": "lbm"}
    groups = {}
    for sample in samples:
        groups.setdefault((sample.metric, h.normalized_source(sample.source)), []).append(sample)
    indexed = {}
    for key, group in groups.items():
        group.sort(key=lambda sample: sample.at)
        indexed[key] = ([sample.at.timestamp() for sample in group], group)
    owners = {}
    verified = {}
    for record in records:
        row = h.state_row(db, record)
        payload = json.loads(row["payload"]) if row else {}
        verified[record.key] = bool(row and row["status"] in ("verified", "verified_manual")
                                    and row["core"] == record.core
                                    and payload.get("bmi") == record.bmi
                                    and (record.lbm is None or payload.get("lbm") == record.lbm))
        for metric, field_name in fields.items():
            stamps, group = indexed.get((metric, h.normalized_source(record.source)), ([], []))
            at = record.at.timestamp()
            exact = group[bisect_left(stamps, at):bisect_right(stamps, at)]
            selected = exact or group[bisect_left(stamps, at - window):bisect_right(stamps, at + window)]
            if len(selected) == 1 and getattr(record, field_name) == selected[0].value:
                owners.setdefault(selected[0].key, []).append(record.key)
    return {key for key, keys in owners.items() if len(keys) == 1 and verified[keys[0]]}


def _restore_quarantine(quarantine, original):
    """Restore without overwriting a concurrent iCloud replacement."""
    try:
        os.link(quarantine, original, follow_symlinks=False)
    except OSError:
        return False
    try:
        quarantine.unlink()
    except OSError:
        return False
    return True


def remove_snapshot(snapshot):
    """Move first, then inspect the moved inode before deleting anything."""
    path = snapshot.path
    try:
        current = capture(path, snapshot.inbox)
        if current.identity != snapshot.identity or current.raw != snapshot.raw:
            return "changed"
    except FileNotFoundError:
        return "missing"
    except (OSError, h.SafeError):
        return "changed"
    try:
        directory = Path(tempfile.mkdtemp(prefix=".hg-archive-", dir=snapshot.inbox))
    except OSError:
        return "failed"
    quarantine = directory / snapshot.name
    try:
        os.replace(path, quarantine)
    except OSError:
        directory.rmdir()
        return "failed"
    try:
        moved = capture(quarantine, snapshot.inbox)
        # rename updates ctime on some filesystems; inode, size, mtime and bytes
        # still identify the exact snapshot moved out of the original name.
        if moved.identity[:4] != snapshot.identity[:4] or moved.raw != snapshot.raw:
            _restore_quarantine(quarantine, path)
            return "changed"
        quarantine.unlink()
        directory.rmdir()
        if re.fullmatch(r"\.hg-archive-[a-z0-9_]{8}", path.parent.name):
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return "removed"
    except (OSError, h.SafeError):
        _restore_quarantine(quarantine, path)
        return "failed"
    finally:
        try:
            directory.rmdir()
        except OSError:
            pass


def archive_completed(cfg, snapshots, samples, records, db):
    counts = {"archived": 0, "removed": 0, "changed": 0, "failed": 0, "missing": 0}
    if (not cfg.get("archive_after_sync", False) or h.unknown_request_count(db)
            or h.active_operations()):
        return counts
    owned = _verified_owners(samples, records, db, cfg)
    candidates = [snapshot for snapshot in snapshots if snapshot.path is not None
                  and snapshot.samples and all(sample.key in owned for sample in snapshot.samples)]
    if not candidates:
        return counts
    persist(candidates)
    counts["archived"] = len(candidates)
    for snapshot in candidates:
        counts[remove_snapshot(snapshot)] += 1
    return counts


def export_snapshots(snapshots, destination):
    """Write an independently readable, private export set without overwrites."""
    checked = {}
    used = set()
    for snapshot in snapshots:
        if (Path(snapshot.name).name != snapshot.name or not snapshot.name.endswith(".json")
                or not isinstance(snapshot.raw, bytes)
                or not 0 < len(snapshot.raw) <= 4 * 1024 * 1024):
            raise h.SafeError("原始导出恢复内容无效。")
        pair = snapshot.name, snapshot.sha256
        if pair in checked:
            continue
        name = snapshot.name
        normalized_name = lambda value: unicodedata.normalize("NFC", value).casefold()
        if normalized_name(name) in used:
            name = "archive-" + snapshot.sha256 + ".json"
            counter = 0
            while normalized_name(name) in used:
                counter += 1
                name = f"archive-{counter}-{snapshot.sha256}.json"
        used.add(normalized_name(name))
        checked[pair] = name, snapshot.raw
    destination = Path(destination).expanduser()
    if destination.exists() or destination.is_symlink():
        raise h.SafeError("恢复目标已存在；请选择新的空目录，不覆盖原有文件。")
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise h.SafeError("无法创建私密恢复目录；请选择已有父目录中的新目录。") from exc
    for name, raw in checked.values():
        path = destination / name
        with path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
    return len(checked)


def restore_exports(cfg, destination):
    snapshots = read_archive(cfg)  # Validate every archived byte before writing.
    if not snapshots:
        raise h.SafeError("当前 inbox 没有本地原始导出归档。")
    count = export_snapshots(snapshots, destination)
    print(f"已精确恢复 {count} 个原始 JSON：{Path(destination).expanduser()}")
