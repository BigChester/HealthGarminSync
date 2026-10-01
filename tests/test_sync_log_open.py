"""Real private log opens and rollover recovery, entirely in temporary state."""
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import health_garmin as h
import test_agent_attempts as agent_fixtures
import test_diagnostics as fixtures


OFFLINE = '''
import builtins
import socket
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
    if STATE / 'tokens' in path.parents:
        return forbidden()
    return original_read_text(path, *args, **kwargs)
Path.read_text = read_without_tokens
'''


class SyncLogOpenTests(unittest.TestCase):
    setUp = fixtures.DoctorTests.setUp
    save_config = fixtures.DoctorTests.save_config
    snapshot = fixtures.DoctorTests.snapshot
    invoke = fixtures.DoctorTests.invoke
    report = fixtures.DoctorTests.report
    install_temporary_script = agent_fixtures.AgentAttemptTests.install_temporary_script

    def child(self, *arguments):
        child = subprocess.Popen([sys.executable, '-B', *arguments], cwd=self.base,
                                 env={'HOME': str(self.home)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, errors = child.communicate(timeout=2)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            child.stdout.close()
            child.stderr.close()
        self.assertNotIn(fixtures.SECRET.encode(), output + errors)
        self.assertNotIn(b'Traceback', errors)
        self.assertNotIn(b'AssertionError', errors)
        return child.returncode

    def test_real_cli_invalid_initial_log_fails_before_business_and_preserves_evidence(self):
        script = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import health_garmin as h
h.BASE = Path(sys.argv[1])
h.STATE = h.BASE / 'state'
h.CONFIG = h.BASE / 'config.json'
''' + OFFLINE.replace('subprocess.', 'h.subprocess.').replace('getpass.', 'h.getpass.').replace(
            "if STATE /", "if h.STATE /") + '''
h.config = h.api_client = h.run_with_attempt = h.manage_agent = forbidden
sys.argv = ['hg', 'run']
raise SystemExit(h.main())
'''
        stamp = h.now().isoformat()
        marker = self.state / 'last_attempt.json'
        marker.write_text(json.dumps({'outcome': 'completed', 'stage': 'sync', 'started_at': stamp,
                                      'finished_at': stamp, 'error_type': None}))
        before = marker.read_bytes(), self.last.read_bytes(), self.config_path.read_bytes()
        outside = self.root / 'outside-log'
        outside.write_bytes(b'Untouched external target')
        log = self.state / 'sync.log'
        for kind in ('fifo', 'symlink'):
            with self.subTest(kind=kind):
                os.mkfifo(log, 0o600) if kind == 'fifo' else log.symlink_to(outside)
                try:
                    self.assertEqual(self.child('-c', script, str(self.base), str(Path(h.__file__).parent)), 2)
                    self.assertEqual((marker.read_bytes(), self.last.read_bytes(), self.config_path.read_bytes()), before)
                    self.assertEqual(outside.read_bytes(), b'Untouched external target')
                    self.assertFalse((self.state / 'sync.sqlite3').exists())
                finally:
                    log.unlink()
                with (self.state / 'run.lock').open('a') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_fifo_with_reader_is_rejected_and_private_handler_rotates_utf8_normally(self):
        log = self.state / 'sync.log'
        os.mkfifo(log, 0o600)
        native_open = os.open
        reader = native_open(log, os.O_RDONLY | os.O_NONBLOCK)
        opened = []

        def open_fd(*args, **kwargs):
            fd = native_open(*args, **kwargs)
            opened.append(fd)
            return fd

        try:
            with patch.object(h.os, 'open', side_effect=open_fd), self.assertRaises(h.SafeError):
                h.PrivateRotatingFileHandler(log, encoding='utf-8')
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                os.fstat(opened[0])
            os.fstat(reader)
        finally:
            os.close(reader)
            log.unlink()
        with contextlib.closing(h.PrivateRotatingFileHandler(log, maxBytes=80, backupCount=2,
                                                           encoding='utf-8')) as handler:
            for number in range(20):
                handler.emit(logging.LogRecord('test', logging.INFO, '', 0, '记录 %02d café', (number,), None))
        paths = [log, *self.state.glob('sync.log.*')]
        self.assertEqual(len(paths), 3)
        for path in paths:
            path.read_bytes().decode('utf-8')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn('记录 19 café', log.read_text(encoding='utf-8'))

    def test_real_agent_invalid_initial_log_records_failure_without_starting_sync(self):
        self.install_temporary_script(OFFLINE + '\nconfig = api_client = run_with_attempt = manage_agent = forbidden\n')
        stamp = h.now().isoformat()
        marker = self.state / 'last_attempt.json'
        marker.write_text(json.dumps({'outcome': 'completed', 'stage': 'sync', 'started_at': stamp,
                                      'finished_at': stamp, 'error_type': None}))
        before = marker.read_bytes(), self.last.read_bytes(), self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes()
        outside = self.root / 'outside-log'
        outside.write_bytes(b'Untouched external target')
        log = self.state / 'sync.log'
        for kind in ('fifo', 'symlink'):
            with self.subTest(kind=kind):
                os.mkfifo(log, 0o600) if kind == 'fifo' else log.symlink_to(outside)
                try:
                    self.assertEqual(self.child(str(self.entry)), 2)
                    entry = json.loads(self.agent_attempt.read_text())
                    self.assertEqual((entry['outcome'], entry['stage']), ('failed', 'run'))
                    self.assertEqual((marker.read_bytes(), self.last.read_bytes(), self.export.read_bytes(),
                                      (self.tokens / 'oauth.json').read_bytes()), before)
                    self.assertEqual(outside.read_bytes(), b'Untouched external target')
                    self.assertFalse((self.state / 'sync.sqlite3').exists())
                    self.assertEqual(self.report()[0], 2)
                    self.assertNotIn('AssertionError', (self.state / 'agent.err.log').read_text())
                    for name in ('run.lock', 'agent-log.lock'):
                        with (self.state / name).open('a') as probe:
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    log.unlink()

    def test_real_rollover_reopen_failure_preserves_uncertain_and_recovers_without_repost(self):
        self.cfg['settle_seconds'] = 0
        self.save_config()
        source, tokens = self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes()
        measurement = h.load_measurements(self.cfg)[1][0]
        server = self.base / 'synthetic-server.json'
        mode = self.base / 'synthetic-rollover-mode'
        visible = self.base / 'synthetic-visible'
        outside = self.root / 'outside-log'
        outside.write_bytes(b'Untouched external target')
        self.install_temporary_script(OFFLINE + '''
factory = PrivateRotatingFileHandler
class ReopenObstacle(factory):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mode = BASE / 'synthetic-rollover-mode'
        if mode.exists():
            kind = mode.read_text()
            def rotate(source, destination):
                os.replace(source, destination)
                if kind == 'fifo':
                    os.mkfifo(source, 0o600)
                else:
                    Path(source).symlink_to(BASE.parent / 'outside-log')
                mode.unlink()
            self.rotator = rotate
PrivateRotatingFileHandler = ReopenObstacle
class LocalServerAPI:
    path = BASE / 'synthetic-server.json'
    def get_body_composition(self, start, end):
        value = json.loads(self.path.read_text())
        value['reads'] += 1
        write_json(self.path, value)
        return {'dateWeightList': value['rows'] if (BASE / 'synthetic-visible').exists() else []}
    def add_body_composition(self, **kwargs):
        value = json.loads(self.path.read_text())
        value['posts'] += 1
        at = aware(kwargs['timestamp'])
        value['rows'].append({'samplePk': 100, 'calendarDate': at.date().isoformat(),
                             'timestampGMT': int(at.timestamp() * 1000),
                             'weight': kwargs['weight'] * 1000, 'bodyFat': kwargs['percent_fat']})
        write_json(self.path, value)
        raise TimeoutError('private-secret')
    def add_weigh_in(self, **kwargs):
        return forbidden()
    def delete_weigh_in(self, *args, **kwargs):
        value = json.loads(self.path.read_text())
        value['deletes'] += 1
        write_json(self.path, value)
        return forbidden()
def api_client(interactive=False):
    assert not interactive
    return LocalServerAPI()
''')
        for kind in ('fifo', 'symlink'):
            with self.subTest(kind=kind):
                with contextlib.closing(h.database()) as db, db:
                    db.execute('DELETE FROM uploads')
                server.write_text(json.dumps({'posts': 0, 'deletes': 0, 'reads': 0, 'rows': []}))
                visible.unlink(missing_ok=True)
                mode.write_text(kind)
                log = self.state / 'sync.log'
                log.write_bytes(b'x' * 1_000_000)
                self.assertEqual(self.child(str(self.entry)), 2)
                with contextlib.closing(h.database()) as db:
                    self.assertEqual(h.get_status(db, measurement), 'uncertain')
                summary = json.loads(self.last.read_text())
                self.assertEqual(summary['results'], {measurement.key: 'uncertain'})
                self.assertEqual(summary['local_status_counts'], {'uncertain': 1})
                self.assertEqual(json.loads((self.state / 'last_attempt.json').read_text())['outcome'], 'failed')
                self.assertEqual(json.loads(self.agent_attempt.read_text())['outcome'], 'failed')
                self.assertEqual(self.report()[0], 2)
                self.assertEqual(outside.read_bytes(), b'Untouched external target')
                errors = (self.state / 'agent.err.log').read_text()
                self.assertNotIn('Traceback', errors)
                self.assertNotIn('private-secret', errors)
                self.assertNotIn(fixtures.SECRET, errors)
                log.unlink()
                self.assertEqual(self.child(str(self.entry)), 0)
                self.assertEqual(self.report()[0], 2)
                visible.touch()
                self.assertEqual(self.child(str(self.entry)), 0)
                with contextlib.closing(h.database()) as db:
                    self.assertEqual(h.get_status(db, measurement), 'verified')
                self.assertEqual(json.loads(self.last.read_text())['local_status_counts'], {'verified': 1})
                self.assertEqual(self.report()[0], 0)
                received = json.loads(server.read_text())
                self.assertEqual((received['posts'], received['deletes']), (1, 0))
                self.assertEqual((self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes()), (source, tokens))
