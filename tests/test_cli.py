"""Configuration and CLI locking checks using only private temporary files."""
import contextlib
import copy
import fcntl
import io
import json
import logging
import os
from pathlib import Path
import subprocess
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
                    patch.object(h, "PrivateRotatingFileHandler", return_value=logging.NullHandler()), \
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

    def invoke(self, command="run"):
        errors = io.StringIO()
        previous_umask = os.umask(0o077)
        try:
            with patch.object(sys, "argv", ["health_garmin.py", command]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                result = h.main()
        finally:
            os.umask(previous_umask)
        return result, errors.getvalue()

    def test_busy_commands_skip_unopenable_log_without_initializing_logging(self):
        (self.state / 'sync.log').mkdir()
        before = self.config_path.read_bytes()
        with (self.state / 'run.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(h, 'PrivateRotatingFileHandler', wraps=h.PrivateRotatingFileHandler) as factory, \
                    patch.object(h.logging, 'basicConfig') as configure, \
                    patch.object(h.LOG, 'error') as error, patch.object(h, 'config') as config, \
                    patch.object(h, 'run_with_attempt') as run, patch.object(h, 'manage_agent') as service:
                for command, expected in (('run', 0), ('status', 2), ('install-agent', 2)):
                    with self.subTest(command=command):
                        code, errors = self.invoke(command)
                        self.assertEqual(code, expected)
                        self.assertEqual(errors, '' if command == 'run' else
                            f'停止： 另一个进程正在处理，{command} 未执行；请等待当前进程结束后重试。\n')
                for boundary in (factory, configure, error, config, run, service):
                    boundary.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual({path.name for path in self.state.iterdir()}, {'sync.log', 'run.lock'})

    def test_busy_subprocess_does_not_rotate_active_owner_log(self):
        log = self.state / 'sync.log'
        prefix = 'retained owner evidence\n'
        log.write_text(prefix + 'x' * (999_980 - len(prefix)))
        original = log.read_bytes()
        owner = h.RotatingFileHandler(log, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
        script = '''
import builtins, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import health_garmin as h
h.STATE = Path(sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError("real config, API, home or service access forbidden")
h.config = h.api_client = h.manage_agent = forbidden
Path.home = classmethod(lambda cls: forbidden())
socket.create_connection = socket.getaddrinfo = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
builtins.input = h.getpass.getpass = forbidden
command = sys.argv[2]
sys.argv = ['hg', command]
raise SystemExit(h.main())
'''
        try:
            inode = os.fstat(owner.stream.fileno()).st_ino
            with (self.state / 'run.lock').open('a') as holder:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for command in ('status', 'install-agent'):
                    with self.subTest(command=command):
                        child = subprocess.run([sys.executable, '-B', '-c', script, str(self.state),
                                                command, str(Path(h.__file__).resolve().parent)],
                                               cwd=self.root, env={'HOME': str(self.root)},
                                               capture_output=True, text=True, timeout=5)
                        self.assertEqual(child.returncode, 2)
                        self.assertIn(command + ' 未执行', child.stderr)
                        self.assertEqual(log.read_bytes(), original)
                        self.assertEqual(log.stat().st_ino, inode)
                        self.assertEqual(os.fstat(owner.stream.fileno()).st_ino, inode)
                        self.assertEqual(list(self.state.glob('sync.log.*')), [])
                owner.emit(logging.LogRecord('owner', logging.INFO, '', 0, 'owner resumes', (), None))
                self.assertEqual(log.read_bytes(), original + b'owner resumes\n')
                self.assertEqual(log.stat().st_ino, inode)
        finally:
            owner.close()

    def test_log_creation_error_reporting_and_close_remain_inside_run_lock(self):
        factory, log_error = h.PrivateRotatingFileHandler, h.LOG.error
        for primary, expected in ((h.SafeError('controlled'), 2),
                                  (OSError('private-secret'), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(expected=expected, primary=type(primary).__name__):
                observed = []

                def locked():
                    with (self.state / 'run.lock').open('r') as probe:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

                def create(*args, **kwargs):
                    locked()
                    observed.append('create')
                    owned = factory(*args, **kwargs)
                    close = owned.close

                    def close_locked():
                        locked()
                        observed.append('close')
                        close()

                    self.enterContext(patch.object(owned, 'close', side_effect=close_locked))
                    return owned

                def error(*args, **kwargs):
                    locked()
                    observed.append('error')
                    log_error(*args, **kwargs)

                with patch.object(h, 'PrivateRotatingFileHandler', side_effect=create), \
                        patch.object(h.LOG, 'error', side_effect=error), \
                        patch.object(h, 'run_with_attempt', side_effect=primary):
                    code, errors = self.invoke()
                self.assertEqual(code, expected)
                self.assertNotIn('private-secret', errors)
                self.assertEqual(observed, ['create', 'close'] if expected == 130 else ['create', 'error', 'close'])
                self.assertEqual(self.root_logger.handlers, [])
                with (self.state / 'run.lock').open('r') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_lock_open_failure_is_safe_and_does_not_create_log_handler(self):
        (self.state / 'run.lock').mkdir()
        with patch.object(h, 'PrivateRotatingFileHandler') as factory, \
                patch.object(h.LOG, 'error') as error, patch.object(h, 'run_with_attempt') as run:
            code, errors = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn('IsADirectoryError', errors)
        self.assertNotIn('Traceback', errors)
        factory.assert_not_called()
        error.assert_not_called()
        run.assert_not_called()

    def test_invalid_run_lock_paths_fail_quickly_before_any_command_work(self):
        script = '''
import builtins, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import health_garmin as h
h.BASE = Path(sys.argv[1]).parent
h.STATE = Path(sys.argv[1])
h.CONFIG = h.BASE / 'config.json'
def forbidden(*args, **kwargs):
    raise AssertionError('config, API, home, service or logging accessed before lock validation')
h.config = h.api_client = h.manage_agent = h.PrivateRotatingFileHandler = forbidden
h.run_with_attempt = forbidden
Path.home = classmethod(lambda cls: forbidden())
socket.create_connection = socket.getaddrinfo = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
builtins.input = h.getpass.getpass = forbidden
sys.argv = ['hg', 'run']
raise SystemExit(h.main())
'''
        marker = self.state / 'last_attempt.json'
        marker.write_bytes(b'Preserved old run evidence')
        summary = self.state / 'last_run.json'
        summary.write_bytes(b'Preserved old summary')
        before = marker.read_bytes(), summary.read_bytes(), self.config_path.read_bytes()
        target = self.root / 'untouched-target'
        target.write_bytes(b'Preserved external target')
        lock = self.state / 'run.lock'
        for kind in ('fifo', 'symlink'):
            with self.subTest(kind=kind):
                if kind == 'fifo':
                    os.mkfifo(lock, 0o600)
                else:
                    lock.symlink_to(target)
                child = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.state),
                                          str(Path(h.__file__).resolve().parent)], cwd=self.root,
                                         env={'HOME': str(self.root)}, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                try:
                    output, errors = child.communicate(timeout=2)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=2)
                    child.stdout.close()
                    child.stderr.close()
                    lock.unlink()
                self.assertEqual(child.returncode, 2)
                self.assertEqual(output, '')
                self.assertNotIn('AssertionError', errors)
                self.assertNotIn('Traceback', errors)
                self.assertEqual(target.read_bytes(), b'Preserved external target')
                self.assertFalse((self.state / 'sync.log').exists())
                self.assertEqual((marker.read_bytes(), summary.read_bytes(), self.config_path.read_bytes()), before)

    def test_fifo_with_reader_is_rejected_and_owned_descriptor_is_closed(self):
        lock = self.state / 'run.lock'
        os.mkfifo(lock, 0o600)
        native_open, native_close = os.open, os.close
        reader = native_open(lock, os.O_RDONLY | os.O_NONBLOCK)
        opened = []

        def open_fd(*args, **kwargs):
            fd = native_open(*args, **kwargs)
            opened.append(fd)
            return fd

        try:
            with patch.object(h.os, 'open', side_effect=open_fd), patch.object(h.os, 'fdopen') as wrap, \
                    patch.object(h, 'PrivateRotatingFileHandler') as logging_factory, patch.object(h, 'config') as config, \
                    patch.object(h, 'api_client') as api, patch.object(h, 'run_with_attempt') as run:
                code, errors = self.invoke()
            self.assertEqual(code, 2)
            self.assertIn('普通文件', errors)
            self.assertNotIn('Traceback', errors)
            for boundary in (wrap, logging_factory, config, api, run):
                boundary.assert_not_called()
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                os.fstat(opened[0])
            os.fstat(reader)
        finally:
            native_close(reader)

    def test_fdopen_failure_closes_descriptor_and_keeps_primary_exception(self):
        native_open, native_close = os.open, os.close
        for primary, close_failure in ((OSError('private-secret'), False),
                                       (OSError('private-secret'), True), (KeyboardInterrupt(), True)):
            with self.subTest(primary=type(primary).__name__, close_failure=close_failure):
                opened = []

                def open_fd(*args, **kwargs):
                    fd = native_open(*args, **kwargs)
                    opened.append(fd)
                    return fd

                def close(fd):
                    native_close(fd)
                    if close_failure:
                        raise OSError('private-secret')

                errors = io.StringIO()
                with patch.object(h.os, 'open', side_effect=open_fd), patch.object(h.os, 'fdopen', side_effect=primary), \
                        patch.object(h.os, 'close', side_effect=close) as cleanup, contextlib.redirect_stderr(errors), \
                        self.assertRaises(type(primary)) as raised:
                    h.open_run_lock(self.state / 'run.lock')
                self.assertIs(raised.exception, primary)
                self.assertEqual(len(opened), 1)
                cleanup.assert_called_once_with(opened[0])
                with self.assertRaises(OSError):
                    os.fstat(opened[0])
                self.assertNotIn('private-secret', errors.getvalue())
                self.assertNotIn('Traceback', errors.getvalue())

    def test_lock_close_failure_preserves_primary_error_and_interruption_codes(self):
        original_open = h.open_run_lock

        class CloseFailure:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self.stream

            def __exit__(self, *args):
                self.stream.close()
                raise OSError('private-secret')

        def open_with_close_failure(path):
            return CloseFailure(original_open(path))

        for primary, expected in ((None, 2), (h.SafeError('controlled'), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(expected=expected, primary=type(primary).__name__):
                with patch.object(h, 'open_run_lock', side_effect=open_with_close_failure), \
                        patch.object(h, 'run_with_attempt', side_effect=primary):
                    code, errors = self.invoke()
                self.assertEqual(code, expected)
                self.assertIn('OSError', errors)
                self.assertNotIn('private-secret', errors)
                self.assertNotIn('Traceback', errors)
                self.assertEqual(self.root_logger.handlers, [])
                with (self.state / 'run.lock').open('r') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_repeated_commands_close_real_owned_handlers(self):
        handlers = []
        factory = h.PrivateRotatingFileHandler

        def create(*args, **kwargs):
            handler = factory(*args, **kwargs)
            handlers.append(handler)
            return handler

        with patch.object(h, "PrivateRotatingFileHandler", side_effect=create), \
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
        factory = h.PrivateRotatingFileHandler
        owned = factory(self.state / "owned.log")
        with patch.object(h, "PrivateRotatingFileHandler", return_value=owned), \
                patch.object(h, "run_with_attempt"):
            self.assertEqual(self.invoke(), (0, ""))
        self.assertEqual(self.root_logger.handlers, [existing])
        self.assertFalse(existing.stream.closed)
        self.assertIsNone(owned.stream)
        existing.emit(logging.LogRecord("caller", logging.INFO, "", 0, "still open", (), None))
        self.assertIn("still open", (self.root / "caller.log").read_text())

    def test_logging_initialization_failure_is_safe_and_closes_owned_handler(self):
        owned = h.RotatingFileHandler(self.state / "owned.log")
        with patch.object(h, "PrivateRotatingFileHandler", return_value=owned), \
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

                with patch.object(h, "PrivateRotatingFileHandler", return_value=owned), \
                        patch.object(owned, "close", side_effect=failing_close), \
                        patch.object(h, "run_with_attempt", side_effect=exc):
                    result, errors = self.invoke()
                self.assertEqual(result, expected)
                self.assertIn("同步日志未保存： OSError", errors)
                self.assertNotIn("private-secret", errors)
                self.assertIsNone(owned.stream)
                self.assertEqual(self.root_logger.handlers, [])


if __name__ == "__main__":
    unittest.main()
