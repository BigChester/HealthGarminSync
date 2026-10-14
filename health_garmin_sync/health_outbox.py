"""Durable Garmin blood-pressure export and iPhone receipt reconciliation.

Only normalized records enter the cloud directory. SDK response JSON, including
unknown fields, remains in the private local database; it is not raw HTTP bytes.
A receipt attests that the iPhone client reported success, not an independent
read of Apple Health. Sources are never removed when absent from a later query.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import blood_pressure as hg_blood_pressure
from . import domain as hg_domain
from . import file_io as hg_file_io
from . import runtime as hg_runtime

MAX_WIRE_BYTES = 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_UTC_STAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)"
)
_SCHEMA = {
    "binding": {"singleton", "account_id", "outbox", "last_successful_scan_until"},
    "source_responses": {"sequence", "requested_day", "captured_at", "sha256", "raw"},
    "measurements": {"id", "payload", "revision", "delivered_revision"},
    "batch": {"singleton", "batch_id", "raw", "sha256", "status", "receipt"},
    "scan_coverage": {"since_day", "until_day"},
}


def _error(message: str) -> hg_domain.SafeError:
    return hg_domain.SafeError(message)


def _json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise _error("血压响应无法保存为完整 JSON；未发布本次数据。") from exc


def _decode(raw: bytes) -> Any:
    def invalid_constant(value):
        raise ValueError("nonfinite")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    if not isinstance(raw, bytes):
        raise _error("血压本地 JSON 状态类型损坏。")
    try:
        return json.loads(
            raw.decode("utf-8"), parse_constant=invalid_constant, object_pairs_hook=unique_object
        )
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise _error("血压交接文件或本地状态不是完整 JSON。") from exc


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc(value: Any, *, check_future: bool = False) -> None:
    if not isinstance(value, str) or not _UTC_STAMP.fullmatch(value):
        raise _error("血压回执时间必须为有效 UTC ISO 8601 时间。")
    try:
        imported_at = datetime.fromisoformat(value)
        if check_future and imported_at > datetime.now(UTC) + timedelta(minutes=10):
            raise _error("血压回执时间位于未来；请检查设备时间。")
    except ValueError as exc:
        raise _error("血压回执时间无效。") from exc


def _regular(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        raise _error("血压交接文件与本地状态必须是普通文件，不能是符号链接。")
    return True


def _identity(metadata) -> tuple:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read(path: Path) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_WIRE_BYTES:
        raise _error("血压交接文件必须是非空普通文件且不超过 1 MB。")
    with hg_file_io.open_regular_file(path, "rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_WIRE_BYTES + 1)
        after_read = os.fstat(stream.fileno())
    after = path.lstat()
    if (
        len(raw) != before.st_size
        or _identity(before) != _identity(opened)
        or _identity(before) != _identity(after_read)
        or _identity(before) != _identity(after)
    ):
        raise _error("血压交接文件正在变化；保留文件后重试。")
    return raw


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _paths(outbox: Path, state_path: Path | None) -> tuple[Path, Path]:
    outbox = Path(outbox).expanduser().absolute()
    state_path = Path(state_path or hg_runtime.STATE / "garmin_health.sqlite3").expanduser()
    if outbox.is_symlink() or (outbox.exists() and not outbox.is_dir()):
        raise _error("血压 outbox 必须是普通目录，不能是符号链接。")
    outbox = outbox.resolve()
    state_path = state_path.absolute()
    if state_path.parent.is_symlink():
        raise _error("血压本地状态目录不能是符号链接。")
    state_path = state_path.parent.resolve() / state_path.name
    if (
        _overlap(outbox, hg_runtime.STATE.resolve())
        or _overlap(outbox, state_path)
        or _overlap(outbox, state_path.parent / ".garmin-health-cleanup")
    ):
        raise _error("血压 outbox 不能与私密本地状态目录重叠。")
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    if outbox.is_symlink() or not outbox.is_dir():
        raise _error("血压 outbox 必须是普通目录。")
    if state_path.parent == hg_runtime.STATE.resolve() or not state_path.parent.exists():
        hg_file_io.private_dir(state_path.parent)
    elif not state_path.parent.is_dir():
        raise _error("血压本地状态父路径必须是普通目录。")
    for suffix in ("", "-journal", "-wal", "-shm"):
        _regular(Path(str(state_path) + suffix))
    _same_filesystem(outbox, state_path.parent)
    return outbox, state_path


def _same_filesystem(outbox: Path, local_parent: Path) -> None:
    if outbox.stat().st_dev != local_parent.stat().st_dev:
        raise _error("血压 outbox 与本地状态必须位于同一文件系统；未发布交接批次。")


def _database(path: Path, account_id: str, outbox: Path) -> sqlite3.Connection:
    existed = _regular(path)
    if not existed:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    path.chmod(0o600)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        if existed:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise _error("血压本地数据库损坏；请保留状态与交接文件。")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != set(_SCHEMA):
                raise _error("血压本地数据库结构无效；请保留状态与交接文件。")
            for table, expected in _SCHEMA.items():
                if {r[1] for r in db.execute(f"PRAGMA table_info({table})")} != expected:
                    raise _error("血压本地数据库结构无效。")
        else:
            db.executescript(
                "CREATE TABLE binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "account_id TEXT NOT NULL,outbox TEXT NOT NULL,last_successful_scan_until TEXT);"
                "CREATE TABLE source_responses(sequence INTEGER PRIMARY KEY,"
                "requested_day TEXT NOT NULL,captured_at TEXT NOT NULL,sha256 TEXT NOT NULL,"
                "raw BLOB NOT NULL);"
                "CREATE TABLE measurements(id TEXT PRIMARY KEY,payload BLOB NOT NULL,"
                "revision INTEGER NOT NULL,delivered_revision INTEGER NOT NULL);"
                "CREATE TABLE batch(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "batch_id TEXT NOT NULL,raw BLOB NOT NULL,sha256 TEXT NOT NULL,"
                "status TEXT NOT NULL,receipt BLOB);"
                "CREATE TABLE scan_coverage(since_day TEXT PRIMARY KEY,until_day TEXT NOT NULL);"
            )
        binding = db.execute("SELECT account_id,outbox FROM binding WHERE singleton=1").fetchone()
        if binding is None:
            if existed:
                raise _error("血压本地数据库缺少账户绑定。")
            with db:
                db.execute("INSERT INTO binding VALUES(1,?,?,NULL)", (account_id, str(outbox)))
        elif tuple(binding) != (account_id, str(outbox)):
            raise _error("血压本地状态已绑定其他账户或 outbox；未修改交接文件。")
        for row in db.execute("SELECT id,payload,revision,delivered_revision FROM measurements"):
            payload = _checked_payload(row["payload"], account_id, row["id"])
            if (
                not isinstance(row["id"], str)
                or not _HEX64.fullmatch(row["id"])
                or not isinstance(payload, dict)
                or type(row["revision"]) is not int
                or row["revision"] < 1
                or type(row["delivered_revision"]) is not int
                or not 0 <= row["delivered_revision"] <= row["revision"]
            ):
                raise _error("血压本地记录状态损坏。")
        for row in db.execute("SELECT requested_day,captured_at,sha256,raw FROM source_responses"):
            if (
                not isinstance(row["raw"], bytes)
                or _digest(row["raw"]) != row["sha256"]
                or date.fromisoformat(row["requested_day"]).isoformat() != row["requested_day"]
            ):
                raise _error("血压 SDK 响应归档内容损坏。")
            _decode(row["raw"])
            _utc(row["captured_at"])
        _coverage(db)
        return db
    except BaseException:
        db.close()
        raise


def _checked_payload(raw: bytes, account_id: str, record_id: str) -> dict:
    payload = _decode(raw)
    fields = {
        "source_id",
        "at",
        "local_timestamp",
        "source_type",
        "systolic",
        "diastolic",
        "pulse",
        "multi_measurement",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise _error("血压本地标准化记录字段损坏。")
    record = hg_blood_pressure.parse_blood_pressure(
        {
            "measurementSummaries": [
                {
                    "measurements": [
                        {
                            "version": payload["source_id"],
                            "measurementTimestampGMT": payload["at"],
                            "measurementTimestampLocal": payload["local_timestamp"],
                            "sourceType": payload["source_type"],
                            "systolic": payload["systolic"],
                            "diastolic": payload["diastolic"],
                            "pulse": payload["pulse"],
                            "multiMeasurement": payload["multi_measurement"],
                        }
                    ],
                }
            ]
        }
    )[0]
    expected = _digest(f"cn|{account_id}|{record.local_timestamp[:10]}|{record.source_id}".encode())
    if expected != record_id or _json(record.payload()) != raw:
        raise _error("血压本地记录标识或标准化内容损坏。")
    return payload


def _batch(db) -> sqlite3.Row | None:
    row = db.execute("SELECT * FROM batch WHERE singleton=1").fetchone()
    if row is None:
        return None
    if (
        not isinstance(row["raw"], bytes)
        or not 0 < len(row["raw"]) <= MAX_WIRE_BYTES
        or _digest(row["raw"]) != row["sha256"]
        or row["status"] not in ("pending", "acked")
    ):
        raise _error("血压本地批次状态损坏；不会修改交接文件。")
    payload = _decode(row["raw"])
    if (
        not isinstance(payload, dict)
        or payload.get("batch_id") != row["batch_id"]
        or not isinstance(row["batch_id"], str)
        or not _HEX32.fullmatch(row["batch_id"])
        or payload.get("kind") != "garmin_blood_pressure"
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise _error("血压本地批次内容损坏。")
    _pairs(payload.get("records"))
    account_id = db.execute("SELECT account_id FROM binding WHERE singleton=1").fetchone()[0]
    if (
        set(payload)
        != {"schema_version", "kind", "batch_id", "account_id", "created_at", "units", "records"}
        or payload.get("account_id") != account_id
        or payload.get("units") != {"blood_pressure": "mmHg", "pulse": "count/min"}
    ):
        raise _error("血压本地批次字段或账户绑定损坏。")
    _utc(payload["created_at"])
    for record in payload["records"]:
        _checked_payload(
            _json({k: v for k, v in record.items() if k not in {"id", "revision"}}),
            account_id,
            record["id"],
        )
        local = db.execute(
            "SELECT revision FROM measurements WHERE id=?", (record["id"],)
        ).fetchone()
        if local is None or local[0] < record["revision"]:
            raise _error("血压本地批次对应记录损坏。")
    if row["status"] == "acked":
        if not isinstance(row["receipt"], bytes):
            raise _error("血压本地确认状态损坏。")
        _receipt(row["receipt"], row)
    elif row["receipt"] is not None:
        raise _error("血压本地确认状态损坏。")
    return row


def _pairs(records: Any) -> set[tuple[str, int]]:
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise _error("血压交接记录数量无效。")
    pairs = set()
    ids = set()
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("id"), str)
            or not _HEX64.fullmatch(record["id"])
            or type(record.get("revision")) is not int
            or record["revision"] < 1
            or record["id"] in ids
        ):
            raise _error("血压回执记录标识、版本或重复记录无效。")
        ids.add(record["id"])
        pairs.add((record["id"], record["revision"]))
    return pairs


def _receipt(raw: bytes, batch) -> set[tuple[str, int]]:
    obj = _decode(raw)
    if (
        not isinstance(obj, dict)
        or set(obj)
        != {"schema_version", "kind", "batch_id", "payload_sha256", "imported_at", "records"}
        or type(obj.get("schema_version")) is not int
        or obj["schema_version"] != 1
        or obj.get("kind") != "garmin_blood_pressure_receipt"
        or obj.get("batch_id") != batch["batch_id"]
        or obj.get("payload_sha256") != batch["sha256"]
    ):
        raise _error("血压回执与当前批次或校验和不匹配；保留所有交接文件。")
    _utc(obj["imported_at"], check_future=True)
    pairs = _pairs(obj["records"])
    if any(set(record) != {"id", "revision"} for record in obj["records"]):
        raise _error("血压回执记录字段无效。")
    if pairs != _pairs(_decode(batch["raw"])["records"]):
        raise _error("血压回执记录与当前批次不匹配；未确认任何记录。")
    return pairs


def _publish(path: Path, raw: bytes) -> bool:
    current = _read(path)
    if current is not None:
        if current != raw:
            raise _error("现有 pending 与本地批次不同；保留文件，不覆盖。")
        return False
    fd, temporary = tempfile.mkstemp(prefix=".garmin-publish-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # An exclusive atomic link prevents a newly arrived cloud file being replaced.
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read(path) != raw:
                raise _error("发布时 pending 已变化；未覆盖现有文件。") from None
        return True
    finally:
        temporary.unlink()


def _remove_confirmed(path: Path, expected: bytes, batch_id: str, private_dir: Path) -> None:
    """Move atomically out of the cloud namespace; delete only private local files.

    A cloud replacement can race the rename. Verify moved bytes and identity,
    then restore unexpected data without overwriting any newer cloud name.
    Cross-device rename is deliberately never replaced by copy-and-unlink.
    """
    quarantine = private_dir / f".garmin-cleanup-{batch_id}-{path.name}"
    moved = _read(quarantine)
    if moved is not None:
        if moved != expected:
            raise _error("血压确认清理证据发生变化；保留文件。")
        quarantine.unlink()
    metadata = path.lstat() if path.exists() or path.is_symlink() else None
    current = _read(path)
    if current is None:
        return
    if current != expected:
        raise _error("血压已确认文件发生变化；保留文件与本地批次。")
    try:
        os.rename(path, quarantine)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise _error("血压清理时文件系统发生变化；云端原文件与本地已确认批次均保留。") from exc
        raise
    moved = _read(quarantine)
    moved_metadata = quarantine.lstat()
    if (
        moved != expected
        or metadata is None
        or _identity(metadata)[:4] != _identity(moved_metadata)[:4]
    ):
        # Never overwrite a replacement that appeared at the original name.
        try:
            os.link(quarantine, path)
        except FileExistsError:
            pass
        else:
            quarantine.unlink()
        raise _error("血压清理时文件发生变化；保留证据。")
    quarantine.unlink()


def _reconcile(db, outbox: Path, counts: dict, private_dir: Path) -> None:
    batch = _batch(db)
    pending = outbox / "pending.json"
    receipt = outbox / "receipt.json"
    if batch is None:
        if _read(pending) is not None or _read(receipt) is not None:
            raise _error("outbox 存在未绑定交接文件；保留证据，不覆盖。")
        return
    if batch["status"] == "pending":
        existing = _read(pending)
        if existing is not None and existing != batch["raw"]:
            raise _error("现有 pending 与本地批次不同；保留所有文件。")
        raw_receipt = _read(receipt)
        if raw_receipt is None:
            if _publish(pending, batch["raw"]):
                counts["published_batches"] += 1
            return
        pairs = _receipt(raw_receipt, batch)
        with db:
            for record_id, revision in pairs:
                changed = db.execute(
                    "UPDATE measurements SET delivered_revision=max(delivered_revision,?) "
                    "WHERE id=? AND revision>=?",
                    (revision, record_id, revision),
                ).rowcount
                if changed != 1:
                    raise _error("血压回执对应的本地记录损坏。")
            db.execute(
                "UPDATE batch SET status='acked',receipt=? WHERE singleton=1", (raw_receipt,)
            )
        counts["acknowledged_records"] += len(pairs)
        batch = _batch(db)
    _remove_confirmed(pending, batch["raw"], batch["batch_id"], private_dir)
    _remove_confirmed(receipt, batch["receipt"], batch["batch_id"], private_dir)
    # Clear only after both names are absent, keeping ACK evidence during IO failures.
    if _read(pending) is not None or _read(receipt) is not None:
        raise _error("血压交接文件在清理后变化；保留本地确认状态。")
    with db:
        db.execute("DELETE FROM batch WHERE singleton=1")


def _coverage(db) -> list[tuple[date, date]]:
    intervals = []
    for row in db.execute("SELECT since_day,until_day FROM scan_coverage ORDER BY since_day"):
        since, until = date.fromisoformat(row[0]), date.fromisoformat(row[1])
        if (
            since.isoformat() != row[0]
            or until.isoformat() != row[1]
            or since > until
            or (intervals and since.toordinal() <= intervals[-1][1].toordinal() + 1)
        ):
            raise _error("血压成功扫描覆盖区间损坏。")
        intervals.append((since, until))
    return intervals


def _record_coverage(db, since: date, until: date) -> None:
    intervals = sorted([*_coverage(db), (since, until)])
    merged = []
    for start, end in intervals:
        if merged and start.toordinal() <= merged[-1][1].toordinal() + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    db.execute("DELETE FROM scan_coverage")
    db.executemany(
        "INSERT INTO scan_coverage VALUES(?,?)",
        [(start.isoformat(), end.isoformat()) for start, end in merged],
    )


def _fetch(api, db, account_id: str, since: date, until: date, counts: dict) -> None:
    day = since
    while day <= until:
        try:
            response = api.get_blood_pressure(day.isoformat())
        except Exception as exc:
            raise _error("Garmin 血压查询失败；已保存的本地记录与交接文件均保留。") from exc
        raw = _json(response)
        with db:
            db.execute(
                "INSERT INTO source_responses(requested_day,captured_at,sha256,raw) VALUES(?,?,?,?)",
                (day.isoformat(), _stamp(), _digest(raw), raw),
            )
        # Archive even malformed SDK responses before any interpretation.
        records = hg_blood_pressure.parse_blood_pressure(response)
        if any(record.local_timestamp[:10] != day.isoformat() for record in records):
            raise _error("Garmin 血压记录本地日期与请求日期不同；保留响应，未发布该日数据。")
        with db:
            for record in records:
                record_id = _digest(
                    f"cn|{account_id}|{record.local_timestamp[:10]}|{record.source_id}".encode()
                )
                payload = _json(record.payload())
                previous = db.execute(
                    "SELECT payload FROM measurements WHERE id=?", (record_id,)
                ).fetchone()
                if previous is None:
                    db.execute("INSERT INTO measurements VALUES(?,?,1,0)", (record_id, payload))
                    counts["new_records"] += 1
                elif previous["payload"] != payload:
                    db.execute(
                        "UPDATE measurements SET payload=?,revision=revision+1 WHERE id=?",
                        (payload, record_id),
                    )
                    counts["updated_records"] += 1
        counts["fetched_days"] += 1
        counts["source_records"] += len(records)
        if day == until:
            break
        day += timedelta(days=1)
    with db:
        _record_coverage(db, since, until)
        db.execute(
            "UPDATE binding SET last_successful_scan_until="
            "max(coalesce(last_successful_scan_until,''),?) WHERE singleton=1",
            (until.isoformat(),),
        )


def recommended_since(
    default_since: date,
    until: date,
    state_path: Path | None = None,
) -> date:
    """Find the first gap from configured since, with seven recent days reread.

    A successful manual scan proves coverage only for its requested interval.
    The informational maximum scan date is never used to infer earlier coverage.
    """
    if type(default_since) is not date or type(until) is not date or default_since > until:
        raise _error("血压自动扫描范围必须为有序日期。")
    try:
        path = (
            Path(state_path or hg_runtime.STATE / "garmin_health.sqlite3").expanduser().absolute()
        )
        if path.parent.is_symlink():
            raise _error("血压本地状态目录不能是符号链接。")
        if not _regular(path):
            return default_since
        for suffix in ("-journal", "-wal", "-shm"):
            _regular(Path(str(path) + suffix))
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise _error("血压自动扫描状态损坏。")
            columns = {row[1] for row in db.execute("PRAGMA table_info(binding)")}
            if columns != _SCHEMA["binding"]:
                raise _error("血压自动扫描状态结构无效。")
            if db.execute("SELECT account_id FROM binding WHERE singleton=1").fetchone() is None:
                raise _error("血压自动扫描状态缺少账户绑定。")
            if {row[1] for row in db.execute("PRAGMA table_info(scan_coverage)")} != _SCHEMA[
                "scan_coverage"
            ]:
                raise _error("血压自动扫描覆盖区间结构无效。")
            cursor = default_since.toordinal()
            intervals = _coverage(db)
            if any(end > until for _, end in intervals):
                raise _error("血压自动扫描日期位于未来。")
            for start, end in intervals:
                if end.toordinal() < cursor:
                    continue
                if start.toordinal() > cursor:
                    break
                cursor = end.toordinal() + 1
            first_missing = date.fromordinal(min(cursor, date.max.toordinal()))
            overlap = until - timedelta(days=6) if until.toordinal() > 6 else date.min
            return max(default_since, min(first_missing, overlap))
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        raise _error("血压自动扫描状态无法读取；保留本地数据库。") from exc


def _new_batch(db, account_id: str, batch_size: int) -> bool:
    if _batch(db) is not None:
        return False
    rows = db.execute(
        "SELECT id,payload,revision FROM measurements WHERE revision>delivered_revision "
        "ORDER BY id LIMIT ?",
        (batch_size,),
    ).fetchall()
    if not rows:
        return False
    payload = {
        "schema_version": 1,
        "kind": "garmin_blood_pressure",
        "batch_id": uuid.uuid4().hex,
        "account_id": account_id,
        "created_at": _stamp(),
        "units": {"blood_pressure": "mmHg", "pulse": "count/min"},
        "records": [
            {**_decode(row["payload"]), "id": row["id"], "revision": row["revision"]}
            for row in rows
        ],
    }
    raw = _json(payload)
    if len(raw) > MAX_WIRE_BYTES:
        raise _error("血压批次超过 1 MB；请减小 batch_size 后重试。")
    with db:
        db.execute(
            "INSERT INTO batch VALUES(1,?,?,?,'pending',NULL)",
            (payload["batch_id"], raw, _digest(raw)),
        )
    return True


def export_blood_pressure(
    api,
    *,
    outbox: Path,
    since: date,
    until: date,
    batch_size: int = 100,
    state_path: Path | None = None,
) -> dict[str, int]:
    """Query inclusive Garmin local days, reconcile a receipt, and publish one batch.

    Repeating a call preserves unchanged record revisions and an unconfirmed
    pending file. A changed record queues its next revision after the old batch
    is acknowledged. Failures raise SafeError without exposing account or data.
    The returned counts contain no identifiers or medical values.
    """
    if type(since) is not date or type(until) is not date or since > until:
        raise _error("血压查询范围必须为有序日期。")
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise _error("血压 batch_size 必须为 1..100 的整数。")
    profile_id = getattr(api, "profile_id", None)
    if type(profile_id) is not int or profile_id <= 0:
        raise _error("Garmin 登录必须提供稳定的正整数 profile_id。")
    account_id = _digest(f"cn|{profile_id}".encode())
    counts = dict.fromkeys(
        (
            "fetched_days",
            "source_records",
            "new_records",
            "updated_records",
            "acknowledged_records",
            "pending_records",
            "queued_records",
            "published_batches",
        ),
        0,
    )
    try:
        outbox, state_path = _paths(outbox, state_path)
        cleanup_dir = state_path.parent / ".garmin-health-cleanup"
        hg_file_io.private_dir(cleanup_dir)
        lock_path = state_path.with_name(state_path.name + ".lock")
        with hg_file_io.open_regular_append(lock_path) as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.closing(_database(state_path, account_id, outbox)) as db:
                # Reconcile before querying so a network outage cannot stall an ACK.
                _reconcile(db, outbox, counts, cleanup_dir)
                _fetch(api, db, account_id, since, until, counts)
                _new_batch(db, account_id, batch_size)
                _reconcile(db, outbox, counts, cleanup_dir)
                batch = _batch(db)
                counts["pending_records"] = len(_decode(batch["raw"])["records"]) if batch else 0
                counts["queued_records"] = db.execute(
                    "SELECT count(*) FROM measurements WHERE revision>delivered_revision"
                ).fetchone()[0]
        return counts
    except (OSError, sqlite3.Error, OverflowError, TypeError, ValueError) as exc:
        raise _error("血压交接或本地状态操作失败；请保留文件后重试。") from exc
