"""Background entry evidence using private fixtures; no real service or API."""

import argparse
import contextlib
import fcntl
import io
import json
import os
import plistlib
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from datetime import UTC as hg_std_UTC
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_diagnostics as fixtures
import test_run_attempts as run_fixtures
from project_fixtures import BOOTSTRAP_IMPORTS, copy_project
from test_reconciliation import API, row

from health_garmin_sync import background as r
from health_garmin_sync import cli as hg_cli
from health_garmin_sync import diagnostics as d
from health_garmin_sync import domain as hg_domain
from health_garmin_sync import file_io as hg_file_io
from health_garmin_sync import garmin as hg_garmin
from health_garmin_sync import launchagent as hg_launchagent
from health_garmin_sync import operations as hg_operations
from health_garmin_sync import processing as hg_processing
from health_garmin_sync import runtime as hg_runtime
from health_garmin_sync import sources as hg_sources
from health_garmin_sync import state as hg_state
from health_garmin_sync import sync as hg_sync


class AgentAttemptTests(unittest.TestCase):
    setUp = fixtures.DoctorTests.setUp
    save_config = fixtures.DoctorTests.save_config
    snapshot = fixtures.DoctorTests.snapshot
    invoke = fixtures.DoctorTests.invoke
    report = fixtures.DoctorTests.report

    @property
    def marker(self):
        return self.state / "last_agent_attempt.json"

    def completed(self):
        stamp = hg_runtime.now().isoformat()
        return {
            "outcome": "completed",
            "started_at": stamp,
            "finished_at": stamp,
            "stage": "run",
            "error_type": None,
        }

    def run_agent(self, script=True):
        output, errors = io.StringIO(), io.StringIO()
        old_umask = os.umask(0o077)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = r.entrypoint() if script else r.main()
        finally:
            os.umask(old_umask)
        self.assertNotIn(fixtures.SECRET, output.getvalue() + errors.getvalue())
        return result, errors.getvalue()

    def value(self):
        value = json.loads(self.marker.read_text())
        self.assertEqual(
            set(value), {"outcome", "started_at", "finished_at", "stage", "error_type"}
        )
        self.assertNotIn(fixtures.SECRET, json.dumps(value))
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        if value["finished_at"] is not None:
            self.assertGreaterEqual(
                hg_domain.aware(value["finished_at"]), hg_domain.aware(value["started_at"])
            )
        return value

    def test_setup_failure_replaces_old_success_and_doctor_reports_error(self):
        self.marker.write_text(json.dumps(self.completed()))
        old_last = self.last.read_bytes()
        old_run = self.state / "last_attempt.json"
        run_stamp = hg_runtime.now().isoformat()
        old_run.write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "started_at": run_stamp,
                    "finished_at": run_stamp,
                    "stage": "sync",
                    "error_type": None,
                }
            )
        )
        old_run_bytes = old_run.read_bytes()
        with (
            patch.object(r, "BoundedStream", side_effect=OSError(fixtures.SECRET)),
            patch.object(hg_cli, "main") as main,
        ):
            self.assertEqual(self.run_agent()[0], 2)
        main.assert_not_called()
        self.assertEqual(self.value()["outcome"], "failed")
        self.assertEqual(self.value()["stage"], "setup")
        self.assertEqual(self.last.read_bytes(), old_last)
        self.assertEqual(old_run.read_bytes(), old_run_bytes)
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks["last_agent_attempt"]["status"], "error")
        self.assertNotIn("error_type", checks["last_agent_attempt"]["data"])

    def test_stage_evidence_failure_does_not_call_main(self):
        original = hg_file_io.write_json

        def fail_stage(path, value):
            if path == self.marker and value["outcome"] == "running" and value["stage"] == "run":
                raise OSError(fixtures.SECRET)
            return original(path, value)

        with (
            patch.object(hg_file_io, "write_json", side_effect=fail_stage),
            patch.object(hg_cli, "main") as main,
        ):
            self.assertEqual(self.run_agent()[0], 2)
        main.assert_not_called()
        self.assertEqual(self.value()["outcome"], "failed")
        self.assertEqual(self.value()["stage"], "setup")
        self.assertEqual(self.report()[0], 2)

    def test_setup_interruption_keeps_primary_and_reports_setup_stage(self):
        primary = KeyboardInterrupt()
        with (
            patch.object(r, "BoundedStream", side_effect=primary),
            patch.object(hg_cli, "main") as main,
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.run_agent(script=False)
        self.assertIs(raised.exception, primary)
        main.assert_not_called()
        self.assertEqual((self.value()["outcome"], self.value()["stage"]), ("interrupted", "setup"))
        self.assertEqual(self.report()[0], 2)

    def test_completed_is_only_entry_completion(self):
        old_last = self.last.read_bytes()
        with patch.object(hg_cli, "main", return_value=0):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(self.value()["outcome"], "completed")
        self.assertEqual(self.value()["stage"], "run")
        self.assertEqual(self.last.read_bytes(), old_last)
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks["last_agent_attempt"]["status"], "info")
        self.assertIn("入口", checks["last_agent_attempt"]["message"])
        self.assertIn("不代表", checks["last_agent_attempt"]["message"])

    def test_disabled_preview_completes_entry_without_accessing_garmin(self):
        self.cfg["enabled"] = False
        self.save_config()
        old_last = self.last.read_bytes()
        with patch.object(hg_garmin, "api_client") as api:
            self.assertEqual(self.run_agent()[0], 0)
        api.assert_not_called()
        self.assertEqual(self.value()["outcome"], "completed")
        self.assertEqual(
            json.loads((self.state / "last_attempt.json").read_text())["outcome"], "preview"
        )
        self.assertEqual(self.last.read_bytes(), old_last)

    def test_ignored_native_unknown_request_survives_idle_summary_until_explicit_resolution(self):
        from test_sdk_transport import SDKTransportTests, fit_weight_timestamp

        sdk = SDKTransportTests()
        self.addCleanup(sdk.doCleanups)
        sdk.setUp()
        old = hg_sources.load_measurements(self.cfg)[1][0]
        source, token = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        process = hg_processing.process_record
        with (
            patch.object(hg_runtime, "BASE", self.base),
            patch.object(hg_runtime, "STATE", self.state),
            patch.object(hg_runtime, "CONFIG", self.config_path),
            patch.object(Path, "home", return_value=self.home),
        ):
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                patch.object(
                    hg_processing,
                    "process_record",
                    side_effect=lambda *a, **k: process(*a, **k, sleep=lambda _: None),
                ),
            ):
                self.assertEqual(self.run_agent()[0], 0)
            wire = fit_weight_timestamp(sdk.fit_files[-1])
            self.assertEqual(wire, old.at.astimezone(hg_std_UTC).replace(microsecond=0))
            with (
                patch("builtins.input", return_value="IGNORE"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_operations.resolve_record(
                    argparse.Namespace(record_id=old.key, present=False, absent=False, ignore=True)
                )
            with contextlib.closing(hg_state.database()) as db:
                ignored = dict(hg_state.state_row(db, old))
                self.assertEqual(ignored["status"], "ignored")
            for empty in (False, True):
                with self.subTest(empty_export=empty):
                    if empty:
                        value = json.loads(self.export.read_text())
                        value["metrics"] = {"weight": []}
                        self.export.write_text(json.dumps(value))
                    source = self.export.read_bytes()
                    with patch.object(
                        hg_garmin,
                        "api_client",
                        side_effect=AssertionError("Idle run must not authenticate"),
                    ):
                        self.assertEqual(self.run_agent()[0], 0)
                    summary = json.loads(self.last.read_text())
                    self.assertEqual(summary["results"], {})
                    self.assertEqual(summary["local_status_counts"], {"ignored": 1})
                    self.assertEqual(summary["unknown_request_count"], 1)
                    code, _, checks = self.report()
                    self.assertEqual(code, 2)
                    self.assertEqual(checks["last_run"]["status"], "error")
                    self.assertEqual(checks["last_run"]["data"]["unknown_request_count"], 1)
                    self.assertIn("不要重复提交", checks["last_run"]["hint"])
                    self.assertEqual(checks["last_agent_attempt"]["status"], "info")
                    code, text = self.invoke("doctor")
                    self.assertEqual(code, 2)
                    self.assertIn("请求结果未确定", text)
                    with contextlib.closing(hg_state.database()) as db:
                        self.assertEqual(dict(hg_state.state_row(db, old)), ignored)
                        self.assertEqual(hg_state.unknown_request_context(db)[0][2], wire)
                    self.assertEqual(self.export.read_bytes(), source)
                    self.assertEqual((self.tokens / "oauth.json").read_bytes(), token)
            # Explicit receipt/absence resolves requests; ignoring an unsent task does not create one.
            for choice in ("PRESENT", "ABSENT", "never_sent"):
                if choice == "PRESENT":
                    record = old
                else:
                    record = hg_domain.Measurement(
                        old.at + timedelta(days=1 if choice == "ABSENT" else 2),
                        old.weight,
                        old.fat,
                        old.source,
                    )
                    if choice == "ABSENT":
                        with contextlib.closing(hg_state.database()) as db:
                            self.assertEqual(
                                process(sdk.api, db, record, sleep=lambda _: None), "uncertain"
                            )
                    else:
                        with contextlib.closing(hg_state.database()) as db:
                            hg_state.set_status(db, record, "pending")
                    with (
                        patch("builtins.input", return_value="IGNORE"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_operations.resolve_record(
                            argparse.Namespace(
                                record_id=record.key, present=False, absent=False, ignore=True
                            )
                        )
                if choice != "never_sent":
                    with (
                        patch("builtins.input", return_value=choice),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_operations.resolve_record(
                            argparse.Namespace(
                                record_id=record.key,
                                present=choice == "PRESENT",
                                absent=choice == "ABSENT",
                                ignore=False,
                            )
                        )
                with patch.object(
                    hg_garmin,
                    "api_client",
                    side_effect=AssertionError("Empty export must not authenticate"),
                ):
                    self.assertEqual(self.run_agent()[0], 0)
                self.assertEqual(json.loads(self.last.read_text())["unknown_request_count"], 0)
                self.assertEqual(self.report()[0], 0)
                self.assertEqual(self.export.read_bytes(), source)
                self.assertEqual((self.tokens / "oauth.json").read_bytes(), token)
            self.assertEqual(sum(request["method"] == "POST" for request in sdk.sent), 2)
            self.assertFalse(any(request["method"] == "DELETE" for request in sdk.sent))

    def test_empty_export_without_database_records_zero_unknown_requests_without_creating_it(self):
        value = json.loads(self.export.read_text())
        value["metrics"] = {"weight": []}
        self.export.write_text(json.dumps(value))
        with (
            patch.object(
                hg_state, "database", side_effect=AssertionError("No database should be opened")
            ),
            patch.object(
                hg_garmin, "api_client", side_effect=AssertionError("No API should be created")
            ),
        ):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertFalse((self.state / "sync.sqlite3").exists())
        self.assertEqual(json.loads(self.last.read_text())["unknown_request_count"], 0)
        self.assertEqual(self.report()[0], 0)

    def test_unallowed_source_refresh_cannot_keep_ready_measurement_in_settling(self):
        import socket

        from test_reconciliation import ReconciliationTests

        def forbidden(*args, **kwargs):
            raise AssertionError("No real network or service")

        current = hg_runtime.now()
        original = json.loads(self.export.read_text())
        original["exported_at"] = (current - timedelta(minutes=10)).isoformat()
        self.export.write_text(json.dumps(original))
        source_bytes, token = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        other = self.inbox / "other.json"
        backend = API()
        api = ReconciliationTests.detailed_api(self, backend)
        with (
            patch.object(socket.socket, "connect", forbidden),
            patch.object(socket, "create_connection", forbidden),
            patch.object(hg_launchagent, "manage_agent", forbidden),
        ):
            for minute in (0, 30, 60):
                with self.subTest(minute=minute):
                    reference = current + timedelta(minutes=minute)
                    other.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "metric": "weight",
                                "exported_at": reference.isoformat(),
                                "samples": [
                                    {
                                        "at": reference.isoformat(),
                                        "value": "80 kg",
                                        "source": "OTHER",
                                    }
                                ],
                            }
                        )
                    )
                    other_bytes = other.read_bytes()
                    with (
                        patch.object(hg_runtime, "now", return_value=reference),
                        patch.object(hg_garmin, "api_client", return_value=api) as factory,
                    ):
                        self.assertEqual(self.run_agent()[0], 0)
                        self.assertEqual(self.report()[0], 0)
                    self.assertEqual(factory.call_count, 1 if minute == 0 else 0)
                    self.assertEqual(len(backend.posts), 1)
                    self.assertFalse(backend.deletes)
                    summary = json.loads(self.last.read_text())
                    self.assertNotIn("deferred", summary)
                    self.assertEqual(
                        hg_domain.aware(summary["exports"]["weight"]),
                        hg_domain.aware(original["exported_at"]),
                    )
                    self.assertEqual(
                        list(summary["results"].values()), ["verified"] if minute == 0 else []
                    )
                    self.assertEqual(summary["local_status_counts"], {"verified": 1})
                    self.assertEqual(self.value()["outcome"], "completed")
                    self.assertEqual(self.export.read_bytes(), source_bytes)
                    self.assertEqual(other.read_bytes(), other_bytes)
                    self.assertEqual((self.tokens / "oauth.json").read_bytes(), token)
            # An allowed new core export still starts its own normal settling window.
            reference = current + timedelta(minutes=65)
            latest = json.loads(source_bytes)
            latest["exported_at"] = reference.isoformat()
            at = (
                hg_domain.aware(latest["metrics"]["weight"][0]["at"]) + timedelta(days=1)
            ).isoformat()
            allowed = self.cfg["allowed_sources"][0]
            latest["metrics"]["weight"].append({"at": at, "value": "74 kg", "source": allowed})
            latest["metrics"]["body_fat"].append({"at": at, "value": "27%", "source": allowed})
            self.export.write_text(json.dumps(latest))
            source_bytes = self.export.read_bytes()
            with (
                patch.object(hg_runtime, "now", return_value=reference),
                patch.object(hg_garmin, "api_client", return_value=api),
            ):
                self.assertEqual(self.run_agent()[0], 0)
            summary = json.loads(self.last.read_text())
            self.assertEqual(summary["deferred"], "settling")
            self.assertIn("deferred", summary["results"].values())
            self.assertEqual(hg_domain.aware(summary["exports"]["weight"]), reference)
            self.assertEqual(len(backend.posts), 1)
            self.assertFalse(backend.deletes)
            self.assertEqual(self.export.read_bytes(), source_bytes)
            self.assertEqual((self.tokens / "oauth.json").read_bytes(), token)

    def test_equivalent_time_offsets_preserve_recent_rechecks_and_history_lbm_evidence(self):
        import socket

        from test_reconciliation import ReconciliationTests

        def forbidden(*args, **kwargs):
            raise AssertionError("No real network or service")

        reference = hg_domain.aware("2026-10-02T12:00:00+08:00")
        local_date = reference.astimezone().date()
        template = json.loads(self.export.read_text())
        token = (self.tokens / "oauth.json").read_bytes()
        with (
            patch.object(socket.socket, "connect", forbidden),
            patch.object(socket, "create_connection", forbidden),
            patch.object(hg_launchagent, "manage_agent", forbidden),
            patch.object(hg_runtime, "now", return_value=reference),
        ):
            for days_before_cutoff in (0, 1):
                with self.subTest(days_before_cutoff=days_before_cutoff):
                    with contextlib.closing(hg_state.database()) as db:
                        db.execute("DELETE FROM uploads")
                        db.commit()
                    (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                    at = (
                        datetime.combine(
                            local_date - timedelta(days=30 + days_before_cutoff),
                            datetime.min.time(),
                        )
                        .replace(minute=30)
                        .astimezone()
                    )
                    initial = json.loads(json.dumps(template))
                    initial["exported_at"] = (reference - timedelta(minutes=5)).isoformat()
                    for samples in initial["metrics"].values():
                        for item in samples:
                            item["at"] = at.isoformat()
                    self.export.write_text(json.dumps(initial))
                    old = hg_sources.load_measurements(self.cfg)[1][0]
                    backend = API()
                    api = ReconciliationTests.detailed_api(self, backend)
                    with (
                        patch.object(hg_runtime, "now", return_value=reference),
                        patch.object(hg_garmin, "api_client", return_value=api),
                    ):
                        self.assertEqual(self.run_agent()[0], 0)
                    with contextlib.closing(hg_state.database()) as db:
                        previous = dict(hg_state.state_row(db, old))
                    self.assertEqual(previous["status"], "verified")
                    self.assertEqual(len(backend.posts), 1)
                    alternate = next(
                        at.astimezone(timezone(timedelta(hours=offset)))
                        for offset in (-12, 14)
                        if at.astimezone(timezone(timedelta(hours=offset))).date() != at.date()
                    )
                    self.assertNotEqual(alternate.date(), at.date())
                    changed = json.loads(json.dumps(initial))
                    for samples in changed["metrics"].values():
                        for item in samples:
                            item["at"] = alternate.isoformat()
                    if days_before_cutoff:
                        changed["metrics"]["lbm"] = [
                            {"at": alternate.isoformat(), "value": "53.55 kg", "source": old.source}
                        ]
                    self.export.write_text(json.dumps(changed))
                    source = self.export.read_bytes()
                    current = hg_sources.load_measurements(self.cfg)[1][0]
                    self.assertEqual((current.key, current.core), (old.key, old.core))
                    backend.rows = []
                    with (
                        patch.object(
                            hg_runtime, "now", return_value=reference + timedelta(seconds=10)
                        ),
                        patch.object(hg_garmin, "api_client", return_value=api) as factory,
                    ):
                        self.assertEqual(self.run_agent()[0], 0)
                        code, report, checks = self.report()
                    with contextlib.closing(hg_state.database()) as db:
                        latest = dict(hg_state.state_row(db, current))
                    self.assertEqual(factory.call_count, 0 if days_before_cutoff else 1)
                    self.assertEqual(len(backend.posts), 1)
                    self.assertFalse(backend.deletes)
                    summary = json.loads(self.last.read_text())
                    if days_before_cutoff:
                        self.assertEqual(summary["results"], {})
                        self.assertEqual(report["status"], "ok")
                        self.assertEqual(
                            {
                                key: latest[key]
                                for key in ("core", "status", "updated_at", "detail")
                            },
                            {
                                key: previous[key]
                                for key in ("core", "status", "updated_at", "detail")
                            },
                        )
                        self.assertEqual(
                            json.loads(latest["payload"]),
                            {**json.loads(previous["payload"]), "lbm": 53.55},
                        )
                    else:
                        self.assertEqual(latest["status"], "remote_missing")
                        self.assertEqual(summary["results"], {current.key: "remote_missing"})
                        self.assertEqual(checks["last_run"]["status"], "warn")
                    self.assertEqual(code, 0)
                    self.assertEqual(summary["unknown_request_count"], 0)
                    self.assertEqual(self.export.read_bytes(), source)
                    self.assertEqual((self.tokens / "oauth.json").read_bytes(), token)

    def test_sync_log_rollover_failure_finishes_batch_but_reports_failed_evidence(self):
        first = hg_sources.load_measurements(self.cfg)[1][0]
        second = hg_domain.Measurement(first.at + timedelta(hours=1), 74, 27, first.source)
        data = json.loads(self.export.read_text())
        for metric, value in (("weight", second.weight), ("body_fat", f"{second.fat}%")):
            data["metrics"][metric].append(
                {"at": second.at.isoformat(), "value": value, "source": second.source}
            )
        self.export.write_text(json.dumps(data))
        source, tokens = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        log = self.state / "sync.log"
        log.write_bytes(b"x" * 1_000_000)
        (self.state / "sync.log.2").write_text("Retained older log")
        (self.state / "sync.log.3").mkdir()
        api = API([row(first)])
        original_write = hg_file_io.write_json
        terminal = []

        def write(path, value):
            if path == self.state / "last_attempt.json" and value["outcome"] == "failed":
                with (
                    (self.state / "run.lock").open("a") as probe,
                    self.assertRaises(BlockingIOError),
                ):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                terminal.append(value["outcome"])
            return original_write(path, value)

        with (
            run_fixtures.isolated_logging(),
            patch.object(hg_garmin, "api_client", return_value=api),
            patch.object(hg_file_io, "write_json", side_effect=write),
        ):
            self.assertEqual(self.run_agent()[0], 2)
        self.assertEqual(terminal, ["failed"])
        self.assertEqual(self.value()["outcome"], "failed")
        self.assertEqual(
            json.loads((self.state / "last_attempt.json").read_text())["outcome"], "failed"
        )
        self.assertEqual(log.stat().st_size, 1_000_000)
        self.assertEqual(len(api.posts), 1)
        self.assertFalse(api.deletes)
        with contextlib.closing(hg_state.database()) as db:
            self.assertEqual(hg_state.get_status(db, first), "verified")
            self.assertEqual(hg_state.get_status(db, second), "verified")
        summary = json.loads(self.last.read_text())
        self.assertEqual(summary["results"], {first.key: "verified", second.key: "verified"})
        self.assertEqual(summary["local_status_counts"], {"verified": 2})
        error_log = (self.state / "agent.err.log").read_text()
        self.assertEqual(error_log.count("同步日志未保存："), 1)
        self.assertNotIn("Traceback", error_log)
        self.assertNotIn("Arguments:", error_log)
        self.assertNotIn(fixtures.SECRET, error_log)
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks["last_attempt"]["status"], "error")
        self.assertEqual(checks["last_agent_attempt"]["status"], "error")
        self.assertEqual(checks["last_run"]["status"], "info")
        self.assertEqual(
            (self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()), (source, tokens)
        )

    def test_sync_log_failure_does_not_retry_an_uncertain_upload_and_recovers(self):
        measurement = hg_sources.load_measurements(self.cfg)[1][0]
        source, tokens = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        (self.state / "sync.log").write_bytes(b"x" * 1_000_000)
        (self.state / "sync.log.2").write_text("Retained older log")
        obstacle = self.state / "sync.log.3"
        obstacle.mkdir()
        api = API()
        api.visible = False
        native_process = hg_processing.process_record

        def process(*args, **kwargs):
            return native_process(*args, **kwargs, sleep=lambda _: None)

        for _ in range(2):
            with (
                run_fixtures.isolated_logging(),
                patch.object(hg_garmin, "api_client", return_value=api),
                patch.object(hg_processing, "process_record", side_effect=process),
            ):
                self.assertEqual(self.run_agent()[0], 2)
            self.assertEqual(len(api.posts), 1)
            self.assertFalse(api.deletes)
            self.assertEqual(self.value()["outcome"], "failed")
            with contextlib.closing(hg_state.database()) as db:
                self.assertEqual(hg_state.get_status(db, measurement), "uncertain")
            self.assertEqual(
                json.loads(self.last.read_text())["local_status_counts"], {"uncertain": 1}
            )
            self.assertEqual(self.report()[0], 2)
        obstacle.rmdir()
        api.rows.append(row(measurement))
        with (
            run_fixtures.isolated_logging(),
            patch.object(hg_garmin, "api_client", return_value=api),
        ):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(len(api.posts), 1)
        self.assertFalse(api.deletes)
        self.assertEqual(self.value()["outcome"], "completed")
        with contextlib.closing(hg_state.database()) as db:
            self.assertEqual(hg_state.get_status(db, measurement), "verified")
        self.assertEqual(self.report()[0], 0)
        self.assertEqual(
            (self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()), (source, tokens)
        )

    def test_native_log_close_failure_keeps_uploaded_state_and_recovers_without_reposting(self):
        measurement = hg_sources.load_measurements(self.cfg)[1][0]
        source, tokens = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        api = API()
        native_write = hg_file_io.write_json
        terminal = []

        def write(path, value):
            if path == self.state / "last_attempt.json" and value["outcome"] == "failed":
                self.assertEqual(observed[0]["close_calls"], 1)
                self.assertIsNone(observed[0]["handler"].stream)
                with (
                    (self.state / "run.lock").open("a") as probe,
                    self.assertRaises(BlockingIOError),
                ):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                terminal.append(value["outcome"])
            return native_write(path, value)

        with (
            run_fixtures.isolated_logging(),
            run_fixtures.failing_log_close(self) as observed,
            patch.object(hg_garmin, "api_client", return_value=api),
            patch.object(hg_file_io, "write_json", side_effect=write),
        ):
            self.assertEqual(self.run_agent()[0], 2)
        self.assertEqual(terminal, ["failed"])
        self.assertEqual(observed[0]["close_calls"], 1)
        self.assertEqual(self.value()["outcome"], "failed")
        self.assertEqual(
            json.loads((self.state / "last_attempt.json").read_text())["outcome"], "failed"
        )
        self.assertEqual(len(api.posts), 1)
        self.assertFalse(api.deletes)
        with contextlib.closing(hg_state.database()) as db:
            self.assertEqual(hg_state.get_status(db, measurement), "verified")
        summary = json.loads(self.last.read_text())
        self.assertEqual(summary["results"], {measurement.key: "verified"})
        self.assertEqual(summary["local_status_counts"], {"verified": 1})
        errors = (self.state / "agent.err.log").read_text()
        self.assertNotIn(fixtures.SECRET, errors)
        self.assertNotIn("Traceback", errors)
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks["last_attempt"]["status"], "error")
        self.assertEqual(checks["last_agent_attempt"]["status"], "error")
        self.assertEqual(checks["last_run"]["status"], "info")
        with (
            run_fixtures.isolated_logging(),
            patch.object(hg_garmin, "api_client", return_value=api) as login,
        ):
            self.assertEqual(self.run_agent()[0], 0)
        login.assert_not_called()
        self.assertEqual(len(api.posts), 1)
        self.assertFalse(api.deletes)
        self.assertEqual(self.value()["outcome"], "completed")
        self.assertEqual(self.report()[0], 0)
        self.assertEqual(
            (self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()), (source, tokens)
        )

    def test_post_interruption_and_next_read_preserve_inflight_without_repeating_post(self):
        class InterruptedAPI(API):
            def add_body_composition(self, **kwargs):
                super().add_body_composition(**kwargs)
                raise KeyboardInterrupt()

        api = InterruptedAPI()
        api.visible = False
        with patch.object(hg_garmin, "api_client", return_value=api):
            self.assertEqual(self.run_agent()[0], 130)
        self.assertEqual(self.value()["outcome"], "interrupted")
        self.assertEqual(
            json.loads((self.state / "last_attempt.json").read_text())["outcome"], "interrupted"
        )
        with contextlib.closing(hg_state.database()) as db:
            self.assertEqual(db.execute("SELECT status FROM uploads").fetchone()[0], "inflight")
        self.assertEqual(len(api.posts), 1)
        with patch.object(hg_garmin, "api_client", return_value=api):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(self.value()["outcome"], "completed")
        self.assertEqual(len(api.posts), 1)
        self.assertFalse(api.deletes)
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks["last_agent_attempt"]["status"], "info")
        self.assertEqual(checks["last_run"]["status"], "error")
        self.assertEqual(checks["last_run"]["data"]["results"], {"uncertain": 1})

    def test_missing_legacy_marker_warns_only_when_enabled(self):
        self.marker.unlink(missing_ok=True)
        result, report, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "warning")
        self.assertEqual(checks["last_agent_attempt"]["status"], "warn")
        self.cfg["enabled"] = False
        self.save_config()
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks["last_agent_attempt"]["status"], "info")

    def test_busy_log_lock_preserves_entry_and_run_evidence(self):
        self.marker.write_text(json.dumps(self.completed()))
        prior_entry, prior_last = self.marker.read_bytes(), self.last.read_bytes()
        with (self.state / "agent-log.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(hg_cli, "main") as main:
                self.assertEqual(self.run_agent()[0], 0)
        main.assert_not_called()
        self.assertEqual(self.marker.read_bytes(), prior_entry)
        self.assertEqual(self.last.read_bytes(), prior_last)

    def test_busy_run_lock_completes_entry_without_overwriting_run_evidence(self):
        old_run = self.state / "last_attempt.json"
        old_run.write_text("synthetic prior protected bytes")
        prior_last = self.last.read_bytes()
        with (self.state / "run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch.object(hg_garmin, "api_client") as api,
                patch.object(hg_sync, "run_sync") as sync,
            ):
                self.assertEqual(self.run_agent()[0], 0)
        api.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(self.value()["outcome"], "completed")
        self.assertEqual(old_run.read_text(), "synthetic prior protected bytes")
        self.assertEqual(self.last.read_bytes(), prior_last)
        self.assertIn("另一个进程", (self.state / "agent.out.log").read_text())

    def test_start_evidence_failure_does_not_call_main_and_preserves_old_marker(self):
        before = self.marker.read_bytes()
        original = hg_file_io.write_json

        def fail_start(path, value):
            if path == self.marker:
                raise OSError(fixtures.SECRET)
            return original(path, value)

        with (
            patch.object(hg_file_io, "write_json", side_effect=fail_start),
            patch.object(hg_cli, "main") as main,
        ):
            code, errors = self.run_agent()
        self.assertEqual(code, 2)
        self.assertIn("开始证据未保存：OSError", errors)
        main.assert_not_called()
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertFalse((self.state / "agent.out.log").exists())

    def test_start_and_stage_are_persisted_before_their_protected_work(self):
        original = r.BoundedStream
        observed = []

        def stream(*args, **kwargs):
            observed.append(self.value()["stage"])
            return original(*args, **kwargs)

        def command():
            value = self.value()
            self.assertEqual(value["outcome"], "running")
            self.assertEqual(value["stage"], "run")
            self.assertIsNone(value["finished_at"])
            return 0

        with (
            patch.object(r, "BoundedStream", side_effect=stream),
            patch.object(hg_cli, "main", side_effect=command),
        ):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(observed, ["setup", "setup"])

    def test_stdio_failures_are_evidenced_without_changing_primary_exit_codes(self):
        for failure in ("write", "flush", "close"):
            for code in (0, 2, 130):
                with self.subTest(failure=failure, code=code):
                    output_fd = None
                    original_close = os.close
                    failed_close = False

                    def command():
                        nonlocal output_fd
                        output_fd = sys.stdout._fd
                        if failure == "write":
                            with patch.object(r, "write_all", side_effect=OSError(fixtures.SECRET)):
                                print("synthetic output")
                        elif failure == "flush":
                            original_stat = os.fstat

                            def bad_stat(fd):
                                if fd == output_fd:
                                    raise OSError(fixtures.SECRET)
                                return original_stat(fd)

                            with patch.object(r.os, "fstat", side_effect=bad_stat):
                                sys.stdout.flush()
                        return code

                    def close(fd):
                        nonlocal failed_close
                        original_close(fd)
                        if failure == "close" and fd == output_fd and not failed_close:
                            failed_close = True
                            raise OSError(fixtures.SECRET)

                    with (
                        patch.object(hg_cli, "main", side_effect=command),
                        patch.object(r.os, "close", side_effect=close),
                    ):
                        result, _ = self.run_agent()
                    self.assertEqual(result, 2 if code == 0 else code)
                    self.assertEqual(
                        self.value()["outcome"], "interrupted" if code == 130 else "failed"
                    )
                    self.assertEqual(self.value()["stage"], "run")
                    self.assertEqual(self.report()[0], 2)
                    self.assertNotIn(fixtures.SECRET, (self.state / "agent.err.log").read_text())

    def test_primary_exceptions_are_preserved_and_terminal_errors_do_not_replace_them(self):
        for primary in (RuntimeError(fixtures.SECRET), KeyboardInterrupt()):
            for terminal_failure in (False, True):
                with self.subTest(
                    primary=type(primary).__name__, terminal_failure=terminal_failure
                ):
                    original_write = hg_file_io.write_json

                    def write(path, value):
                        if (
                            terminal_failure
                            and path == self.marker
                            and value["outcome"] != "running"
                        ):
                            raise OSError(fixtures.SECRET)
                        return original_write(path, value)

                    with (
                        patch.object(hg_file_io, "write_json", side_effect=write),
                        patch.object(hg_cli, "main", side_effect=primary),
                        self.assertRaises(type(primary)) as raised,
                    ):
                        self.run_agent(script=False)
                    self.assertIs(raised.exception, primary)
                    marker = self.value()
                    if terminal_failure:
                        self.assertEqual(marker["outcome"], "running")
                        self.assertEqual(self.report()[2]["last_agent_attempt"]["status"], "warn")
                    else:
                        self.assertEqual(
                            marker["outcome"],
                            "interrupted" if isinstance(primary, KeyboardInterrupt) else "failed",
                        )
                        self.assertEqual(marker["error_type"], type(primary).__name__)
                        self.assertEqual(self.report()[0], 2)

    def test_terminal_write_failure_changes_success_only_and_keeps_unfinished_marker(self):
        for result in (0, 2, 130):
            with self.subTest(result=result):
                original = hg_file_io.write_json

                def write(path, value):
                    if path == self.marker and value["outcome"] != "running":
                        raise OSError(fixtures.SECRET)
                    return original(path, value)

                with (
                    patch.object(hg_file_io, "write_json", side_effect=write),
                    patch.object(hg_cli, "main", return_value=result),
                ):
                    code, errors = self.run_agent()
                self.assertEqual(code, 2 if result == 0 else result)
                self.assertIn("结束证据未保存：OSError", errors)
                self.assertEqual(self.value()["outcome"], "running")
                self.assertEqual(self.value()["stage"], "run")

    def test_terminal_write_occurs_with_log_lock_held_and_output_fds_closed(self):
        owned = []
        original_stream, original_write = r.BoundedStream, hg_file_io.write_json
        terminals = []

        def stream(*args, **kwargs):
            result = original_stream(*args, **kwargs)
            owned.append(result)
            return result

        def write(path, value):
            if path == self.marker and value["outcome"] != "running":
                self.assertTrue(all(stream.closed and stream._fd is None for stream in owned))
                with (self.state / "agent-log.lock").open("a") as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                terminals.append(value["outcome"])
            return original_write(path, value)

        with (
            patch.object(r, "BoundedStream", side_effect=stream),
            patch.object(hg_file_io, "write_json", side_effect=write),
            patch.object(hg_cli, "main", return_value=0),
        ):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(terminals, ["completed"])
        with (self.state / "agent-log.lock").open("a") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_last_lock_close_failure_does_not_write_after_release(self):
        original_open, original_close = r.regular_fd, os.close
        lock_fd = None
        writes = []
        released = False
        original_write = hg_file_io.write_json

        def open_fd(path, *args, **kwargs):
            nonlocal lock_fd
            fd = original_open(path, *args, **kwargs)
            if Path(path).name == "agent-log.lock":
                lock_fd = fd
            return fd

        def close(fd):
            nonlocal released
            original_close(fd)
            if fd == lock_fd:
                released = True
                raise OSError(fixtures.SECRET)

        def write(path, value):
            self.assertFalse(released)
            writes.append(value["outcome"])
            return original_write(path, value)

        with (
            patch.object(r, "regular_fd", side_effect=open_fd),
            patch.object(r.os, "close", side_effect=close),
            patch.object(hg_file_io, "write_json", side_effect=write),
            patch.object(hg_cli, "main", return_value=0),
        ):
            result, errors = self.run_agent()
        self.assertEqual(result, 2)
        self.assertIn("OSError", errors)
        self.assertEqual(writes, ["running", "running", "completed"])
        self.assertEqual(self.value()["outcome"], "completed")

    def test_running_marker_only_warns_without_asserting_process_liveness(self):
        value = dict(self.completed(), outcome="running", stage="setup", finished_at=None)
        self.marker.write_text(json.dumps(value))
        result, report, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "warning")
        check = checks["last_agent_attempt"]
        self.assertEqual(check["status"], "warn")
        self.assertIn("可能仍在运行或曾被中断", check["message"])

    def test_malformed_marker_fields_times_and_combinations_are_controlled_errors(self):
        base = self.completed()
        earlier = (hg_domain.aware(base["started_at"]) - timedelta(seconds=1)).isoformat()
        cases = [
            dict(base, extra=fixtures.SECRET),
            {key: value for key, value in base.items() if key != "stage"},
            dict(base, stage="sync"),
            dict(base, stage="config"),
            dict(base, stage="setup"),
            dict(base, stage=[]),
            dict(base, outcome="preview"),
            dict(base, outcome=fixtures.SECRET),
            dict(base, started_at=fixtures.SECRET),
            dict(base, started_at="2026-10-02T08:00:00"),
            dict(base, started_at=None),
            dict(base, finished_at=None),
            dict(base, finished_at=earlier),
            dict(base, error_type=fixtures.SECRET),
            dict(base, outcome="running"),
            dict(base, outcome="running", finished_at=None, error_type="OSError"),
            dict(base, outcome="failed", error_type=None),
            dict(base, outcome="failed", error_type=[]),
            dict(base, outcome="failed", error_type="OSError HTTP 500"),
            dict(base, outcome="failed", error_type="private token"),
            [],
            None,
        ]
        for value in cases:
            with self.subTest(value=value):
                self.marker.write_text(json.dumps(value))
                result, _, checks = self.report()
                self.assertEqual(result, 2)
                self.assertEqual(checks["last_agent_attempt"]["status"], "error")
                self.assertNotIn(fixtures.SECRET, json.dumps(checks["last_agent_attempt"]))

    def test_real_entry_clock_rollback_is_diagnosed_without_changing_outcomes_and_recovers(self):
        import socket

        def forbidden(*args, **kwargs):
            raise AssertionError("No real network or service")

        source, token = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        clock = [hg_runtime.now()]
        backend = API()
        post = backend.add_body_composition

        def rewind_post(**kwargs):
            clock[0] -= timedelta(seconds=1)
            return post(**kwargs)

        backend.add_body_composition = rewind_post
        with (
            patch.object(hg_runtime, "now", side_effect=lambda: clock[0]),
            patch.object(hg_garmin, "api_client", return_value=backend),
            patch.object(hg_launchagent, "manage_agent", forbidden),
            patch.object(socket.socket, "connect", forbidden),
            patch.object(socket, "create_connection", forbidden),
        ):
            for outcome, exception, exit_code in (
                ("completed", None, 0),
                ("failed", ValueError, 2),
                ("interrupted", KeyboardInterrupt, 130),
            ):
                with self.subTest(outcome=outcome):

                    def fail_sync(*args, **kwargs):
                        clock[0] -= timedelta(seconds=1)
                        raise exception()

                    with contextlib.ExitStack() as stack:
                        if exception:
                            stack.enter_context(
                                patch.object(hg_sync, "run_sync", side_effect=fail_sync)
                            )
                        self.assertEqual(self.run_agent()[0], exit_code)
                    evidence = {}
                    for name in ("last_attempt.json", "last_agent_attempt.json"):
                        evidence[name] = (self.state / name).read_bytes()
                        value = json.loads(evidence[name])
                        self.assertEqual(value["outcome"], outcome)
                        self.assertEqual(
                            (
                                hg_domain.aware(value["finished_at"])
                                - hg_domain.aware(value["started_at"])
                            ).total_seconds(),
                            -1,
                        )
                    result, _, checks = self.report()
                    self.assertEqual(result, 2)
                    for key in ("last_attempt", "last_agent_attempt"):
                        self.assertEqual(checks[key]["status"], "error")
                        self.assertIn("结束时间早于开始时间", checks[key]["message"])
                        self.assertIn("核对 Mac 时间", checks[key]["hint"])
                        self.assertEqual(
                            (self.state / (key + ".json")).read_bytes(), evidence[key + ".json"]
                        )
                    result, text = self.invoke("doctor")
                    self.assertEqual(result, 2)
                    self.assertIn("核对 Mac 时间", text)
                    for name in ("run.lock", "agent-log.lock"):
                        with (self.state / name).open("r") as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(self.run_agent()[0], 0)
                    self.assertEqual(self.report()[0], 0)
                    self.assertEqual(len(backend.posts), 1)
                    self.assertFalse(backend.deletes)
                    self.assertEqual(
                        (self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()),
                        (source, token),
                    )

    def test_clock_order_diagnosis_requires_other_attempt_fields_to_be_valid(self):
        base = self.completed()
        base["finished_at"] = (
            hg_domain.aware(base["started_at"]) - timedelta(seconds=1)
        ).isoformat()
        for key, stage in (("last_attempt", "sync"), ("last_agent_attempt", "run")):
            for changes in (
                {"extra": fixtures.SECRET},
                {"stage": "setup"},
                {"error_type": fixtures.SECRET},
                {"outcome": "failed", "error_type": None},
                {"outcome": "interrupted", "error_type": []},
            ):
                with self.subTest(key=key, changes=changes):
                    value = {**base, "stage": stage, **changes}
                    (self.state / (key + ".json")).write_text(json.dumps(value))
                    result, _, checks = self.report()
                    self.assertEqual(result, 2)
                    self.assertIn("结构无效", checks[key]["message"])
                    self.assertNotIn("Mac 时间", checks[key]["hint"])

    def test_corrupt_symlink_or_unreadable_marker_is_error_without_leaking_contents(self):
        self.marker.write_text("{invalid " + fixtures.SECRET)
        self.assertEqual(self.report()[0], 2)
        self.marker.unlink()
        outside = self.root / "outside-marker.json"
        outside.write_text(json.dumps(self.completed()))
        self.marker.symlink_to(outside)
        self.assertEqual(self.report()[0], 2)
        self.marker.unlink()
        self.marker.write_text(json.dumps(self.completed()))
        original = hg_file_io.read_regular_text

        def read(path, *args, **kwargs):
            if path == self.marker:
                raise PermissionError(fixtures.SECRET)
            return original(path, *args, **kwargs)

        # invoke snapshots files before/after; collect is the read-only check under test here.
        before = self.snapshot()
        with patch.object(hg_file_io, "read_regular_text", read):
            report = d.collect()
        self.assertEqual(report["status"], "error")
        self.assertNotIn(fixtures.SECRET, json.dumps(report))
        self.assertEqual(self.snapshot(), before)

    def test_agent_parameters_do_not_relax_existing_run_attempt_validation(self):
        path = self.state / "last_attempt.json"
        for stage in ("setup", "run"):
            path.write_text(json.dumps(dict(self.completed(), stage=stage)))
            self.assertEqual(self.report()[2]["last_attempt"]["status"], "error")
        path.write_text(json.dumps(dict(self.completed(), stage="config", outcome="preview")))
        self.assertEqual(self.report()[2]["last_attempt"]["status"], "error")

    def install_temporary_script(self, override):
        copy_project(self.base, BOOTSTRAP_IMPORTS + override)

    def test_generated_plist_runs_enabled_sync_with_minimal_environment_and_spaces(self):
        previous_base = self.base
        self.base = self.root / "project with spaces"
        previous_base.rename(self.base)
        self.state = self.base / "state"
        self.config_path = self.base / "config.json"
        self.entry = self.base / "health_garmin_sync" / "background.py"
        self.python = self.base / ".venv/bin/python"
        self.last = self.state / "last_run.json"
        self.tokens = self.state / "tokens"
        for name, value in (
            ("BASE", self.base),
            ("STATE", self.state),
            ("CONFIG", self.config_path),
        ):
            self.enterContext(patch.object(hg_runtime, name, value))
        self.python.unlink()
        self.python.symlink_to(sys.executable)
        self.cfg.update(enabled=True, settle_seconds=0)
        self.save_config()
        source_bytes = self.export.read_bytes()
        token_bytes = (self.tokens / "oauth.json").read_bytes()
        server = self.base / "synthetic-server.json"
        server.write_text(json.dumps({"posts": 0, "reads": 0, "rows": []}))
        self.install_temporary_script("""
# Keep the actual entry, sync, comparison and SQLite paths; replace only API I/O.
import builtins
import socket
assert Path.cwd() == hg_runtime.BASE
assert not {'PATH', 'PYTHONPATH', 'VIRTUAL_ENV'} & os.environ.keys()
def forbidden(*args, **kwargs):
    raise AssertionError("real network, home, credentials or service access forbidden")
Path.home = classmethod(lambda cls: forbidden())
socket.create_connection = socket.getaddrinfo = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
subprocess.run = subprocess.Popen = forbidden
builtins.input = getpass.getpass = forbidden
original_import = builtins.__import__
def offline_import(name, *args, **kwargs):
    if name.split('.')[0] in ('garminconnect', 'requests', 'urllib3', 'curl_cffi', 'garth'):
        return forbidden()
    return original_import(name, *args, **kwargs)
builtins.__import__ = offline_import
original_read_text = Path.read_text
def read_without_tokens(path, *args, **kwargs):
    if hg_runtime.STATE / 'tokens' in path.parents:
        return forbidden()
    return original_read_text(path, *args, **kwargs)
Path.read_text = read_without_tokens
class LocalServerAPI:
    path = hg_runtime.BASE / 'synthetic-server.json'
    def get_body_composition(self, start, end):
        value = json.loads(self.path.read_text())
        value['reads'] += 1
        hg_file_io.write_json(self.path, value)
        return {'dateWeightList': [row for row in value['rows'] if start <= row['calendarDate'] <= end]}
    def add_body_composition(self, **kwargs):
        value = json.loads(self.path.read_text())
        value['posts'] += 1
        at = hg_domain.aware(kwargs['timestamp'])
        value['rows'].append({'samplePk': 100, 'calendarDate': at.date().isoformat(),
                             'timestampGMT': int(at.timestamp() * 1000),
                             'weight': kwargs['weight'] * 1000,
                             'bodyFat': kwargs['percent_fat'], 'bmi': kwargs.get('bmi')})
        hg_file_io.write_json(self.path, value)
        return {'detailedImportResult': {'successes': [{}], 'failures': []}}
    add_weigh_in = delete_weigh_in = forbidden
def api_client(interactive=False):
    assert not interactive
    return LocalServerAPI()
hg_garmin.api_client = api_client
""")
        with contextlib.redirect_stdout(io.StringIO()):
            hg_launchagent.manage_agent(True, self.cfg)
        document = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(
            [call.args[0][1] for call in self.service.call_args_list],
            ["print", "bootout", "bootstrap"],
        )
        self.assertEqual(
            document["ProgramArguments"], [str(self.python), "-m", "health_garmin_sync.background"]
        )
        self.assertEqual(document["WorkingDirectory"], str(self.base))
        self.assertEqual(
            document["EnvironmentVariables"], {"HOME": str(self.home), "PYTHONUNBUFFERED": "1"}
        )
        self.assertNotEqual(Path.cwd(), self.base)
        children = []
        try:
            for run in (1, 2):
                with self.subTest(run=run):
                    child = subprocess.Popen(
                        document["ProgramArguments"],
                        cwd=document["WorkingDirectory"],
                        env=document["EnvironmentVariables"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    children.append(child)
                    output, errors = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, 0, (output, errors))
                    self.assertEqual(output + errors, b"")
                    self.assertEqual(json.loads(server.read_text())["posts"], 1)
                    self.assertGreater(json.loads(server.read_text())["reads"], 0)
                    self.assertEqual(
                        (self.value()["outcome"], self.value()["stage"]), ("completed", "run")
                    )
                    attempt = json.loads((self.state / "last_attempt.json").read_text())
                    self.assertEqual((attempt["outcome"], attempt["stage"]), ("completed", "sync"))
                    summary = json.loads(self.last.read_text())
                    self.assertEqual(summary["local_status_counts"], {"verified": 1})
                    self.assertEqual(
                        list(summary["results"].values()), ["verified"] if run == 1 else []
                    )
                    with contextlib.closing(sqlite3.connect(self.state / "sync.sqlite3")) as db:
                        self.assertEqual(
                            db.execute("SELECT status FROM uploads").fetchall(), [("verified",)]
                        )
                    for name in ("agent.out.log", "agent.err.log", "sync.log"):
                        log = self.state / name
                        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                    self.assertIn("verified", (self.state / "agent.out.log").read_text())
                    for name in ("agent-log.lock", "run.lock"):
                        with (self.state / name).open("a") as probe:
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(self.export.read_bytes(), source_bytes)
                    self.assertEqual((self.tokens / "oauth.json").read_bytes(), token_bytes)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_real_script_setup_failure_replaces_success_without_starting_sync(self):
        self.install_temporary_script("""
def main():
    raise AssertionError("main must not be entered")
hg_cli.main = main
def api_client(*args, **kwargs):
    raise AssertionError("network forbidden")
hg_garmin.api_client = api_client
""")
        old_run = self.state / "last_attempt.json"
        stamp = hg_runtime.now().isoformat()
        old_run.write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "stage": "sync",
                    "started_at": stamp,
                    "finished_at": stamp,
                    "error_type": None,
                }
            )
        )
        before_run, before_last = old_run.read_bytes(), self.last.read_bytes()
        outside = self.root / "outside-log"
        outside.write_text(fixtures.SECRET)
        (self.state / "agent.out.log").symlink_to(outside)
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", "health_garmin_sync.background"],
            cwd=self.base,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            output, errors = child.communicate(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        self.assertEqual(child.returncode, 2)
        self.assertNotIn(fixtures.SECRET.encode(), output + errors)
        value = self.value()
        self.assertEqual((value["outcome"], value["stage"]), ("failed", "setup"))
        self.assertEqual(old_run.read_bytes(), before_run)
        self.assertEqual(self.last.read_bytes(), before_last)
        self.assertEqual(outside.read_text(), fixtures.SECRET)
        self.assertEqual(self.report()[2]["last_agent_attempt"]["status"], "error")
        with (self.state / "agent-log.lock").open("a") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_real_script_fifo_run_lock_fails_quickly_without_overwriting_run_evidence(self):
        self.install_temporary_script("""
import builtins
import socket
def forbidden(*args, **kwargs):
    raise AssertionError("config, API, home, service or command log accessed before lock validation")
hg_runtime.config = hg_garmin.api_client = hg_launchagent.manage_agent = hg_file_io.PrivateRotatingFileHandler = forbidden
hg_sync.run_with_attempt = forbidden
Path.home = classmethod(lambda cls: forbidden())
socket.create_connection = socket.getaddrinfo = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
builtins.input = getpass.getpass = forbidden
""")
        os.mkfifo(self.state / "run.lock", 0o600)
        stamp = hg_runtime.now().isoformat()
        run_marker = self.state / "last_attempt.json"
        run_marker.write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "stage": "sync",
                    "started_at": stamp,
                    "finished_at": stamp,
                    "error_type": None,
                }
            )
        )
        before = (
            run_marker.read_bytes(),
            self.last.read_bytes(),
            self.export.read_bytes(),
            (self.tokens / "oauth.json").read_bytes(),
        )
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", "health_garmin_sync.background"],
            cwd=self.base,
            env={"HOME": str(self.home)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            output, errors = child.communicate(timeout=2)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            child.stdout.close()
            child.stderr.close()
        self.assertEqual(child.returncode, 2)
        self.assertNotIn(fixtures.SECRET.encode(), output + errors)
        self.assertEqual((self.value()["outcome"], self.value()["stage"]), ("failed", "run"))
        self.assertEqual(
            (
                run_marker.read_bytes(),
                self.last.read_bytes(),
                self.export.read_bytes(),
                (self.tokens / "oauth.json").read_bytes(),
            ),
            before,
        )
        self.assertFalse((self.state / "sync.log").exists())
        self.assertFalse((self.state / "sync.sqlite3").exists())
        log = (self.state / "agent.err.log").read_text()
        self.assertNotIn("AssertionError", log)
        self.assertNotIn("Traceback", log)
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks["last_agent_attempt"]["status"], "error")
        with (self.state / "agent-log.lock").open("a") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_real_script_sigkill_keeps_unfinished_evidence_and_releases_locks(self):
        self.install_temporary_script("""
import time
def run_sync(cfg, selected_id=None):
    print("offline synthetic sync entered", flush=True)
    time.sleep(60)
hg_sync.run_sync = run_sync
def api_client(*args, **kwargs):
    raise AssertionError("network forbidden")
hg_garmin.api_client = api_client
""")
        old_last = self.last.read_bytes()
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", "health_garmin_sync.background"],
            cwd=self.base,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            run_marker = self.state / "last_attempt.json"
            while time.monotonic() < deadline:
                if run_marker.exists() and (self.state / "agent.out.log").exists():
                    run = json.loads(run_marker.read_text())
                    if (
                        run["stage"] == "sync"
                        and "offline synthetic sync entered"
                        in (self.state / "agent.out.log").read_text()
                    ):
                        break
                if child.poll() is not None:
                    self.fail(f"temporary runner exited: {child.returncode}")
                time.sleep(0.01)
            else:
                self.fail("temporary runner did not reach synthetic sync")
            entry_bytes, run_bytes = self.marker.read_bytes(), run_marker.read_bytes()
            self.assertEqual(self.value()["outcome"], "running")
            self.assertEqual(self.value()["stage"], "run")
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(self.marker.read_bytes(), entry_bytes)
            self.assertEqual(run_marker.read_bytes(), run_bytes)
            self.assertEqual(self.last.read_bytes(), old_last)
            self.assertIsNone(self.value()["finished_at"])
            for name in ("agent-log.lock", "run.lock"):
                with (self.state / name).open("a") as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, _, checks = self.report()
            self.assertEqual(result, 0)
            self.assertEqual(checks["last_agent_attempt"]["status"], "warn")
            self.assertEqual(checks["last_attempt"]["status"], "warn")
            self.assertIn("可能仍在运行或曾被中断", checks["last_agent_attempt"]["message"])
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_real_post_sigkill_recovers_across_entries_without_repeating_the_write(self):
        self.cfg["settle_seconds"] = 0
        self.save_config()
        source_bytes, token_bytes = (
            self.export.read_bytes(),
            (self.tokens / "oauth.json").read_bytes(),
        )
        old_last = self.last.read_bytes()
        server = self.base / "synthetic-server.json"
        ready = self.base / "synthetic-post-ready.json"
        visible = self.base / "synthetic-visible"
        server.write_text(json.dumps({"posts": 0, "deletes": 0, "reads": 0, "rows": []}))
        self.install_temporary_script("""
# Replace only the network boundary. The runner, main, sync and process_record
# remain the actual copied production implementation in every process.
import builtins
import socket
import time

def offline_only(*args, **kwargs):
    raise AssertionError("real network, credentials or service access forbidden")

Path.home = classmethod(lambda cls: offline_only())
socket.create_connection = socket.getaddrinfo = offline_only
socket.socket.connect = socket.socket.connect_ex = offline_only
subprocess.run = subprocess.Popen = offline_only
builtins.input = getpass.getpass = offline_only
original_import = builtins.__import__
def offline_import(name, *args, **kwargs):
    if name.split('.')[0] in ('garminconnect', 'requests', 'urllib3', 'curl_cffi', 'garth'):
        return offline_only()
    return original_import(name, *args, **kwargs)
builtins.__import__ = offline_import
original_read_text = Path.read_text
def read_without_tokens(path, *args, **kwargs):
    if hg_runtime.STATE / 'tokens' in path.parents:
        return offline_only()
    return original_read_text(path, *args, **kwargs)
Path.read_text = read_without_tokens

class LocalServerAPI:
    path = hg_runtime.BASE / 'synthetic-server.json'
    def get_body_composition(self, start, end):
        value = json.loads(self.path.read_text())
        value['reads'] += 1
        hg_file_io.write_json(self.path, value)
        rows = value['rows'] if (hg_runtime.BASE / 'synthetic-visible').exists() else []
        return {'dateWeightList': [row for row in rows if start <= row['calendarDate'] <= end]}

    def add_body_composition(self, **kwargs):
        # A separate read-only connection must see the committed gate BEFORE
        # this simulated remote service receives and durably saves the POST.
        with contextlib.closing(sqlite3.connect(
                f"file:{hg_runtime.STATE / 'sync.sqlite3'}?mode=ro", uri=True)) as probe:
            assert probe.execute('SELECT status FROM uploads').fetchall() == [('inflight',)]
        value = json.loads(self.path.read_text())
        value['posts'] += 1
        at = hg_domain.aware(kwargs['timestamp'])
        value['rows'].append({'samplePk': 100, 'calendarDate': at.date().isoformat(),
                             'timestampGMT': int(at.timestamp() * 1000),
                             'weight': kwargs['weight'] * 1000,
                             'bodyFat': kwargs['percent_fat'], 'bmi': kwargs.get('bmi')})
        value['durable_inflight_before_post'] = True
        hg_file_io.write_json(self.path, value)
        hg_file_io.write_json(hg_runtime.BASE / 'synthetic-post-ready.json', {'received': True})
        # Parent SIGKILLs this real entry after receipt, before any response.
        while True:
            time.sleep(0.02)

    def add_weigh_in(self, **kwargs):
        raise AssertionError("fixture must exercise the FIT POST path")

    def delete_weigh_in(self, *args, **kwargs):
        value = json.loads(self.path.read_text())
        value['deletes'] += 1
        hg_file_io.write_json(self.path, value)
        raise AssertionError("recovery must never delete")

def api_client(interactive=False):
    assert not interactive
    return LocalServerAPI()
hg_garmin.api_client = api_client
""")
        children = []
        run_marker = self.state / "last_attempt.json"

        def launch():
            child = subprocess.Popen(
                [sys.executable, "-B", "-m", "health_garmin_sync.background"],
                cwd=self.base,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            children.append(child)
            return child

        def local_status():
            with contextlib.closing(
                sqlite3.connect(f"file:{self.state / 'sync.sqlite3'}?mode=ro", uri=True)
            ) as db:
                return db.execute("SELECT status FROM uploads").fetchall()

        def unlocked():
            for name in ("agent-log.lock", "run.lock"):
                with (self.state / name).open("a") as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            first = launch()
            deadline = time.monotonic() + 5
            while not ready.exists():
                if first.poll() is not None:
                    self.fail(f"temporary entry exited before simulated POST: {first.returncode}")
                if time.monotonic() >= deadline:
                    self.fail("temporary entry did not reach simulated POST")
                time.sleep(0.01)
            self.assertIsNone(first.poll())
            self.assertEqual(json.loads(ready.read_text()), {"received": True})
            accepted = json.loads(server.read_text())
            self.assertEqual(accepted["posts"], 1)
            self.assertEqual(accepted["deletes"], 0)
            self.assertTrue(accepted["durable_inflight_before_post"])
            self.assertEqual(local_status(), [("inflight",)])
            self.assertEqual(self.export.read_bytes(), source_bytes)
            run = json.loads(run_marker.read_text())
            self.assertEqual(
                (run["outcome"], run["stage"], run["finished_at"]), ("running", "sync", None)
            )
            self.assertEqual((self.value()["outcome"], self.value()["stage"]), ("running", "run"))
            self.assertIsNone(self.value()["finished_at"])
            entry_bytes, run_bytes = self.marker.read_bytes(), run_marker.read_bytes()
            for name in ("agent-log.lock", "run.lock"):
                with (self.state / name).open("a") as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            first.kill()
            first.wait(timeout=5)
            self.assertEqual(first.returncode, -signal.SIGKILL)
            self.assertEqual(self.marker.read_bytes(), entry_bytes)
            self.assertEqual(run_marker.read_bytes(), run_bytes)
            self.assertEqual(self.last.read_bytes(), old_last)
            self.assertEqual(json.loads(server.read_text()), accepted)
            self.assertEqual(local_status(), [("inflight",)])
            unlocked()

            second = launch()
            second.wait(timeout=5)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(local_status(), [("uncertain",)])
            self.assertEqual(json.loads(server.read_text())["posts"], 1)
            summary = json.loads(self.last.read_text())
            self.assertEqual(list(summary["results"].values()), ["uncertain"])
            self.assertEqual(summary["local_status_counts"], {"uncertain": 1})
            self.assertEqual(self.value()["outcome"], "completed")
            self.assertEqual(json.loads(run_marker.read_text())["outcome"], "completed")
            result, _, checks = self.report()
            self.assertEqual(result, 2)
            self.assertEqual(checks["last_run"]["status"], "error")
            self.assertEqual(checks["last_run"]["data"]["local_status_counts"], {"uncertain": 1})
            unlocked()

            visible.touch()
            third = launch()
            third.wait(timeout=5)
            self.assertEqual(third.returncode, 0)
            self.assertEqual(local_status(), [("verified",)])
            summary = json.loads(self.last.read_text())
            self.assertEqual(list(summary["results"].values()), ["verified"])
            self.assertEqual(summary["local_status_counts"], {"verified": 1})
            result, _, checks = self.report()
            self.assertEqual(result, 0)
            self.assertEqual(checks["last_run"]["status"], "info")
            final = json.loads(server.read_text())
            self.assertEqual((final["posts"], final["deletes"]), (1, 0))
            self.assertGreater(final["reads"], accepted["reads"])
            self.assertEqual(self.export.read_bytes(), source_bytes)
            self.assertEqual((self.tokens / "oauth.json").read_bytes(), token_bytes)
            unlocked()
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
