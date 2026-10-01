"""Nonblocking JSON reads and atomic writes against real temporary paths."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import health_garmin as h
import test_agent_attempts as agent_fixtures
import test_diagnostics as fixtures
import test_sync_log_open as log_fixtures


class JSONFileIOTests(unittest.TestCase):
    setUp = fixtures.DoctorTests.setUp
    save_config = fixtures.DoctorTests.save_config
    snapshot = fixtures.DoctorTests.snapshot
    invoke = fixtures.DoctorTests.invoke
    report = fixtures.DoctorTests.report
    install_temporary_script = agent_fixtures.AgentAttemptTests.install_temporary_script
    child = log_fixtures.SyncLogOpenTests.child

    def test_peer_connected_fifo_is_rejected_before_truncate_and_owned_fds_close(self):
        fifo = self.root / 'connected-fifo'
        os.mkfifo(fifo, 0o600)
        native_open = os.open
        reader = native_open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        writer = native_open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.write(writer, b'Untouched queued bytes')
        try:
            for mode in ('r', 'w'):
                with self.subTest(mode=mode):
                    opened = []

                    def open_fd(*args, **kwargs):
                        fd = native_open(*args, **kwargs)
                        opened.append(fd)
                        return fd

                    with patch.object(h.os, 'open', side_effect=open_fd), patch.object(h.os, 'ftruncate') as truncate, \
                            patch.object(h.os, 'fdopen') as wrap, self.assertRaises(h.SafeError):
                        h.open_regular_text(fifo, mode, encoding='utf-8')
                    truncate.assert_not_called()
                    wrap.assert_not_called()
                    self.assertEqual(len(opened), 1)
                    with self.assertRaises(OSError):
                        os.fstat(opened[0])
            self.assertEqual(os.read(reader, 100), b'Untouched queued bytes')
        finally:
            os.close(writer)
            os.close(reader)

    def test_config_regular_symlink_remains_supported_but_other_json_links_are_rejected(self):
        target = self.base / 'actual-config.json'
        self.config_path.rename(target)
        self.config_path.symlink_to(target)
        self.assertEqual(h.config(), self.cfg)
        before = target.read_bytes()
        with self.assertRaises(OSError):
            h.read_regular_text(self.config_path)
        with self.assertRaises(h.SafeError):
            h.write_json(self.config_path, {'replacement': True})
        self.assertEqual(target.read_bytes(), before)

    def test_dump_and_native_close_failures_do_not_replace_target_or_hide_primary(self):
        target = self.state / 'evidence.json'
        h.write_json(target, {'previous': '保留'})
        original = target.read_bytes()
        with self.assertRaises(ValueError):
            h.write_json(target, {'invalid': float('nan')})
        self.assertEqual(target.read_bytes(), original)
        native_open = h.open_regular_text

        class CloseFailure:
            def __init__(self, stream):
                self.stream = stream
                self.fd = stream.fileno()
                self.calls = 0

            def write(self, value):
                return self.stream.write(value)

            def close(self):
                self.calls += 1
                self.stream.close()
                raise OSError(fixtures.SECRET)

        for primary in (None, ValueError(fixtures.SECRET), KeyboardInterrupt()):
            with self.subTest(primary=type(primary).__name__):
                observed = []

                def open_with_close_failure(*args, **kwargs):
                    stream = CloseFailure(native_open(*args, **kwargs))
                    observed.append(stream)
                    return stream

                errors = io.StringIO()
                with patch.object(h, 'open_regular_text', side_effect=open_with_close_failure), \
                        contextlib.redirect_stderr(errors), contextlib.ExitStack() as stack:
                    if primary is not None:
                        stack.enter_context(patch.object(h.json, 'dump', side_effect=primary))
                    with self.assertRaises(type(primary) if primary else OSError) as raised:
                        h.write_json(target, {'new': True})
                if primary is not None:
                    self.assertIs(raised.exception, primary)
                self.assertEqual(observed[0].calls, 1)
                with self.assertRaises(OSError):
                    os.fstat(observed[0].fd)
                self.assertEqual(target.read_bytes(), original)
                self.assertNotIn(fixtures.SECRET, errors.getvalue())
        h.write_json(target, {'new': '完整 UTF-8'})
        self.assertEqual(json.loads(target.read_text()), {'new': '完整 UTF-8'})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(target.with_name(target.name + '.tmp').exists())

    def test_real_cli_config_status_and_active_operation_fifo_fail_quickly(self):
        script = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import health_garmin as h
h.BASE = Path(sys.argv[1]); h.STATE = h.BASE / 'state'; h.CONFIG = h.BASE / 'config.json'
''' + log_fixtures.OFFLINE.replace('subprocess.', 'h.subprocess.').replace('getpass.', 'h.getpass.').replace(
            'if STATE /', 'if h.STATE /') + '''
class ReadOnlyAPI:
    def get_body_composition(self, start, end):
        return {'dateWeightList': []}
    def add_body_composition(self, **kwargs):
        return forbidden()
    add_weigh_in = add_body_composition
h.api_client = lambda: ReadOnlyAPI()
sys.argv = ['hg', sys.argv[3]]
raise SystemExit(h.main())
'''
        self.cfg['settle_seconds'] = 0
        self.save_config()
        source, tokens = self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes()
        cases = ((self.config_path, 'run'), (self.last, 'status'),
                 (self.state / 'operations' / 'unfinished.json', 'run'))
        for path, command in cases:
            with self.subTest(path=path.name):
                previous = path.read_bytes() if path.exists() else None
                path.parent.mkdir(exist_ok=True)
                path.unlink(missing_ok=True)
                os.mkfifo(path, 0o600)
                try:
                    self.assertEqual(self.child('-c', script, str(self.base), str(Path(h.__file__).parent), command), 2)
                    self.assertEqual((self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes()), (source, tokens))
                finally:
                    path.unlink()
                    if previous is not None:
                        path.write_bytes(previous)

    def test_real_write_fifo_start_interrupt_and_agent_terminal_do_not_hang(self):
        override = log_fixtures.OFFLINE + '''
kind = (BASE / 'synthetic-mode').read_text()
api_client = manage_agent = forbidden
if kind == 'start':
    config = run_sync = forbidden
else:
    def run_sync(cfg):
        name = 'last_attempt.json.tmp' if kind == 'interrupt' else 'last_agent_attempt.json.tmp'
        os.mkfifo(STATE / name, 0o600)
        if kind == 'interrupt':
            raise KeyboardInterrupt()
'''
        self.install_temporary_script(override)
        cli = '''
import health_garmin as h
import sys
sys.argv = ['hg', 'run']
raise SystemExit(h.main())
'''
        stamp = h.now().isoformat()
        marker = self.state / 'last_attempt.json'
        old = {'outcome': 'completed', 'stage': 'sync', 'started_at': stamp,
               'finished_at': stamp, 'error_type': None}
        source, tokens, summary = self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes(), self.last.read_bytes()
        for kind, expected in (('start', 2), ('interrupt', 130), ('agent-terminal', 2)):
            with self.subTest(kind=kind):
                marker.write_text(json.dumps(old))
                before = marker.read_bytes()
                (self.base / 'synthetic-mode').write_text(kind)
                if kind == 'start':
                    os.mkfifo(self.state / 'last_attempt.json.tmp', 0o600)
                try:
                    self.assertEqual(self.child(str(self.entry)) if kind == 'agent-terminal' else self.child('-c', cli), expected)
                    if kind == 'start':
                        self.assertEqual(marker.read_bytes(), before)
                    else:
                        self.assertEqual(json.loads(marker.read_text())['outcome'], 'running' if kind == 'interrupt' else 'completed')
                    if kind == 'agent-terminal':
                        self.assertEqual(json.loads(self.agent_attempt.read_text())['outcome'], 'running')
                        self.assertEqual(self.report()[0], 2)
                    self.assertEqual((self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes(), self.last.read_bytes()),
                                     (source, tokens, summary))
                finally:
                    for name in ('last_attempt.json.tmp', 'last_agent_attempt.json.tmp'):
                        (self.state / name).unlink(missing_ok=True)
