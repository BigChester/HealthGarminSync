"""Offline outbox tests using synthetic SDK responses and a temporary cloud folder."""

import contextlib
import copy
import errno
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

from health_garmin_sync import domain as hg_domain
from health_garmin_sync import health_outbox as hg_outbox

DAY = date(2026, 10, 15)


def measurement(**changes):
    return {
        "version": 1001,
        "measurementTimestampGMT": "2026-10-14T16:30:00.123",
        "measurementTimestampLocal": "2026-10-15T00:30:00.123",
        "sourceType": "MANUAL",
        "systolic": 120,
        "diastolic": 80,
        "pulse": 65,
        "multiMeasurement": False,
        **changes,
    }


def envelope(*records):
    return {
        "measurementSummaries": [{"numOfMeasurements": len(records), "measurements": list(records)}]
    }


class FakeAPI:
    def __init__(self, responses=None, profile_id=123456):
        self.profile_id = profile_id
        self.responses = responses or {DAY.isoformat(): envelope(measurement())}
        self.calls = []

    def get_blood_pressure(self, day):
        self.calls.append(day)
        response = self.responses.get(day, envelope())
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class HealthOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.outbox = self.base / "cloud"
        self.state = self.base / "private" / "garmin_health.sqlite3"
        self.api = FakeAPI()

    def export(self, api=None, **changes):
        return hg_outbox.export_blood_pressure(
            api or self.api,
            **{
                "outbox": self.outbox,
                "state_path": self.state,
                "since": DAY,
                "until": DAY,
                **changes,
            },
        )

    def pending(self):
        return json.loads((self.outbox / "pending.json").read_bytes())

    def receipt(self, **changes):
        raw = (self.outbox / "pending.json").read_bytes()
        pending = json.loads(raw)
        receipt = {
            "schema_version": 1,
            "kind": "garmin_blood_pressure_receipt",
            "batch_id": pending["batch_id"],
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
            "imported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "records": [
                {"id": row["id"], "revision": row["revision"]} for row in pending["records"]
            ],
            **changes,
        }
        (self.outbox / "receipt.json").write_text(json.dumps(receipt))
        return receipt

    def rows(self, table):
        with contextlib.closing(sqlite3.connect(self.state)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def test_export_exact_wire_contract_hash_and_sdk_archive(self):
        response = self.api.responses[DAY.isoformat()]
        response["private_account"] = "PRIVATE_ACCOUNT"
        response["measurementSummaries"][0]["measurements"][0]["notes"] = "PRIVATE_NOTES"
        result = self.export()
        self.assertEqual(result["fetched_days"], 1)
        self.assertEqual(result["new_records"], 1)
        self.assertEqual(result["published_batches"], 1)
        self.assertEqual(self.api.calls, [DAY.isoformat()])
        pending = self.pending()
        self.assertEqual(
            set(pending),
            {"schema_version", "kind", "batch_id", "account_id", "created_at", "units", "records"},
        )
        self.assertEqual(pending["account_id"], hashlib.sha256(b"cn|123456").hexdigest())
        self.assertEqual(pending["kind"], "garmin_blood_pressure")
        record = pending["records"][0]
        expected_id = hashlib.sha256(
            f"cn|{pending['account_id']}|2026-10-15|1001".encode()
        ).hexdigest()
        self.assertEqual(record["id"], expected_id)
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["at"], "2026-10-14T16:30:00.123Z")
        self.assertEqual(record["local_timestamp"], "2026-10-15T00:30:00.123")
        cloud = (self.outbox / "pending.json").read_text()
        self.assertNotIn("PRIVATE", cloud)
        self.assertNotIn("123456", cloud)
        archived = self.rows("source_responses")[0]
        self.assertEqual(json.loads(archived["raw"]), response)
        self.assertEqual(hashlib.sha256(archived["raw"]).hexdigest(), archived["sha256"])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.outbox / "pending.json").stat().st_mode), 0o600)
        self.assertEqual(len(list(self.outbox.iterdir())), 1)

    def test_repeated_queries_preserve_pending_and_record_revision(self):
        self.export()
        before = (self.outbox / "pending.json").read_bytes()
        metadata = (self.outbox / "pending.json").stat()
        result = self.export()
        self.assertEqual((self.outbox / "pending.json").read_bytes(), before)
        self.assertEqual((self.outbox / "pending.json").stat().st_ino, metadata.st_ino)
        self.assertEqual(result["new_records"], 0)
        self.assertEqual(result["updated_records"], 0)
        self.assertEqual(result["published_batches"], 0)
        self.assertEqual(self.rows("measurements")[0]["revision"], 1)
        self.assertEqual(len(self.rows("source_responses")), 2)
        self.assertEqual(self.api.calls, [DAY.isoformat()] * 2)

    def test_batch_acknowledgement_advances_to_next_batch_without_repolling(self):
        self.api.responses[DAY.isoformat()] = envelope(
            *[measurement(version=1000 + number) for number in range(5)]
        )
        result = self.export(batch_size=2)
        self.assertEqual((result["pending_records"], result["queued_records"]), (2, 5))
        batches = []
        for expected_pending, expected_queued in ((2, 3), (1, 1), (0, 0)):
            batches.append(self.pending()["batch_id"])
            self.receipt()
            result = self.export(batch_size=2)
            self.assertEqual(
                (result["pending_records"], result["queued_records"]),
                (expected_pending, expected_queued),
            )
        self.assertEqual(len(set(batches)), 3)
        self.assertEqual(len(self.api.calls), 4)
        self.assertFalse((self.outbox / "receipt.json").exists())
        self.assertFalse((self.outbox / "pending.json").exists())
        self.assertEqual(len(self.rows("source_responses")), 4)

    def test_changed_record_does_not_replace_unconfirmed_batch(self):
        self.export()
        before = (self.outbox / "pending.json").read_bytes()
        self.api.responses[DAY.isoformat()] = envelope(measurement(systolic=130))
        result = self.export()
        self.assertEqual(result["updated_records"], 1)
        self.assertEqual((self.outbox / "pending.json").read_bytes(), before)
        self.assertEqual(self.rows("measurements")[0]["revision"], 2)
        self.receipt()
        result = self.export()
        row = self.rows("measurements")[0]
        self.assertEqual((row["revision"], row["delivered_revision"]), (2, 1))
        self.assertEqual(self.pending()["records"][0]["revision"], 2)
        self.assertEqual(result["queued_records"], 1)
        self.receipt()
        self.export()
        self.assertEqual(self.rows("measurements")[0]["delivered_revision"], 2)

    def test_change_discovered_after_receipt_stays_queued(self):
        self.export()
        self.receipt()
        self.api.responses[DAY.isoformat()] = envelope(measurement(pulse=70))
        self.export()
        row = self.rows("measurements")[0]
        self.assertEqual((row["revision"], row["delivered_revision"]), (2, 1))
        self.assertEqual(self.pending()["records"][0]["revision"], 2)

    def test_invalid_receipts_never_acknowledge_or_clean(self):
        self.export()
        pending = self.pending()
        valid = self.receipt()
        invalids = [
            {"payload_sha256": "0" * 64},
            {"batch_id": "0" * 32},
            {"schema_version": True},
            {"kind": "other"},
            {"imported_at": "2026-02-30T10:00:00Z"},
            {"imported_at": "2026-10-15T00:00:00+08:00"},
            {"imported_at": "9999-01-01T00:00:00Z"},
            {"records": []},
            {"records": valid["records"] * 2},
            {"records": [{"id": "0" * 64, "revision": 1}]},
            {"records": [{"id": pending["records"][0]["id"], "revision": 2}]},
            {"records": [{"id": pending["records"][0]["id"], "revision": True}]},
            {"extra": "PRIVATE"},
        ]
        before = (self.outbox / "pending.json").read_bytes()
        for changes in invalids:
            with self.subTest(changes=changes):
                self.receipt(**changes)
                with self.assertRaises(hg_domain.SafeError):
                    self.export()
                self.assertEqual(self.rows("measurements")[0]["delivered_revision"], 0)
                self.assertEqual((self.outbox / "pending.json").read_bytes(), before)
                self.assertTrue((self.outbox / "receipt.json").exists())

    def test_old_receipt_cannot_ack_new_revision(self):
        self.export()
        old = self.receipt()
        self.api.responses[DAY.isoformat()] = envelope(measurement(systolic=130))
        self.export()
        (self.outbox / "receipt.json").write_text(json.dumps(old))
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(self.rows("measurements")[0]["delivered_revision"], 1)
        self.assertEqual(self.pending()["records"][0]["revision"], 2)

    def test_missing_pending_is_republished_using_identical_bytes(self):
        self.export()
        before = (self.outbox / "pending.json").read_bytes()
        (self.outbox / "pending.json").unlink()
        result = self.export()
        self.assertEqual((self.outbox / "pending.json").read_bytes(), before)
        self.assertEqual(result["published_batches"], 1)

    def test_network_failure_does_not_stop_receipt_ack(self):
        self.export()
        self.receipt()
        self.api.responses[DAY.isoformat()] = RuntimeError("PRIVATE_TOKEN")
        with self.assertRaises(hg_domain.SafeError) as raised:
            self.export()
        self.assertNotIn("PRIVATE_TOKEN", str(raised.exception))
        self.assertEqual(self.rows("measurements")[0]["delivered_revision"], 1)
        self.assertFalse((self.outbox / "pending.json").exists())
        self.assertFalse((self.outbox / "receipt.json").exists())

    def test_partial_query_failure_retains_days_without_advancing_scan(self):
        self.api.responses["2026-10-16"] = RuntimeError("offline")
        with self.assertRaises(hg_domain.SafeError):
            self.export(until=date(2026, 10, 16))
        self.assertEqual(len(self.rows("source_responses")), 1)
        self.assertEqual(len(self.rows("measurements")), 1)
        self.assertIsNone(self.rows("binding")[0]["last_successful_scan_until"])
        self.assertEqual(self.rows("scan_coverage"), [])
        self.assertFalse((self.outbox / "pending.json").exists())
        self.api.responses["2026-10-16"] = envelope()
        result = self.export(until=date(2026, 10, 16))
        self.assertEqual(result["new_records"], 0)
        self.assertEqual(result["fetched_days"], 2)
        self.assertEqual(self.rows("binding")[0]["last_successful_scan_until"], "2026-10-16")

    def test_malformed_response_is_archived_before_parser_failure(self):
        self.api.responses[DAY.isoformat()] = {"private": "PRIVATE", "measurementSummaries": None}
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(
            json.loads(self.rows("source_responses")[0]["raw"]), self.api.responses[DAY.isoformat()]
        )
        self.assertEqual(self.rows("measurements"), [])
        self.assertFalse((self.outbox / "pending.json").exists())

    def test_nonfinite_sdk_response_is_not_published(self):
        self.api.responses[DAY.isoformat()] = envelope(measurement(systolic=float("nan")))
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(self.rows("source_responses"), [])
        self.assertFalse((self.outbox / "pending.json").exists())

    def test_requested_day_uses_garmin_local_date(self):
        self.export()
        self.api.responses["2026-10-14"] = envelope(measurement())
        with self.assertRaises(hg_domain.SafeError):
            self.export(since=date(2026, 10, 14), until=date(2026, 10, 14))
        self.assertEqual(len(self.rows("measurements")), 1)
        self.assertEqual(len(self.rows("source_responses")), 2)

    def test_same_version_on_different_local_days_has_distinct_ids(self):
        self.api.responses["2026-10-16"] = envelope(
            measurement(
                measurementTimestampGMT="2026-10-15T16:30:00",
                measurementTimestampLocal="2026-10-16T00:30:00",
            )
        )
        self.export(until=date(2026, 10, 16))
        rows = self.pending()["records"]
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertEqual({row["source_id"] for row in rows}, {"1001"})

    def test_empty_day_retains_sources_and_unconfirmed_pending(self):
        self.export()
        before = (self.outbox / "pending.json").read_bytes()
        self.api.responses[DAY.isoformat()] = envelope()
        result = self.export()
        self.assertEqual(result["source_records"], 0)
        self.assertEqual((self.outbox / "pending.json").read_bytes(), before)
        self.assertEqual(len(self.rows("measurements")), 1)
        self.receipt()
        self.export()
        self.assertEqual(len(self.rows("measurements")), 1)

    def test_initial_empty_days_do_not_create_pending(self):
        self.api.responses[DAY.isoformat()] = envelope()
        result = self.export()
        self.assertEqual(result["pending_records"], 0)
        self.assertEqual(result["queued_records"], 0)
        self.assertEqual(len(self.rows("source_responses")), 1)
        self.assertEqual(list(self.outbox.iterdir()), [])

    def test_publication_failure_recovers_exact_saved_bytes(self):
        with mock.patch.object(hg_outbox.os, "link", side_effect=OSError("PRIVATE")):
            with self.assertRaises(hg_domain.SafeError):
                self.export()
        batch = self.rows("batch")[0]
        self.assertEqual(batch["status"], "pending")
        self.assertEqual(list(self.outbox.iterdir()), [])
        self.export()
        self.assertEqual((self.outbox / "pending.json").read_bytes(), batch["raw"])

    def test_cleanup_failure_preserves_committed_ack_and_resumes(self):
        self.export()
        self.receipt()
        original = hg_outbox._remove_confirmed

        def fail_receipt(path, *args):
            if path.name == "receipt.json":
                raise OSError("offline")
            return original(path, *args)

        with mock.patch.object(hg_outbox, "_remove_confirmed", side_effect=fail_receipt):
            with self.assertRaises(hg_domain.SafeError):
                self.export()
        self.assertEqual(self.rows("measurements")[0]["delivered_revision"], 1)
        self.assertEqual(self.rows("batch")[0]["status"], "acked")
        self.assertFalse((self.outbox / "pending.json").exists())
        self.assertTrue((self.outbox / "receipt.json").exists())
        self.export()
        self.assertEqual(self.rows("batch"), [])
        self.assertEqual(list(self.outbox.iterdir()), [])

    def test_cleanup_crash_after_quarantine_move_resumes(self):
        self.export()
        self.receipt()
        unlink = Path.unlink

        def crash(path, *args, **kwargs):
            if path.name.startswith(".garmin-cleanup-"):
                raise OSError("simulated crash")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", crash):
            with self.assertRaises(hg_domain.SafeError):
                self.export()
        self.assertEqual(self.rows("batch")[0]["status"], "acked")
        self.assertTrue(
            any(
                path.name.startswith(".garmin-cleanup-")
                for path in (self.state.parent / ".garmin-health-cleanup").iterdir()
            )
        )
        self.export()
        self.assertEqual(list(self.outbox.iterdir()), [])

    def test_changed_private_recovery_evidence_is_preserved(self):
        self.export()
        self.receipt()
        unlink = Path.unlink
        evidence = b'{"private_evidence":"changed"}'

        def interrupted_cleanup(path, *args, **kwargs):
            if path.name.startswith(".garmin-cleanup-"):
                path.write_bytes(evidence)
                raise OSError("interrupted")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", interrupted_cleanup):
            with self.assertRaises(hg_domain.SafeError):
                self.export()
        paths = list((self.state.parent / ".garmin-health-cleanup").iterdir())
        self.assertEqual(len(paths), 1)
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(paths[0].read_bytes(), evidence)
        self.assertEqual(self.rows("batch")[0]["status"], "acked")
        self.assertTrue((self.outbox / "receipt.json").exists())

    def test_final_unlink_happens_locally_and_preserves_new_cloud_file(self):
        self.export()
        self.receipt()
        cleanup = self.state.parent.resolve() / ".garmin-health-cleanup"
        self.assertEqual(stat.S_IMODE(cleanup.stat().st_mode), 0o700)
        unlink = Path.unlink
        changed = b'{"cloud_evidence":"replacement"}'
        removed = []

        def cloud_arrives_during_private_delete(path, *args, **kwargs):
            if path.parent == cleanup:
                removed.append(path)
                if path.name.endswith("pending.json"):
                    (self.outbox / "pending.json").write_bytes(changed)
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", cloud_arrives_during_private_delete):
            with self.assertRaises(hg_domain.SafeError):
                self.export()
        self.assertEqual((self.outbox / "pending.json").read_bytes(), changed)
        self.assertEqual(self.rows("batch")[0]["status"], "acked")
        self.assertTrue(removed)
        self.assertTrue(all(path.parent == cleanup for path in removed))
        self.assertEqual(list(cleanup.iterdir()), [])

    def test_runtime_cross_device_cleanup_preserves_files_and_committed_ack(self):
        self.export()
        self.receipt()
        before = {path.name: path.read_bytes() for path in self.outbox.iterdir()}
        with mock.patch.object(
            hg_outbox.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device")
        ):
            for _ in range(3):
                with self.assertRaises(hg_domain.SafeError):
                    self.export()
                self.assertEqual(
                    {path.name: path.read_bytes() for path in self.outbox.iterdir()}, before
                )
                self.assertEqual(self.rows("batch")[0]["status"], "acked")
                self.assertEqual(self.rows("measurements")[0]["delivered_revision"], 1)
                self.assertEqual(list((self.state.parent / ".garmin-health-cleanup").iterdir()), [])
        self.export()
        self.assertEqual(list(self.outbox.iterdir()), [])
        self.assertEqual(self.rows("batch"), [])

    def test_cross_device_configuration_is_rejected_before_fetch_and_publish(self):
        original_stat = Path.stat
        resolved_outbox = self.outbox.resolve()

        def different_device(path, *args, **kwargs):
            metadata = original_stat(path, *args, **kwargs)
            if path in (self.outbox, resolved_outbox):
                values = list(metadata)
                values[2] += 1
                return os.stat_result(values)
            return metadata

        with mock.patch.object(Path, "stat", different_device):
            with self.assertRaises(hg_domain.SafeError):
                self.export()
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.outbox.iterdir()), [])

    def test_private_cleanup_directory_symlink_is_rejected(self):
        self.state.parent.mkdir()
        cleanup = self.state.parent / ".garmin-health-cleanup"
        target = self.base / "evidence"
        target.mkdir(mode=0o755)
        cleanup.symlink_to(target, target_is_directory=True)
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertFalse(self.state.exists())

    def test_changed_file_during_ack_cleanup_is_preserved(self):
        self.export()
        self.receipt()
        rename = hg_outbox.os.rename
        changed = b'{"evidence":"changed"}'

        def replace_before_move(source, target):
            if Path(source).name == "pending.json":
                Path(source).write_bytes(changed)
            return rename(source, target)

        with mock.patch.object(hg_outbox.os, "rename", side_effect=replace_before_move):
            with self.assertRaises(hg_domain.SafeError):
                self.export()
        self.assertEqual((self.outbox / "pending.json").read_bytes(), changed)
        self.assertEqual(self.rows("batch")[0]["status"], "acked")
        self.assertTrue((self.outbox / "receipt.json").exists())

    def test_conflicting_pending_is_preserved(self):
        self.export()
        (self.outbox / "pending.json").write_bytes(b'{"evidence":"PRIVATE"}')
        with self.assertRaises(hg_domain.SafeError) as raised:
            self.export()
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertEqual((self.outbox / "pending.json").read_bytes(), b'{"evidence":"PRIVATE"}')
        self.assertEqual(self.rows("measurements")[0]["delivered_revision"], 0)

    def test_cloud_file_without_local_batch_is_not_overwritten(self):
        self.outbox.mkdir()
        (self.outbox / "pending.json").write_bytes(b'{"evidence":true}')
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(self.api.calls, [])
        self.assertEqual((self.outbox / "pending.json").read_bytes(), b'{"evidence":true}')

    def test_account_and_outbox_binding_cannot_change(self):
        self.export()
        original = (self.outbox / "pending.json").read_bytes()
        with self.assertRaises(hg_domain.SafeError):
            self.export(FakeAPI(profile_id=999))
        with self.assertRaises(hg_domain.SafeError):
            self.export(outbox=self.base / "other-cloud")
        self.assertEqual((self.outbox / "pending.json").read_bytes(), original)

    def test_bad_profile_id_and_batch_size_do_not_query(self):
        for invalid in (None, 0, -1, True, "123", 1.5):
            with self.subTest(profile=invalid), self.assertRaises(hg_domain.SafeError):
                self.export(FakeAPI(profile_id=invalid))
        for invalid in (True, 0, -1, 101, "10", 1.5):
            with self.subTest(batch=invalid), self.assertRaises(hg_domain.SafeError):
                self.export(batch_size=invalid)
        self.assertEqual(self.api.calls, [])

    def test_nonregular_pending_receipt_database_and_journal_are_rejected(self):
        self.export()
        original = (self.outbox / "pending.json").read_bytes()
        evidence = self.base / "evidence"
        evidence.write_bytes(original)
        paths = [
            self.outbox / "pending.json",
            self.outbox / "receipt.json",
            self.state,
            Path(str(self.state) + "-journal"),
        ]
        for path in paths:
            original_content = path.read_bytes() if path.exists() else None
            for kind in ("symlink", "fifo"):
                with self.subTest(path=path.name, kind=kind):
                    if path.exists() or path.is_symlink():
                        path.unlink()
                    if kind == "symlink":
                        path.symlink_to(evidence)
                    else:
                        os.mkfifo(path)
                    with self.assertRaises(hg_domain.SafeError):
                        self.export()
                    path.unlink()
                    if original_content is not None:
                        path.write_bytes(original_content)
        self.assertEqual(evidence.read_bytes(), original)

    def test_nonregular_outbox_and_overlapping_local_state_are_rejected(self):
        self.outbox.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.outbox.unlink()
        self.outbox.write_text("evidence")
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.outbox.unlink()
        for path in (self.state.parent, self.base):
            with self.subTest(path=path), self.assertRaises(hg_domain.SafeError):
                self.export(outbox=path)

    def test_custom_database_and_outbox_can_share_unchanged_parent(self):
        self.base.chmod(0o755)
        self.export(state_path=self.base / "garmin_health.sqlite3")
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.base / "garmin_health.sqlite3").stat().st_mode), 0o600)

    def test_existing_cloud_directory_permissions_are_preserved(self):
        self.outbox.mkdir(mode=0o755)
        self.base.chmod(0o755)
        self.export()
        self.assertEqual(stat.S_IMODE(self.outbox.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), 0o755)

    def test_corrupt_database_and_batch_sha_fail_without_cloud_changes(self):
        self.export()
        pending = (self.outbox / "pending.json").read_bytes()
        with contextlib.closing(sqlite3.connect(self.state)) as db:
            db.execute("UPDATE batch SET sha256=?", ("0" * 64,))
            db.commit()
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual((self.outbox / "pending.json").read_bytes(), pending)
        self.state.write_bytes(b"corrupt database PRIVATE")
        with self.assertRaises(hg_domain.SafeError) as raised:
            self.export()
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertEqual((self.outbox / "pending.json").read_bytes(), pending)

    def test_bounded_read_rejects_oversized_cloud_file(self):
        self.export()
        (self.outbox / "pending.json").write_bytes(b"x" * (hg_outbox.MAX_WIRE_BYTES + 1))
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(
            (self.outbox / "pending.json").stat().st_size, hg_outbox.MAX_WIRE_BYTES + 1
        )

    def test_oversized_batch_remains_local_until_smaller_batch_selected(self):
        self.api.responses[DAY.isoformat()] = envelope(
            measurement(sourceType="x" * 550000),
            measurement(version=1002, sourceType="x" * 550000),
        )
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(len(self.rows("measurements")), 2)
        self.assertEqual(self.rows("batch"), [])
        self.export(batch_size=1)
        self.assertEqual(len(self.pending()["records"]), 1)
        self.assertLess((self.outbox / "pending.json").stat().st_size, hg_outbox.MAX_WIRE_BYTES)

    def test_duplicate_json_keys_in_receipt_are_rejected(self):
        self.export()
        receipt = self.receipt()
        raw = json.dumps(receipt)
        raw = raw[:-1] + ', "payload_sha256": "' + receipt["payload_sha256"] + '"}'
        (self.outbox / "receipt.json").write_text(raw)
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual(self.rows("measurements")[0]["delivered_revision"], 0)

    def test_tampered_payload_and_raw_archive_are_not_exported(self):
        self.export()
        pending = (self.outbox / "pending.json").read_bytes()
        with contextlib.closing(sqlite3.connect(self.state)) as db:
            db.execute("UPDATE source_responses SET raw=?", (b'{"private":"PRIVATE"}',))
            db.commit()
        with self.assertRaises(hg_domain.SafeError):
            self.export()
        self.assertEqual((self.outbox / "pending.json").read_bytes(), pending)

    def test_concurrent_run_is_rejected_without_querying_or_changing_cloud(self):
        self.export()
        pending = (self.outbox / "pending.json").read_bytes()
        self.api.calls.clear()
        with mock.patch.object(hg_outbox.fcntl, "flock", side_effect=BlockingIOError):
            with self.assertRaises(hg_domain.SafeError):
                self.export()
        self.assertEqual(self.api.calls, [])
        self.assertEqual((self.outbox / "pending.json").read_bytes(), pending)

    def test_manual_recent_day_cannot_hide_unscanned_history(self):
        self.export()
        earlier = date(2026, 10, 1)
        self.assertEqual(hg_outbox.recommended_since(earlier, DAY, self.state), earlier)
        self.assertEqual(
            self.rows("scan_coverage"),
            [{"since_day": DAY.isoformat(), "until_day": DAY.isoformat()}],
        )

    def test_manual_disjoint_ranges_resume_at_first_gap(self):
        self.export(since=date(2026, 10, 1), until=date(2026, 10, 3))
        self.export(since=date(2026, 10, 8), until=date(2026, 10, 10))
        self.assertEqual(
            hg_outbox.recommended_since(date(2026, 10, 1), DAY, self.state), date(2026, 10, 4)
        )
        self.assertEqual(len(self.rows("scan_coverage")), 2)
        self.export(since=date(2026, 10, 4), until=date(2026, 10, 7))
        self.assertEqual(
            self.rows("scan_coverage"), [{"since_day": "2026-10-01", "until_day": "2026-10-10"}]
        )
        self.assertEqual(
            hg_outbox.recommended_since(date(2026, 10, 1), DAY, self.state), date(2026, 10, 9)
        )

    def test_moving_config_since_earlier_backfills_new_history(self):
        self.export(since=date(2026, 10, 8))
        self.assertEqual(
            hg_outbox.recommended_since(date(2026, 10, 8), DAY, self.state), date(2026, 10, 9)
        )
        self.assertEqual(
            hg_outbox.recommended_since(date(2026, 10, 1), DAY, self.state), date(2026, 10, 1)
        )

    def test_failed_range_does_not_fill_existing_coverage_gap(self):
        self.export(since=date(2026, 10, 1), until=date(2026, 10, 2))
        self.export(since=date(2026, 10, 8), until=date(2026, 10, 10))
        before = self.rows("scan_coverage")
        self.api.responses["2026-10-05"] = RuntimeError("offline")
        with self.assertRaises(hg_domain.SafeError):
            self.export(since=date(2026, 10, 3), until=date(2026, 10, 7))
        self.assertEqual(self.rows("scan_coverage"), before)
        self.assertEqual(
            hg_outbox.recommended_since(date(2026, 10, 1), DAY, self.state), date(2026, 10, 3)
        )

    def test_recommended_since_covers_gaps_and_overlaps_latest_week(self):
        start = date(2026, 10, 1)
        self.assertEqual(hg_outbox.recommended_since(start, DAY, self.state), start)
        self.export(since=start)
        self.assertEqual(hg_outbox.recommended_since(start, DAY, self.state), date(2026, 10, 9))
        self.assertEqual(
            hg_outbox.recommended_since(start, date(2026, 11, 1), self.state), date(2026, 10, 16)
        )
        self.assertEqual(hg_outbox.recommended_since(DAY, DAY, self.state), DAY)
        with self.assertRaises(hg_domain.SafeError):
            hg_outbox.recommended_since(start, date(2026, 10, 14), self.state)


if __name__ == "__main__":
    unittest.main()
