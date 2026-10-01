"""SQLite ownership and transaction behavior using temporary state only."""
import argparse
import contextlib
import io
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
