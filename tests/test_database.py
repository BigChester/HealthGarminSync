"""SQLite ownership and transaction behavior using temporary state only."""
import argparse
import contextlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

import health_garmin as h
import replace_history as bulk
import test_reconciliation as fixtures


class BackupFailureConnection(sqlite3.Connection):
    def backup(self, target, **kwargs):
        raise OSError('simulated backup failure')


class DatabaseLifecycleTests(unittest.TestCase):
    setUp = fixtures.ReconciliationTests.setUp
    tearDown = fixtures.ReconciliationTests.tearDown
    write_export = fixtures.ReconciliationTests.write_export
    plan = fixtures.ReconciliationTests.plan
    reviewed = fixtures.ReconciliationTests.reviewed
    sync = fixtures.ReconciliationTests.sync

    def track_connections(self, connection_type=sqlite3.Connection):
        connections = []
        connect = sqlite3.connect

        def tracked(*args, **kwargs):
            db = connect(*args, factory=connection_type, **kwargs)
            connections.append(db)
            self.addCleanup(db.close)
            return db

        return connections, patch.object(sqlite3, 'connect', side_effect=tracked)

    def assert_closed(self, connections):
        self.assertTrue(connections)
        for db in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_sync_commits_and_closes_owned_connection(self):
        connections, tracking = self.track_connections()

        def comparison(cfg, records, api, db, **kwargs):
            db.execute('INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?)',
                       (self.r.key, self.r.core, '{}', 'pending', 'test', ''))
            return {'counts': {}, 'entries': []}

        with tracking, patch.object(h, 'load_local_comparison', side_effect=comparison):
            self.sync(fixtures.API())
        self.assertEqual(h.get_status(self.db, self.r), 'pending')
        self.assert_closed(connections)

    def test_sync_exception_rolls_back_and_closes_owned_connection(self):
        connections, tracking = self.track_connections()

        def comparison(cfg, records, api, db, **kwargs):
            db.execute('INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?)',
                       (self.r.key, self.r.core, '{}', 'verified', 'test', ''))
            raise h.SafeError('simulated comparison failure')

        with tracking, patch.object(h, 'load_local_comparison', side_effect=comparison):
            with self.assertRaisesRegex(h.SafeError, 'simulated comparison failure'):
                self.sync(fixtures.API())
        self.assertIsNone(h.state_row(self.db, self.r))
        self.assert_closed(connections)

    def test_database_initialization_failure_closes_connection(self):
        connections, tracking = self.track_connections()
        with tracking, patch('pathlib.Path.chmod', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                h.database()
        self.assert_closed(connections)

    def test_existing_six_column_uploads_gain_independent_request_time_evidence(self):
        h.set_status(self.db, self.r, 'uncertain')
        original = dict(h.state_row(self.db, self.r))
        self.db.execute('DROP TABLE request_times'); self.db.commit()
        with contextlib.closing(h.database()) as upgraded:
            self.assertEqual(len(upgraded.execute('PRAGMA table_info(uploads)').fetchall()), 6)
            self.assertEqual(dict(h.state_row(upgraded, self.r)), original)
            self.assertFalse(upgraded.execute('SELECT * FROM request_times').fetchall())
            context = h.unknown_request_context(upgraded)
            self.assertEqual(context[0][2], h.measurement_wire_time(self.r))
        self.db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?)',
                        ('legacy-six-column', 'core', '{}', 'pending', 'time', 'detail'))
        self.db.commit()

    def test_request_time_stays_durable_while_latest_source_changes_and_releases_atomically(self):
        at = self.r.at.replace(microsecond=500001)
        local = h.Measurement(at, 70, None, 'PICOOC', lbm=53.55)
        request = h.Measurement(at, 70, 23.5, 'PICOOC', 22)
        h.set_status(self.db, local, 'pending')
        self.assertTrue(h.save_snapshot_status(self.db, local, 'inflight', 'submitted', request=request))
        wire_at = h.measurement_wire_time(request)
        with contextlib.closing(h.database()) as independent:
            self.assertEqual(h.get_status(independent, local), 'inflight')
            self.assertEqual(h.aware(independent.execute('SELECT wire_at FROM request_times WHERE record_id=?',
                                                       (local.key,)).fetchone()[0]), wire_at)
        latest = h.Measurement(at, 72, None, 'PICOOC', lbm=54)
        for status in ('uncertain', 'inflight', 'repair_uncertain'):
            h.set_status(self.db, latest, status)
            self.assertEqual(json.loads(h.state_row(self.db, latest)['payload']), latest.payload())
            self.assertEqual(h.state_row(self.db, latest)['core'], latest.core)
            self.assertEqual(h.aware(self.db.execute('SELECT wire_at FROM request_times WHERE record_id=?',
                                                   (local.key,)).fetchone()[0]), wire_at)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(h.save_snapshot_status(self.db, local, 'inflight', 'stale upload', request=request))
            self.assertFalse(h.save_verified_snapshot(self.db, local, 'stale completion'))
        self.assertEqual(h.aware(self.db.execute('SELECT wire_at FROM request_times WHERE record_id=?',
                                               (local.key,)).fetchone()[0]), wire_at)
        for status in ('verified', 'pending', 'verified_manual', 'ignored'):
            with self.subTest(release=status):
                h.set_status(self.db, latest, status)
                with contextlib.closing(h.database()) as independent:
                    self.assertEqual(h.get_status(independent, latest), status)
                    self.assertIsNone(independent.execute('SELECT wire_at FROM request_times WHERE record_id=?',
                                                          (local.key,)).fetchone())
                h.set_status(self.db, latest, 'inflight')
        # An older orphan cannot replace the actual format of the next permitted request.
        h.set_status(self.db, latest, 'pending')
        self.db.execute('INSERT INTO request_times VALUES (?,?)', (latest.key, wire_at.isoformat())); self.db.commit()
        h.set_status(self.db, latest, 'inflight', request=latest)
        self.assertEqual(h.aware(self.db.execute('SELECT wire_at FROM request_times WHERE record_id=?',
                                               (latest.key,)).fetchone()[0]), h.measurement_wire_time(latest))

    def test_request_time_and_upload_status_fail_together_before_post_or_release(self):
        for failure in ('wire_insert', 'upload_insert', 'release_upload'):
            with self.subTest(failure=failure):
                self.db.execute('DELETE FROM uploads'); self.db.execute('DELETE FROM request_times'); self.db.commit()
                if failure == 'release_upload':
                    h.set_status(self.db, self.r, 'inflight')
                table = 'request_times' if failure == 'wire_insert' else 'uploads'
                self.db.execute(f"CREATE TRIGGER fail_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
                self.db.commit()
                api = fixtures.API([fixtures.row(self.r)] if failure == 'release_upload' else [])
                try:
                    with self.assertRaises(sqlite3.IntegrityError), self.db:
                        h.process_record(api, self.db, self.r)
                    self.assertFalse(api.posts)
                    with contextlib.closing(h.database()) as independent:
                        saved = h.state_row(independent, self.r)
                        wire = independent.execute('SELECT wire_at FROM request_times WHERE record_id=?', (self.r.key,)).fetchone()
                        if failure == 'release_upload':
                            self.assertEqual(saved['status'], 'inflight')
                            self.assertEqual(h.aware(wire[0]), h.measurement_wire_time(self.r))
                        else:
                            self.assertIsNone(saved)
                            self.assertIsNone(wire)
                finally:
                    self.db.execute('DROP TRIGGER fail_write'); self.db.commit()

    def check_backup(self, full, failure=False):
        api = fixtures.API([dict(fixtures.row(self.r), bodyFat=None)])
        path = self.plan(api.rows, save=True)
        h.set_status(self.db, self.r, 'pending')
        connection_type = BackupFailureConnection if failure else sqlite3.Connection
        connections, tracking = self.track_connections(connection_type)

        def invoke():
            if full:
                args = argparse.Namespace(plan=str(path), complete_export=True)
                with patch('builtins.input', return_value='REPLACE ALL'), contextlib.redirect_stdout(io.StringIO()):
                    bulk.execute(args, self.cfg, api)
            else:
                self.reviewed(api, path)

        with tracking:
            if failure:
                with self.assertRaisesRegex(OSError, 'simulated backup failure'):
                    invoke()
            else:
                invoke()
        self.assertGreaterEqual(len(connections), 2)
        self.assert_closed(connections)
        if failure:
            self.assertFalse(api.deletes)
            self.assertFalse(api.posts)
        else:
            backup_path = next((self.state / 'backups').glob('*/sync.sqlite3'))
            with contextlib.closing(sqlite3.connect(backup_path)) as backup:
                status = backup.execute('SELECT status FROM uploads WHERE record_id=?',
                                        (self.r.key,)).fetchone()[0]
            self.assertEqual(status, 'pending')
            self.assertEqual(len(api.deletes), 1)
            self.assertEqual(len(api.posts), 1)

    def test_reviewed_backup_preserves_state_and_closes_connections(self):
        self.check_backup(full=False)

    def test_reviewed_backup_failure_closes_connections_before_remote_mutation(self):
        self.check_backup(full=False, failure=True)

    def test_full_replacement_backup_preserves_state_and_closes_connections(self):
        self.check_backup(full=True)

    def test_full_replacement_backup_failure_closes_connections_before_remote_mutation(self):
        self.check_backup(full=True, failure=True)


if __name__ == '__main__':
    unittest.main()
