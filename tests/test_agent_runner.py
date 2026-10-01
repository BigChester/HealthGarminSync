"""Bounded background output using synthetic files, locks and subprocesses only."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_runner as r
import health_garmin as h


class TemporaryRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / 'state'
        self.state.mkdir()
        self.enterContext(patch.object(h, 'BASE', self.base))
        self.enterContext(patch.object(h, 'STATE', self.state))
        self.enterContext(patch.object(h, 'CONFIG', self.base / 'config.json'))
        self.cfg = {'is_cn': True, 'enabled': False, 'inbox': str(self.base),
                    'since': '2020-01-01', 'allowed_sources': ['synthetic'],
                    'weight_bare_unit': 'kg', 'fat_bare_unit': 'percent',
                    'pair_window_seconds': 0, 'settle_seconds': 0, 'max_records_per_run': 10}
        h.CONFIG.write_text(json.dumps(self.cfg))

    def invoke(self):
        output, errors = io.StringIO(), io.StringIO()
        original_argv = sys.argv
        previous_umask = os.umask(0o077)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = r.main()
                self.assertIs(sys.stdout, output)
                self.assertIs(sys.stderr, errors)
                self.assertIs(sys.argv, original_argv)
        finally:
            os.umask(previous_umask)
        return result, output.getvalue(), errors.getvalue()

    def assert_generations(self, path, limit, backups=3):
        paths = r.checked_paths(path)
        existing = [item for item in paths if item.exists()]
        self.assertLessEqual(len(existing), backups + 1)
        for item in existing:
            raw = item.read_bytes()
            self.assertLessEqual(len(raw), limit)
            raw.decode('utf-8', errors='strict')
            self.assertEqual(item.stat().st_mode & 0o777, 0o600)
        return b''.join(item.read_bytes() for item in reversed(
            [path, *(path.with_name(f'{path.name}.{i}') for i in range(1, backups + 1))]) if item.exists())


class BoundedStreamTests(TemporaryRunner):
    def test_small_writes_rotate_bytes_preserving_recent_utf8_tail(self):
        path = self.state / 'out'
        text = ''.join(f'{i}:中🙂abc\n' for i in range(50))
        with r.BoundedStream(path, max_bytes=47) as stream:
            for line in text.splitlines(keepends=True):
                self.assertEqual(stream.write(line), len(line))
                stream.flush()
                self.assertLessEqual(path.stat().st_size, 47)
        retained = self.assert_generations(path, 47)
        self.assertTrue(text.encode().endswith(retained))
        self.assertIn('49:中🙂abc', retained.decode())

    def test_one_huge_write_is_bounded_without_splitting_codepoints(self):
        path = self.state / 'out'
        text = 'ab中🙂é' * 100000 + '最后一行\n'
        with r.BoundedStream(path, max_bytes=4095) as stream:
            self.assertEqual(stream.write(text), len(text))
        retained = self.assert_generations(path, 4095)
        self.assertTrue(text.encode().endswith(retained))
        self.assertTrue(retained.decode().endswith('最后一行\n'))

    def test_default_limit_caps_each_generation_of_multibyte_large_write(self):
        path = self.state / 'out'
        with r.BoundedStream(path) as stream:
            stream.write('中' * 1600000)
        retained = self.assert_generations(path, 1_000_000)
        self.assertTrue(retained.endswith('中'.encode()))
        self.assertEqual(len([item for item in r.checked_paths(path) if item.exists()]), 4)

    def test_old_oversized_generations_are_normalized_and_extra_backups_removed(self):
        path = self.state / 'out'
        for suffix in ('', '.1', '.2', '.3', '.4', '.9', '.01', '.0'):
            target = path.with_name(path.name + suffix)
            target.write_bytes(('中🙂' * 50 + '末尾').encode())
            target.chmod(0o666)
        with r.BoundedStream(path, max_bytes=41):
            pass
        self.assert_generations(path, 41)
        for suffix in ('', '.1', '.2', '.3'):
            self.assertTrue(path.with_name(path.name + suffix).read_text().endswith('末尾'))
        for suffix in ('.4', '.9', '.01', '.0'):
            self.assertFalse(path.with_name(path.name + suffix).exists())

    def test_invalid_legacy_bytes_become_valid_utf8(self):
        path = self.state / 'out'
        path.write_bytes(b'\xffprefix' + '中🙂尾'.encode() + b'\xfe')
        with r.BoundedStream(path, max_bytes=12):
            pass
        self.assert_generations(path, 12)
        self.assertTrue(path.read_text().endswith('中🙂尾'))

    def test_symlink_and_nonregular_base_or_backup_are_rejected(self):
        outside = self.base / 'outside'
        outside.write_bytes(b'unchanged')
        for suffix, kind in (('', 'link'), ('.1', 'link'), ('.8', 'link'), ('', 'directory')):
            with self.subTest(suffix=suffix, kind=kind):
                path = self.state / 'out'
                target = path.with_name(path.name + suffix)
                target.symlink_to(outside) if kind == 'link' else target.mkdir()
                with self.assertRaises(ValueError):
                    r.BoundedStream(path, max_bytes=20)
                self.assertEqual(outside.read_bytes(), b'unchanged')
                target.unlink() if kind == 'link' else target.rmdir()

    def test_short_os_writes_are_completed(self):
        path = self.state / 'out'
        real_write = os.write
        with r.BoundedStream(path, max_bytes=31) as stream, \
                patch.object(r.os, 'write', side_effect=lambda fd, data: real_write(fd, data[:2])):
            stream.write('短写🙂测试\n')
        self.assertEqual(path.read_text(), '短写🙂测试\n')

    def test_partial_codepoint_write_failure_rolls_back_to_complete_prior_output(self):
        path = self.state / 'out'
        real_write = os.write
        calls = 0

        def interrupted_write(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:1])
            raise OSError('private-secret')

        with r.BoundedStream(path, max_bytes=20) as stream:
            stream.write('先前🙂')
            with patch.object(r.os, 'write', side_effect=interrupted_write):
                stream.write('中新输出')
            self.assertTrue(stream.failed)
        self.assertEqual(path.read_text(), '先前🙂')
        self.assert_generations(path, 20)

    def test_legacy_tail_short_reads_do_not_drop_recent_bytes(self):
        path = self.state / 'out'
        original = ('中文🙂' * 10 + '最新尾部').encode()
        path.write_bytes(original)
        real_read = os.read
        with patch.object(r.os, 'read', side_effect=lambda fd, size: real_read(fd, min(size, 2))), \
                r.BoundedStream(path, max_bytes=47):
            pass
        retained = self.assert_generations(path, 47)
        self.assertTrue(original.endswith(retained))
        self.assertTrue(retained.decode().endswith('最新尾部'))


class RunnerTests(TemporaryRunner):
    def test_wrapper_forces_run_and_restores_process_state(self):
        seen = []

        def command():
            seen.append(list(sys.argv))
            print('背景正常中🙂')
            print('背景错误流', file=sys.stderr)
            return 0

        with patch.object(h, 'main', side_effect=command):
            self.assertEqual(self.invoke(), (0, '', ''))
        self.assertEqual(seen, [[str(self.base / 'health_garmin.py'), 'run']])
        self.assertEqual((self.state / 'agent.out.log').read_text(), '背景正常中🙂\n')
        self.assertEqual((self.state / 'agent.err.log').read_text(), '背景错误流\n')
        self.assertEqual((self.state / 'agent-log.lock').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_real_agent_log_lock_busy_skips_main_and_preserves_attempt(self):
        attempt = self.state / 'last_attempt.json'
        attempt.write_bytes(b'original evidence')
        with (self.state / 'agent-log.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(h, 'main') as main:
                self.assertEqual(self.invoke(), (0, '', ''))
                main.assert_not_called()
        self.assertEqual(attempt.read_bytes(), b'original evidence')
        self.assertFalse((self.state / 'agent.out.log').exists())

    def test_real_run_lock_busy_has_bounded_skip_and_preserves_attempt(self):
        attempt = self.state / 'last_attempt.json'
        attempt.write_bytes(b'original evidence')
        with (self.state / 'run.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(h, 'run_sync') as sync:
                self.assertEqual(self.invoke(), (0, '', ''))
                sync.assert_not_called()
        self.assertIn('另一个进程', (self.state / 'agent.out.log').read_text())
        self.assert_generations(self.state / 'agent.out.log', r.MAX_BYTES)
        self.assertEqual(attempt.read_bytes(), b'original evidence')

    def test_manual_run_keeps_stdout_and_does_not_create_agent_logs(self):
        with patch.object(sys, 'argv', ['hg', 'run']), \
                patch.object(h, 'run_with_attempt', side_effect=lambda: print('manual output')):
            output = io.StringIO()
            previous_umask = os.umask(0o077)
            try:
                with contextlib.redirect_stdout(output):
                    self.assertEqual(h.main(), 0)
            finally:
                os.umask(previous_umask)
        self.assertEqual(output.getvalue(), 'manual output\n')
        self.assertFalse((self.state / 'agent.out.log').exists())
        self.assertFalse((self.state / 'agent-log.lock').exists())

    def test_setup_symlink_or_open_failure_prevents_main(self):
        outside = self.base / 'outside'
        outside.write_text('secret-untouched')
        for name in ('agent-log.lock', 'agent.out.log', 'agent.err.log.1'):
            with self.subTest(name=name):
                path = self.state / name
                path.symlink_to(outside)
                with patch.object(h, 'main') as main:
                    result, _, errors = self.invoke()
                self.assertEqual(result, 2)
                self.assertNotIn('secret-untouched', errors)
                main.assert_not_called()
                self.assertEqual(outside.read_text(), 'secret-untouched')
                path.unlink()
        real_open = r.regular_fd
        opened = []

        def failing_open(path, *args, **kwargs):
            if Path(path).name == 'agent.err.log':
                raise OSError('private-secret')
            fd = real_open(path, *args, **kwargs)
            opened.append(fd)
            return fd

        with patch.object(r, 'regular_fd', side_effect=failing_open), patch.object(h, 'main') as main:
            result, _, errors = self.invoke()
        self.assertEqual(result, 2)
        self.assertIn('OSError', errors)
        self.assertNotIn('private-secret', errors)
        main.assert_not_called()
        self.assertTrue(opened)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_legacy_normalization_failure_prevents_main_and_releases_lock(self):
        (self.state / 'agent.out.log').write_bytes(b'x' * (r.MAX_BYTES + 1))
        real_open, opened = r.regular_fd, []

        def track_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        with patch.object(r, 'write_all', side_effect=OSError('private-secret')), \
                patch.object(r, 'regular_fd', side_effect=track_open), patch.object(h, 'main') as main:
            result, _, errors = self.invoke()
        self.assertEqual(result, 2)
        self.assertNotIn('private-secret', errors)
        main.assert_not_called()
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)
        with (self.state / 'agent-log.lock').open('a') as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_write_failure_never_throws_into_main_and_preserves_existing_exit_codes(self):
        for code in (0, 2, 130):
            with self.subTest(code=code):
                def command():
                    with patch.object(r, 'write_all', side_effect=OSError('private-secret')):
                        print('first output')
                        print('later output')
                    return code

                with patch.object(h, 'main', side_effect=command):
                    result, _, errors = self.invoke()
                self.assertEqual(result, 2 if code == 0 else code)
                self.assertNotIn('private-secret', errors)
                self.assertNotIn('private-secret', (self.state / 'agent.err.log').read_text())

    def test_rotation_failure_does_not_interrupt_main(self):
        (self.state / 'agent.out.log').write_bytes(b'x' * r.MAX_BYTES)

        def command():
            with patch.object(Path, 'replace', side_effect=OSError('private-secret')):
                print('forces rotation')
            return 0

        with patch.object(h, 'main', side_effect=command):
            self.assertEqual(self.invoke()[0], 2)
        self.assertIn('OSError', (self.state / 'agent.err.log').read_text())
        self.assert_generations(self.state / 'agent.out.log', r.MAX_BYTES)

    def test_flush_failure_is_flagged_and_reported_safely(self):
        def command():
            print('before flush')
            real_stat = os.fstat
            output_fd = sys.stdout._fd
            with patch.object(r.os, 'fstat', side_effect=lambda fd: (_ for _ in ()).throw(OSError('private-secret')) if fd == output_fd else real_stat(fd)):
                sys.stdout.flush()
            return 0

        with patch.object(h, 'main', side_effect=command):
            self.assertEqual(self.invoke()[0], 2)
        text = (self.state / 'agent.err.log').read_text()
        self.assertIn('OSError', text)
        self.assertNotIn('private-secret', text)

    def test_close_failure_preserves_return_codes_and_closes_fd(self):
        for code in (0, 2, 130):
            with self.subTest(code=code):
                real_close = os.close
                broken_fd = None
                raised = False

                def command():
                    nonlocal broken_fd
                    broken_fd = sys.stdout._fd
                    return code

                def fail_after_close(fd):
                    nonlocal raised
                    real_close(fd)
                    if fd == broken_fd and not raised:
                        raised = True
                        raise OSError('private-secret')

                with patch.object(h, 'main', side_effect=command), patch.object(r.os, 'close', side_effect=fail_after_close):
                    result, _, errors = self.invoke()
                self.assertEqual(result, 2 if code == 0 else code)
                self.assertNotIn('private-secret', errors)
                with self.assertRaises(OSError):
                    os.fstat(broken_fd)

    def test_primary_exception_survives_cleanup_and_restores_streams(self):
        for exc in (RuntimeError('primary-secret'), KeyboardInterrupt()):
            with self.subTest(kind=type(exc).__name__):
                out, err, argv = sys.stdout, sys.stderr, sys.argv
                with patch.object(h, 'main', side_effect=exc), self.assertRaises(type(exc)) as raised:
                    self.invoke()
                self.assertIs(raised.exception, exc)
                self.assertIs(sys.stdout, out)
                self.assertIs(sys.stderr, err)
                self.assertIs(sys.argv, argv)
                with (self.state / 'agent-log.lock').open('a') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                text = (self.state / 'agent.err.log').read_text()
                self.assertIn('后台运行失败：' + type(exc).__name__, text)
                self.assertNotIn('primary-secret', text)

    def test_real_entry_maps_early_main_exception_and_interruption_without_traceback(self):
        source = Path(r.__file__).resolve().parent
        shutil.copyfile(source / 'agent_runner.py', self.base / 'agent_runner.py')
        for category, code in (('RuntimeError', 2), ('KeyboardInterrupt', 130)):
            with self.subTest(category=category):
                (self.base / 'health_garmin.py').write_text(
                    (source / 'health_garmin.py').read_text() +
                    f'\ndef main():\n    raise {category}("primary-secret")\n')
                process = subprocess.run([sys.executable, '-B', str(self.base / 'agent_runner.py')],
                                         capture_output=True, timeout=5)
                self.assertEqual(process.returncode, code)
                self.assertEqual(process.stdout, b'')
                self.assertEqual(process.stderr, b'')
                text = (self.state / 'agent.err.log').read_text()
                self.assertIn('后台运行失败：' + category, text)
                self.assertNotIn('primary-secret', text)
                self.assertNotIn('Traceback', text)

    def test_real_temporary_entry_sigkill_flushes_output_and_releases_both_locks(self):
        source = Path(r.__file__).resolve().parent
        shutil.copyfile(source / 'agent_runner.py', self.base / 'agent_runner.py')
        health = (source / 'health_garmin.py').read_text()
        health += '''\n# Temporary offline test override; never call Garmin.\nimport time\ndef run_sync(cfg, selected_id=None):\n    print("子进程日志中🙂", flush=True)\n    print("子进程错误流", file=sys.stderr, flush=True)\n    time.sleep(60)\ndef api_client(*args, **kwargs):\n    raise AssertionError("network forbidden")\n'''
        (self.base / 'health_garmin.py').write_text(health)
        child = subprocess.Popen([sys.executable, '-B', str(self.base / 'agent_runner.py')],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            attempt_path = self.state / 'last_attempt.json'
            while time.monotonic() < deadline:
                if attempt_path.exists() and (self.state / 'agent.err.log').exists():
                    attempt = json.loads(attempt_path.read_text())
                    if attempt['stage'] == 'sync' and '子进程错误流' in (self.state / 'agent.err.log').read_text():
                        break
                if child.poll() is not None:
                    self.fail(f'temporary runner exited: {child.returncode}')
                time.sleep(0.01)
            else:
                self.fail('temporary runner did not reach synthetic sync')
            evidence = attempt_path.read_bytes()
            self.assertIn('子进程日志中🙂', (self.state / 'agent.out.log').read_text())
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(attempt_path.read_bytes(), evidence)
            self.assertEqual(json.loads(evidence)['outcome'], 'running')
            for name in ('agent-log.lock', 'run.lock'):
                with (self.state / name).open('a') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_generations(self.state / 'agent.out.log', r.MAX_BYTES)
            self.assert_generations(self.state / 'agent.err.log', r.MAX_BYTES)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
