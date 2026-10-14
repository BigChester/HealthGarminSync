"""Run evidence is serialized by the existing lock and never authorizes retries."""

import contextlib
import fcntl
import io
import json
import logging
import logging as hg_std_logging
import os
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from logging.handlers import RotatingFileHandler as hg_std_RotatingFileHandler
from unittest.mock import Mock, patch, seal

import test_diagnostics as fixtures
from project_fixtures import REPO_ROOT
from test_reconciliation import API, row

from health_garmin_sync import cli as hg_cli
from health_garmin_sync import domain as hg_domain
from health_garmin_sync import file_io as hg_file_io
from health_garmin_sync import garmin as hg_garmin
from health_garmin_sync import launchagent as hg_launchagent
from health_garmin_sync import runtime as hg_runtime
from health_garmin_sync import sources as hg_sources
from health_garmin_sync import state as hg_state
from health_garmin_sync import sync as hg_sync

SECRET = fixtures.SECRET


@contextlib.contextmanager
def isolated_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    root.handlers = []
    try:
        yield
    finally:
        for handler in root.handlers:
            if handler not in handlers:
                handler.close()
        root.handlers = handlers
        root.setLevel(level)


@contextlib.contextmanager
def failing_log_close(case, error=None):
    """Fail only the final flush inside native FileHandler.close, with real fds."""
    factory = hg_file_io.PrivateRotatingFileHandler
    observed = []
    failure = error if error is not None else OSError(SECRET)

    def create(*args, **kwargs):
        handler = factory(*args, **kwargs)
        native_flush, native_close = handler.flush, handler.close
        state = {"handler": handler, "close_calls": 0, "flush": native_flush}
        observed.append(state)
        closing = False

        def flush():
            if closing:
                raise failure
            return native_flush()

        def close():
            nonlocal closing
            state["close_calls"] += 1
            with (case.state / "run.lock").open("a") as probe, case.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = handler.stream.fileno()
            closing = True
            try:
                return native_close()
            finally:
                case.assertIsNone(handler.stream)
                with case.assertRaises(OSError):
                    os.fstat(fd)

        handler.flush, handler.close = flush, close
        return handler

    try:
        with patch.object(hg_file_io, "PrivateRotatingFileHandler", side_effect=create):
            yield observed
    finally:
        for state in observed:
            state["handler"].flush = state["flush"]


class RunAttemptTests(unittest.TestCase):
    setUp = fixtures.DoctorTests.setUp
    save_config = fixtures.DoctorTests.save_config
    snapshot = fixtures.DoctorTests.snapshot
    invoke = fixtures.DoctorTests.invoke
    report = fixtures.DoctorTests.report

    def run_main(self, command="run"):
        output, errors = io.StringIO(), io.StringIO()
        old_umask = os.umask(0o077)
        try:
            with (
                patch.object(sys, "argv", ["health-garmin-sync", command]),
                patch.object(
                    hg_file_io, "PrivateRotatingFileHandler", return_value=logging.NullHandler()
                ),
                patch.object(hg_std_logging, "basicConfig"),
                patch.object(hg_runtime.LOG, "error"),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                result = hg_cli.main()
        finally:
            os.umask(old_umask)
        return result, output.getvalue(), errors.getvalue()

    def run_with_logging(self, command="run"):
        output, errors = io.StringIO(), io.StringIO()
        old_umask = os.umask(0o077)
        try:
            with (
                isolated_logging(),
                patch.object(sys, "argv", ["health-garmin-sync", command]),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                result = hg_cli.main()
        finally:
            os.umask(old_umask)
        return result, output.getvalue(), errors.getvalue()

    def test_command_log_guard_safely_latches_write_and_flush_errors_but_preserves_interrupt(self):
        record = logging.LogRecord("health_garmin", logging.INFO, "", 0, "%s", (SECRET,), None)
        previous_raise = logging.raiseExceptions
        for method in ("write", "flush"):
            with self.subTest(method=method):
                handler = hg_std_RotatingFileHandler(self.state / "guard.log", encoding="utf-8")
                check = hg_file_io.guard_command_logging(handler)
                errors = io.StringIO()
                with (
                    contextlib.closing(handler),
                    patch.object(handler.stream, method, side_effect=OSError(SECRET)) as failed,
                    contextlib.redirect_stderr(errors),
                ):
                    handler.emit(record)
                    handler.emit(record)
                    self.assertEqual(failed.call_count, 1)
                    with self.assertRaises(hg_domain.SafeError) as raised:
                        check()
                    self.assertIn("OSError", str(raised.exception))
                    self.assertNotIn(SECRET, str(raised.exception))
                self.assertEqual(errors.getvalue().count("同步日志未保存："), 1)
                self.assertNotIn(SECRET, errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertNotIn("Arguments:", errors.getvalue())
        handler = hg_std_RotatingFileHandler(self.state / "guard.log", encoding="utf-8")
        check = hg_file_io.guard_command_logging(handler)
        primary = KeyboardInterrupt()
        with (
            contextlib.closing(handler),
            patch.object(handler.stream, "write", side_effect=primary),
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                handler.emit(record)
            self.assertIs(raised.exception, primary)
            check()
        self.assertEqual(logging.raiseExceptions, previous_raise)

    def test_command_log_failure_and_broken_notice_preserve_primary_exit_results(self):
        factory = hg_file_io.PrivateRotatingFileHandler

        def create(*args, **kwargs):
            handler = factory(*args, **kwargs)
            handler.stream.write = Mock(side_effect=OSError(SECRET))
            return handler

        for primary, expected in (
            (hg_domain.SafeError("Controlled original failure"), 2),
            (KeyboardInterrupt(), 130),
        ):
            with self.subTest(expected=expected):

                def sync(cfg):
                    hg_runtime.LOG.info("PRIVATE %s", SECRET)
                    raise primary

                with (
                    patch.object(hg_file_io, "PrivateRotatingFileHandler", side_effect=create),
                    patch.object(hg_sync, "run_sync", side_effect=sync),
                ):
                    result, _, errors = self.run_with_logging()
                self.assertEqual(result, expected)
                self.assertEqual(self.attempt()["error_type"], type(primary).__name__)
                self.assertEqual(
                    self.attempt()["outcome"], "interrupted" if expected == 130 else "failed"
                )
                self.assertNotIn(SECRET, errors)
                self.assertNotIn("Traceback", errors)
                self.assertEqual(errors.count("同步日志未保存："), 1)
                broken_errors = Mock()
                broken_errors.write.side_effect = BrokenPipeError(SECRET)
                with (
                    isolated_logging(),
                    patch.object(sys, "argv", ["health-garmin-sync", "run"]),
                    patch.object(sys, "stderr", broken_errors),
                    patch.object(hg_file_io, "PrivateRotatingFileHandler", side_effect=create),
                    patch.object(hg_sync, "run_sync", side_effect=sync),
                ):
                    old_umask = os.umask(0o077)
                    try:
                        self.assertEqual(hg_cli.main(), expected)
                    finally:
                        os.umask(old_umask)
                self.assertEqual(self.attempt()["error_type"], type(primary).__name__)

    def test_late_log_failure_survives_terminal_save_and_notice_failure(self):
        primary = hg_domain.SafeError("Controlled log failure")
        original_write = hg_file_io.write_json

        def write(path, value):
            if path == self.state / "last_attempt.json" and value["outcome"] == "failed":
                raise OSError(SECRET)
            return original_write(path, value)

        broken_errors = Mock()
        broken_errors.write.side_effect = BrokenPipeError(SECRET)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.cfg["enabled"] = enabled
                self.save_config()
                with (
                    patch.object(hg_sync, "run_sync"),
                    patch.object(hg_file_io, "write_json", side_effect=write),
                    patch.object(hg_runtime.LOG, "error", side_effect=OSError(SECRET)),
                    patch.object(sys, "stderr", broken_errors),
                    self.assertRaises(hg_domain.SafeError) as raised,
                ):
                    hg_sync.run_with_attempt(check_logging=Mock(side_effect=primary))
                self.assertIs(raised.exception, primary)
                self.assertEqual(
                    json.loads((self.state / "last_attempt.json").read_text())["outcome"], "running"
                )

    def test_other_successful_commands_check_owned_logging_before_returning(self):
        factory = hg_file_io.PrivateRotatingFileHandler

        def create(*args, **kwargs):
            handler = factory(*args, **kwargs)
            handler.stream.write = Mock(side_effect=OSError(SECRET))
            return handler

        def command(*args):
            hg_runtime.LOG.info("PRIVATE %s", SECRET)

        for name, module, helper in (
            ("init", hg_cli, "init_config"),
            ("uninstall-agent", hg_launchagent, "manage_agent"),
            ("status", hg_cli, "show_status"),
        ):
            with (
                self.subTest(command=name),
                patch.object(hg_file_io, "PrivateRotatingFileHandler", side_effect=create),
                patch.object(module, helper, side_effect=command),
            ):
                result, _, errors = self.run_with_logging(name)
                self.assertEqual(result, 2)
                self.assertNotIn(SECRET, errors)
                self.assertNotIn("Traceback", errors)
                self.assertEqual(errors.count("同步日志未保存："), 1)
                self.assertFalse((self.state / "last_attempt.json").exists())

    def test_native_log_close_flush_failure_precedes_manual_success_or_preview_marker(self):
        measurement = hg_sources.load_measurements(self.cfg)[1][0]
        source, tokens = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.cfg["enabled"] = enabled
                self.save_config()
                old_last = self.last.read_bytes()
                api = API([row(measurement)])
                with (
                    failing_log_close(self) as observed,
                    patch.object(hg_garmin, "api_client", return_value=api),
                ):
                    result, _, errors = self.run_with_logging()
                self.assertEqual(result, 2)
                self.assertEqual(observed[0]["close_calls"], 1)
                self.assertIsNone(observed[0]["handler"].stream)
                self.assertEqual(self.attempt()["outcome"], "failed")
                self.assertEqual(self.attempt()["stage"], "sync")
                self.assertNotIn(SECRET, errors)
                self.assertNotIn("Traceback", errors)
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)
                if enabled:
                    with contextlib.closing(hg_state.database()) as db:
                        self.assertEqual(hg_state.get_status(db, measurement), "verified")
                    self.assertEqual(
                        json.loads(self.last.read_text())["local_status_counts"], {"verified": 1}
                    )
                else:
                    self.assertEqual(self.last.read_bytes(), old_last)
                    self.assertFalse(api.reads)
                self.assertEqual(self.report()[0], 2)
                self.assertEqual(
                    (self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()),
                    (source, tokens),
                )

    def test_closed_log_handler_cannot_emit_or_reopen_and_only_closes_once(self):
        record = logging.LogRecord("health_garmin", logging.ERROR, "", 0, "%s", (SECRET,), None)
        errors = io.StringIO()
        with (self.state / "run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with failing_log_close(self) as observed, contextlib.redirect_stderr(errors):
                handler = hg_file_io.PrivateRotatingFileHandler(self.state / "guard-close.log")
                check = hg_file_io.guard_command_logging(handler)
                handler.close()
                handler.close()
                before = (self.state / "guard-close.log").read_bytes()
                handler.emit(record)
                self.assertEqual((self.state / "guard-close.log").read_bytes(), before)
                self.assertIsNone(handler.stream)
                self.assertEqual(observed[0]["close_calls"], 1)
                with self.assertRaises(hg_domain.SafeError):
                    check()
        self.assertNotIn(SECRET, errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_log_close_failure_keeps_primary_results_and_does_not_swallow_interrupt(self):
        cases = (
            (hg_domain.SafeError("Original controlled failure"), OSError(SECRET), 2),
            (KeyboardInterrupt(), OSError(SECRET), 130),
            (None, KeyboardInterrupt(), 130),
        )
        for primary, closing, expected in cases:
            with self.subTest(primary=type(primary).__name__, closing=type(closing).__name__):
                with (
                    failing_log_close(self, closing) as observed,
                    patch.object(hg_sync, "run_sync", side_effect=primary),
                ):
                    result, _, errors = self.run_with_logging()
                self.assertEqual(result, expected)
                self.assertEqual(observed[0]["close_calls"], 1)
                self.assertEqual(
                    self.attempt()["outcome"], "failed" if expected == 2 else "interrupted"
                )
                self.assertEqual(
                    self.attempt()["error_type"],
                    "SafeError" if expected == 2 else "KeyboardInterrupt",
                )
                self.assertNotIn(SECRET, errors)
                self.assertNotIn("Traceback", errors)

    def test_other_successful_commands_include_log_close_result(self):
        for name, module, helper in (
            ("init", hg_cli, "init_config"),
            ("uninstall-agent", hg_launchagent, "manage_agent"),
            ("status", hg_cli, "show_status"),
        ):
            with (
                self.subTest(command=name),
                failing_log_close(self) as observed,
                patch.object(module, helper),
            ):
                result, _, errors = self.run_with_logging(name)
                self.assertEqual(result, 2)
                self.assertEqual(observed[0]["close_calls"], 1)
                self.assertNotIn(SECRET, errors)
                self.assertNotIn("Traceback", errors)
                self.assertFalse((self.state / "last_attempt.json").exists())

    def attempt(self):
        value = json.loads((self.state / "last_attempt.json").read_text())
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertEqual(
            set(value), {"outcome", "stage", "started_at", "finished_at", "error_type"}
        )
        self.assertGreaterEqual(
            hg_domain.aware(value["finished_at"]), hg_domain.aware(value["started_at"])
        )
        return value

    def completed_marker(self):
        stamp = hg_runtime.now().isoformat()
        return {
            "outcome": "completed",
            "stage": "sync",
            "started_at": stamp,
            "finished_at": stamp,
            "error_type": None,
        }

    def test_login_failure_replaces_attempt_but_preserves_prior_last_run(self):
        prior = self.last.read_bytes()
        with patch.object(hg_garmin, "api_client", side_effect=TimeoutError(SECRET + " HTTP 503")):
            result, _, errors = self.run_main()
        self.assertEqual(result, 2)
        self.assertNotIn(SECRET, errors)
        attempt = self.attempt()
        self.assertEqual(
            (attempt["outcome"], attempt["stage"], attempt["error_type"]),
            ("failed", "sync", "TimeoutError HTTP 503"),
        )
        self.assertEqual(self.last.read_bytes(), prior)
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks["last_attempt"]["status"], "error")
        self.assertNotIn("error_type", checks["last_attempt"]["data"])
        self.assertEqual(checks["last_run"]["data"]["results"], {"verified": 1})

    def test_configuration_and_export_failures_record_correct_stage_before_api(self):
        prior = self.last.read_bytes()
        for config_failure in (True, False):
            with self.subTest(config_failure=config_failure):
                self.save_config()
                if config_failure:
                    self.config_path.write_text(json.dumps(dict(self.cfg, enabled="false")))
                else:
                    self.export.write_text("{ invalid " + SECRET)
                with patch.object(hg_garmin, "api_client") as api:
                    result, _, _ = self.run_main()
                self.assertEqual(result, 2)
                value = self.attempt()
                self.assertEqual(value["outcome"], "failed")
                self.assertEqual(value["stage"], "config" if config_failure else "sync")
                self.assertEqual(value["error_type"], "SafeError")
                self.assertEqual(self.last.read_bytes(), prior)
                api.assert_not_called()
                self.assertEqual(self.report()[0], 2)

    def test_comparison_failure_keeps_previous_last_run_and_makes_no_remote_write(self):
        prior = self.last.read_bytes()
        api = API()
        api.fail_chunk = 1
        with patch.object(hg_garmin, "api_client", return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 2)
        self.assertEqual(self.attempt()["outcome"], "failed")
        self.assertEqual(self.last.read_bytes(), prior)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_duplicate_range_details_stop_all_writes_and_recover_from_complete_snapshot(self):
        self.cfg["settle_seconds"] = 0
        self.save_config()
        current = hg_sources.load_measurements(self.cfg)[1][0]
        older = hg_domain.Measurement(current.at - timedelta(days=45), 72, 24, current.source)
        data = json.loads(self.export.read_text())
        for metric, value in (("weight", older.weight), ("body_fat", f"{older.fat}%")):
            data["metrics"][metric].append(
                {"at": older.at.isoformat(), "value": value, "source": older.source}
            )
        self.export.write_text(json.dumps(data))
        source, tokens = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        groups = []
        for number, record in enumerate((older, current)):
            neighbor = hg_domain.Measurement(record.at + timedelta(hours=1), 74, 27, record.source)
            groups.append(
                (
                    record.at.date().isoformat(),
                    row(record, 100 + number),
                    row(neighbor, 200 + number),
                )
            )
        for malformed_chunk in (1, 2):
            with self.subTest(malformed_chunk=malformed_chunk):
                with contextlib.closing(hg_state.database()) as db, db:
                    db.execute("DELETE FROM uploads")
                prior = self.last.read_bytes()
                malformed = True

                def details(start, end):
                    summaries = []
                    for number, (day, existing, other) in enumerate(groups, start=1):
                        if not start <= day <= end:
                            continue
                        if malformed and number == malformed_chunk:
                            items = [dict(other), dict(other)]
                        elif malformed and malformed_chunk == 2 and number == 1:
                            # An earlier valid chunk omits a pending source;
                            # later malformed data must still stop all writes.
                            items = []
                        else:
                            items = [existing, other]
                        summaries.append(
                            {
                                "summaryDate": day,
                                "numOfWeightEntries": len(items),
                                "allWeightMetrics": items,
                            }
                        )
                    return {"dailyWeightSummaries": summaries}

                raw = Mock(
                    spec_set=[
                        "get_weigh_ins",
                        "get_daily_weigh_ins",
                        "add_body_composition",
                        "add_weigh_in",
                        "delete_weigh_in",
                    ]
                )
                raw.get_weigh_ins.side_effect = details
                raw.get_daily_weigh_ins.side_effect = AssertionError("daily endpoint unexpected")
                for name in ("add_body_composition", "add_weigh_in", "delete_weigh_in"):
                    getattr(raw, name).side_effect = AssertionError(SECRET)
                seal(raw)
                adapter = hg_garmin.GarminMeasurementAPI(raw)
                with patch.object(hg_garmin, "api_client", return_value=adapter):
                    result, _, errors = self.run_main()
                self.assertEqual(result, 2)
                self.assertNotIn(SECRET, errors)
                self.assertEqual(raw.get_weigh_ins.call_count, malformed_chunk)
                self.assertEqual(self.last.read_bytes(), prior)
                self.assertEqual(self.attempt()["outcome"], "failed")
                with contextlib.closing(hg_state.database()) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 0)
                self.assertEqual(self.report()[0], 2)
                malformed = False
                with patch.object(hg_garmin, "api_client", return_value=adapter):
                    self.assertEqual(self.run_main()[0], 0)
                with contextlib.closing(hg_state.database()) as db:
                    self.assertEqual(
                        [hg_state.get_status(db, record) for record in (older, current)],
                        ["verified", "verified"],
                    )
                self.assertEqual(self.attempt()["outcome"], "completed")
                self.assertEqual(self.report()[0], 0)
                for name in (
                    "get_daily_weigh_ins",
                    "add_body_composition",
                    "add_weigh_in",
                    "delete_weigh_in",
                ):
                    getattr(raw, name).assert_not_called()
                self.assertEqual(
                    (self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()),
                    (source, tokens),
                )

    def test_multiple_anonymous_details_remain_ambiguous_across_runs_and_recover(self):
        self.cfg["settle_seconds"] = 0
        self.save_config()
        record = hg_sources.load_measurements(self.cfg)[1][0]
        known = row(record)
        source, tokens = self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()
        for missing_key in (False, True):
            for previous in ("pending", "uncertain", "inflight"):
                with self.subTest(missing_key=missing_key, previous=previous):
                    anonymous = dict(known)
                    if missing_key:
                        del anonymous["samplePk"]
                    else:
                        anonymous["samplePk"] = None
                    ambiguous = True

                    def details(start, end):
                        items = [dict(anonymous), dict(anonymous)] if ambiguous else [known]
                        return {
                            "dailyWeightSummaries": [
                                {
                                    "summaryDate": record.at.date().isoformat(),
                                    "numOfWeightEntries": len(items),
                                    "allWeightMetrics": items,
                                }
                            ]
                        }

                    raw = Mock(
                        spec_set=[
                            "get_weigh_ins",
                            "get_daily_weigh_ins",
                            "add_body_composition",
                            "add_weigh_in",
                            "delete_weigh_in",
                        ]
                    )
                    raw.get_weigh_ins.side_effect = details
                    for name in (
                        "get_daily_weigh_ins",
                        "add_body_composition",
                        "add_weigh_in",
                        "delete_weigh_in",
                    ):
                        getattr(raw, name).side_effect = AssertionError(SECRET)
                    seal(raw)
                    adapter = hg_garmin.GarminMeasurementAPI(raw)
                    with contextlib.closing(hg_state.database()) as db:
                        hg_state.set_status(db, record, previous)
                    expected = "conflict" if previous == "pending" else "uncertain"
                    for _ in range(2):
                        with patch.object(hg_garmin, "api_client", return_value=adapter) as api:
                            self.assertEqual(self.run_main()[0], 0)
                        api.assert_called_once_with()
                        with contextlib.closing(hg_state.database()) as db:
                            self.assertEqual(hg_state.get_status(db, record), expected)
                        summary = json.loads(self.last.read_text())
                        self.assertEqual(summary["results"], {record.key: expected})
                        self.assertEqual(summary["local_status_counts"], {expected: 1})
                        code, report, _ = self.report()
                        self.assertEqual(code, 0 if expected == "conflict" else 2)
                        self.assertEqual(
                            report["status"], "warning" if expected == "conflict" else "error"
                        )
                    ambiguous = False
                    with patch.object(hg_garmin, "api_client", return_value=adapter):
                        self.assertEqual(self.run_main()[0], 0)
                    with contextlib.closing(hg_state.database()) as db:
                        self.assertEqual(hg_state.get_status(db, record), "verified")
                    self.assertEqual(self.report()[0], 0)
                    for name in (
                        "get_daily_weigh_ins",
                        "add_body_composition",
                        "add_weigh_in",
                        "delete_weigh_in",
                    ):
                        getattr(raw, name).assert_not_called()
                    self.assertEqual(
                        (self.export.read_bytes(), (self.tokens / "oauth.json").read_bytes()),
                        (source, tokens),
                    )

    def test_future_export_stops_before_api_and_recovers_after_corrected_metadata(self):
        current = hg_runtime.now()
        previous = self.last.read_bytes()
        data = json.loads(self.export.read_text())
        data["exported_at"] = (current + timedelta(days=1)).isoformat()
        self.export.write_text(json.dumps(data))
        source = self.export.read_bytes()
        with (
            patch.object(hg_runtime, "now", return_value=current),
            patch.object(hg_garmin, "api_client") as api,
        ):
            result, _, errors = self.run_main()
        self.assertEqual(result, 2)
        self.assertIn("未来导出时间", errors)
        self.assertEqual(self.attempt()["outcome"], "failed")
        self.assertEqual(self.attempt()["stage"], "sync")
        self.assertEqual(self.last.read_bytes(), previous)
        self.assertEqual(self.export.read_bytes(), source)
        api.assert_not_called()
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks["inbox"]["status"], "error")
        self.assertEqual(checks["last_attempt"]["status"], "error")
        data["exported_at"] = (current - timedelta(minutes=3)).isoformat()
        self.export.write_text(json.dumps(data))
        source = self.export.read_bytes()
        api = API()
        with patch.object(hg_garmin, "api_client", return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(self.attempt()["outcome"], "completed")
        self.assertEqual(self.export.read_bytes(), source)
        self.assertEqual(self.report()[2]["inbox"]["status"], "ok")

    def test_interrupted_post_keeps_inflight_and_subsequent_run_never_reposts(self):
        class InterruptedAPI(API):
            def add_body_composition(self, **kwargs):
                super().add_body_composition(**kwargs)
                raise KeyboardInterrupt()

        api = InterruptedAPI()
        api.visible = False
        prior = self.last.read_bytes()
        with patch.object(hg_garmin, "api_client", return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 130)
        value = self.attempt()
        self.assertEqual(value["outcome"], "interrupted")
        self.assertEqual(value["error_type"], "KeyboardInterrupt")
        with contextlib.closing(hg_state.database()) as db:
            self.assertEqual(db.execute("SELECT status FROM uploads").fetchone()[0], "inflight")
        self.assertEqual(self.last.read_bytes(), prior)
        self.assertEqual(self.report()[0], 2)
        with patch.object(hg_garmin, "api_client", return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(self.attempt()["outcome"], "completed")
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertIn("不代表全部记录已验证", checks["last_attempt"]["message"])
        self.assertEqual(checks["last_run"]["status"], "error")
        self.assertEqual(checks["last_run"]["data"]["results"], {"uncertain": 1})

    def test_disabled_preview_is_separate_and_does_not_replace_last_run(self):
        self.cfg["enabled"] = False
        self.save_config()
        prior = self.last.read_bytes()
        with patch.object(hg_garmin, "api_client") as api:
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(self.attempt()["outcome"], "preview")
        self.assertEqual(self.last.read_bytes(), prior)
        api.assert_not_called()
        self.assertEqual(self.report()[2]["last_attempt"]["status"], "info")

    def test_no_records_and_normal_upload_include_local_state_counts(self):
        with (
            patch.object(hg_sources, "load_measurements", return_value=([], [], [], {})),
            patch.object(hg_garmin, "api_client") as api,
        ):
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(self.attempt()["outcome"], "completed")
        summary = json.loads(self.last.read_text())
        self.assertEqual(
            set(summary),
            {
                "run_at",
                "exports",
                "complete_measurements",
                "waiting_pairs",
                "results",
                "local_status_counts",
                "unknown_request_count",
            },
        )
        self.assertEqual(summary["results"], {})
        self.assertEqual(summary["local_status_counts"], {})
        self.assertEqual(summary["unknown_request_count"], 0)
        self.assertFalse((self.state / "sync.sqlite3").exists())
        api.assert_not_called()
        api = API()
        with patch.object(hg_garmin, "api_client", return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(self.attempt()["outcome"], "completed")
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(self.report()[2]["last_run"]["data"]["results"], {"verified": 1})
        self.assertEqual(
            self.report()[2]["last_run"]["data"]["local_status_counts"], {"verified": 1}
        )

    def test_empty_export_keeps_uncertain_database_states_visible_without_api(self):
        record = hg_sources.load_measurements(self.cfg)[1][0]
        data = json.loads(self.export.read_text())
        for samples in data["metrics"].values():
            samples.clear()
        self.export.write_text(json.dumps(data))
        for status in ("inflight", "uncertain", "repair_uncertain"):
            with self.subTest(status=status):
                with contextlib.closing(hg_state.database()) as db:
                    hg_state.set_status(db, record, status)
                with patch.object(hg_garmin, "api_client") as api:
                    self.assertEqual(self.run_main()[0], 0)
                api.assert_not_called()
                self.assertEqual(self.attempt()["outcome"], "completed")
                summary = json.loads(self.last.read_text())
                self.assertEqual(summary["results"], {})
                self.assertEqual(summary["local_status_counts"], {status: 1})
                code, _, checks = self.report()
                self.assertEqual(code, 2)
                self.assertEqual(checks["last_run"]["status"], "error")
                self.assertEqual(checks["last_run"]["data"]["local_status_counts"], {status: 1})
        with contextlib.closing(hg_state.database()) as db:
            hg_state.set_status(db, record, "verified_manual")
        with patch.object(hg_garmin, "api_client") as api:
            self.assertEqual(self.run_main()[0], 0)
        api.assert_not_called()
        code, _, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(checks["last_run"]["status"], "info")
        self.assertEqual(checks["last_run"]["data"]["local_status_counts"], {"verified_manual": 1})

    def test_current_match_and_single_upload_do_not_hide_other_uncertain_records(self):
        record = hg_sources.load_measurements(self.cfg)[1][0]
        missing = hg_domain.Measurement(record.at - timedelta(days=1), 70, 23.5, SECRET)
        with contextlib.closing(hg_state.database()) as db:
            hg_state.set_status(db, missing, "uncertain")
        api = API([row(record)])
        with patch.object(hg_garmin, "api_client", return_value=api):
            self.assertEqual(self.run_main()[0], 0)
        for single in (False, True):
            with self.subTest(single=single):
                if single:
                    with (
                        patch.object(hg_garmin, "api_client", return_value=api),
                        patch("builtins.input", return_value="UPLOAD"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_sync.run_sync(self.cfg, record.key)
                summary = json.loads(self.last.read_text())
                self.assertEqual(summary["results"], {record.key: "verified"})
                self.assertEqual(summary["local_status_counts"], {"uncertain": 1, "verified": 1})
                code, _, checks = self.report()
                self.assertEqual(code, 2)
                self.assertEqual(checks["last_run"]["status"], "error")
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_idle_historical_run_completes_offline_and_retains_local_lbm(self):
        record = hg_sources.load_measurements(self.cfg)[1][0]
        with contextlib.closing(hg_state.database()) as db:
            hg_state.set_status(db, record, "verified")
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        data = json.loads(self.export.read_text())
        data["metrics"]["lbm"] = [
            {"at": record.at.isoformat(), "value": "53.55 kg", "source": record.source}
        ]
        self.export.write_text(json.dumps(data))
        source = self.export.read_bytes()
        with patch.object(hg_garmin, "api_client", side_effect=TimeoutError(SECRET)) as api:
            self.assertEqual(self.run_main()[0], 0)
        api.assert_not_called()
        self.assertEqual(self.attempt()["outcome"], "completed")
        summary = json.loads(self.last.read_text())
        self.assertEqual(summary["results"], {})
        self.assertEqual(summary["local_status_counts"], {"verified": 1})
        with contextlib.closing(hg_state.database()) as db:
            self.assertEqual(json.loads(hg_state.state_row(db, record)["payload"])["lbm"], 53.55)
        code, _, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(checks["last_run"]["data"]["results"], {})
        self.assertEqual(checks["last_run"]["data"]["local_status_counts"], {"verified": 1})
        self.assertIn("不代表当前记录已核验", checks["last_run"]["message"])
        self.assertEqual(self.export.read_bytes(), source)

    def test_empty_export_cannot_mask_unreadable_existing_database(self):
        self.export.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "exported_at": hg_runtime.now().isoformat(),
                    "metrics": {"weight": []},
                }
            )
        )
        previous = self.last.read_bytes()
        database = self.state / "sync.sqlite3"
        database.write_text("invalid database")
        with patch.object(hg_garmin, "api_client") as api:
            self.assertEqual(self.run_main()[0], 2)
        api.assert_not_called()
        self.assertEqual(self.attempt()["outcome"], "failed")
        self.assertEqual(self.last.read_bytes(), previous)

    def test_terminal_write_occurs_while_independent_descriptor_cannot_acquire_lock(self):
        writer = hg_file_io.write_json
        observed = []

        def checked_write(path, value):
            if path == self.state / "last_attempt.json" and value["outcome"] != "running":
                with (self.state / "run.lock").open("r") as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append(value["outcome"])
            return writer(path, value)

        with (
            patch.object(hg_file_io, "write_json", side_effect=checked_write),
            patch.object(hg_sync, "run_sync"),
        ):
            self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(observed, ["completed"])

    def test_busy_skip_and_other_commands_preserve_existing_attempt_bytes(self):
        path = self.state / "last_attempt.json"
        path.write_text(json.dumps(self.completed_marker()))
        prior = path.read_bytes()
        with (self.state / "run.lock").open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch.object(hg_runtime, "config") as cfg,
                patch.object(hg_sync, "run_sync") as sync,
            ):
                self.assertEqual(self.run_main()[0], 0)
            cfg.assert_not_called()
            sync.assert_not_called()
        self.assertEqual(path.read_bytes(), prior)
        self.assertEqual(self.run_main("disable")[0], 0)
        self.assertEqual(path.read_bytes(), prior)
        self.report()
        self.assertEqual(path.read_bytes(), prior)

    def test_start_or_sync_stage_persistence_failure_stops_before_sync(self):
        writer = hg_file_io.write_json
        for failure_stage in ("config", "sync"):
            with self.subTest(failure_stage=failure_stage):

                def failed_write(path, value):
                    if (
                        path == self.state / "last_attempt.json"
                        and value["outcome"] == "running"
                        and value["stage"] == failure_stage
                    ):
                        raise OSError(SECRET)
                    return writer(path, value)

                with (
                    patch.object(hg_file_io, "write_json", side_effect=failed_write),
                    patch.object(hg_sync, "run_sync") as sync,
                ):
                    result, _, errors = self.run_main()
                self.assertEqual(result, 2)
                self.assertNotIn(SECRET, errors)
                sync.assert_not_called()
                if failure_stage == "sync":
                    self.assertEqual(self.attempt()["outcome"], "failed")

    def test_terminal_io_failure_preserves_primary_exception_identity(self):
        writer = hg_file_io.write_json
        primary = hg_domain.SafeError("primary failure")

        def failed_final(path, value):
            if path == self.state / "last_attempt.json" and value["outcome"] == "failed":
                raise PermissionError(SECRET)
            return writer(path, value)

        errors = io.StringIO()
        with (self.state / "run.lock").open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch.object(hg_file_io, "write_json", side_effect=failed_final),
                patch.object(hg_sync, "run_sync", side_effect=primary),
                patch.object(hg_runtime.LOG, "error"),
                contextlib.redirect_stderr(errors),
            ):
                with self.assertRaises(hg_domain.SafeError) as raised:
                    hg_sync.run_with_attempt()
        self.assertIs(raised.exception, primary)
        self.assertIn("PermissionError", errors.getvalue())
        self.assertNotIn(SECRET, errors.getvalue())

    def test_terminal_io_failure_does_not_replace_keyboard_interrupt_exit(self):
        writer = hg_file_io.write_json

        def failed_final(path, value):
            if path == self.state / "last_attempt.json" and value["outcome"] == "interrupted":
                raise PermissionError(SECRET)
            return writer(path, value)

        with (
            patch.object(hg_file_io, "write_json", side_effect=failed_final),
            patch.object(hg_sync, "run_sync", side_effect=KeyboardInterrupt()),
        ):
            result, _, errors = self.run_main()
        self.assertEqual(result, 130)
        self.assertIn("PermissionError", errors)
        self.assertNotIn(SECRET, errors)
        code, _, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(checks["last_attempt"]["status"], "warn")

    def test_successful_body_with_terminal_write_failure_returns_error_and_unfinished_evidence(
        self,
    ):
        writer = hg_file_io.write_json

        def failed_final(path, value):
            if path == self.state / "last_attempt.json" and value["outcome"] == "completed":
                raise OSError(SECRET)
            return writer(path, value)

        with (
            patch.object(hg_file_io, "write_json", side_effect=failed_final),
            patch.object(hg_sync, "run_sync") as sync,
        ):
            result, _, errors = self.run_main()
        self.assertEqual(result, 2)
        self.assertNotIn(SECRET, errors)
        sync.assert_called_once()
        self.assertEqual(self.report()[2]["last_attempt"]["status"], "warn")

    def test_running_evidence_only_warns_without_asserting_process_liveness(self):
        marker = self.completed_marker()
        marker.update(
            outcome="running",
            finished_at=None,
            started_at=(hg_runtime.now() - timedelta(days=1)).isoformat(),
        )
        (self.state / "last_attempt.json").write_text(json.dumps(marker))
        code, _, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(checks["last_attempt"]["status"], "warn")
        self.assertIn("可能仍在运行", checks["last_attempt"]["message"])

    def test_killed_child_leaves_unfinished_evidence_and_releases_run_lock(self):
        code = """
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import threading
from health_garmin_sync import cli as hg_cli, domain as hg_domain, file_io as hg_file_io, garmin as hg_garmin, runtime as hg_runtime, sources as hg_sources, state as hg_state, sync as hg_sync
from logging.handlers import RotatingFileHandler as hg_std_RotatingFileHandler
import logging as hg_std_logging
hg_runtime.BASE, hg_runtime.STATE, hg_runtime.CONFIG = map(Path, sys.argv[2:5])
def block(cfg):
    threading.Event().wait(60)
def forbid_api(*args, **kwargs):
    raise AssertionError("API forbidden")
hg_sync.run_sync = block
hg_garmin.api_client = forbid_api
sys.argv = ["health-garmin-sync", "run"]
raise SystemExit(hg_cli.main())
"""
        prior = self.last.read_bytes()
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                code,
                str(REPO_ROOT),
                str(self.base),
                str(self.state),
                str(self.config_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        marker = self.state / "last_attempt.json"
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll(), "child exited before blocking stage")
                try:
                    value = json.loads(marker.read_text())
                except FileNotFoundError:
                    value = {}
                if value.get("outcome") == "running" and value.get("stage") == "sync":
                    break
                time.sleep(0.02)
            else:
                self.fail("child did not persist sync stage before timeout")
            before_kill = marker.read_bytes()
            process.kill()
            process.wait(timeout=5)
            self.assertLess(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.assertEqual(marker.read_bytes(), before_kill)
        self.assertEqual(self.last.read_bytes(), prior)
        self.assertIsNone(json.loads(marker.read_text())["finished_at"])
        with (self.state / "run.lock").open("r") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks["last_attempt"]["status"], "warn")
        self.assertIn("可能仍在运行或曾被中断", checks["last_attempt"]["message"])

    def test_missing_attempt_is_optional_for_existing_install(self):
        code, report, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(checks["last_attempt"]["status"], "info")

    def test_invalid_attempt_fields_values_and_times_are_errors_without_raw_details(self):
        valid = self.completed_marker()
        invalid = [
            None,
            {},
            dict(valid, outcome=SECRET),
            dict(valid, stage=[SECRET]),
            dict(valid, started_at=SECRET),
            dict(valid, started_at="2020-02-20T08:00:00"),
            dict(valid, finished_at="2000-01-01T00:00:00+00:00"),
            dict(valid, outcome="running"),
            dict(valid, error_type=SECRET),
            dict(valid, outcome="failed", error_type={SECRET: SECRET}),
            dict(valid, outcome="preview", stage="config"),
            dict(valid, pid=SECRET),
        ]
        for value in invalid:
            with self.subTest(value=value):
                (self.state / "last_attempt.json").write_text(json.dumps(value))
                code, _, checks = self.report()
                self.assertEqual(code, 2)
                self.assertEqual(checks["last_attempt"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
