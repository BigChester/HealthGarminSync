"""Bulk replacement invariants with an offline Garmin substitute."""
import argparse
import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
from unittest.mock import patch
import unittest

import health_garmin as h
import replace_history as bulk
import test_reconciliation as fixtures
from test_reconciliation import API, row


class BulkReplacementTests(unittest.TestCase):
    setUp = fixtures.ReconciliationTests.setUp
    tearDown = fixtures.ReconciliationTests.tearDown
    write_export = fixtures.ReconciliationTests.write_export
    plan = fixtures.ReconciliationTests.plan
    def invoke(self, api, path, answer='REPLACE ALL', complete=True):
        args = argparse.Namespace(plan=str(path), complete_export=complete)
        with patch('builtins.input', return_value=answer), contextlib.redirect_stdout(io.StringIO()):
            bulk.execute(args, self.cfg, api)

    def test_full_replacement_preserves_match_removes_near_conflict_and_extra(self):
        first = self.r
        second = h.Measurement(first.at + timedelta(hours=1), 71, 24, 'PICOOC', 23, 54)
        third = h.Measurement(first.at + timedelta(days=1), 72, 25, 'PICOOC', 24, 54)
        self.write_export([first, second, third])
        near = h.Measurement(second.at + timedelta(seconds=20), 73, None, 'PICOOC')
        extra = h.Measurement(first.at + timedelta(hours=2), 74, None, 'PICOOC')
        api = API([row(first, 100), row(near, 101), row(extra, 102)])
        obj = h.fetch_remote_snapshot(api, first.at.date(), third.at.date())
        path = h.save_comparison(h.comparison_plan(self.db, [first, second, third], obj,
                                 first.at.date(), third.at.date()), self.cfg)
        self.invoke(api, path)
        self.assertEqual(api.deletes, [('101', first.at.date().isoformat()), ('102', first.at.date().isoformat())])
        self.assertEqual(len(api.posts), 2)
        self.assertIn(100, [r['samplePk'] for r in api.rows])
        self.assertEqual(len(api.rows), 3)
        journal = json.loads(next((self.state / 'operations').glob('full-*.json')).read_text())
        self.assertEqual(journal['phase'], 'verified')
        self.assertEqual(journal['verification']['remote_count'], 3)
        self.assertTrue((Path(journal['backup']) / 'sync.sqlite3').exists())
        self.assertTrue((Path(journal['backup']) / 'exports' / 'health.json').exists())
        self.assertEqual(json.loads(h.state_row(self.db, second)['payload'])['lbm'], 54)

    def test_cancel_performs_no_remote_mutation(self):
        api = API([dict(row(self.r), bodyFat=None)])
        path = self.plan(api.rows, save=True)
        self.invoke(api, path, answer='CANCEL')
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_complete_export_required(self):
        api = API([row(self.r)]); path = self.plan(api.rows, save=True)
        with self.assertRaises(h.SafeError):
            self.invoke(api, path, complete=False)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_remote_change_rejects_entire_batch_before_writes(self):
        api = API([row(self.r)]); path = self.plan(api.rows, save=True)
        api.rows[0]['weight'] = 71000
        with self.assertRaises(h.SafeError):
            self.invoke(api, path)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_remote_change_during_confirmation_rejects_before_writes(self):
        api = API([row(self.r)]); path = self.plan(api.rows, save=True)
        def confirm(_):
            api.rows[0]['weight'] = 71000
            return 'REPLACE ALL'
        args = argparse.Namespace(plan=str(path), complete_export=True)
        with patch('builtins.input', side_effect=confirm), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(h.SafeError): bulk.execute(args, self.cfg, api)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_delete_timeout_keeps_backup_and_never_starts_upload(self):
        api = API([dict(row(self.r), bodyFat=None)]); api.delete_timeout = True
        path = self.plan(api.rows, save=True)
        with self.assertRaises(h.SafeError): self.invoke(api, path)
        self.assertEqual(len(api.deletes), 1)
        self.assertFalse(api.posts)
        journal = json.loads(next((self.state / 'operations').glob('full-*.json')).read_text())
        self.assertEqual(journal['phase'], 'uncertain')
        self.assertTrue((Path(journal['backup']) / 'remote-before.json').exists())
        self.assertNotIn('secret-ticket', json.dumps(journal))

    def test_uncertain_upload_stops_without_retry_and_blocks_other_jobs(self):
        api = API(); api.visible = False
        path = self.plan([], save=True)
        with patch.object(h, 'process_record', side_effect=lambda api, db, r, **kw: h_process(api, db, r, sleep=lambda _: None, **kw)):
            with self.assertRaises(h.SafeError): self.invoke(api, path)
        self.assertEqual(len(api.posts), 1)
        self.assertIn(self.r.key, h.active_operations())
        with self.assertRaises(h.SafeError): self.invoke(api, path)
        self.assertEqual(len(api.posts), 1)

    def test_verification_rejects_nearby_unclaimed_duplicate(self):
        extra = h.Measurement(self.r.at + timedelta(seconds=30), 75, None, 'PICOOC')
        api = API([row(self.r, 100), row(extra, 101)])
        backup = self.state / 'backups' / 'test'; h.private_dir(backup)
        path = self.state / 'operations' / 'full-test.json'
        h.write_json(path, {'type': 'full_replacement', 'key': 'full-test',
            'phase': 'uncertain', 'start': self.r.at.date().isoformat(), 'end': self.r.at.date().isoformat(),
            'targets': [self.r.payload()], 'record_ids': [self.r.key], 'backup': str(backup)})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(bulk.verify(path, api))
        self.assertEqual(json.loads(path.read_text())['phase'], 'uncertain')
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_two_reviewed_weigh_ins_in_same_minute_are_both_uploaded(self):
        second = h.Measurement(self.r.at + timedelta(seconds=30), 71, 24, 'PICOOC', 23, 54)
        self.write_export([self.r, second]); api = API()
        obj = {'dateWeightList': []}
        path = h.save_comparison(h.comparison_plan(self.db, [self.r, second], obj,
                                 self.r.at.date(), self.r.at.date()), self.cfg)
        self.invoke(api, path)
        self.assertEqual(len(api.posts), 2)
        self.assertEqual(len(api.rows), 2)
        self.assertTrue(all(h.classify_remote(r, {'dateWeightList': api.rows}) == 'matched' for r in [self.r, second]))

    def test_unreviewed_nearby_measurement_is_not_hidden(self):
        other = h.Measurement(self.r.at + timedelta(seconds=30), 71, 24, 'PICOOC')
        api = API([row(other)])
        scoped = bulk.TargetAPI(api, self.r, [self.r])
        self.assertEqual(h.classify_remote(self.r, h.remote_for(scoped, self.r)), 'conflict')
        self.assertFalse(api.posts)

    def test_partial_close_refuses_any_unverified_upload_intent(self):
        path = self.state / 'operations' / 'full-test.json'
        h.write_json(path, {'type': 'full_replacement', 'key': 'full-test', 'phase': 'uncertain',
            'record_ids': [self.r.key], 'events': [{'kind': 'upload_intent', 'id': self.r.key}]})
        with self.assertRaises(h.SafeError):
            bulk.close_verified_partial(path, dict(self.cfg, enabled=False), API())
        self.assertIn(self.r.key, h.active_operations())
        self.assertEqual(json.loads(path.read_text())['phase'], 'uncertain')

    def test_partial_close_independently_checks_completed_uploads(self):
        backup = self.state / 'backups' / 'partial'; h.private_dir(backup)
        h.write_json(backup / 'remote-before.json', {'dateWeightList': []})
        path = self.state / 'operations' / 'full-test.json'
        h.write_json(path, {'type': 'full_replacement', 'key': 'full-test', 'phase': 'uncertain',
            'record_ids': [self.r.key], 'start': self.r.at.date().isoformat(), 'end': self.r.at.date().isoformat(),
            'targets': [self.r.payload()], 'backup': str(backup),
            'events': [{'kind': 'upload_intent', 'id': self.r.key}, {'kind': 'upload_verified', 'id': self.r.key}]})
        api = API([row(self.r)])
        with contextlib.redirect_stdout(io.StringIO()):
            obj = bulk.close_verified_partial(path, dict(self.cfg, enabled=False), api)
        self.assertEqual(len(obj['dateWeightList']), 1)
        self.assertEqual(json.loads(path.read_text())['phase'], 'closed_partial')
        self.assertNotIn(self.r.key, h.active_operations())
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def partial_with_summary_backup(self, hidden_after_latest=False):
        original = h.Measurement(self.r.at + timedelta(hours=4), 75, None, 'PICOOC')
        hidden = h.Measurement(original.at + timedelta(hours=1 if hidden_after_latest else -1), 76, None, 'PICOOC')
        backup = self.state / 'backups' / 'summary'; h.private_dir(backup)
        h.write_json(backup / 'remote-before.json', {'dateWeightList': [row(original, 100)]})
        path = self.state / 'operations' / 'full-test.json'
        h.write_json(path, {'type': 'full_replacement', 'key': 'full-test', 'phase': 'uncertain',
            'record_ids': [self.r.key], 'start': self.r.at.date().isoformat(), 'end': self.r.at.date().isoformat(),
            'targets': [self.r.payload()], 'backup': str(backup),
            'events': [{'kind': 'upload_intent', 'id': self.r.key}, {'kind': 'upload_verified', 'id': self.r.key}]})
        return path, backup, API([row(self.r, 200), row(original, 100), row(hidden, 101)])

    def test_partial_close_supplements_only_earlier_hidden_day_details(self):
        path, backup, api = self.partial_with_summary_backup()
        with contextlib.redirect_stdout(io.StringIO()):
            bulk.close_verified_partial(path, dict(self.cfg, enabled=False), api)
        self.assertEqual(json.loads(path.read_text())['phase'], 'closed_partial')
        self.assertEqual(len(json.loads((backup / 'remote-before.json').read_text())['dateWeightList']), 1)
        self.assertEqual(len(json.loads((backup / 'remote-before-details-reconstructed.json').read_text())['dateWeightList']), 2)

    def test_partial_close_does_not_accept_a_later_unexpected_measurement(self):
        path, backup, api = self.partial_with_summary_backup(hidden_after_latest=True)
        with self.assertRaises(h.SafeError):
            bulk.close_verified_partial(path, dict(self.cfg, enabled=False), api)
        self.assertEqual(json.loads(path.read_text())['phase'], 'uncertain')
        self.assertFalse((backup / 'remote-before-details-reconstructed.json').exists())


h_process = h.process_record
