"""Bulk replacement invariants with an offline Garmin substitute."""
import argparse
import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
import threading
from unittest.mock import Mock, patch, seal
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
    native_deletion_api = fixtures.ReconciliationTests.native_deletion_api
    def invoke(self, api, path, answer='REPLACE ALL', complete=True):
        args = argparse.Namespace(plan=str(path), complete_export=complete)
        with patch('builtins.input', return_value=answer), contextlib.redirect_stdout(io.StringIO()):
            bulk.execute(args, self.cfg, api)

    def detailed_api(self, backend):
        raw = Mock(spec_set=['get_weigh_ins', 'get_daily_weigh_ins', 'add_body_composition',
                             'add_weigh_in', 'delete_weigh_in'])
        def summaries(start, end):
            rows = backend.get_body_composition(start, end)['dateWeightList']
            days = {}
            for item in rows:
                days.setdefault(item['calendarDate'], []).append(item)
            return {'dailyWeightSummaries': [
                {'summaryDate': day, 'numOfWeightEntries': len(items), 'allWeightMetrics': items}
                for day, items in sorted(days.items())
            ]}
        raw.get_weigh_ins.side_effect = summaries
        raw.get_daily_weigh_ins.side_effect = lambda day: backend.get_body_composition(day, day)
        raw.add_body_composition.side_effect = backend.add_body_composition
        raw.add_weigh_in.side_effect = backend.add_weigh_in
        raw.delete_weigh_in.side_effect = backend.delete_weigh_in
        seal(raw)
        return h.GarminMeasurementAPI(raw)

    def assert_concurrent_export_backup_rejected(self, intermediate):
        source = self.inbox / 'health.json'
        original = source.read_bytes()
        token = self.state / 'tokens' / 'synthetic'
        h.private_dir(token.parent)
        token.write_bytes(b'synthetic private token')
        backend = API([dict(row(self.r), bodyFat=None)])
        api = self.detailed_api(backend)
        obj = h.fetch_remote_snapshot(api, self.r.at.date(), self.r.at.date())
        plan = h.save_comparison(h.comparison_plan(self.db, [self.r], obj,
            self.r.at.date(), self.r.at.date()), self.cfg)
        begin, ready, copied, restored = [threading.Event() for _ in range(4)]
        failures = []
        def exporter():
            try:
                if not begin.wait(2):
                    raise AssertionError('Copy did not start')
                with source.open('wb') as stream:
                    stream.write(intermediate)
                    stream.flush()
                    ready.set()
                    if not copied.wait(2):
                        raise AssertionError('Copy did not finish')
                    stream.seek(0)
                    stream.write(original)
                    stream.truncate()
                    stream.flush()
            except BaseException as exc:
                failures.append(exc)
            finally:
                restored.set()
        worker = threading.Thread(target=exporter, daemon=True)
        native_copy = bulk.shutil.copyfile
        def concurrent_copy(src, dst):
            self.assertEqual(src, source)
            begin.set()
            self.assertTrue(ready.wait(2))
            try:
                return native_copy(src, dst)
            finally:
                copied.set()
                self.assertTrue(restored.wait(2))
        worker.start()
        try:
            with (patch.object(bulk.shutil, 'copyfile', side_effect=concurrent_copy),
                  patch.object(h, 'api_client', side_effect=AssertionError('No login')),
                  patch.object(Path, 'home', side_effect=AssertionError('No real home')),
                  patch('socket.socket', side_effect=AssertionError('No network')),
                  patch('subprocess.run', side_effect=AssertionError('No service'))):
                with self.assertRaises(h.SafeError):
                    self.invoke(api, plan)
        finally:
            begin.set()
            copied.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)
        self.assertFalse(backend.deletes)
        self.assertFalse(backend.posts)
        self.assertEqual(backend.rows, [dict(row(self.r), bodyFat=None)])
        self.assertIsNone(h.state_row(self.db, self.r))
        self.assertFalse(list((self.state / 'operations').glob('*.json')))
        backup = next((self.state / 'backups').glob('full-*'))
        self.assertEqual((backup / 'exports' / source.name).read_bytes(), intermediate)
        self.assertEqual(json.loads((backup / 'targets.json').read_text()), [self.r.payload()])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(token.read_bytes(), b'synthetic private token')

    def test_torn_export_copy_is_rejected_even_after_active_source_recovers(self):
        original = (self.inbox / 'health.json').read_bytes()
        intermediate = original[:len(original) // 2]
        with self.assertRaises(json.JSONDecodeError):
            json.loads(intermediate)
        self.assert_concurrent_export_backup_rejected(intermediate)

    def test_valid_backup_with_different_targets_is_rejected_after_source_recovers(self):
        obj = json.loads((self.inbox / 'health.json').read_text())
        obj['metrics']['weight'][0]['value'] = self.r.weight + 1
        intermediate = json.dumps(obj).encode('utf-8')
        self.assert_concurrent_export_backup_rejected(intermediate)

    def test_delete_readback_unknown_or_same_normalized_id_stops_entire_batch(self):
        original = h.Measurement(self.r.at + timedelta(hours=1), 74, None, 'PICOOC')
        untouched = row(h.Measurement(self.r.at + timedelta(hours=2), 75, None, 'PICOOC'), 101)
        source = (self.inbox / 'health.json').read_bytes()
        for number, replacement_id in enumerate((None, 'invalid-id', '00100')):
            with self.subTest(readback_id=replacement_id), patch.object(h, 'STATE', self.state / str(number)):
                h.private_dir(h.STATE)
                token = h.STATE / 'tokens' / 'synthetic.json'
                h.write_json(token, {'synthetic': 'private-cache'})
                token_bytes = token.read_bytes()
                backend = API([row(original, 100), untouched])
                delete = backend.delete_weigh_in
                def incomplete_identity(pk, day):
                    if pk == '100':
                        backend.deletes.append((pk, day))
                        backend.rows[0]['samplePk'] = replacement_id
                    else:
                        delete(pk, day)
                backend.delete_weigh_in = incomplete_identity
                api = self.detailed_api(backend)
                with contextlib.closing(h.database()) as db:
                    obj = h.fetch_remote_snapshot(api, self.r.at.date(), self.r.at.date())
                    path = h.save_comparison(h.comparison_plan(db, [self.r], obj,
                        self.r.at.date(), self.r.at.date()), self.cfg)
                    with (patch.object(h, 'api_client', side_effect=AssertionError('No login')),
                          patch('socket.create_connection', side_effect=AssertionError('No network')),
                          patch('subprocess.run', side_effect=AssertionError('No service')),
                          patch.object(Path, 'home', side_effect=AssertionError('No real home'))):
                        with self.assertRaises(h.SafeError):
                            self.invoke(api, path)
                    self.assertEqual(backend.deletes, [('100', original.at.date().isoformat())])
                    self.assertFalse(backend.posts)
                    self.assertEqual(backend.rows[1], untouched)
                    self.assertIsNone(h.state_row(db, self.r))
                job = h.read_operation(next((h.STATE / 'operations').glob('full-*.json')))
                self.assertEqual(job['phase'], 'uncertain')
                self.assertEqual([(event['kind'], event['id']) for event in job['events']],
                                 [('delete_intent', '100')])
                self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
                self.assertEqual((Path(job['backup']) / 'exports' / 'health.json').read_bytes(), source)
                self.assertEqual(token.read_bytes(), token_bytes)

    def test_complete_delete_readback_allows_remaining_deletes_and_one_upload(self):
        extras = [row(h.Measurement(self.r.at + timedelta(hours=n), 73 + n, None, 'PICOOC'), 99 + n)
                  for n in (1, 2)]
        backend = API(extras)
        api = self.detailed_api(backend)
        source = (self.inbox / 'health.json').read_bytes()
        token = self.state / 'tokens' / 'synthetic.json'
        h.write_json(token, {'synthetic': 'private-cache'})
        token_bytes = token.read_bytes()
        obj = h.fetch_remote_snapshot(api, self.r.at.date(), self.r.at.date())
        path = h.save_comparison(h.comparison_plan(self.db, [self.r], obj,
            self.r.at.date(), self.r.at.date()), self.cfg)
        self.invoke(api, path)
        self.assertEqual(backend.deletes, [('100', self.r.at.date().isoformat()),
                                          ('101', self.r.at.date().isoformat())])
        self.assertEqual(len(backend.posts), 1)
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        job = h.read_operation(next((self.state / 'operations').glob('full-*.json')))
        self.assertEqual(job['phase'], 'verified')
        self.assertEqual([event['kind'] for event in job['events']],
                         ['delete_intent', 'delete_verified', 'delete_intent', 'delete_verified',
                          'upload_intent', 'upload_verified'])
        self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
        self.assertEqual(token.read_bytes(), token_bytes)

    def test_native_batch_keeps_same_id_only_on_its_matched_calendar_day(self):
        other = h.Measurement(self.r.at + timedelta(days=1), 71, 24, 'PICOOC')
        records = [self.r, other]
        self.write_export(records)
        source = (self.inbox / 'health.json').read_bytes()
        for matched in (False, True):
            with self.subTest(second_day_matched=matched), patch.object(h, 'STATE', self.state / str(matched)):
                h.private_dir(h.STATE)
                token = h.STATE / 'tokens' / 'synthetic.json'
                h.write_json(token, {'synthetic': 'private-cache'})
                token_bytes = token.read_bytes()
                keeper = row(self.r, 100)
                second = row(other, 100) if matched else dict(row(other, 100), bodyFat=None)
                backend = API([keeper, second])
                api = self.native_deletion_api(backend, fail_at=None)
                with contextlib.closing(h.database()) as db:
                    obj = h.fetch_remote_snapshot(api, self.r.at.date(), other.at.date())
                    path = h.save_comparison(h.comparison_plan(db, records, obj,
                        self.r.at.date(), other.at.date()), self.cfg)
                    output = io.StringIO()
                    with patch('builtins.input', return_value='REPLACE ALL'), contextlib.redirect_stdout(output):
                        bulk.execute(argparse.Namespace(plan=str(path), complete_export=True), self.cfg, api)
                    self.assertIn('目标 2 条；保留匹配 ' + ('2' if matched else '1') +
                                  ' 条；删除旧记录 ' + ('0' if matched else '1') +
                                  ' 条；重建/新增 ' + ('0' if matched else '1') + ' 条。', output.getvalue())
                    self.assertEqual([h.get_status(db, record) for record in records], ['verified', 'verified'])
                job = h.read_operation(next((h.STATE / 'operations').glob('full-*.json')))
                self.assertEqual(job['phase'], 'verified')
                self.assertEqual(backend.deletes, [] if matched else [('100', other.at.date().isoformat())])
                self.assertEqual(len(backend.posts), 0 if matched else 1)
                self.assertEqual(backend.rows[0], keeper)
                self.assertEqual([event['kind'] for event in job['events']], [] if matched else
                                 ['delete_intent', 'delete_verified', 'upload_intent', 'upload_verified'])
                backup = Path(job['backup'])
                self.assertEqual(json.loads((backup / 'remote-before.json').read_text()), obj)
                self.assertEqual((backup / 'exports' / 'health.json').read_bytes(), source)
                self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
                self.assertEqual(token.read_bytes(), token_bytes)

    def test_native_batch_deletes_reused_id_only_on_each_original_calendar_day(self):
        other = h.Measurement(self.r.at + timedelta(days=1), 71, 24, 'PICOOC')
        records = [self.r, other]
        self.write_export(records)
        source = (self.inbox / 'health.json').read_bytes()
        token = self.state / 'tokens' / 'synthetic.json'
        h.write_json(token, {'synthetic': 'private-cache'})
        token_bytes = token.read_bytes()
        backend = API([dict(row(self.r, 100), bodyFat=None), dict(row(other, 100), bodyFat=None)])
        api = self.native_deletion_api(backend, fail_at=None)
        obj = h.fetch_remote_snapshot(api, self.r.at.date(), other.at.date())
        path = h.save_comparison(h.comparison_plan(self.db, records, obj,
            self.r.at.date(), other.at.date()), self.cfg)
        self.invoke(api, path)
        job = h.read_operation(next((self.state / 'operations').glob('full-*.json')))
        self.assertEqual(job['phase'], 'verified')
        self.assertEqual(backend.deletes, [('100', self.r.at.date().isoformat()),
                                          ('100', other.at.date().isoformat())])
        self.assertEqual(len(backend.posts), 2)
        self.assertEqual([event['id'] for event in job['events'] if event['kind'] == 'delete_verified'],
                         ['100', '100'])
        self.assertEqual([h.get_status(self.db, record) for record in records], ['verified', 'verified'])
        self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
        self.assertEqual(token.read_bytes(), token_bytes)

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

    def test_old_full_verification_preserves_current_measurement_and_ignore(self):
        original = h.Measurement(self.r.at, 70, 23.5, 'PICOOC', 22, 53.55)
        variants = (
            ('lbm', h.Measurement(original.at, 70, 23.5, 'PICOOC', 22, 54)),
            ('weight', h.Measurement(original.at, 71, 23.5, 'PICOOC', 22, 54)),
            ('fat', h.Measurement(original.at, 70, 24, 'PICOOC', 22, 54)),
            ('bmi', h.Measurement(original.at, 70, 23.5, 'PICOOC', 23, 54)),
            ('ignored', original),
        )
        for mode, current in variants:
            with self.subTest(mode=mode), patch.object(h, 'STATE', self.state / mode):
                h.private_dir(h.STATE)
                self.write_export([original])
                token = h.STATE / 'tokens' / 'synthetic'
                h.private_dir(token.parent)
                token.write_bytes(b'synthetic private token')
                backend = API([row(original)])
                api = self.native_deletion_api(backend, fail_at=None)
                with contextlib.closing(h.database()) as db:
                    obj = h.fetch_remote_snapshot(api, original.at.date(), original.at.date())
                    plan = h.save_comparison(h.comparison_plan(db, [original], obj,
                        original.at.date(), original.at.date()), self.cfg)
                    self.invoke(api, plan)
                    journal = next((h.STATE / 'operations').glob('full-*.json'))
                    job = h.read_operation(journal)
                    self.assertEqual(job['phase'], 'verified')
                    backup = Path(job['backup'])
                    evidence = {p.name: p.read_bytes() for p in backup.glob('*.json')}
                    self.write_export([current])
                    with (patch.object(h, 'api_client', return_value=api),
                          contextlib.redirect_stdout(io.StringIO())):
                        h.run_sync(self.cfg)
                        if mode == 'ignored':
                            with patch('builtins.input', return_value='IGNORE'):
                                h.resolve_record(argparse.Namespace(record_id=current.key,
                                    present=False, absent=False, ignore=True))
                    stored = dict(h.state_row(db, current))
                    self.assertEqual(json.loads(stored['payload']), current.payload())
                    self.assertEqual(stored['status'], 'ignored' if mode == 'ignored' else
                                     'verified' if mode == 'lbm' else 'verified_conflict')
                    source = (self.inbox / 'health.json').read_bytes()
                    for _ in range(2):
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertTrue(bulk.verify(journal, api))
                        checked = dict(h.state_row(db, current))
                        if mode == 'lbm':
                            self.assertEqual({key: checked[key] for key in ('core', 'payload', 'status')},
                                             {key: stored[key] for key in ('core', 'payload', 'status')})
                        else:
                            self.assertEqual(checked, stored)
                        with patch.object(h, 'api_client', return_value=api), contextlib.redirect_stdout(io.StringIO()):
                            h.run_sync(self.cfg)
                        refreshed = dict(h.state_row(db, current))
                        self.assertEqual(refreshed['status'], stored['status'])
                        self.assertEqual(refreshed['core'], stored['core'])
                        self.assertEqual(refreshed['payload'], stored['payload'])
                        stored = refreshed
                    self.assertEqual(h.read_operation(journal)['targets'], [original.payload()])
                    self.assertEqual(h.read_operation(journal)['phase'], 'verified')
                    self.assertEqual({p.name: p.read_bytes() for p in backup.glob('*.json')}, evidence)
                    self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
                    self.assertEqual(token.read_bytes(), b'synthetic private token')
                    self.assertFalse(backend.posts)
                    self.assertFalse(backend.deletes)

    def test_complete_export_required(self):
        api = API([row(self.r)]); path = self.plan(api.rows, save=True)
        with self.assertRaises(h.SafeError):
            self.invoke(api, path, complete=False)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_full_upload_readback_preserves_local_evidence_updated_during_submission(self):
        original = h.Measurement(self.r.at, 70, 23.5, 'PICOOC', 22, 53.55)
        for weight in (70, 72):
            with self.subTest(new_weight=weight), patch.object(h, 'STATE', self.state / str(weight)):
                h.private_dir(h.STATE)
                self.write_export([original])
                current = h.Measurement(original.at, weight, 23.5, 'PICOOC', 22, 54)
                token = h.STATE / 'tokens' / 'synthetic'
                h.private_dir(token.parent)
                token.write_bytes(b'synthetic private token')
                backend = API([row(h.Measurement(original.at, 71, 23.5, 'PICOOC', 22))])
                post = backend.add_body_composition
                revised_bytes = []
                def update_during_post(**kwargs):
                    response = post(**kwargs)
                    self.write_export([current])
                    revised_bytes.append((self.inbox / 'health.json').read_bytes())
                    with patch.object(h, 'api_client', return_value=api):
                        h.run_sync(self.cfg)
                    return response
                backend.add_body_composition = update_during_post
                api = self.native_deletion_api(backend, fail_at=None)
                with contextlib.closing(h.database()) as db:
                    obj = h.fetch_remote_snapshot(api, original.at.date(), original.at.date())
                    plan = h.save_comparison(h.comparison_plan(db, [original], obj,
                        original.at.date(), original.at.date()), self.cfg)
                    self.invoke(api, plan)
                    stored = h.state_row(db, current)
                    self.assertEqual((stored['core'], stored['status']),
                                     (current.core, 'verified' if weight == 70 else 'repair_uncertain'))
                    self.assertEqual(json.loads(stored['payload']), current.payload())
                    journal = next((h.STATE / 'operations').glob('full-*.json'))
                    job = h.read_operation(journal)
                    self.assertEqual(job['phase'], 'verified')
                    self.assertEqual(job['targets'], [original.payload()])
                    self.assertEqual((self.inbox / 'health.json').read_bytes(),
                                     revised_bytes[0])
                    self.assertEqual(h.load_measurements(self.cfg)[1], [current])
                    self.assertEqual(h.load_measurements(dict(self.cfg,
                        inbox=str(Path(job['backup']) / 'exports')))[1], [original])
                    self.assertEqual(token.read_bytes(), b'synthetic private token')
                    self.assertEqual(backend.deletes, [('100', original.at.date().isoformat())])
                    self.assertEqual(len(backend.posts), 1)
                    self.assertNotIn('lbm', backend.posts[0])
                    self.assertNotIn('muscle_mass', backend.posts[0])

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

    def paused_two_day_batch(self, second_id=101):
        other = h.Measurement(self.r.at + timedelta(days=1), 71, 24, 'PICOOC')
        records = [self.r, other]
        self.write_export(records)
        backend = API([dict(row(self.r, 100), bodyFat=None), dict(row(other, second_id), bodyFat=None)])
        api = self.native_deletion_api(backend, fail_at=None)
        native_get, stopped = backend.get_body_composition, False
        def fail_next_day(start, end):
            nonlocal stopped
            if start == end == other.at.date().isoformat() and backend.posts and not stopped:
                stopped = True
                raise TimeoutError('synthetic day-two interruption')
            return native_get(start, end)
        backend.get_body_composition = fail_next_day
        obj = h.fetch_remote_snapshot(api, self.r.at.date(), other.at.date())
        plan = h.save_comparison(h.comparison_plan(self.db, records, obj,
                                 self.r.at.date(), other.at.date()), self.cfg)
        with self.assertRaises(h.SafeError):
            self.invoke(api, plan)
        journal = next((h.STATE / 'operations').glob('full-*.json'))
        self.assertEqual(h.read_operation(journal)['phase'], 'uncertain')
        self.assertEqual(len(backend.deletes), 1)
        self.assertEqual(len(backend.posts), 1)
        return api, backend, journal, other

    def test_partial_close_uses_durable_dates_for_reused_ids_and_legacy_unique_ids(self):
        for mode in ('dated', 'alias', 'legacy_unique', 'legacy_ambiguous', 'wrong_day', 'invalid_day'):
            with self.subTest(mode=mode), patch.object(h, 'STATE', self.state / mode):
                h.private_dir(h.STATE)
                api, backend, journal, other = self.paused_two_day_batch(
                    second_id=101 if mode == 'legacy_unique' else 100)
                original = h.read_operation(journal)
                deletes = [e for e in original['events'] if e['kind'].startswith('delete_')]
                self.assertEqual([e['calendarDate'] for e in deletes], [self.r.at.date().isoformat()] * 2)
                if mode.startswith('legacy_'):
                    for event in deletes:
                        event.pop('calendarDate')
                elif mode in ('wrong_day', 'invalid_day'):
                    for event in deletes:
                        event['calendarDate'] = other.at.date().isoformat() if mode == 'wrong_day' else '2020-02-30'
                if mode != 'dated' and mode != 'alias':
                    h.write_json(journal, original)
                if mode == 'alias':
                    next(item for item in backend.rows if item['samplePk'] == 100)['samplePk'] = '00100'
                journal_bytes = journal.read_bytes()
                backup = Path(original['backup'])
                evidence = {p.name: p.read_bytes() for p in backup.glob('*.json')}
                source = (self.inbox / 'health.json').read_bytes()
                token = h.STATE / 'tokens' / 'synthetic'
                h.private_dir(token.parent)
                token.write_bytes(b'synthetic private token')
                remote = json.loads(json.dumps(backend.rows))
                with contextlib.redirect_stdout(io.StringIO()):
                    if mode in ('dated', 'alias', 'legacy_unique'):
                        result = bulk.close_verified_partial(journal, dict(self.cfg, enabled=False), api)
                        self.assertCountEqual(result['dateWeightList'], remote)
                        updated = h.read_operation(journal)
                        self.assertEqual(updated['phase'], 'closed_partial')
                        self.assertEqual(updated['partial_verification'], {'uploads': 1, 'deletes': 1, 'remote_count': 2})
                        self.assertEqual(updated['events'], original['events'])
                    else:
                        with self.assertRaises(h.SafeError):
                            bulk.close_verified_partial(journal, dict(self.cfg, enabled=False), api)
                        self.assertEqual(journal.read_bytes(), journal_bytes)
                self.assertEqual(backend.deletes, [('100', self.r.at.date().isoformat())])
                self.assertEqual(len(backend.posts), 1)
                self.assertEqual(backend.rows, remote)
                self.assertEqual({p.name: p.read_bytes() for p in backup.glob('*.json')}, evidence)
                self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
                self.assertEqual(token.read_bytes(), b'synthetic private token')

    def test_partial_close_rejects_second_unverified_delete_with_reused_id(self):
        other = h.Measurement(self.r.at + timedelta(days=1), 71, 24, 'PICOOC')
        records = [self.r, other]
        self.write_export(records)
        source = (self.inbox / 'health.json').read_bytes()
        token = self.state / 'tokens' / 'synthetic'
        h.private_dir(token.parent)
        token.write_bytes(b'synthetic private token')
        backend = API([dict(row(r, 100), bodyFat=None) for r in records])
        api = self.native_deletion_api(backend, fail_at=None)
        sdk = api.api.delete_weigh_in.side_effect.__self__
        request = sdk.client.request.side_effect
        def accepted_then_timeout(method, domain, url, **kwargs):
            response = request(method, domain, url, **kwargs)
            if len(backend.deletes) == 2:
                raise TimeoutError('synthetic accepted second deletion')
            return response
        sdk.client.request.side_effect = accepted_then_timeout
        obj = h.fetch_remote_snapshot(api, self.r.at.date(), other.at.date())
        plan = h.save_comparison(h.comparison_plan(self.db, records, obj,
            self.r.at.date(), other.at.date()), self.cfg)
        with self.assertRaises(h.SafeError):
            self.invoke(api, plan)
        journal = next((self.state / 'operations').glob('full-*.json'))
        job = h.read_operation(journal)
        delete_events = [e for e in job['events'] if e['kind'].startswith('delete_')]
        self.assertEqual([(e['kind'], e['id'], e['calendarDate']) for e in delete_events],
                         [('delete_intent', '100', self.r.at.date().isoformat()),
                          ('delete_verified', '100', self.r.at.date().isoformat()),
                          ('delete_intent', '100', other.at.date().isoformat())])
        self.assertEqual(backend.deletes, [('100', r.at.date().isoformat()) for r in records])
        self.assertEqual(len(backend.posts), 1)
        journal_bytes = journal.read_bytes()
        backup = Path(job['backup'])
        evidence = {p.name: p.read_bytes() for p in backup.glob('*.json')}
        remote = json.loads(json.dumps(backend.rows))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(h.SafeError):
            bulk.close_verified_partial(journal, dict(self.cfg, enabled=False), api)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertEqual(backend.rows, remote)
        self.assertEqual(len(backend.deletes), 2)
        self.assertEqual(len(backend.posts), 1)
        self.assertEqual({p.name: p.read_bytes() for p in backup.glob('*.json')}, evidence)
        self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
        self.assertEqual(token.read_bytes(), b'synthetic private token')

    def test_partial_close_normalizes_only_known_ids_and_preserves_other_evidence(self):
        for case in ('integer', 'string', 'leading_zero', 'anonymous_upload', 'weight', 'date',
                     'unknown_id', 'invalid_id', 'body_fat', 'extra'):
            with self.subTest(case=case), patch.object(h, 'STATE', self.state / case):
                h.private_dir(h.STATE)
                api, backend, journal, other = self.paused_two_day_batch()
                original_job = h.read_operation(journal)
                journal_bytes = journal.read_bytes()
                backup = Path(original_job['backup']) / 'remote-before.json'
                backup_bytes = backup.read_bytes()
                source = (self.inbox / 'health.json').read_bytes()
                token = h.STATE / 'tokens' / 'synthetic'
                h.private_dir(token.parent)
                token.write_bytes(b'synthetic private token')
                untouched = next(item for item in backend.rows if item['samplePk'] == 101)
                if case == 'string':
                    untouched['samplePk'] = '101'
                elif case == 'leading_zero':
                    untouched['samplePk'] = '00101'
                elif case == 'anonymous_upload':
                    next(item for item in backend.rows if item['samplePk'] == 201)['samplePk'] = None
                elif case == 'weight':
                    untouched.update(samplePk='00101', weight=72000)
                elif case == 'date':
                    untouched.update(samplePk='00101', calendarDate=self.r.at.date().isoformat())
                elif case == 'unknown_id':
                    untouched['samplePk'] = None
                elif case == 'invalid_id':
                    untouched['samplePk'] = 101.0
                elif case == 'body_fat':
                    untouched.update(samplePk='00101', bodyFat=22)
                elif case == 'extra':
                    backend.rows.append(row(h.Measurement(other.at + timedelta(hours=2), 75, None, 'PICOOC'), 102))
                remote_before_check = json.loads(json.dumps(backend.rows))
                with contextlib.redirect_stdout(io.StringIO()):
                    if case in ('integer', 'string', 'leading_zero', 'anonymous_upload'):
                        result = bulk.close_verified_partial(journal, dict(self.cfg, enabled=False), api)
                        self.assertCountEqual(result['dateWeightList'], remote_before_check)
                        updated = h.read_operation(journal)
                        self.assertEqual(updated['phase'], 'closed_partial')
                        for key, value in original_job.items():
                            if key != 'phase':
                                self.assertEqual(updated[key], value)
                    else:
                        with self.assertRaises(h.SafeError):
                            bulk.close_verified_partial(journal, dict(self.cfg, enabled=False), api)
                        self.assertEqual(journal.read_bytes(), journal_bytes)
                self.assertEqual(len(backend.deletes), 1)
                self.assertEqual(len(backend.posts), 1)
                self.assertEqual(backend.rows, remote_before_check)
                self.assertEqual(backup.read_bytes(), backup_bytes)
                self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
                self.assertEqual(token.read_bytes(), b'synthetic private token')

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
