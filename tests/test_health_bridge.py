"""Exercise reverse export commands and scheduling with isolated local fixtures."""

import contextlib
import io
import json
import logging
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from project_fixtures import copy_project
from test_health_outbox import DAY, FakeAPI

from health_garmin_sync import cli as hg_cli
from health_garmin_sync import domain as hg_domain
from health_garmin_sync import file_io as hg_file_io
from health_garmin_sync import garmin as hg_garmin
from health_garmin_sync import health_bridge as hg_bridge
from health_garmin_sync import health_outbox as hg_outbox
from health_garmin_sync import launchagent as hg_launchagent
from health_garmin_sync import runtime as hg_runtime
from health_garmin_sync import state as hg_state
from health_garmin_sync import sync as hg_sync


class HealthBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "project"
        self.base.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.outbox = self.root / "outbox"
        self.config_path = self.base / "config.json"
        for key, value in (
            ("BASE", self.base),
            ("STATE", self.state),
            ("CONFIG", self.config_path),
        ):
            self.enterContext(patch.object(hg_runtime, key, value))
        self.enterContext(patch.object(hg_bridge, "china_today", return_value=DAY))
        self.cfg = {
            "is_cn": True,
            "enabled": False,
            "inbox": str(self.inbox),
            "since": DAY.isoformat(),
            "allowed_sources": ["PICOOC"],
            "weight_bare_unit": "kg",
            "fat_bare_unit": "percent",
            "pair_window_seconds": 0,
            "settle_seconds": 0,
            "max_records_per_run": 10,
        }
        self.save_config()
        self.api = FakeAPI()
        self.client = self.enterContext(
            patch.object(hg_garmin, "api_client", return_value=self.api)
        )

    def save_config(self):
        hg_file_io.write_json(self.config_path, self.cfg)

    def read_json(self, path):
        return json.loads(path.read_text())

    def invoke(self, *arguments, answer=""):
        output, errors = io.StringIO(), io.StringIO()
        old_umask = os.umask(0o077)
        try:
            with (
                patch.object(sys, "argv", ["health-garmin-sync", *arguments]),
                patch("builtins.input", return_value=answer),
                patch.object(
                    hg_file_io, "PrivateRotatingFileHandler", return_value=logging.NullHandler()
                ),
                patch.object(logging, "basicConfig"),
                patch.object(hg_runtime.LOG, "error"),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                result = hg_cli.main()
        finally:
            os.umask(old_umask)
        return result, output.getvalue(), errors.getvalue()

    def configure_health(self, *, enabled=True):
        self.cfg.update(health_export_enabled=enabled, health_outbox=str(self.outbox))
        self.save_config()

    def test_export_command_publishes_real_outbox_and_records_summary(self):
        result, _, errors = self.invoke(
            "export-health",
            "--outbox",
            str(self.outbox),
            "--since",
            DAY.isoformat(),
            "--until",
            DAY.isoformat(),
            "--batch-size",
            "1",
        )
        self.assertEqual((result, errors), (0, ""))
        self.client.assert_called_once_with()
        self.assertEqual(self.api.calls, [DAY.isoformat()])
        pending = self.read_json(self.outbox / "pending.json")
        self.assertEqual(len(pending["records"]), 1)
        summary = self.read_json(self.state / "last_health_export.json")
        self.assertEqual(summary["since"], DAY.isoformat())
        self.assertEqual(summary["until"], DAY.isoformat())
        self.assertEqual(summary["results"]["published_batches"], 1)
        self.assertFalse(self.read_json(self.config_path)["enabled"])

    def test_export_command_rejects_invalid_dates_and_sizes_before_sdk(self):
        cases = (
            ("--since", "2026-02-30"),
            ("--until", "yesterday"),
            ("--until", "2026-10-16"),
            ("--since", "2026-10-15", "--until", "2026-10-14"),
            ("--batch-size", "0"),
            ("--batch-size", "101"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result, _, errors = self.invoke(
                    "export-health", "--outbox", str(self.outbox), *arguments
                )
                self.assertEqual(result, 2)
                self.assertTrue(errors)
        self.client.assert_not_called()
        self.assertFalse(self.outbox.exists())
        self.assertFalse((self.state / "last_health_export.json").exists())

    def test_export_rejects_protected_directory_overlap_in_both_directions(self):
        for path in (
            self.inbox,
            self.inbox / "nested",
            self.root,
            self.state,
            self.state / "nested",
            self.base,
        ):
            with self.subTest(path=path):
                result, _, errors = self.invoke("export-health", "--outbox", str(path))
                self.assertEqual(result, 2)
                self.assertIn("独立", errors)
        self.client.assert_not_called()

    def test_export_rejects_non_directory_and_symlink(self):
        regular = self.root / "file"
        regular.write_text("preserve")
        link = self.root / "link"
        link.symlink_to(self.inbox, target_is_directory=True)
        for path in (regular, link):
            with self.subTest(path=path):
                self.assertEqual(self.invoke("export-health", "--outbox", str(path))[0], 2)
        self.assertEqual(regular.read_text(), "preserve")
        self.client.assert_not_called()

    def test_settings_sets_absolute_health_path_and_off_clears_it(self):
        result, _, errors = self.invoke("settings", "--health-outbox", str(self.outbox))
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(self.read_json(self.config_path)["health_outbox"], str(self.outbox))
        self.assertEqual(self.invoke("settings", "--health-outbox", "off")[0], 0)
        self.assertIsNone(self.read_json(self.config_path)["health_outbox"])
        self.client.assert_not_called()

    def test_settings_requires_disabling_either_automatic_direction(self):
        for weight_enabled, health_enabled in ((True, False), (False, True)):
            self.cfg.update(enabled=weight_enabled)
            self.configure_health(enabled=health_enabled)
            original = self.config_path.read_bytes()
            for value in (str(self.root / "replacement"), "off"):
                with self.subTest(weights=weight_enabled, health=health_enabled, value=value):
                    result, _, errors = self.invoke("settings", "--health-outbox", value)
                    self.assertEqual(result, 2)
                    self.assertIn("disable", errors)
                    self.assertEqual(self.config_path.read_bytes(), original)

    def test_settings_protected_outbox_does_not_modify_config(self):
        original = self.config_path.read_bytes()
        for path in (self.inbox, self.state / "health"):
            with self.subTest(path=path):
                self.assertEqual(self.invoke("settings", "--health-outbox", str(path))[0], 2)
                self.assertEqual(self.config_path.read_bytes(), original)
        self.client.assert_not_called()

    def test_enable_health_requires_configured_directory(self):
        result, _, errors = self.invoke("enable-health", answer="ENABLE HEALTH")
        self.assertEqual(result, 2)
        self.assertIn("--health-outbox", errors)
        self.client.assert_not_called()

    def test_enable_health_requires_exact_confirmation_without_weight_verification(self):
        self.configure_health(enabled=False)
        with patch.object(
            hg_state, "database", side_effect=AssertionError("must not require verified weights")
        ):
            for answer in ("", "ENABLE", "enable health"):
                with self.subTest(answer=answer):
                    self.assertEqual(self.invoke("enable-health", answer=answer)[0], 0)
                    self.assertFalse(self.read_json(self.config_path)["health_export_enabled"])
            self.assertEqual(self.invoke("enable-health", answer="ENABLE HEALTH")[0], 0)
        saved = self.read_json(self.config_path)
        self.assertTrue(saved["health_export_enabled"])
        self.assertFalse(saved["enabled"])
        self.client.assert_not_called()

    def test_disable_health_preserves_weights_and_disable_stops_both(self):
        self.cfg["enabled"] = True
        self.configure_health()
        self.assertEqual(self.invoke("disable-health")[0], 0)
        saved = self.read_json(self.config_path)
        self.assertTrue(saved["enabled"])
        self.assertFalse(saved["health_export_enabled"])
        self.configure_health()
        self.assertEqual(self.invoke("disable")[0], 0)
        saved = self.read_json(self.config_path)
        self.assertFalse(saved["enabled"])
        self.assertFalse(saved["health_export_enabled"])

    def test_config_health_flag_is_boolean_and_enabled_requires_path(self):
        for value in (0, 1, "false", None, []):
            with self.subTest(value=value), self.assertRaises(hg_domain.SafeError):
                hg_runtime.validate_config({**self.cfg, "health_export_enabled": value})
        with self.assertRaises(hg_domain.SafeError):
            hg_runtime.validate_config({**self.cfg, "health_export_enabled": True})
        for value in (False, True):
            hg_runtime.validate_config(
                {**self.cfg, "health_export_enabled": value, "health_outbox": str(self.outbox)}
            )

    def test_config_health_path_requires_absolute_text_or_null(self):
        for value in ("relative", "", " ", 4, True, []):
            with self.subTest(value=value), self.assertRaises(hg_domain.SafeError):
                hg_runtime.validate_config({**self.cfg, "health_outbox": value})
        for value in (None, str(self.outbox)):
            hg_runtime.validate_config({**self.cfg, "health_outbox": value})

    def test_invalid_health_config_stops_real_run_before_sdk(self):
        cases = (
            {"health_export_enabled": "false"},
            {"health_export_enabled": True, "health_outbox": None},
            {"health_export_enabled": True, "health_outbox": "relative"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                hg_file_io.write_json(self.config_path, {**self.cfg, **fields})
                self.assertEqual(self.invoke("run")[0], 2)
                attempt = self.read_json(self.state / "last_attempt.json")
                self.assertEqual(attempt["outcome"], "failed")
                self.assertEqual(attempt["stage"], "config")
        self.client.assert_not_called()

    def test_legacy_default_run_never_logs_in_or_exports(self):
        hg_file_io.write_json(
            self.inbox / "weights.json",
            {
                "schema_version": 2,
                "exported_at": "2026-10-15T00:00:00+08:00",
                "metrics": {"weight": []},
            },
        )
        with patch.object(
            hg_outbox,
            "export_blood_pressure",
            side_effect=AssertionError("unexpected reverse export"),
        ):
            result, _, errors = self.invoke("run")
        self.assertEqual((result, errors), (0, ""))
        self.client.assert_not_called()
        self.assertEqual(self.read_json(self.state / "last_attempt.json")["outcome"], "preview")

    def test_health_only_run_reads_api_and_completes_without_enabling_weights(self):
        self.configure_health()
        result, _, errors = self.invoke("run")
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(self.api.calls, [DAY.isoformat()])
        self.assertTrue((self.outbox / "pending.json").exists())
        attempt = self.read_json(self.state / "last_attempt.json")
        self.assertEqual(attempt["outcome"], "completed")
        self.assertIsNotNone(attempt["finished_at"])
        self.assertFalse(self.read_json(self.config_path)["enabled"])

    def test_reverse_failure_marks_attempt_failed_and_preserves_weight_summary(self):
        self.cfg["enabled"] = True
        self.configure_health()
        weight_summary = {"results": {"uploaded": 1}, "run_at": "2026-10-15T00:00:00+00:00"}

        def weights(cfg):
            self.assertTrue(cfg["enabled"])
            hg_file_io.write_json(self.state / "last_run.json", weight_summary)

        self.api.responses[DAY.isoformat()] = RuntimeError("synthetic Garmin read failure")
        with patch.object(hg_sync, "run_sync", side_effect=weights):
            result, _, errors = self.invoke("run")
        self.assertEqual(result, 2)
        self.assertTrue(errors)
        self.assertEqual(self.api.calls, [DAY.isoformat()])
        self.assertEqual(self.read_json(self.state / "last_run.json"), weight_summary)
        attempt = self.read_json(self.state / "last_attempt.json")
        self.assertEqual(attempt["outcome"], "failed")
        self.assertIsNotNone(attempt["error_type"])
        self.assertFalse((self.state / "last_health_export.json").exists())

    def test_automatic_export_uses_existing_outbox_recommended_window(self):
        self.configure_health()
        self.cfg["since"] = "2020-01-01"
        self.save_config()
        with patch.object(hg_outbox, "recommended_since", return_value=DAY) as recommended:
            self.assertEqual(self.invoke("run")[0], 0)
        recommended.assert_called_once_with(date(2020, 1, 1), DAY)
        self.assertEqual(self.api.calls, [DAY.isoformat()])

    def test_health_only_install_agent_writes_plist_with_mocked_service(self):
        self.configure_health()
        home = self.root / "home"
        home.mkdir()
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(Path, "home", return_value=home),
            patch.object(hg_launchagent, "agent_service_command", return_value=1) as service,
        ):
            result, _, errors = self.invoke("install-agent")
        self.assertEqual((result, errors), (0, ""))
        plist = home / "Library" / "LaunchAgents" / f"{hg_runtime.LABEL}.plist"
        document = plistlib.loads(plist.read_bytes())
        self.assertEqual(document["ProgramArguments"][-1], "health_garmin_sync.background")
        self.assertEqual(document["StartInterval"], 1800)
        self.assertTrue(document["RunAtLoad"])
        self.assertEqual([call.args[0] for call in service.call_args_list], ["print", "bootstrap"])
        self.client.assert_not_called()

    def test_python_module_entry_runs_export_with_isolated_fake_sdk(self):
        bootstrap = """from datetime import date
from health_garmin_sync import garmin, health_bridge
class API:
    profile_id = 123456
    def get_blood_pressure(self, day):
        assert day == '2026-10-15'
        return {'measurementSummaries': []}
garmin.api_client = lambda: API()
health_bridge.china_today = lambda: date(2026, 10, 15)
"""
        copy_project(self.base, bootstrap)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "health_garmin_sync",
                "export-health",
                "--outbox",
                str(self.outbox),
                "--since",
                DAY.isoformat(),
                "--until",
                DAY.isoformat(),
                "--batch-size",
                "1",
            ],
            cwd=self.base,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Garmin 血压导出", result.stdout)
        self.assertEqual(
            self.read_json(self.state / "last_health_export.json")["results"]["fetched_days"], 1
        )


if __name__ == "__main__":
    unittest.main()
