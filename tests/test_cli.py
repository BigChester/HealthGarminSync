"""Configuration and CLI locking checks using only private temporary files."""
import contextlib
import copy
import fcntl
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import health_garmin as h


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config_path = self.root / "config.json"
        self.cfg = {
            "is_cn": True, "enabled": False, "inbox": str(self.root),
            "since": "2020-02-20", "allowed_sources": ["PICOOC"],
            "weight_bare_unit": "kg", "fat_bare_unit": "percent",
            "pair_window_seconds": 0, "settle_seconds": 120,
            "max_records_per_run": 10,
        }
        self.enterContext(patch.object(h, "CONFIG", self.config_path))
        self.enterContext(patch.object(h, "STATE", self.state))
        self.save(self.cfg)

    def save(self, cfg):
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")


class ConfigTests(ConfigFixture):
    def test_valid_config_is_preserved(self):
        for enabled in (True, False):
            cfg = dict(self.cfg, enabled=enabled, custom_note="retain")
            self.save(cfg)
            self.assertEqual(h.config(), cfg)

    def test_enabled_requires_json_boolean(self):
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value):
                self.save(dict(self.cfg, enabled=value))
                with self.assertRaisesRegex(h.SafeError, "enabled"):
                    h.config()

    def test_required_consumed_fields_cannot_be_missing(self):
        for key in self.cfg:
            with self.subTest(key=key):
                cfg = dict(self.cfg)
                del cfg[key]
                self.save(cfg)
                with self.assertRaisesRegex(h.SafeError, key):
                    h.config()

    def test_invalid_shapes_and_units_report_field(self):
        invalid = {
            "is_cn": (False, "true", 1),
            "inbox": ("", "  ", 42, []),
            "since": ("", "2020-02-30", 20200220),
            "allowed_sources": ("PICOOC", [], [""], [" "], [1], [None]),
            "weight_bare_unit": ("stone", "KG", [], None),
            "fat_bare_unit": ("ratio", "PERCENT", [], None),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.save(dict(self.cfg, **{key: value}))
                    with self.assertRaisesRegex(h.SafeError, key):
                        h.config()

    def test_integer_settings_reject_coercion_and_invalid_ranges(self):
        invalid = {
            "pair_window_seconds": (-1, 121, True, "60", 0.5),
            "settle_seconds": (-1, False, "120", 0.5),
            "max_records_per_run": (-1, 0, True, "10", 1.5),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.save(dict(self.cfg, **{key: value}))
                    with self.assertRaisesRegex(h.SafeError, key):
                        h.config()

    def test_valid_integer_boundaries(self):
        self.save(dict(self.cfg, pair_window_seconds=120,
                       settle_seconds=0, max_records_per_run=1))
        self.assertEqual(h.config()["pair_window_seconds"], 120)

    def test_invalid_json_and_top_level_report_safe_error(self):
        for content in ('{"secret":"private-token",', "[]", "null", "true"):
            with self.subTest(content=content):
                self.config_path.write_text(content, encoding="utf-8")
                with self.assertRaises(h.SafeError) as raised:
                    h.config()
                self.assertIn("config.json", str(raised.exception))
                self.assertNotIn("private-token", str(raised.exception))

    def test_rejected_settings_preserve_file_and_caller_config(self):
        invalid = ({"source": "   "}, {"source": ""}, {"inbox": ""},
                   {"inbox": "  "}, {"since": "2020-02-30"},
                   {"pair_window": 121}, {"inbox": str(self.root / "missing")})
        for changes in invalid:
            with self.subTest(changes=changes):
                cfg = copy.deepcopy(self.cfg)
                before = copy.deepcopy(cfg)
                stored = self.config_path.read_bytes()
                args = SimpleNamespace(fat_unit=None, weight_unit=None, pair_window=None,
                                       since=None, inbox=None, source="ChangedSource")
                for key, value in changes.items():
                    setattr(args, key, value)
                with self.assertRaises(h.SafeError):
                    h.update_settings(args, cfg)
                self.assertEqual(cfg, before)
                self.assertEqual(self.config_path.read_bytes(), stored)
                self.assertEqual(h.config(), self.cfg)


class CliTests(ConfigFixture):
    def invoke(self, command, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        previous_umask = os.umask(0o077)
        try:
            with patch.object(sys, "argv", ["health_garmin.py", command, *arguments]), \
                    patch.object(h, "RotatingFileHandler", return_value=logging.NullHandler()), \
                    patch.object(h.logging, "basicConfig"), \
                    patch.object(h.LOG, "error"), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = h.main()
        finally:
            os.umask(previous_umask)
        return result, output.getvalue(), errors.getvalue()

    def test_invalid_enabled_stops_run_before_api_or_processing(self):
        self.save(dict(self.cfg, enabled="false"))
        with patch.object(h, "api_client") as api, patch.object(h, "run_sync") as sync:
            result, _, errors = self.invoke("run")
        self.assertEqual(result, 2)
        self.assertIn("enabled", errors)
        api.assert_not_called()
        sync.assert_not_called()

    def test_busy_scheduled_run_skips_successfully(self):
        with (self.state / "run.lock").open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(h, "run_sync") as sync:
                result, output, errors = self.invoke("run")
        self.assertEqual(result, 0)
        self.assertIn("另一个进程", output)
        self.assertEqual(errors, "")
        sync.assert_not_called()

    def test_busy_explicit_commands_fail_without_changing_config(self):
        self.save(dict(self.cfg, enabled=True))
        before = self.config_path.read_bytes()
        with (self.state / "run.lock").open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for command in ("disable", "enable", "settings", "status", "uninstall-agent"):
                with self.subTest(command=command):
                    result, _, errors = self.invoke(command)
                    self.assertEqual(result, 2)
                    self.assertIn(command + " 未执行", errors)
                    self.assertIn("重试", errors)
                    self.assertEqual(self.config_path.read_bytes(), before)

    def test_disable_when_idle_updates_only_temporary_config(self):
        self.save(dict(self.cfg, enabled=True))
        with patch.object(h, "api_client") as api:
            result, output, errors = self.invoke("disable")
        self.assertEqual(result, 0)
        self.assertEqual(errors, "")
        self.assertIn("自动写入已关闭", output)
        self.assertIs(json.loads(self.config_path.read_text())["enabled"], False)
        api.assert_not_called()

    def test_rejected_cli_settings_allow_valid_settings_and_disable(self):
        for option, value, field in (("--source", "   ", "allowed_sources"),
                                     ("--source", "", "allowed_sources"),
                                     ("--inbox", "", "inbox")):
            with self.subTest(option=option, value=value):
                self.save(self.cfg)
                stored = self.config_path.read_bytes()
                with patch.object(h, "api_client") as api:
                    result, _, errors = self.invoke("settings", option, value)
                    self.assertEqual(result, 2)
                    self.assertIn(field, errors)
                    self.assertEqual(self.config_path.read_bytes(), stored)
                    self.assertEqual(h.config(), self.cfg)
                    result, _, errors = self.invoke("settings", "--source", "NewSource")
                    self.assertEqual(result, 0)
                    self.assertEqual(errors, "")
                    self.assertEqual(h.config()["allowed_sources"], ["NewSource"])
                    result, _, errors = self.invoke("disable")
                    self.assertEqual(result, 0)
                    self.assertEqual(errors, "")
                    self.assertIs(h.config()["enabled"], False)
                    api.assert_not_called()


class LoggingOwnershipTests(ConfigFixture):
    def setUp(self):
        super().setUp()
        self.root_logger = logging.getLogger()
        self.previous_handlers = list(self.root_logger.handlers)
        self.previous_level = self.root_logger.level
        self.root_logger.handlers = []
        self.addCleanup(self.restore_logging)

    def restore_logging(self):
        for handler in self.root_logger.handlers:
            if handler not in self.previous_handlers:
                handler.close()
        self.root_logger.handlers = self.previous_handlers
        self.root_logger.setLevel(self.previous_level)

    def invoke(self):
        errors = io.StringIO()
        previous_umask = os.umask(0o077)
        try:
            with patch.object(sys, "argv", ["health_garmin.py", "run"]), \
                    contextlib.redirect_stderr(errors):
                result = h.main()
        finally:
            os.umask(previous_umask)
        return result, errors.getvalue()

    def test_repeated_commands_close_real_owned_handlers(self):
        handlers = []
        factory = h.RotatingFileHandler

        def create(*args, **kwargs):
            handler = factory(*args, **kwargs)
            handlers.append(handler)
            return handler

        with patch.object(h, "RotatingFileHandler", side_effect=create), \
                patch.object(h, "run_with_attempt"):
            for _ in range(3):
                self.assertEqual(self.invoke(), (0, ""))
                self.assertEqual(self.root_logger.handlers, [])
        self.assertEqual(len(handlers), 3)
        self.assertTrue(all(handler.stream is None for handler in handlers))

    def test_existing_handler_survives_basic_config_ignored_owned_handler(self):
        existing = logging.FileHandler(self.root / "caller.log")
        self.addCleanup(existing.close)
        self.root_logger.addHandler(existing)
        factory = h.RotatingFileHandler
        owned = factory(self.state / "owned.log")
        with patch.object(h, "RotatingFileHandler", return_value=owned), \
                patch.object(h, "run_with_attempt"):
            self.assertEqual(self.invoke(), (0, ""))
        self.assertEqual(self.root_logger.handlers, [existing])
        self.assertFalse(existing.stream.closed)
        self.assertIsNone(owned.stream)
        existing.emit(logging.LogRecord("caller", logging.INFO, "", 0, "still open", (), None))
        self.assertIn("still open", (self.root / "caller.log").read_text())

    def test_logging_initialization_failure_is_safe_and_closes_owned_handler(self):
        owned = h.RotatingFileHandler(self.state / "owned.log")
        with patch.object(h, "RotatingFileHandler", return_value=owned), \
                patch.object(h.logging, "basicConfig", side_effect=OSError("private-secret")), \
                patch.object(h.LOG, "error"), patch.object(h, "run_with_attempt") as run:
            result, errors = self.invoke()
        self.assertEqual(result, 2)
        self.assertIn("OSError", errors)
        self.assertNotIn("private-secret", errors)
        self.assertIsNone(owned.stream)
        run.assert_not_called()
        self.assertEqual(self.root_logger.handlers, [])

    def test_close_failure_preserves_error_and_interruption_results(self):
        for exc, expected in ((h.SafeError("controlled"), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(expected=expected):
                owned = h.RotatingFileHandler(self.state / "owned.log")
                close = owned.close

                def failing_close():
                    close()
                    raise OSError("private-secret")

                with patch.object(h, "RotatingFileHandler", return_value=owned), \
                        patch.object(owned, "close", side_effect=failing_close), \
                        patch.object(h, "run_with_attempt", side_effect=exc):
                    result, errors = self.invoke()
                self.assertEqual(result, expected)
                self.assertIn("日志关闭失败： OSError", errors)
                self.assertNotIn("private-secret", errors)
                self.assertIsNone(owned.stream)
                self.assertEqual(self.root_logger.handlers, [])


if __name__ == "__main__":
    unittest.main()
