"""LaunchAgent commands with private files and mocked launchctl only."""
import contextlib
import fcntl
import io
import logging
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import unittest
from unittest.mock import patch

import health_garmin as h
import test_cli as fixtures


SECRET = 'private-service-output-and-config'


class AgentManagementTests(unittest.TestCase):
    save = fixtures.ConfigFixture.save

    def setUp(self):
        fixtures.ConfigFixture.setUp(self)
        self.cfg['enabled'] = True
        self.save(self.cfg)
        self.home = self.root / 'home'
        self.directory = self.home / 'Library/LaunchAgents'
        self.directory.mkdir(parents=True)
        self.plist = self.directory / f'{h.LABEL}.plist'
        self.original = b'synthetic previous configuration'
        self.plist.write_bytes(self.original)
        self.enterContext(patch.object(h, 'BASE', self.root))
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.enterContext(patch.object(sys, 'platform', 'darwin'))
        self.service = self.enterContext(patch.object(h.subprocess, 'run',
            return_value=subprocess.CompletedProcess([], 0, SECRET, SECRET)))

    def command(self, command):
        output, errors = io.StringIO(), io.StringIO()
        previous_umask = os.umask(0o077)
        try:
            with patch.object(sys, 'argv', ['hg', command]), \
                    patch.object(h, 'RotatingFileHandler', return_value=logging.NullHandler()), \
                    patch.object(h.logging, 'basicConfig'), patch.object(h.LOG, 'error'), \
                    patch.object(h, 'api_client') as api, \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = h.main()
        finally:
            os.umask(previous_umask)
        api.assert_not_called()
        self.assertNotIn(SECRET, output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def assert_no_drafts(self):
        self.assertEqual(list(self.directory.glob(f'.{h.LABEL}.*.tmp')), [])

    def test_status_success_queries_only_print_with_timeout_and_held_run_lock(self):
        previous = self.config_path.read_bytes()

        def query(command, **kwargs):
            self.assertEqual(command, ['/bin/launchctl', 'print', f'gui/{os.getuid()}/{h.LABEL}'])
            self.assertEqual(kwargs, {'capture_output': True, 'timeout': 5})
            with (self.state / 'run.lock').open('r') as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return subprocess.CompletedProcess(command, 0, SECRET, SECRET)

        self.service.side_effect = query
        code, output, errors = self.command('status')
        self.assertEqual(code, 0)
        self.assertIn('LaunchAgent 已载入： True', output)
        self.assertEqual(errors, '')
        self.assertEqual(self.service.call_count, 1)
        self.assertEqual(self.config_path.read_bytes(), previous)
        self.assertEqual(self.plist.read_bytes(), self.original)

    def test_status_nonzero_query_is_error_without_claiming_known_service_state(self):
        previous = self.config_path.read_bytes()
        for result in (3, 5, 113):
            with self.subTest(result=result):
                self.service.reset_mock()
                self.service.return_value = subprocess.CompletedProcess([], result, SECRET, SECRET)
                code, output, errors = self.command('status')
                self.assertEqual(code, 2)
                self.assertNotIn('LaunchAgent 已载入：', output)
                self.assertIn('未载入或服务查询未成功', errors)
                self.assertIn(f'返回码 {result}', errors)
                self.assertIn('./hg doctor', errors)
                self.service.assert_called_once_with(['/bin/launchctl', 'print',
                    f'gui/{os.getuid()}/{h.LABEL}'], capture_output=True, timeout=5)
                self.assertEqual(self.config_path.read_bytes(), previous)
                self.assertEqual(self.plist.read_bytes(), self.original)

    def test_status_query_failure_releases_lock_and_keeps_run_evidence(self):
        marker = self.state / 'last_attempt.json'
        marker.write_text('previous run evidence')
        previous = self.config_path.read_bytes()
        for failure in (subprocess.TimeoutExpired('launchctl', 5, SECRET, SECRET), OSError(SECRET)):
            with self.subTest(failure=type(failure).__name__):
                self.service.reset_mock()
                self.service.side_effect = failure
                code, output, errors = self.command('status')
                self.assertEqual(code, 2)
                self.assertNotIn('LaunchAgent 已载入：', output)
                self.assertIn('超时（5 秒）' if isinstance(failure, subprocess.TimeoutExpired) else '无法执行', errors)
                self.service.assert_called_once_with(['/bin/launchctl', 'print',
                    f'gui/{os.getuid()}/{h.LABEL}'], capture_output=True, timeout=5)
                self.assertEqual(marker.read_text(), 'previous run evidence')
                self.assertEqual(self.config_path.read_bytes(), previous)
                self.assertEqual(self.plist.read_bytes(), self.original)
                with (self.state / 'run.lock').open('r') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(h, 'run_sync') as sync:
                    self.assertEqual(self.command('run')[0], 0)
                sync.assert_called_once_with(self.cfg)
                marker.write_text('previous run evidence')

    def test_status_on_other_platforms_does_not_query_launchctl(self):
        with patch.object(sys, 'platform', 'linux'):
            code, output, errors = self.command('status')
        self.assertEqual(code, 0)
        self.assertEqual(errors, '')
        self.assertNotIn('LaunchAgent 已载入：', output)
        self.service.assert_not_called()

    def test_uninstall_bootout_failure_keeps_plist_and_never_claims_success(self):
        self.service.return_value = subprocess.CompletedProcess([], 5, SECRET, SECRET)
        result, output, errors = self.command('uninstall-agent')
        self.assertEqual(result, 2)
        self.assertNotIn('已卸载', output)
        self.assertIn('bootout', errors)
        self.assertIn('5', errors)
        self.assertEqual(self.plist.read_bytes(), self.original)

    def test_install_symlink_preflight_does_not_stop_existing_service(self):
        target = self.root / 'previous-plist'
        target.write_bytes(self.original)
        self.plist.unlink()
        self.plist.symlink_to(target)
        result, _, _ = self.command('install-agent')
        self.assertEqual(result, 2)
        self.service.assert_not_called()
        self.assertEqual(target.read_bytes(), self.original)
        self.assert_no_drafts()

    def test_first_install_nonzero_query_uses_bootstrap_without_bootout(self):
        self.plist.unlink()
        self.service.side_effect = [subprocess.CompletedProcess([], 113, SECRET, SECRET),
                                    subprocess.CompletedProcess([], 0, SECRET, SECRET)]
        result, output, _ = self.command('install-agent')
        self.assertEqual(result, 0)
        self.assertIn('定时任务已注册', output)
        self.assertEqual([call.args[0][1] for call in self.service.call_args_list], ['print', 'bootstrap'])
        for call in self.service.call_args_list:
            self.assertEqual(call.kwargs, {'capture_output': True, 'timeout': 5})
        self.assertEqual(plistlib.loads(self.plist.read_bytes()), h.agent_plist())
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o600)
        self.assert_no_drafts()

    def test_uninstall_bad_or_missing_config_still_only_manages_service(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.plist.write_bytes(self.original)
                self.service.reset_mock()
                if missing:
                    self.config_path.unlink(missing_ok=True)
                else:
                    self.config_path.write_text('{broken ' + SECRET)
                result, output, _ = self.command('uninstall-agent')
                self.assertEqual(result, 0)
                self.assertIn('已卸载', output)
                self.assertFalse(self.plist.exists())
                self.assertEqual(self.service.call_count, 1)
                self.assertEqual(self.service.call_args.args[0][1], 'bootout')

    def test_loaded_update_prepares_closed_private_draft_before_touching_service(self):
        factory = h.tempfile.NamedTemporaryFile
        streams, descriptors, calls = [], [], []

        def create(*args, **kwargs):
            self.assertEqual(kwargs['suffix'], '.tmp')
            self.assertTrue(kwargs['prefix'].startswith('.'))
            stream = factory(*args, **kwargs)
            streams.append(stream)
            descriptors.append(stream.fileno())
            return stream

        def service(command, **kwargs):
            action = command[1]
            calls.append(action)
            self.assertEqual(kwargs, {'capture_output': True, 'timeout': 5})
            self.assertTrue(all(stream.closed for stream in streams))
            for fd in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            if action in ('print', 'bootout'):
                self.assertEqual(self.plist.read_bytes(), self.original)
                drafts = list(self.directory.glob(f'.{h.LABEL}.*.tmp'))
                self.assertEqual(len(drafts), 1)
                self.assertEqual(drafts[0].stat().st_mode & 0o777, 0o600)
                self.assertEqual(plistlib.loads(drafts[0].read_bytes()), h.agent_plist())
            elif action == 'bootstrap':
                self.assertEqual(plistlib.loads(self.plist.read_bytes()), h.agent_plist())
                self.assert_no_drafts()
            return subprocess.CompletedProcess(command, 0, SECRET, SECRET)

        self.service.side_effect = service
        with patch.object(h.tempfile, 'NamedTemporaryFile', side_effect=create):
            self.assertEqual(self.command('install-agent')[0], 0)
        self.assertEqual(calls, ['print', 'bootout', 'bootstrap'])
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o600)

    def test_nonregular_target_is_rejected_without_service_calls(self):
        self.plist.unlink()
        for kind in ('directory', 'fifo'):
            with self.subTest(kind=kind):
                self.plist.mkdir() if kind == 'directory' else os.mkfifo(self.plist)
                result, _, errors = self.command('install-agent')
                self.assertEqual(result, 2)
                self.assertIn('非普通文件', errors)
                self.service.assert_not_called()
                self.assert_no_drafts()
                self.plist.rmdir() if kind == 'directory' else self.plist.unlink()

    def test_serialization_directory_creation_and_temp_creation_errors_do_not_call_service(self):
        original_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == self.directory:
                raise PermissionError(SECRET)
            return original_mkdir(path, *args, **kwargs)

        patches = (patch.object(h.plistlib, 'dumps', side_effect=TypeError(SECRET)),
                   patch.object(Path, 'mkdir', mkdir),
                   patch.object(h.tempfile, 'NamedTemporaryFile', side_effect=OSError(SECRET)))
        for broken in patches:
            with self.subTest(patch=broken), broken:
                self.assertEqual(self.command('install-agent')[0], 2)
            self.service.assert_not_called()
            self.assertEqual(self.plist.read_bytes(), self.original)
            self.assert_no_drafts()

    def test_draft_write_flush_close_or_chmod_failure_closes_fd_and_preserves_old_plist(self):
        factory = h.tempfile.NamedTemporaryFile
        for phase in ('write', 'flush', 'close', 'chmod'):
            with self.subTest(phase=phase):
                streams, descriptors = [], []

                def create(*args, **kwargs):
                    stream = factory(*args, **kwargs)
                    streams.append(stream)
                    descriptors.append(stream.fileno())
                    class BrokenDraft:
                        name = stream.name
                        def fileno(self):
                            return stream.fileno()
                        def write(self, data):
                            if phase == 'write':
                                raise OSError(SECRET)
                            return stream.write(data)
                        def flush(self):
                            if phase == 'flush':
                                raise OSError(SECRET)
                            stream.flush()
                        def close(self):
                            stream.close()
                            if phase == 'close':
                                raise OSError(SECRET)
                    return BrokenDraft()

                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(h.tempfile, 'NamedTemporaryFile', side_effect=create))
                    if phase == 'chmod':
                        stack.enter_context(patch.object(h.os, 'fchmod', side_effect=OSError(SECRET)))
                    self.assertEqual(self.command('install-agent')[0], 2)
                self.service.assert_not_called()
                self.assertEqual(self.plist.read_bytes(), self.original)
                self.assert_no_drafts()
                self.assertTrue(all(stream.closed for stream in streams))
                for fd in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(fd)

    def test_preparation_keyboard_interrupt_survives_secondary_close_error(self):
        factory = h.tempfile.NamedTemporaryFile
        for phase in ('write', 'flush'):
            with self.subTest(phase=phase):
                streams = []
                def create(*args, **kwargs):
                    stream = factory(*args, **kwargs)
                    streams.append(stream)
                    class InterruptedDraft:
                        name = stream.name
                        def fileno(self):
                            return stream.fileno()
                        def write(self, data):
                            if phase == 'write':
                                raise KeyboardInterrupt()
                            return stream.write(data)
                        def flush(self):
                            raise KeyboardInterrupt()
                        def close(self):
                            stream.close()
                            raise OSError(SECRET)
                    return InterruptedDraft()
                with patch.object(h.tempfile, 'NamedTemporaryFile', side_effect=create):
                    result, _, errors = self.command('install-agent')
                self.assertEqual(result, 130)
                self.assertIn('关闭失败： OSError', errors)
                self.service.assert_not_called()
                self.assertTrue(all(stream.closed for stream in streams))
                self.assertEqual(self.plist.read_bytes(), self.original)
                self.assert_no_drafts()

    def test_query_failure_stops_before_bootout_or_replace(self):
        for error in (subprocess.TimeoutExpired('launchctl', 5, SECRET, SECRET), OSError(SECRET)):
            with self.subTest(kind=type(error).__name__):
                self.service.reset_mock()
                self.service.side_effect = error
                result, _, errors = self.command('install-agent')
                self.assertEqual(result, 2)
                self.assertIn('print', errors)
                self.assertEqual(self.plist.read_bytes(), self.original)
                self.assertEqual(self.service.call_count, 1)
                self.assertEqual(self.service.call_args.args[0][1], 'print')
                self.assert_no_drafts()

    def test_loaded_bootout_failure_preserves_old_plist_and_never_bootstraps(self):
        for failure in (3, 5, 113, subprocess.TimeoutExpired('launchctl', 5, SECRET, SECRET), OSError(SECRET)):
            with self.subTest(failure=type(failure).__name__):
                self.service.reset_mock()
                response = subprocess.CompletedProcess([], failure, SECRET, SECRET) if isinstance(failure, int) else failure
                self.service.side_effect = [subprocess.CompletedProcess([], 0, SECRET, SECRET), response]
                result, output, errors = self.command('install-agent')
                self.assertEqual(result, 2)
                self.assertNotIn('已注册', output)
                self.assertIn('bootout', errors)
                self.assertEqual(self.plist.read_bytes(), self.original)
                self.assertEqual([call.args[0][1] for call in self.service.call_args_list], ['print', 'bootout'])
                self.assert_no_drafts()

    def test_bootstrap_failure_leaves_prepared_plist_and_reports_only_safe_status(self):
        for failure in (5, subprocess.TimeoutExpired('launchctl', 5, SECRET, SECRET), OSError(SECRET)):
            with self.subTest(kind=type(failure).__name__):
                self.service.reset_mock()
                response = subprocess.CompletedProcess([], failure, SECRET, SECRET) if isinstance(failure, int) else failure
                self.service.side_effect = [subprocess.CompletedProcess([], 113, SECRET, SECRET), response]
                result, output, errors = self.command('install-agent')
                self.assertEqual(result, 2)
                self.assertNotIn('已注册', output)
                self.assertIn('bootstrap', errors)
                self.assertEqual(plistlib.loads(self.plist.read_bytes()), h.agent_plist())
                self.assertEqual(self.plist.stat().st_mode & 0o777, 0o600)
                self.assertEqual([call.args[0][1] for call in self.service.call_args_list], ['print', 'bootstrap'])
                self.assert_no_drafts()

    def test_uninstall_nonzero_timeout_and_oserror_keep_plist(self):
        for failure in (3, 113, subprocess.TimeoutExpired('launchctl', 5, SECRET, SECRET), OSError(SECRET)):
            with self.subTest(kind=type(failure).__name__):
                self.service.reset_mock()
                if isinstance(failure, int):
                    self.service.side_effect = None
                    self.service.return_value = subprocess.CompletedProcess([], failure, SECRET, SECRET)
                else:
                    self.service.side_effect = failure
                result, output, errors = self.command('uninstall-agent')
                self.assertEqual(result, 2)
                self.assertNotIn('已卸载', output)
                self.assertIn('bootout', errors)
                self.assertEqual(self.plist.read_bytes(), self.original)
                self.assertEqual(self.service.call_count, 1)
                self.assertEqual(self.service.call_args.kwargs, {'capture_output': True, 'timeout': 5})

    def test_successful_repeat_uninstall_accepts_missing_plist_but_nonzero_is_not_absence(self):
        for _ in range(2):
            result, output, _ = self.command('uninstall-agent')
            self.assertEqual(result, 0)
            self.assertIn('已卸载', output)
            self.assertFalse(self.plist.exists())
        self.service.return_value = subprocess.CompletedProcess([], 113, SECRET, SECRET)
        result, output, errors = self.command('uninstall-agent')
        self.assertEqual(result, 2)
        self.assertNotIn('已卸载', output)
        self.assertIn('113', errors)

    def test_disabled_or_invalid_config_install_never_calls_service(self):
        for enabled in (False, 'false'):
            with self.subTest(enabled=enabled):
                self.save(dict(self.cfg, enabled=enabled))
                self.assertEqual(self.command('install-agent')[0], 2)
                self.service.assert_not_called()
                self.assertEqual(self.plist.read_bytes(), self.original)
                self.assert_no_drafts()

    def test_real_busy_run_lock_blocks_both_management_commands(self):
        with (self.state / 'run.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for command in ('install-agent', 'uninstall-agent'):
                with self.subTest(command=command):
                    result, _, errors = self.command(command)
                    self.assertEqual(result, 2)
                    self.assertIn(command + ' 未执行', errors)
                    self.service.assert_not_called()
                    self.assertEqual(self.plist.read_bytes(), self.original)
                    self.assert_no_drafts()

    def test_cleanup_failure_does_not_replace_primary_error_or_keyboard_interrupt(self):
        original_unlink = Path.unlink
        for primary, expected in ((h.SafeError('controlled bootout failure'), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(expected=expected):
                def unlink(path, *args, **kwargs):
                    if path.suffix == '.tmp':
                        raise OSError(SECRET)
                    return original_unlink(path, *args, **kwargs)
                self.service.side_effect = [subprocess.CompletedProcess([], 0, SECRET, SECRET), primary]
                with patch.object(Path, 'unlink', unlink):
                    result, _, errors = self.command('install-agent')
                self.assertEqual(result, expected)
                self.assertIn('清理失败： OSError', errors)
                if expected == 2:
                    self.assertIn('controlled bootout failure', errors)
                self.assertEqual(self.plist.read_bytes(), self.original)
                for path in self.directory.glob(f'.{h.LABEL}.*.tmp'):
                    path.unlink()


if __name__ == '__main__':
    unittest.main()
