"""Background entry evidence using private fixtures; no real service or API."""
import contextlib
from datetime import timedelta
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import agent_runner as r
import diagnostics as d
import health_garmin as h
import test_diagnostics as fixtures
from test_reconciliation import API


class AgentAttemptTests(unittest.TestCase):
    setUp = fixtures.DoctorTests.setUp
    save_config = fixtures.DoctorTests.save_config
    snapshot = fixtures.DoctorTests.snapshot
    invoke = fixtures.DoctorTests.invoke
    report = fixtures.DoctorTests.report

    @property
    def marker(self):
        return self.state / 'last_agent_attempt.json'

    def completed(self):
        stamp = h.now().isoformat()
        return {'outcome': 'completed', 'started_at': stamp, 'finished_at': stamp,
                'stage': 'run', 'error_type': None}

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
        self.assertEqual(set(value), {'outcome', 'started_at', 'finished_at', 'stage', 'error_type'})
        self.assertNotIn(fixtures.SECRET, json.dumps(value))
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        if value['finished_at'] is not None:
            self.assertGreaterEqual(h.aware(value['finished_at']), h.aware(value['started_at']))
        return value

    def test_setup_failure_replaces_old_success_and_doctor_reports_error(self):
        self.marker.write_text(json.dumps(self.completed()))
        old_last = self.last.read_bytes()
        old_run = self.state / 'last_attempt.json'
        run_stamp = h.now().isoformat()
        old_run.write_text(json.dumps({'outcome': 'completed', 'started_at': run_stamp,
                                     'finished_at': run_stamp, 'stage': 'sync', 'error_type': None}))
        old_run_bytes = old_run.read_bytes()
        with patch.object(r, 'BoundedStream', side_effect=OSError(fixtures.SECRET)), \
                patch.object(h, 'main') as main:
            self.assertEqual(self.run_agent()[0], 2)
        main.assert_not_called()
        self.assertEqual(self.value()['outcome'], 'failed')
        self.assertEqual(self.value()['stage'], 'setup')
        self.assertEqual(self.last.read_bytes(), old_last)
        self.assertEqual(old_run.read_bytes(), old_run_bytes)
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['last_agent_attempt']['status'], 'error')
        self.assertNotIn('error_type', checks['last_agent_attempt']['data'])

    def test_stage_evidence_failure_does_not_call_main(self):
        original = h.write_json

        def fail_stage(path, value):
            if path == self.marker and value['outcome'] == 'running' and value['stage'] == 'run':
                raise OSError(fixtures.SECRET)
            return original(path, value)

        with patch.object(h, 'write_json', side_effect=fail_stage), patch.object(h, 'main') as main:
            self.assertEqual(self.run_agent()[0], 2)
        main.assert_not_called()
        self.assertEqual(self.value()['outcome'], 'failed')
        self.assertEqual(self.value()['stage'], 'setup')
        self.assertEqual(self.report()[0], 2)

    def test_setup_interruption_keeps_primary_and_reports_setup_stage(self):
        primary = KeyboardInterrupt()
        with patch.object(r, 'BoundedStream', side_effect=primary), patch.object(h, 'main') as main, \
                self.assertRaises(KeyboardInterrupt) as raised:
            self.run_agent(script=False)
        self.assertIs(raised.exception, primary)
        main.assert_not_called()
        self.assertEqual((self.value()['outcome'], self.value()['stage']), ('interrupted', 'setup'))
        self.assertEqual(self.report()[0], 2)

    def test_completed_is_only_entry_completion(self):
        old_last = self.last.read_bytes()
        with patch.object(h, 'main', return_value=0):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(self.value()['outcome'], 'completed')
        self.assertEqual(self.value()['stage'], 'run')
        self.assertEqual(self.last.read_bytes(), old_last)
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks['last_agent_attempt']['status'], 'info')
        self.assertIn('入口', checks['last_agent_attempt']['message'])
        self.assertIn('不代表', checks['last_agent_attempt']['message'])

    def test_disabled_preview_completes_entry_without_accessing_garmin(self):
        self.cfg['enabled'] = False
        self.save_config()
        old_last = self.last.read_bytes()
        with patch.object(h, 'api_client') as api:
            self.assertEqual(self.run_agent()[0], 0)
        api.assert_not_called()
        self.assertEqual(self.value()['outcome'], 'completed')
        self.assertEqual(json.loads((self.state / 'last_attempt.json').read_text())['outcome'], 'preview')
        self.assertEqual(self.last.read_bytes(), old_last)

    def test_post_interruption_and_next_read_preserve_inflight_without_repeating_post(self):
        class InterruptedAPI(API):
            def add_body_composition(self, **kwargs):
                super().add_body_composition(**kwargs)
                raise KeyboardInterrupt()

        api = InterruptedAPI()
        api.visible = False
        with patch.object(h, 'api_client', return_value=api):
            self.assertEqual(self.run_agent()[0], 130)
        self.assertEqual(self.value()['outcome'], 'interrupted')
        self.assertEqual(json.loads((self.state / 'last_attempt.json').read_text())['outcome'], 'interrupted')
        with contextlib.closing(h.database()) as db:
            self.assertEqual(db.execute('SELECT status FROM uploads').fetchone()[0], 'inflight')
        self.assertEqual(len(api.posts), 1)
        with patch.object(h, 'api_client', return_value=api):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(self.value()['outcome'], 'completed')
        self.assertEqual(len(api.posts), 1)
        self.assertFalse(api.deletes)
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks['last_agent_attempt']['status'], 'info')
        self.assertEqual(checks['last_run']['status'], 'error')
        self.assertEqual(checks['last_run']['data']['results'], {'uncertain': 1})

    def test_missing_legacy_marker_warns_only_when_enabled(self):
        self.marker.unlink(missing_ok=True)
        result, report, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(report['status'], 'warning')
        self.assertEqual(checks['last_agent_attempt']['status'], 'warn')
        self.cfg['enabled'] = False
        self.save_config()
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks['last_agent_attempt']['status'], 'info')

    def test_busy_log_lock_preserves_entry_and_run_evidence(self):
        self.marker.write_text(json.dumps(self.completed()))
        prior_entry, prior_last = self.marker.read_bytes(), self.last.read_bytes()
        with (self.state / 'agent-log.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(h, 'main') as main:
                self.assertEqual(self.run_agent()[0], 0)
        main.assert_not_called()
        self.assertEqual(self.marker.read_bytes(), prior_entry)
        self.assertEqual(self.last.read_bytes(), prior_last)

    def test_busy_run_lock_completes_entry_without_overwriting_run_evidence(self):
        old_run = self.state / 'last_attempt.json'
        old_run.write_text('synthetic prior protected bytes')
        prior_last = self.last.read_bytes()
        with (self.state / 'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(h, 'api_client') as api, patch.object(h, 'run_sync') as sync:
                self.assertEqual(self.run_agent()[0], 0)
        api.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(self.value()['outcome'], 'completed')
        self.assertEqual(old_run.read_text(), 'synthetic prior protected bytes')
        self.assertEqual(self.last.read_bytes(), prior_last)
        self.assertIn('另一个进程', (self.state / 'agent.out.log').read_text())

    def test_start_evidence_failure_does_not_call_main_and_preserves_old_marker(self):
        before = self.marker.read_bytes()
        original = h.write_json

        def fail_start(path, value):
            if path == self.marker:
                raise OSError(fixtures.SECRET)
            return original(path, value)

        with patch.object(h, 'write_json', side_effect=fail_start), patch.object(h, 'main') as main:
            code, errors = self.run_agent()
        self.assertEqual(code, 2)
        self.assertIn('开始证据未保存：OSError', errors)
        main.assert_not_called()
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertFalse((self.state / 'agent.out.log').exists())

    def test_start_and_stage_are_persisted_before_their_protected_work(self):
        original = r.BoundedStream
        observed = []

        def stream(*args, **kwargs):
            observed.append(self.value()['stage'])
            return original(*args, **kwargs)

        def command():
            value = self.value()
            self.assertEqual(value['outcome'], 'running')
            self.assertEqual(value['stage'], 'run')
            self.assertIsNone(value['finished_at'])
            return 0

        with patch.object(r, 'BoundedStream', side_effect=stream), patch.object(h, 'main', side_effect=command):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(observed, ['setup', 'setup'])

    def test_stdio_failures_are_evidenced_without_changing_primary_exit_codes(self):
        for failure in ('write', 'flush', 'close'):
            for code in (0, 2, 130):
                with self.subTest(failure=failure, code=code):
                    output_fd = None
                    original_close = os.close
                    failed_close = False

                    def command():
                        nonlocal output_fd
                        output_fd = sys.stdout._fd
                        if failure == 'write':
                            with patch.object(r, 'write_all', side_effect=OSError(fixtures.SECRET)):
                                print('synthetic output')
                        elif failure == 'flush':
                            original_stat = os.fstat
                            def bad_stat(fd):
                                if fd == output_fd:
                                    raise OSError(fixtures.SECRET)
                                return original_stat(fd)
                            with patch.object(r.os, 'fstat', side_effect=bad_stat):
                                sys.stdout.flush()
                        return code

                    def close(fd):
                        nonlocal failed_close
                        original_close(fd)
                        if failure == 'close' and fd == output_fd and not failed_close:
                            failed_close = True
                            raise OSError(fixtures.SECRET)

                    with patch.object(h, 'main', side_effect=command), patch.object(r.os, 'close', side_effect=close):
                        result, _ = self.run_agent()
                    self.assertEqual(result, 2 if code == 0 else code)
                    self.assertEqual(self.value()['outcome'], 'interrupted' if code == 130 else 'failed')
                    self.assertEqual(self.value()['stage'], 'run')
                    self.assertEqual(self.report()[0], 2)
                    self.assertNotIn(fixtures.SECRET, (self.state / 'agent.err.log').read_text())

    def test_primary_exceptions_are_preserved_and_terminal_errors_do_not_replace_them(self):
        for primary in (RuntimeError(fixtures.SECRET), KeyboardInterrupt()):
            for terminal_failure in (False, True):
                with self.subTest(primary=type(primary).__name__, terminal_failure=terminal_failure):
                    original_write = h.write_json

                    def write(path, value):
                        if terminal_failure and path == self.marker and value['outcome'] != 'running':
                            raise OSError(fixtures.SECRET)
                        return original_write(path, value)

                    with patch.object(h, 'write_json', side_effect=write), \
                            patch.object(h, 'main', side_effect=primary), self.assertRaises(type(primary)) as raised:
                        self.run_agent(script=False)
                    self.assertIs(raised.exception, primary)
                    marker = self.value()
                    if terminal_failure:
                        self.assertEqual(marker['outcome'], 'running')
                        self.assertEqual(self.report()[2]['last_agent_attempt']['status'], 'warn')
                    else:
                        self.assertEqual(marker['outcome'], 'interrupted' if isinstance(primary, KeyboardInterrupt) else 'failed')
                        self.assertEqual(marker['error_type'], type(primary).__name__)
                        self.assertEqual(self.report()[0], 2)

    def test_terminal_write_failure_changes_success_only_and_keeps_unfinished_marker(self):
        for result in (0, 2, 130):
            with self.subTest(result=result):
                original = h.write_json

                def write(path, value):
                    if path == self.marker and value['outcome'] != 'running':
                        raise OSError(fixtures.SECRET)
                    return original(path, value)

                with patch.object(h, 'write_json', side_effect=write), patch.object(h, 'main', return_value=result):
                    code, errors = self.run_agent()
                self.assertEqual(code, 2 if result == 0 else result)
                self.assertIn('结束证据未保存：OSError', errors)
                self.assertEqual(self.value()['outcome'], 'running')
                self.assertEqual(self.value()['stage'], 'run')

    def test_terminal_write_occurs_with_log_lock_held_and_output_fds_closed(self):
        owned = []
        original_stream, original_write = r.BoundedStream, h.write_json
        terminals = []

        def stream(*args, **kwargs):
            result = original_stream(*args, **kwargs)
            owned.append(result)
            return result

        def write(path, value):
            if path == self.marker and value['outcome'] != 'running':
                self.assertTrue(all(stream.closed and stream._fd is None for stream in owned))
                with (self.state / 'agent-log.lock').open('a') as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                terminals.append(value['outcome'])
            return original_write(path, value)

        with patch.object(r, 'BoundedStream', side_effect=stream), patch.object(h, 'write_json', side_effect=write), patch.object(h, 'main', return_value=0):
            self.assertEqual(self.run_agent()[0], 0)
        self.assertEqual(terminals, ['completed'])
        with (self.state / 'agent-log.lock').open('a') as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_last_lock_close_failure_does_not_write_after_release(self):
        original_open, original_close = r.regular_fd, os.close
        lock_fd = None
        writes = []
        released = False
        original_write = h.write_json

        def open_fd(path, *args, **kwargs):
            nonlocal lock_fd
            fd = original_open(path, *args, **kwargs)
            if Path(path).name == 'agent-log.lock':
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
            writes.append(value['outcome'])
            return original_write(path, value)

        with patch.object(r, 'regular_fd', side_effect=open_fd), patch.object(r.os, 'close', side_effect=close), \
                patch.object(h, 'write_json', side_effect=write), patch.object(h, 'main', return_value=0):
            result, errors = self.run_agent()
        self.assertEqual(result, 2)
        self.assertIn('OSError', errors)
        self.assertEqual(writes, ['running', 'running', 'completed'])
        self.assertEqual(self.value()['outcome'], 'completed')

    def test_running_marker_only_warns_without_asserting_process_liveness(self):
        value = dict(self.completed(), outcome='running', stage='setup', finished_at=None)
        self.marker.write_text(json.dumps(value))
        result, report, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(report['status'], 'warning')
        check = checks['last_agent_attempt']
        self.assertEqual(check['status'], 'warn')
        self.assertIn('可能仍在运行或曾被中断', check['message'])

    def test_malformed_marker_fields_times_and_combinations_are_controlled_errors(self):
        base = self.completed()
        earlier = (h.aware(base['started_at']) - timedelta(seconds=1)).isoformat()
        cases = [dict(base, extra=fixtures.SECRET), {key: value for key, value in base.items() if key != 'stage'},
                 dict(base, stage='sync'), dict(base, stage='config'), dict(base, stage='setup'),
                 dict(base, stage=[]), dict(base, outcome='preview'), dict(base, outcome=fixtures.SECRET),
                 dict(base, started_at=fixtures.SECRET), dict(base, started_at='2026-10-02T08:00:00'),
                 dict(base, started_at=None), dict(base, finished_at=None), dict(base, finished_at=earlier),
                 dict(base, error_type=fixtures.SECRET), dict(base, outcome='running'),
                 dict(base, outcome='running', finished_at=None, error_type='OSError'),
                 dict(base, outcome='failed', error_type=None), dict(base, outcome='failed', error_type=[]),
                 dict(base, outcome='failed', error_type='OSError HTTP 500'),
                 dict(base, outcome='failed', error_type='private token'), [], None]
        for value in cases:
            with self.subTest(value=value):
                self.marker.write_text(json.dumps(value))
                result, _, checks = self.report()
                self.assertEqual(result, 2)
                self.assertEqual(checks['last_agent_attempt']['status'], 'error')
                self.assertNotIn(fixtures.SECRET, json.dumps(checks['last_agent_attempt']))

    def test_corrupt_symlink_or_unreadable_marker_is_error_without_leaking_contents(self):
        self.marker.write_text('{invalid ' + fixtures.SECRET)
        self.assertEqual(self.report()[0], 2)
        self.marker.unlink()
        outside = self.root / 'outside-marker.json'
        outside.write_text(json.dumps(self.completed()))
        self.marker.symlink_to(outside)
        self.assertEqual(self.report()[0], 2)
        self.marker.unlink()
        self.marker.write_text(json.dumps(self.completed()))
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.marker:
                raise PermissionError(fixtures.SECRET)
            return original(path, *args, **kwargs)

        # invoke snapshots files before/after; collect is the read-only check under test here.
        before = self.snapshot()
        with patch.object(Path, 'read_text', read):
            report = d.collect()
        self.assertEqual(report['status'], 'error')
        self.assertNotIn(fixtures.SECRET, json.dumps(report))
        self.assertEqual(self.snapshot(), before)

    def test_agent_parameters_do_not_relax_existing_run_attempt_validation(self):
        path = self.state / 'last_attempt.json'
        for stage in ('setup', 'run'):
            path.write_text(json.dumps(dict(self.completed(), stage=stage)))
            self.assertEqual(self.report()[2]['last_attempt']['status'], 'error')
        path.write_text(json.dumps(dict(self.completed(), stage='config', outcome='preview')))
        self.assertEqual(self.report()[2]['last_attempt']['status'], 'error')

    def install_temporary_script(self, override):
        source = Path(r.__file__).resolve().parent
        shutil.copyfile(source / 'agent_runner.py', self.entry)
        health = (source / 'health_garmin.py').read_text()
        (self.base / 'health_garmin.py').write_text(health + '\n' + override)

    def test_real_script_setup_failure_replaces_success_without_starting_sync(self):
        self.install_temporary_script('''
def main():
    raise AssertionError("main must not be entered")
def api_client(*args, **kwargs):
    raise AssertionError("network forbidden")
''')
        old_run = self.state / 'last_attempt.json'
        stamp = h.now().isoformat()
        old_run.write_text(json.dumps({'outcome': 'completed', 'stage': 'sync',
                                     'started_at': stamp, 'finished_at': stamp, 'error_type': None}))
        before_run, before_last = old_run.read_bytes(), self.last.read_bytes()
        outside = self.root / 'outside-log'
        outside.write_text(fixtures.SECRET)
        (self.state / 'agent.out.log').symlink_to(outside)
        child = subprocess.Popen([sys.executable, '-B', str(self.entry)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, errors = child.communicate(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        self.assertEqual(child.returncode, 2)
        self.assertNotIn(fixtures.SECRET.encode(), output + errors)
        value = self.value()
        self.assertEqual((value['outcome'], value['stage']), ('failed', 'setup'))
        self.assertEqual(old_run.read_bytes(), before_run)
        self.assertEqual(self.last.read_bytes(), before_last)
        self.assertEqual(outside.read_text(), fixtures.SECRET)
        self.assertEqual(self.report()[2]['last_agent_attempt']['status'], 'error')
        with (self.state / 'agent-log.lock').open('a') as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_real_script_sigkill_keeps_unfinished_evidence_and_releases_locks(self):
        self.install_temporary_script('''
import time
def run_sync(cfg, selected_id=None):
    print("offline synthetic sync entered", flush=True)
    time.sleep(60)
def api_client(*args, **kwargs):
    raise AssertionError("network forbidden")
''')
        old_last = self.last.read_bytes()
        child = subprocess.Popen([sys.executable, '-B', str(self.entry)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            run_marker = self.state / 'last_attempt.json'
            while time.monotonic() < deadline:
                if run_marker.exists() and (self.state / 'agent.out.log').exists():
                    run = json.loads(run_marker.read_text())
                    if run['stage'] == 'sync' and 'offline synthetic sync entered' in (self.state / 'agent.out.log').read_text():
                        break
                if child.poll() is not None:
                    self.fail(f'temporary runner exited: {child.returncode}')
                time.sleep(0.01)
            else:
                self.fail('temporary runner did not reach synthetic sync')
            entry_bytes, run_bytes = self.marker.read_bytes(), run_marker.read_bytes()
            self.assertEqual(self.value()['outcome'], 'running')
            self.assertEqual(self.value()['stage'], 'run')
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(self.marker.read_bytes(), entry_bytes)
            self.assertEqual(run_marker.read_bytes(), run_bytes)
            self.assertEqual(self.last.read_bytes(), old_last)
            self.assertIsNone(self.value()['finished_at'])
            for name in ('agent-log.lock', 'run.lock'):
                with (self.state / name).open('a') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, _, checks = self.report()
            self.assertEqual(result, 0)
            self.assertEqual(checks['last_agent_attempt']['status'], 'warn')
            self.assertEqual(checks['last_attempt']['status'], 'warn')
            self.assertIn('可能仍在运行或曾被中断', checks['last_agent_attempt']['message'])
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_real_post_sigkill_recovers_across_entries_without_repeating_the_write(self):
        self.cfg['settle_seconds'] = 0
        self.save_config()
        source_bytes, token_bytes = self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes()
        old_last = self.last.read_bytes()
        server = self.base / 'synthetic-server.json'
        ready = self.base / 'synthetic-post-ready.json'
        visible = self.base / 'synthetic-visible'
        server.write_text(json.dumps({'posts': 0, 'deletes': 0, 'reads': 0, 'rows': []}))
        self.install_temporary_script('''
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
    if STATE / 'tokens' in path.parents:
        return offline_only()
    return original_read_text(path, *args, **kwargs)
Path.read_text = read_without_tokens

class LocalServerAPI:
    path = BASE / 'synthetic-server.json'
    def get_body_composition(self, start, end):
        value = json.loads(self.path.read_text())
        value['reads'] += 1
        write_json(self.path, value)
        rows = value['rows'] if (BASE / 'synthetic-visible').exists() else []
        return {'dateWeightList': [row for row in rows if start <= row['calendarDate'] <= end]}

    def add_body_composition(self, **kwargs):
        # A separate read-only connection must see the committed gate BEFORE
        # this simulated remote service receives and durably saves the POST.
        with contextlib.closing(sqlite3.connect(
                f"file:{STATE / 'sync.sqlite3'}?mode=ro", uri=True)) as probe:
            assert probe.execute('SELECT status FROM uploads').fetchall() == [('inflight',)]
        value = json.loads(self.path.read_text())
        value['posts'] += 1
        at = aware(kwargs['timestamp'])
        value['rows'].append({'samplePk': 100, 'calendarDate': at.date().isoformat(),
                             'timestampGMT': int(at.timestamp() * 1000),
                             'weight': kwargs['weight'] * 1000,
                             'bodyFat': kwargs['percent_fat'], 'bmi': kwargs.get('bmi')})
        value['durable_inflight_before_post'] = True
        write_json(self.path, value)
        write_json(BASE / 'synthetic-post-ready.json', {'received': True})
        # Parent SIGKILLs this real entry after receipt, before any response.
        while True:
            time.sleep(0.02)

    def add_weigh_in(self, **kwargs):
        raise AssertionError("fixture must exercise the FIT POST path")

    def delete_weigh_in(self, *args, **kwargs):
        value = json.loads(self.path.read_text())
        value['deletes'] += 1
        write_json(self.path, value)
        raise AssertionError("recovery must never delete")

def api_client(interactive=False):
    assert not interactive
    return LocalServerAPI()
''')
        children = []
        run_marker = self.state / 'last_attempt.json'

        def launch():
            child = subprocess.Popen([sys.executable, '-B', str(self.entry)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children.append(child)
            return child

        def local_status():
            with contextlib.closing(sqlite3.connect(
                    f"file:{self.state / 'sync.sqlite3'}?mode=ro", uri=True)) as db:
                return db.execute('SELECT status FROM uploads').fetchall()

        def unlocked():
            for name in ('agent-log.lock', 'run.lock'):
                with (self.state / name).open('a') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            first = launch()
            deadline = time.monotonic() + 5
            while not ready.exists():
                if first.poll() is not None:
                    self.fail(f'temporary entry exited before simulated POST: {first.returncode}')
                if time.monotonic() >= deadline:
                    self.fail('temporary entry did not reach simulated POST')
                time.sleep(0.01)
            self.assertIsNone(first.poll())
            self.assertEqual(json.loads(ready.read_text()), {'received': True})
            accepted = json.loads(server.read_text())
            self.assertEqual(accepted['posts'], 1)
            self.assertEqual(accepted['deletes'], 0)
            self.assertTrue(accepted['durable_inflight_before_post'])
            self.assertEqual(local_status(), [('inflight',)])
            self.assertEqual(self.export.read_bytes(), source_bytes)
            run = json.loads(run_marker.read_text())
            self.assertEqual((run['outcome'], run['stage'], run['finished_at']), ('running', 'sync', None))
            self.assertEqual((self.value()['outcome'], self.value()['stage']), ('running', 'run'))
            self.assertIsNone(self.value()['finished_at'])
            entry_bytes, run_bytes = self.marker.read_bytes(), run_marker.read_bytes()
            for name in ('agent-log.lock', 'run.lock'):
                with (self.state / name).open('a') as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            first.kill()
            first.wait(timeout=5)
            self.assertEqual(first.returncode, -signal.SIGKILL)
            self.assertEqual(self.marker.read_bytes(), entry_bytes)
            self.assertEqual(run_marker.read_bytes(), run_bytes)
            self.assertEqual(self.last.read_bytes(), old_last)
            self.assertEqual(json.loads(server.read_text()), accepted)
            self.assertEqual(local_status(), [('inflight',)])
            unlocked()

            second = launch()
            second.wait(timeout=5)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(local_status(), [('uncertain',)])
            self.assertEqual(json.loads(server.read_text())['posts'], 1)
            summary = json.loads(self.last.read_text())
            self.assertEqual(list(summary['results'].values()), ['uncertain'])
            self.assertEqual(summary['local_status_counts'], {'uncertain': 1})
            self.assertEqual(self.value()['outcome'], 'completed')
            self.assertEqual(json.loads(run_marker.read_text())['outcome'], 'completed')
            result, _, checks = self.report()
            self.assertEqual(result, 2)
            self.assertEqual(checks['last_run']['status'], 'error')
            self.assertEqual(checks['last_run']['data']['local_status_counts'], {'uncertain': 1})
            unlocked()

            visible.touch()
            third = launch()
            third.wait(timeout=5)
            self.assertEqual(third.returncode, 0)
            self.assertEqual(local_status(), [('verified',)])
            summary = json.loads(self.last.read_text())
            self.assertEqual(list(summary['results'].values()), ['verified'])
            self.assertEqual(summary['local_status_counts'], {'verified': 1})
            result, _, checks = self.report()
            self.assertEqual(result, 0)
            self.assertEqual(checks['last_run']['status'], 'info')
            final = json.loads(server.read_text())
            self.assertEqual((final['posts'], final['deletes']), (1, 0))
            self.assertGreater(final['reads'], accepted['reads'])
            self.assertEqual(self.export.read_bytes(), source_bytes)
            self.assertEqual((self.tokens / 'oauth.json').read_bytes(), token_bytes)
            unlocked()
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
