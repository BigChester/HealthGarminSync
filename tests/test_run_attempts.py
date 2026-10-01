"""Run evidence is serialized by the existing lock and never authorizes retries."""
import contextlib
from datetime import timedelta
import fcntl
import io
import json
import logging
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import health_garmin as h
import test_diagnostics as fixtures
from test_reconciliation import API

SECRET = fixtures.SECRET


class RunAttemptTests(unittest.TestCase):
    setUp = fixtures.DoctorTests.setUp
    save_config = fixtures.DoctorTests.save_config
    snapshot = fixtures.DoctorTests.snapshot
    invoke = fixtures.DoctorTests.invoke
    report = fixtures.DoctorTests.report

    def run_main(self, command='run'):
        output, errors = io.StringIO(), io.StringIO()
        old_umask = os.umask(0o077)
        try:
            with patch.object(sys, 'argv', ['health_garmin.py', command]), \
                    patch.object(h, 'RotatingFileHandler', return_value=logging.NullHandler()), \
                    patch.object(h.logging, 'basicConfig'), patch.object(h.LOG, 'error'), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = h.main()
        finally:
            os.umask(old_umask)
        return result, output.getvalue(), errors.getvalue()

    def attempt(self):
        value = json.loads((self.state / 'last_attempt.json').read_text())
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertEqual(set(value), {'outcome', 'stage', 'started_at', 'finished_at', 'error_type'})
        self.assertGreaterEqual(h.aware(value['finished_at']), h.aware(value['started_at']))
        return value

    def completed_marker(self):
        stamp = h.now().isoformat()
        return {'outcome': 'completed', 'stage': 'sync', 'started_at': stamp,
                'finished_at': stamp, 'error_type': None}

    def test_login_failure_replaces_attempt_but_preserves_prior_last_run(self):
        prior = self.last.read_bytes()
        with patch.object(h, 'api_client', side_effect=TimeoutError(SECRET + ' HTTP 503')):
            result, _, errors = self.run_main()
        self.assertEqual(result, 2)
        self.assertNotIn(SECRET, errors)
        attempt = self.attempt()
        self.assertEqual((attempt['outcome'], attempt['stage'], attempt['error_type']),
                         ('failed', 'sync', 'TimeoutError HTTP 503'))
        self.assertEqual(self.last.read_bytes(), prior)
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks['last_attempt']['status'], 'error')
        self.assertNotIn('error_type', checks['last_attempt']['data'])
        self.assertEqual(checks['last_run']['data']['results'], {'verified': 1})

    def test_configuration_and_export_failures_record_correct_stage_before_api(self):
        prior = self.last.read_bytes()
        for config_failure in (True, False):
            with self.subTest(config_failure=config_failure):
                self.save_config()
                if config_failure:
                    self.config_path.write_text(json.dumps(dict(self.cfg, enabled='false')))
                else:
                    self.export.write_text('{ invalid ' + SECRET)
                with patch.object(h, 'api_client') as api:
                    result, _, _ = self.run_main()
                self.assertEqual(result, 2)
                value = self.attempt()
                self.assertEqual(value['outcome'], 'failed')
                self.assertEqual(value['stage'], 'config' if config_failure else 'sync')
                self.assertEqual(value['error_type'], 'SafeError')
                self.assertEqual(self.last.read_bytes(), prior)
                api.assert_not_called()
                self.assertEqual(self.report()[0], 2)

    def test_comparison_failure_keeps_previous_last_run_and_makes_no_remote_write(self):
        prior = self.last.read_bytes()
        api = API()
        api.fail_chunk = 1
        with patch.object(h, 'api_client', return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 2)
        self.assertEqual(self.attempt()['outcome'], 'failed')
        self.assertEqual(self.last.read_bytes(), prior)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_future_export_stops_before_api_and_recovers_after_corrected_metadata(self):
        current = h.now()
        previous = self.last.read_bytes()
        data = json.loads(self.export.read_text())
        data['exported_at'] = (current + timedelta(days=1)).isoformat()
        self.export.write_text(json.dumps(data))
        source = self.export.read_bytes()
        with patch.object(h, 'now', return_value=current), patch.object(h, 'api_client') as api:
            result, _, errors = self.run_main()
        self.assertEqual(result, 2)
        self.assertIn('未来导出时间', errors)
        self.assertEqual(self.attempt()['outcome'], 'failed')
        self.assertEqual(self.attempt()['stage'], 'sync')
        self.assertEqual(self.last.read_bytes(), previous)
        self.assertEqual(self.export.read_bytes(), source)
        api.assert_not_called()
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks['inbox']['status'], 'error')
        self.assertEqual(checks['last_attempt']['status'], 'error')
        data['exported_at'] = (current - timedelta(minutes=3)).isoformat()
        self.export.write_text(json.dumps(data))
        source = self.export.read_bytes()
        api = API()
        with patch.object(h, 'api_client', return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(self.attempt()['outcome'], 'completed')
        self.assertEqual(self.export.read_bytes(), source)
        self.assertEqual(self.report()[2]['inbox']['status'], 'ok')

    def test_interrupted_post_keeps_inflight_and_subsequent_run_never_reposts(self):
        class InterruptedAPI(API):
            def add_body_composition(self, **kwargs):
                super().add_body_composition(**kwargs)
                raise KeyboardInterrupt()
        api = InterruptedAPI()
        api.visible = False
        prior = self.last.read_bytes()
        with patch.object(h, 'api_client', return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 130)
        value = self.attempt()
        self.assertEqual(value['outcome'], 'interrupted')
        self.assertEqual(value['error_type'], 'KeyboardInterrupt')
        with contextlib.closing(h.database()) as db:
            self.assertEqual(db.execute('SELECT status FROM uploads').fetchone()[0], 'inflight')
        self.assertEqual(self.last.read_bytes(), prior)
        self.assertEqual(self.report()[0], 2)
        with patch.object(h, 'api_client', return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(self.attempt()['outcome'], 'completed')
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertIn('不代表全部记录已验证', checks['last_attempt']['message'])
        self.assertEqual(checks['last_run']['status'], 'error')
        self.assertEqual(checks['last_run']['data']['results'], {'uncertain': 1})

    def test_disabled_preview_is_separate_and_does_not_replace_last_run(self):
        self.cfg['enabled'] = False
        self.save_config()
        prior = self.last.read_bytes()
        with patch.object(h, 'api_client') as api:
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(self.attempt()['outcome'], 'preview')
        self.assertEqual(self.last.read_bytes(), prior)
        api.assert_not_called()
        self.assertEqual(self.report()[2]['last_attempt']['status'], 'info')

    def test_no_records_and_normal_upload_complete_without_changing_last_run_schema(self):
        with patch.object(h, 'load_measurements', return_value=([], [], [], {})), patch.object(h, 'api_client') as api:
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(self.attempt()['outcome'], 'completed')
        summary = json.loads(self.last.read_text())
        self.assertEqual(set(summary), {'run_at', 'exports', 'complete_measurements', 'waiting_pairs', 'results'})
        self.assertEqual(summary['results'], {})
        api.assert_not_called()
        api = API()
        with patch.object(h, 'api_client', return_value=api):
            result, _, _ = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(self.attempt()['outcome'], 'completed')
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(self.report()[2]['last_run']['data']['results'], {'verified': 1})

    def test_terminal_write_occurs_while_independent_descriptor_cannot_acquire_lock(self):
        writer = h.write_json
        observed = []
        def checked_write(path, value):
            if path == self.state / 'last_attempt.json' and value['outcome'] != 'running':
                with (self.state / 'run.lock').open('r') as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append(value['outcome'])
            return writer(path, value)
        with patch.object(h, 'write_json', side_effect=checked_write), patch.object(h, 'run_sync'):
            self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(observed, ['completed'])

    def test_busy_skip_and_other_commands_preserve_existing_attempt_bytes(self):
        path = self.state / 'last_attempt.json'
        path.write_text(json.dumps(self.completed_marker()))
        prior = path.read_bytes()
        with (self.state / 'run.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(h, 'config') as cfg, patch.object(h, 'run_sync') as sync:
                self.assertEqual(self.run_main()[0], 0)
            cfg.assert_not_called()
            sync.assert_not_called()
        self.assertEqual(path.read_bytes(), prior)
        self.assertEqual(self.run_main('disable')[0], 0)
        self.assertEqual(path.read_bytes(), prior)
        self.report()
        self.assertEqual(path.read_bytes(), prior)

    def test_start_or_sync_stage_persistence_failure_stops_before_sync(self):
        writer = h.write_json
        for failure_stage in ('config', 'sync'):
            with self.subTest(failure_stage=failure_stage):
                def failed_write(path, value):
                    if path == self.state / 'last_attempt.json' and value['outcome'] == 'running' and value['stage'] == failure_stage:
                        raise OSError(SECRET)
                    return writer(path, value)
                with patch.object(h, 'write_json', side_effect=failed_write), patch.object(h, 'run_sync') as sync:
                    result, _, errors = self.run_main()
                self.assertEqual(result, 2)
                self.assertNotIn(SECRET, errors)
                sync.assert_not_called()
                if failure_stage == 'sync':
                    self.assertEqual(self.attempt()['outcome'], 'failed')

    def test_terminal_io_failure_preserves_primary_exception_identity(self):
        writer = h.write_json
        primary = h.SafeError('primary failure')
        def failed_final(path, value):
            if path == self.state / 'last_attempt.json' and value['outcome'] == 'failed':
                raise PermissionError(SECRET)
            return writer(path, value)
        errors = io.StringIO()
        with (self.state / 'run.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(h, 'write_json', side_effect=failed_final), \
                    patch.object(h, 'run_sync', side_effect=primary), patch.object(h.LOG, 'error'), \
                    contextlib.redirect_stderr(errors):
                with self.assertRaises(h.SafeError) as raised:
                    h.run_with_attempt()
        self.assertIs(raised.exception, primary)
        self.assertIn('PermissionError', errors.getvalue())
        self.assertNotIn(SECRET, errors.getvalue())

    def test_terminal_io_failure_does_not_replace_keyboard_interrupt_exit(self):
        writer = h.write_json
        def failed_final(path, value):
            if path == self.state / 'last_attempt.json' and value['outcome'] == 'interrupted':
                raise PermissionError(SECRET)
            return writer(path, value)
        with patch.object(h, 'write_json', side_effect=failed_final), patch.object(h, 'run_sync', side_effect=KeyboardInterrupt()):
            result, _, errors = self.run_main()
        self.assertEqual(result, 130)
        self.assertIn('PermissionError', errors)
        self.assertNotIn(SECRET, errors)
        code, _, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(checks['last_attempt']['status'], 'warn')

    def test_successful_body_with_terminal_write_failure_returns_error_and_unfinished_evidence(self):
        writer = h.write_json
        def failed_final(path, value):
            if path == self.state / 'last_attempt.json' and value['outcome'] == 'completed':
                raise OSError(SECRET)
            return writer(path, value)
        with patch.object(h, 'write_json', side_effect=failed_final), patch.object(h, 'run_sync') as sync:
            result, _, errors = self.run_main()
        self.assertEqual(result, 2)
        self.assertNotIn(SECRET, errors)
        sync.assert_called_once()
        self.assertEqual(self.report()[2]['last_attempt']['status'], 'warn')

    def test_running_evidence_only_warns_without_asserting_process_liveness(self):
        marker = self.completed_marker()
        marker.update(outcome='running', finished_at=None, started_at=(h.now() - timedelta(days=1)).isoformat())
        (self.state / 'last_attempt.json').write_text(json.dumps(marker))
        code, _, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(checks['last_attempt']['status'], 'warn')
        self.assertIn('可能仍在运行', checks['last_attempt']['message'])

    def test_killed_child_leaves_unfinished_evidence_and_releases_run_lock(self):
        code = '''
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import threading
import health_garmin as h
h.BASE, h.STATE, h.CONFIG = map(Path, sys.argv[2:5])
def block(cfg):
    threading.Event().wait(60)
def forbid_api(*args, **kwargs):
    raise AssertionError("API forbidden")
h.run_sync = block
h.api_client = forbid_api
sys.argv = ["health_garmin.py", "run"]
raise SystemExit(h.main())
'''
        prior = self.last.read_bytes()
        process = subprocess.Popen([sys.executable, '-B', '-c', code, os.path.dirname(h.__file__),
                                    str(self.base), str(self.state), str(self.config_path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marker = self.state / 'last_attempt.json'
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll(), 'child exited before blocking stage')
                try:
                    value = json.loads(marker.read_text())
                except FileNotFoundError:
                    value = {}
                if value.get('outcome') == 'running' and value.get('stage') == 'sync':
                    break
                time.sleep(0.02)
            else:
                self.fail('child did not persist sync stage before timeout')
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
        self.assertIsNone(json.loads(marker.read_text())['finished_at'])
        with (self.state / 'run.lock').open('r') as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks['last_attempt']['status'], 'warn')
        self.assertIn('可能仍在运行或曾被中断', checks['last_attempt']['message'])

    def test_missing_attempt_is_optional_for_existing_install(self):
        code, report, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(checks['last_attempt']['status'], 'info')

    def test_invalid_attempt_fields_values_and_times_are_errors_without_raw_details(self):
        valid = self.completed_marker()
        invalid = [None, {}, dict(valid, outcome=SECRET), dict(valid, stage=[SECRET]),
                   dict(valid, started_at=SECRET), dict(valid, started_at='2020-02-20T08:00:00'),
                   dict(valid, finished_at='2000-01-01T00:00:00+00:00'),
                   dict(valid, outcome='running'), dict(valid, error_type=SECRET),
                   dict(valid, outcome='failed', error_type={SECRET: SECRET}),
                   dict(valid, outcome='preview', stage='config'), dict(valid, pid=SECRET)]
        for value in invalid:
            with self.subTest(value=value):
                (self.state / 'last_attempt.json').write_text(json.dumps(value))
                code, _, checks = self.report()
                self.assertEqual(code, 2)
                self.assertEqual(checks['last_attempt']['status'], 'error')


if __name__ == '__main__':
    unittest.main()
