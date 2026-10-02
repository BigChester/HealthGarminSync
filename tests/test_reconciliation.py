"""Remote comparison and reviewed mutation tests, using no accounts or network."""
import argparse
import contextlib
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import health_garmin as h

AT = h.aware('2020-02-20T08:00:00+08:00')


def row(r, pk=100):
    return {'samplePk': pk, 'calendarDate': r.at.date().isoformat(),
            'timestampGMT': int(r.at.timestamp() * 1000),
            'weight': r.weight * 1000, 'bodyFat': r.fat, 'bmi': r.bmi,
            'sourceType': 'INDEX_SCALE'}


class API:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))
        self.reads = []
        self.posts = []
        self.deletes = []
        self.delete_timeout = False
        self.visible = True
        self.fail_chunk = None

    def get_body_composition(self, start, end):
        self.reads.append((start, end))
        if self.fail_chunk == len(self.reads):
            return {'unknown': []}
        return {'dateWeightList': copy.deepcopy([r for r in self.rows if start <= r['calendarDate'] <= end])}

    def add_body_composition(self, **kw):
        self.posts.append(kw)
        if self.visible:
            r = h.Measurement(h.aware(kw['timestamp']), kw['weight'], kw['percent_fat'], 'PICOOC', kw.get('bmi'))
            item = row(r, pk=200 + len(self.posts))
            # Garmin's calendar date follows account local time.
            item['calendarDate'] = r.at.astimezone(AT.tzinfo).date().isoformat()
            self.rows.append(item)
        return {'detailedImportResult': {'successes': [{}], 'failures': []}}

    def add_weigh_in(self, **kw):
        self.posts.append(kw)
        if self.visible:
            self.rows.append(row(h.Measurement(h.aware(kw['timestamp']), kw['weight'], None, 'PICOOC'), 200 + len(self.posts)))
        return {'ok': True}

    def delete_weigh_in(self, pk, day):
        self.deletes.append((pk, day))
        self.rows = [r for r in self.rows if str(r['samplePk']) != pk]
        if self.delete_timeout:
            raise TimeoutError('secret-ticket')


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        h.private_dir(self.state)
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        self.state_patch = patch.object(h, 'STATE', self.state)
        self.state_patch.start()
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC')
        self.cfg = {'allowed_sources': ['PICOOC'], 'since': '2020-01-01',
                    'weight_bare_unit': 'kg', 'fat_bare_unit': 'percent', 'pair_window_seconds': 0,
                    'inbox': str(self.inbox), 'enabled': True, 'settle_seconds': 0, 'max_records_per_run': 10}
        self.db = h.database()
        self.write_export([self.r])

    def tearDown(self):
        self.db.close()
        self.state_patch.stop()
        self.temp.cleanup()

    def write_export(self, records):
        metrics = {'weight': [], 'body_fat': [], 'bmi': [], 'lbm': []}
        for r in records:
            def sample(value): return {'at': r.at.isoformat(), 'value': value, 'source': r.source}
            metrics['weight'].append(sample(r.weight))
            if r.fat is not None: metrics['body_fat'].append(sample(f'{r.fat}%'))
            if r.bmi is not None: metrics['bmi'].append(sample(r.bmi))
            if r.lbm is not None: metrics['lbm'].append(sample(f'{r.lbm} kg'))
        (self.inbox / 'health.json').write_text(json.dumps({'schema_version': 2,
            'exported_at': '2020-02-20T09:00:00+08:00', 'metrics': metrics}))

    def plan(self, rows, records=None, save=False):
        records = records if records is not None else [self.r]
        obj = {'dateWeightList': copy.deepcopy(rows)}
        plan = h.comparison_plan(self.db, records, obj, AT.date(), AT.date())
        if save: return h.save_comparison(plan, self.cfg)
        return plan

    def action(self, rows, r=None):
        return self.plan(rows, [r or self.r])['entries'][0]['action']

    def sync(self, api):
        with patch.object(h, 'api_client', return_value=api), contextlib.redirect_stdout(io.StringIO()):
            h.run_sync(self.cfg)

    def reviewed(self, api, path, delete_extra=False, complete=True):
        args = argparse.Namespace(plan=str(path), record_id=self.r.key, remote_id='100', complete_export=complete)
        confirm = 'DELETE 100' if delete_extra else 'REPLACE ' + self.r.key
        with patch.object(h, 'api_client', return_value=api), patch('builtins.input', return_value=confirm), contextlib.redirect_stdout(io.StringIO()):
            h.reviewed_operation(args, self.cfg, delete_extra)

    def test_classifies_missing_match_repair_replace(self):
        self.assertEqual(self.action([]), 'missing')
        self.assertEqual(self.action([row(self.r)]), 'matched')
        missing = row(self.r); missing['bodyFat'] = None
        self.assertEqual(self.action([missing]), 'repair')
        wrong = row(self.r); wrong['weight'] = 71000
        self.assertEqual(self.action([wrong]), 'replace')

    def test_boolean_remote_numbers_are_conflicts_for_valid_exported_measurement(self):
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22)
        self.write_export([self.r])
        _, records, waiting, _ = h.load_measurements(self.cfg)
        self.assertFalse(waiting)
        self.assertEqual(records, [self.r])
        for field in ('weight', 'bodyFat', 'bmi'):
            for value in (True, False):
                with self.subTest(field=field, value=value):
                    remote = dict(row(self.r), **{field: value})
                    entry = self.plan([remote], records)['entries'][0]
                    self.assertEqual(entry['action'], 'conflict')
                    self.assertEqual(entry['reason'], '远端数值无法可靠解析')

    def test_legacy_replace_plan_with_boolean_remote_cannot_authorize_mutation(self):
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22)
        self.write_export([self.r])
        source = (self.inbox / 'health.json').read_bytes()
        api = API([dict(row(self.r), bodyFat=True)])
        legacy = self.plan(api.rows)
        legacy['entries'][0].update(action='replace', reason='同次测量数值不同')
        legacy['counts'] = {'replace': 1}
        path = h.save_comparison(legacy, self.cfg)
        with self.assertRaises(h.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertFalse((self.state / 'operations').exists())
        self.assertIsNone(h.state_row(self.db, self.r))
        self.assertEqual((self.inbox / 'health.json').read_bytes(), source)

    def test_boolean_optional_values_cannot_verify_uncertain_but_numeric_strings_can(self):
        self.r = h.Measurement(AT, 70, 1, 'PICOOC', 1)
        self.write_export([self.r])
        _, records, waiting, _ = h.load_measurements(self.cfg)
        self.assertFalse(waiting)
        self.assertEqual(records, [self.r])
        for status in ('uncertain', 'inflight'):
            with self.subTest(status=status):
                h.set_status(self.db, self.r, status, 'awaiting visibility')
                api = API([dict(row(self.r), bodyFat=True, bmi=True)])
                self.assertEqual(h.process_record(api, self.db, records[0]), 'uncertain')
                self.assertEqual(h.get_status(self.db, self.r), 'uncertain')
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)
        api = API([dict(row(self.r), weight='70000', bodyFat='1', bmi='1')])
        self.assertEqual(h.process_record(api, self.db, records[0]), 'verified')
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_optional_remote_values_not_erased_when_apple_has_none(self):
        self.assertEqual(self.action([row(self.r)], h.Measurement(AT, 70, None, 'PICOOC')), 'matched')
        self.assertEqual(self.action([dict(row(self.r), bodyFat=True, bmi=True)],
                                     h.Measurement(AT, 70, None, 'PICOOC')), 'matched')

    def test_missing_bmi_becomes_repair(self):
        self.assertEqual(self.action([row(self.r)], h.Measurement(AT, 70, 23.5, 'PICOOC', 22)), 'repair')

    def test_lbm_is_local_evidence_and_not_garmin_muscle_mass(self):
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22, 53.55)
        self.write_export([self.r]); api = API()
        self.sync(api)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(set(api.posts[0]), {'timestamp', 'weight', 'percent_fat', 'bmi'})
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        payload = json.loads(h.state_row(self.db, self.r)['payload'])
        self.assertEqual(payload['lbm'], 53.55)
        self.assertEqual(self.action(api.rows), 'matched')

    def test_bmi_repair_preserves_local_lbm_in_journal_and_database(self):
        original = self.r
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22, 53.55)
        self.write_export([self.r]); api = API([row(original)])
        path = self.plan(api.rows, save=True)
        self.reviewed(api, path)
        journal_path = next((self.state / 'operations').glob('*.json'))
        journal = json.loads(journal_path.read_text())
        self.assertEqual(journal['replacement']['lbm'], 53.55)
        self.assertEqual(json.loads(h.state_row(self.db, self.r)['payload'])['lbm'], 53.55)
        self.assertNotIn('muscle_mass', api.posts[0])
        self.assertEqual(journal['phase'], 'verified')

    def test_bmi_enrichment_repairs_existing_record_and_verifies_all_fields(self):
        original = self.r
        h.set_status(self.db, original, 'verified')
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22)
        self.assertEqual(self.r.key, original.key)
        self.write_export([self.r])
        api = API([row(original)])
        path = self.plan(api.rows, save=True)
        self.reviewed(api, path)
        self.assertEqual(api.deletes, [('100', '2020-02-20')])
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]['weight'], 70)
        self.assertEqual(api.posts[0]['percent_fat'], 23.5)
        self.assertEqual(api.posts[0]['bmi'], 22)
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        self.assertEqual(len(api.rows), 1)
        self.assertEqual(self.action(api.rows), 'matched')

    def test_old_verified_record_with_late_bmi_is_rechecked(self):
        h.set_status(self.db, self.r, 'verified')
        h.write_json(self.state / 'last_sync_comparison.json', {})
        enriched = h.Measurement(AT, 70, 23.5, 'PICOOC', 22)
        self.write_export([enriched]); api = API([row(self.r)])
        self.sync(api)
        self.assertTrue(api.reads)
        self.assertFalse(api.posts)
        summary = json.loads((self.state / 'last_run.json').read_text())
        self.assertEqual(summary['results'][self.r.key], 'repair')

    def test_old_completed_record_retains_late_lbm_without_changing_verification(self):
        original = h.Measurement(AT, 70, 23.5, 'PICOOC', 22)
        h.write_json(self.state / 'last_sync_comparison.json', {})
        for status in ('verified', 'verified_manual', 'ignored'):
            with self.subTest(status=status):
                h.set_status(self.db, original, status, 'previous decision')
                before = dict(h.state_row(self.db, original))
                payload = json.loads(before['payload'])
                for lbm in (53.55, 54.0):
                    enriched = h.Measurement(AT, 70, 23.5, 'PICOOC', lbm=lbm)
                    self.write_export([enriched])
                    source = (self.inbox / 'health.json').read_bytes()
                    with patch.object(h, 'api_client', side_effect=AssertionError('No remote work expected')) as api, \
                            contextlib.redirect_stdout(io.StringIO()):
                        h.run_sync(self.cfg)
                    payload['lbm'] = lbm
                    after = dict(h.state_row(self.db, original))
                    self.assertEqual(json.loads(after.pop('payload')), payload)
                    self.assertEqual(after, {key: value for key, value in before.items() if key != 'payload'})
                    api.assert_not_called()
                    self.assertEqual((self.inbox / 'health.json').read_bytes(), source)
                    summary = json.loads((self.state / 'last_run.json').read_text())
                    self.assertEqual(summary['results'], {})
                    self.assertEqual(summary['local_status_counts'], {status: 1})

    def test_missing_or_unchanged_lbm_preserves_existing_local_evidence(self):
        original = h.Measurement(AT, 70, 23.5, 'PICOOC', 22, 53.55)
        h.write_json(self.state / 'last_sync_comparison.json', {})
        for status in ('verified', 'verified_manual', 'ignored'):
            for lbm in (None, 53.55):
                with self.subTest(status=status, lbm=lbm):
                    h.set_status(self.db, original, status, 'previous decision')
                    before = dict(h.state_row(self.db, original))
                    self.write_export([h.Measurement(AT, 70, 23.5, 'PICOOC', lbm=lbm)])
                    api = API([row(original)])
                    self.sync(api)
                    self.assertEqual(dict(h.state_row(self.db, original)), before)
                    self.assertFalse(api.reads)
                    self.assertFalse(api.posts)
                    self.assertFalse(api.deletes)

    def test_late_lbm_with_remote_field_change_still_requires_comparison(self):
        for weight, bmi, action in ((71, None, 'replace'), (70, 22, 'repair')):
            with self.subTest(weight=weight, bmi=bmi):
                h.set_status(self.db, self.r, 'verified')
                h.write_json(self.state / 'last_sync_comparison.json', {})
                changed = h.Measurement(AT, weight, 23.5, 'PICOOC', bmi, 53.55)
                self.write_export([changed])
                api = API([row(self.r)])
                self.sync(api)
                self.assertTrue(api.reads)
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)
                self.assertEqual(h.get_status(self.db, changed), 'verified_conflict')
                summary = json.loads((self.state / 'last_run.json').read_text())
                self.assertEqual(summary['results'], {self.r.key: action})

    def test_idle_historical_sync_finishes_without_login_or_overwriting_review_plan(self):
        h.write_json(self.state / 'last_sync_comparison.json', {})
        for status in ('verified', 'verified_manual', 'ignored'):
            with self.subTest(status=status):
                h.set_status(self.db, self.r, status)
                plan = self.plan([row(self.r)], save=True)
                previous = plan.read_bytes(), (self.state / 'last_compare.json').read_bytes()
                with patch.object(h, 'api_client', side_effect=TimeoutError('Unavailable login')) as api, \
                        contextlib.redirect_stdout(io.StringIO()):
                    h.run_sync(self.cfg)
                api.assert_not_called()
                summary = json.loads((self.state / 'last_run.json').read_text())
                self.assertEqual(summary['results'], {})
                self.assertEqual(summary['comparison'], {})
                self.assertEqual(summary['local_status_counts'], {status: 1})
                self.assertEqual((plan.read_bytes(), (self.state / 'last_compare.json').read_bytes()), previous)

    def test_skipped_completed_sources_still_protect_selected_record_correspondence(self):
        for status in ('verified', 'verified_manual', 'ignored'):
            for offset, source in ((0, 'Second Scale'), (0.5, 'PICOOC'), (2, 'PICOOC')):
                for visible in ((True,) if offset == 2 else (False, True)):
                    with self.subTest(status=status, offset=offset, visible=visible):
                        old = self.r
                        new = h.Measurement(AT + timedelta(seconds=offset), 70, 23.5, source)
                        self.cfg['allowed_sources'] = ['PICOOC', 'Second Scale']
                        h.set_status(self.db, old, status, 'Retained completed decision')
                        self.db.execute('DELETE FROM uploads WHERE record_id!=?', (old.key,))
                        self.db.commit()
                        before = dict(h.state_row(self.db, old))
                        h.write_json(self.state / 'last_sync_comparison.json', {})
                        self.write_export([old, new])
                        source_bytes = (self.inbox / 'health.json').read_bytes()
                        remote = h.Measurement(AT + timedelta(seconds=1 if offset == 2 else 0),
                                               70, 23.5, 'PICOOC')
                        api = API([row(remote)] if visible else [])
                        self.sync(api)
                        self.assertFalse(api.posts)
                        self.assertFalse(api.deletes)
                        self.assertEqual(dict(h.state_row(self.db, old)), before)
                        self.assertEqual(h.get_status(self.db, new), 'conflict')
                        summary = json.loads((self.state / 'last_run.json').read_text())
                        self.assertEqual(summary['comparison'], {'conflict': 1})
                        self.assertEqual(summary['results'], {new.key: 'conflict'})
                        self.assertEqual(summary['local_status_counts'], {status: 1, 'conflict': 1})
                        plan = json.loads((self.state / 'plans' / 'sync-latest.json').read_text())
                        self.assertEqual([entry['id'] for entry in plan['entries']], [new.key])
                        self.assertEqual(plan['counts'], {'conflict': 1})
                        self.assertEqual(plan['extra_remote'], [])
                        self.assertEqual((self.inbox / 'health.json').read_bytes(), source_bytes)

    def test_unrelated_completed_context_does_not_expand_queries_or_change_status(self):
        old = self.r
        new = h.Measurement(AT + timedelta(days=40), 71, 24, 'PICOOC')
        later = h.Measurement(AT + timedelta(days=80), 72, 25, 'PICOOC')
        neighbor = h.Measurement(new.at - timedelta(minutes=10), 73, 26, 'PICOOC')
        retained = (old, neighbor, later)
        for record in retained:
            h.set_status(self.db, record, 'verified', 'Keep existing evidence')
        before = [dict(h.state_row(self.db, record)) for record in retained]
        h.write_json(self.state / 'last_sync_comparison.json', {})
        self.write_export([old, neighbor, new, later])
        source = (self.inbox / 'health.json').read_bytes()
        api = API([row(record, pk) for pk, record in enumerate((old, neighbor, new, later), 100)])
        self.sync(api)
        day = new.at.date()
        self.assertEqual(api.reads, [((day - timedelta(days=1)).isoformat(),
                                    (day + timedelta(days=1)).isoformat())])
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertEqual([dict(h.state_row(self.db, record)) for record in retained], before)
        summary = json.loads((self.state / 'last_run.json').read_text())
        self.assertEqual(summary['comparison'], {'matched': 1})
        self.assertEqual(summary['results'], {new.key: 'verified'})
        self.assertEqual(summary['local_status_counts'], {'verified': 4})
        plan = json.loads((self.state / 'plans' / 'sync-latest.json').read_text())
        self.assertEqual([entry['id'] for entry in plan['entries']], [new.key])
        self.assertEqual(plan['start'], day.isoformat())
        self.assertEqual(plan['end'], day.isoformat())
        self.assertEqual(plan['extra_remote'], [])
        self.assertEqual((self.inbox / 'health.json').read_bytes(), source)

    def test_explicit_single_upload_keeps_other_source_correspondence_context(self):
        new = h.Measurement(AT, 70, 23.5, 'Second Scale')
        self.cfg['allowed_sources'].append(new.source)
        self.write_export([self.r, new])
        source = (self.inbox / 'health.json').read_bytes()
        for visible in (False, True):
            with self.subTest(visible=visible):
                h.set_status(self.db, self.r, 'verified', 'Original verification')
                self.db.execute('DELETE FROM uploads WHERE record_id=?', (new.key,))
                self.db.commit()
                before = dict(h.state_row(self.db, self.r))
                api = API([row(self.r)] if visible else [])
                with patch.object(h, 'api_client', return_value=api), \
                        patch('builtins.input', return_value='UPLOAD'), contextlib.redirect_stdout(io.StringIO()):
                    h.run_sync(self.cfg, new.key)
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)
                self.assertEqual(dict(h.state_row(self.db, self.r)), before)
                self.assertEqual(h.get_status(self.db, new), 'conflict')
                summary = json.loads((self.state / 'last_run.json').read_text())
                self.assertEqual(summary['comparison'], {'conflict': 1})
                self.assertEqual(summary['results'], {new.key: 'conflict'})
                self.assertEqual((self.inbox / 'health.json').read_bytes(), source)

    def test_required_comparison_still_logs_in_for_initial_recent_changed_or_pending_records(self):
        recent = h.Measurement(h.now() - timedelta(days=1), 70, 23.5, 'PICOOC')
        cases = [('first', self.r, self.r, 'verified', True),
                 ('recent', recent, recent, 'verified', False),
                 ('BMI', self.r, h.Measurement(AT, 70, 23.5, 'PICOOC', 22), 'verified', False),
                 ('core', self.r, h.Measurement(AT, 71, 23.5, 'PICOOC'), 'verified', False)]
        cases += [(status, self.r, self.r, status, False) for status in
                  ('pending', 'inflight', 'uncertain', 'repair_uncertain', 'verified_conflict', 'remote_missing')]
        for name, previous, current, status, first in cases:
            with self.subTest(name=name):
                h.set_status(self.db, previous, status)
                marker = self.state / 'last_sync_comparison.json'
                if first:
                    marker.unlink(missing_ok=True)
                else:
                    h.write_json(marker, {})
                self.write_export([current])
                remote = API([row(previous)])
                with patch.object(h, 'api_client', return_value=remote) as api, \
                        contextlib.redirect_stdout(io.StringIO()):
                    h.run_sync(self.cfg)
                api.assert_called_once_with()
                self.assertTrue(remote.reads)
                self.assertFalse(remote.posts)
                self.assertFalse(remote.deletes)

    def test_explicit_upload_of_old_verified_record_still_checks_remote(self):
        h.set_status(self.db, self.r, 'verified')
        h.write_json(self.state / 'last_sync_comparison.json', {})
        remote = API([row(self.r)])
        with patch.object(h, 'api_client', return_value=remote) as api, \
                patch('builtins.input', return_value='UPLOAD'), contextlib.redirect_stdout(io.StringIO()):
            h.run_sync(self.cfg, self.r.key)
        api.assert_called_once_with()
        self.assertTrue(remote.reads)
        self.assertFalse(remote.posts)
        self.assertFalse(remote.deletes)
        summary = json.loads((self.state / 'last_run.json').read_text())
        self.assertEqual(summary['results'], {self.r.key: 'verified'})

    def test_login_failure_rolls_back_local_lbm_merge_and_allows_later_recovery(self):
        h.set_status(self.db, self.r, 'verified', 'previous verification')
        before = dict(h.state_row(self.db, self.r))
        h.write_json(self.state / 'last_sync_comparison.json', {})
        h.write_json(self.state / 'last_run.json', {'previous': 'summary'})
        old_summary = (self.state / 'last_run.json').read_bytes()
        enriched = h.Measurement(AT, 70, 23.5, 'PICOOC', lbm=53.55)
        other = h.Measurement(AT + timedelta(hours=1), 71, 24, 'PICOOC')
        self.write_export([enriched, other])
        source = (self.inbox / 'health.json').read_bytes()
        with patch.object(h, 'api_client', side_effect=TimeoutError('Unavailable login')) as api, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(TimeoutError):
            h.run_sync(self.cfg)
        api.assert_called_once_with()
        self.assertEqual(dict(h.state_row(self.db, self.r)), before)
        self.assertEqual((self.state / 'last_run.json').read_bytes(), old_summary)
        remote = API([row(other)])
        self.sync(remote)
        self.assertEqual(json.loads(h.state_row(self.db, self.r)['payload'])['lbm'], 53.55)
        self.assertEqual(h.get_status(self.db, other), 'verified')
        self.assertFalse(remote.posts)
        self.assertFalse(remote.deletes)
        self.assertEqual((self.inbox / 'health.json').read_bytes(), source)

    def test_previously_verified_is_queried_and_detects_change(self):
        h.set_status(self.db, self.r, 'verified')
        wrong = row(self.r); wrong['bodyFat'] = 24
        api = API([wrong]); self.sync(api)
        self.assertTrue(api.reads)
        self.assertFalse(api.posts)
        self.assertEqual(h.get_status(self.db, self.r), 'verified_conflict')

    def test_verified_conflict_does_not_lose_duplicate_protection(self):
        h.set_status(self.db, self.r, 'verified')
        wrong = dict(row(self.r), bodyFat=24)
        api = API([wrong]); self.sync(api)
        api.rows.clear(); self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(h.get_status(self.db, self.r), 'remote_missing')

    def test_previously_verified_missing_is_not_blindly_reuploaded(self):
        h.set_status(self.db, self.r, 'verified')
        api = API(); self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(h.get_status(self.db, self.r), 'remote_missing')

    def test_recent_verified_rechecked_after_initial_run(self):
        self.r = h.Measurement(h.now() - timedelta(days=1), 70, 23.5, 'PICOOC')
        self.write_export([self.r]); h.set_status(self.db, self.r, 'verified')
        h.write_json(self.state / 'last_sync_comparison.json', {})
        api = API([row(self.r)]); self.sync(api)
        self.assertTrue(api.reads)
        self.assertFalse(api.posts)

    def test_repeated_sync_reuses_latest_plan_and_updates_snapshot(self):
        self.r = h.Measurement(h.now() - timedelta(days=1), 70, 23.5, 'PICOOC')
        self.write_export([self.r])
        source = (self.inbox / 'health.json').read_bytes()
        api = API([row(self.r)])
        self.sync(api)
        path = self.state / 'plans' / 'sync-latest.json'
        first = json.loads(path.read_text())
        self.assertEqual(first['counts'], {'matched': 1})
        api.rows[0]['bodyFat'] = 24
        self.sync(api)
        latest = json.loads(path.read_text())
        self.assertEqual(list((self.state / 'plans').iterdir()), [path])
        self.assertEqual(latest['counts'], {'replace': 1})
        self.assertEqual(latest['remote_snapshot']['dateWeightList'], api.rows)
        summary = json.loads((self.state / 'last_compare.json').read_text())
        self.assertEqual(summary['path'], str(path))
        self.assertEqual(summary['created_at'], latest['created_at'])
        self.assertEqual(summary['counts'], latest['counts'])
        self.assertEqual(h.get_status(self.db, self.r), 'verified_conflict')
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertEqual((self.inbox / 'health.json').read_bytes(), source)

    def test_manual_comparisons_survive_later_automatic_sync(self):
        api = API([row(self.r)])
        args = argparse.Namespace(since='2020-02-20', until='2020-02-20', inbox=None)
        with patch.object(h, 'api_client', return_value=api), contextlib.redirect_stdout(io.StringIO()):
            h.compare_exports(args, self.cfg)
            h.compare_exports(args, self.cfg)
        saved = {p: p.read_bytes() for p in (self.state / 'plans').glob('compare-*.json')}
        self.assertEqual(len(saved), 2)
        api.rows[0]['bodyFat'] = None
        self.sync(api)
        self.assertEqual({p: p.read_bytes() for p in saved}, saved)
        summary = json.loads((self.state / 'last_compare.json').read_text())
        self.assertEqual(Path(summary['path']), self.state / 'plans' / 'sync-latest.json')
        self.reviewed(api, Path(summary['path']))
        self.assertEqual(len(api.deletes), 1)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        self.assertEqual({p: p.read_bytes() for p in saved}, saved)

    def test_latest_plan_replace_failure_preserves_evidence_and_stops_upload(self):
        api = API([row(self.r)])
        self.sync(api)
        paths = [self.state / 'plans' / 'sync-latest.json', self.state / 'last_compare.json',
                 self.state / 'last_sync_comparison.json']
        saved = {p: p.read_bytes() for p in paths}
        other = h.Measurement(AT + timedelta(hours=1), 71, 24, 'PICOOC')
        self.write_export([self.r, other])
        original_replace = Path.replace

        def fail_latest(path, target):
            if Path(target) == paths[0]:
                raise OSError('simulated disk failure')
            return original_replace(path, target)

        with patch.object(Path, 'replace', fail_latest), self.assertRaises(OSError):
            self.sync(api)
        self.assertEqual({p: p.read_bytes() for p in paths}, saved)
        self.assertIsNone(h.state_row(self.db, other))
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_explicit_historical_compare_ignores_incremental_marker(self):
        h.set_status(self.db, self.r, 'verified')
        h.write_json(self.state / 'last_sync_comparison.json', {})
        api = API([row(self.r)])
        args = argparse.Namespace(since='2020-02-20', until='2020-02-20', inbox=None)
        with patch.object(h, 'api_client', return_value=api), contextlib.redirect_stdout(io.StringIO()):
            plan = h.compare_exports(args, self.cfg)
        self.assertEqual(plan['counts'], {'matched': 1})
        self.assertTrue(api.reads)
        self.assertFalse(api.posts)

    def test_one_batch_reads_many_existing_records(self):
        records = [h.Measurement(AT + timedelta(minutes=i * 10), 70+i, 23.5, 'PICOOC') for i in range(6)]
        self.write_export(records)
        for r in records: h.set_status(self.db, r, 'verified')
        api = API([row(r, i+1) for i, r in enumerate(records)])
        self.sync(api)
        self.assertEqual(len(api.reads), 1)
        self.assertFalse(api.posts)

    def test_missing_only_is_uploaded(self):
        other = h.Measurement(AT + timedelta(hours=1), 71, 24, 'PICOOC')
        self.write_export([self.r, other]); api = API([row(self.r)])
        self.sync(api)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]['weight'], 71)

    def test_uncertain_recovers_from_batch_without_post(self):
        h.set_status(self.db, self.r, 'uncertain')
        api = API([row(self.r)]); self.sync(api)
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        self.assertFalse(api.posts)

    def test_uncertain_absent_does_not_post(self):
        h.set_status(self.db, self.r, 'uncertain')
        api = API(); self.sync(api)
        self.assertEqual(h.get_status(self.db, self.r), 'uncertain')
        self.assertFalse(api.posts)

    def test_source_overlap_cannot_clear_uncertain_upload_protection(self):
        overlapping = h.Measurement(AT + timedelta(milliseconds=500), 71, 24, 'PICOOC')
        for status in ('uncertain', 'inflight', 'repair_uncertain'):
            with self.subTest(status=status):
                h.set_status(self.db, self.r, status)
                self.write_export([self.r, overlapping])
                api = API()
                self.sync(api)
                self.assertFalse(api.posts)
                self.assertEqual(h.get_status(self.db, self.r), status)
                summary = json.loads((self.state / 'last_run.json').read_text())
                self.assertEqual(summary['results'][self.r.key], status)
                self.assertEqual(summary['comparison'], {'conflict': 2})

                # A corrected export removes the ambiguity, but an empty read
                # still cannot prove that the previous request did not succeed.
                self.write_export([self.r])
                self.sync(api)
                self.assertFalse(api.posts)
                self.assertEqual(h.get_status(self.db, self.r), 'uncertain')
                summary = json.loads((self.state / 'last_run.json').read_text())
                self.assertEqual(summary['results'][self.r.key], 'uncertain')
                self.assertEqual(summary['comparison'], {'uncertain': 1})

    def test_review_actions_report_persisted_uncertain_markers_without_submission(self):
        for marker in ('inflight', 'uncertain', 'repair_uncertain'):
            for action in ('repair', 'replace', 'conflict'):
                with self.subTest(marker=marker, action=action):
                    h.set_status(self.db, self.r, marker)
                    plan = self.plan([])
                    plan['entries'][0]['action'] = action
                    plan['counts'] = {action: 1}
                    api = API()
                    with patch.object(h, 'load_local_comparison', return_value=plan):
                        self.sync(api)
                    summary = json.loads((self.state / 'last_run.json').read_text())
                    self.assertEqual(summary['results'][self.r.key], marker)
                    self.assertEqual(summary['comparison'], {action: 1})
                    self.assertEqual(h.get_status(self.db, self.r), marker)
                    self.assertFalse(api.posts)
                    self.assertFalse(api.deletes)

    def test_every_chunk_is_validated_before_first_write(self):
        other = h.Measurement(AT + timedelta(days=60), 71, 24, 'PICOOC')
        self.write_export([self.r, other]); api = API(); api.fail_chunk = 2
        with self.assertRaises(h.SafeError): self.sync(api)
        self.assertFalse(api.posts)

    def test_unknown_remote_timestamp_stops_entire_comparison(self):
        item = row(self.r); item['timestampGMT'] = 'unparseable'
        with self.assertRaises(h.SafeError): self.plan([item])

    def test_multiple_candidates_block_matching(self):
        self.assertEqual(self.action([row(self.r, 1), row(self.r, 2)]), 'conflict')

    def test_time_drift_never_matches_only_equal_weight(self):
        item = row(self.r); item['timestampGMT'] += 30000
        self.assertEqual(self.action([item]), 'conflict')

    def test_two_apple_records_cannot_claim_same_remote_record(self):
        other = h.Measurement(AT, 70, 23.5, 'Other')
        plan = self.plan([row(self.r)], [self.r, other])
        self.assertEqual([e['action'] for e in plan['entries']], ['conflict', 'conflict'])

    def test_overlapping_source_records_not_both_uploaded_into_empty_remote(self):
        other = h.Measurement(AT, 71, 24, 'Other')
        plan = self.plan([], [self.r, other])
        self.assertEqual([e['action'] for e in plan['entries']], ['conflict', 'conflict'])

    def test_extras_exclude_padding_and_ambiguous_candidates(self):
        adjacent = h.Measurement(AT-timedelta(days=1), 69, 23, 'PICOOC')
        ambiguous = row(self.r); ambiguous['timestampGMT'] += 30000
        plan = self.plan([row(adjacent), ambiguous])
        self.assertEqual(plan['extra_remote'], [])

    def test_exact_match_does_not_hide_unclaimed_nearby_orphan(self):
        orphan = row(h.Measurement(AT + timedelta(seconds=30), 75, None, 'PICOOC'), 101)
        plan = self.plan([row(self.r), orphan])
        self.assertEqual(plan['counts'], {'matched': 1})
        self.assertEqual(plan['extra_remote'], [orphan])

    def test_legitimate_close_source_targets_claim_their_own_rows(self):
        other = h.Measurement(AT + timedelta(seconds=30), 71, 24, 'PICOOC')
        plan = self.plan([row(self.r), row(other, 101)], [self.r, other])
        self.assertEqual(plan['counts'], {'matched': 2})
        self.assertEqual(plan['extra_remote'], [])

    def test_ambiguous_exact_rows_keep_all_nearby_candidates_protected(self):
        orphan = row(h.Measurement(AT + timedelta(seconds=30), 75, None, 'PICOOC'), 102)
        plan = self.plan([row(self.r, 100), row(self.r, 101), orphan])
        self.assertEqual(plan['counts'], {'conflict': 1})
        self.assertEqual(plan['extra_remote'], [])

    def test_unknown_exact_values_keep_nearby_rows_protected(self):
        unknown = dict(row(self.r), weight='unknown')
        nearby = row(h.Measurement(AT + timedelta(seconds=30), 75, None, 'PICOOC'), 101)
        plan = self.plan([unknown, nearby])
        self.assertEqual(plan['counts'], {'conflict': 1})
        self.assertEqual(plan['extra_remote'], [])

    def test_overlapping_source_targets_do_not_make_nearby_rows_deletable(self):
        other = h.Measurement(AT + timedelta(milliseconds=500), 71, 24, 'Other')
        nearby = row(h.Measurement(AT + timedelta(seconds=30), 75, None, 'PICOOC'), 101)
        plan = self.plan([row(self.r), nearby], [self.r, other])
        self.assertEqual(plan['counts'], {'conflict': 2})
        self.assertEqual(plan['extra_remote'], [])

    def test_extra_retained_in_automatic_run(self):
        extra = h.Measurement(AT+timedelta(hours=1), 71, None, 'PICOOC')
        api = API([row(self.r), row(extra, 101)])
        self.sync(api)
        self.assertFalse(api.deletes)
        self.assertEqual(len(api.rows), 2)

    def test_nearby_extra_retained_in_automatic_run(self):
        extra = h.Measurement(AT + timedelta(seconds=30), 71, None, 'PICOOC')
        api = API([row(self.r), row(extra, 101)])
        self.sync(api)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)
        self.assertEqual(len(api.rows), 2)
        path = json.loads((self.state / 'last_compare.json').read_text())['path']
        self.assertEqual(len(json.loads(Path(path).read_text())['extra_remote']), 1)

    def test_automatic_missing_target_with_known_neighbor_still_requires_review(self):
        other = h.Measurement(AT + timedelta(seconds=30), 71, 24, 'PICOOC')
        self.write_export([self.r, other]); api = API([row(other, 101)])
        self.sync(api)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertEqual(h.get_status(self.db, self.r), 'conflict')

    def test_weight_only_history_pairs_and_uploads(self):
        self.r = h.Measurement(AT, 70, None, 'PICOOC')
        self.write_export([self.r]); api = API()
        self.sync(api)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]['unitKey'], 'kg')
        self.assertEqual(h.get_status(self.db, self.r), 'verified')

    def test_weight_and_bmi_without_fat_uploads_available_fields(self):
        self.r = h.Measurement(AT, 70, None, 'PICOOC', 22)
        self.write_export([self.r]); api = API()
        self.sync(api)
        self.assertEqual(api.posts[0]['bmi'], 22)
        self.assertIsNone(api.posts[0]['percent_fat'])
        self.assertEqual(h.get_status(self.db, self.r), 'verified')

    def test_weight_only_does_not_fallback_on_ambiguous_fat(self):
        samples = [h.Sample('weight', AT, 70, 'PICOOC'), h.Sample('body_fat', AT-timedelta(seconds=5), 23, 'PICOOC'), h.Sample('body_fat', AT+timedelta(seconds=5), 24, 'PICOOC')]
        records, waiting = h.pair_samples(samples, 10, allow_weight_only=True)
        self.assertFalse(records)
        self.assertTrue(waiting)

    def test_revised_local_core_can_be_compared(self):
        old = h.Measurement(AT, 69, 23.5, 'PICOOC')
        h.set_status(self.db, old, 'verified')
        self.assertEqual(self.action([row(old)]), 'replace')
        with contextlib.redirect_stdout(io.StringIO()): h.show_preview(self.cfg)

    def test_reviewed_repair_backs_up_and_replaces_exact_record(self):
        original = row(self.r); original['bodyFat'] = None
        path = self.plan([original], save=True); api = API([original])
        self.reviewed(api, path)
        self.assertEqual(api.deletes, [('100', '2020-02-20')])
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        backups = list((self.state/'backups').glob('*/remote.json'))
        self.assertEqual(json.loads(backups[0].read_text())['dateWeightList'], [original])
        self.assertTrue(list((self.state/'backups').glob('*/sync.sqlite3')))

    def test_reviewed_repair_rejects_unknown_neighbor_before_deleting_original(self):
        original = dict(row(self.r), bodyFat=None)
        neighbor = row(h.Measurement(AT + timedelta(seconds=30), 75, None, 'PICOOC'), 101)
        path = self.plan([original, neighbor], save=True); api = API([original, neighbor])
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)
        self.assertEqual(api.rows, [original, neighbor])
        self.assertFalse(list((self.state / 'operations').glob('*.json')))

    def test_reviewed_repair_preserves_uniquely_matched_reviewed_neighbor(self):
        other = h.Measurement(AT + timedelta(seconds=30), 71, 24, 'PICOOC')
        self.write_export([self.r, other])
        original = dict(row(self.r), bodyFat=None)
        neighbor = row(other, 101)
        path = self.plan([original, neighbor], [self.r, other], save=True); api = API([original, neighbor])
        self.reviewed(api, path)
        self.assertEqual(api.deletes, [('100', '2020-02-20')])
        self.assertEqual(len(api.posts), 1)
        self.assertIn(neighbor, api.rows)
        self.assertEqual(len(api.rows), 2)
        self.assertEqual(h.get_status(self.db, self.r), 'verified')

    def test_confirmation_time_new_neighbor_prevents_deletion(self):
        original = dict(row(self.r), bodyFat=None)
        neighbor = row(h.Measurement(AT + timedelta(seconds=30), 75, None, 'PICOOC'), 101)
        path = self.plan([original], save=True); api = API([original])
        args = argparse.Namespace(plan=str(path), record_id=self.r.key)
        def confirm(_):
            api.rows.append(neighbor)
            return 'REPLACE ' + self.r.key
        with patch.object(h, 'api_client', return_value=api), patch('builtins.input', side_effect=confirm), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(h.SafeError): h.reviewed_operation(args, self.cfg)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)
        self.assertEqual(api.rows, [original, neighbor])

    def test_reviewed_repair_rejects_ambiguous_or_unknown_source_neighbor(self):
        other = h.Measurement(AT + timedelta(seconds=30), 71, 24, 'PICOOC')
        self.write_export([self.r, other])
        original = dict(row(self.r), bodyFat=None)
        for neighbors in ([row(other, 101), row(other, 102)], [dict(row(other, 101), weight='unknown')]):
            with self.subTest(neighbors=neighbors):
                rows = [original, *neighbors]
                path = self.plan(rows, [self.r, other], save=True); api = API(rows)
                with self.assertRaises(h.SafeError): self.reviewed(api, path)
                self.assertFalse(api.deletes)
                self.assertFalse(api.posts)
                self.assertEqual(api.rows, rows)

    def test_new_source_neighbor_not_in_reviewed_plan_cannot_allow_replacement(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.plan([original], save=True)
        other = h.Measurement(AT + timedelta(seconds=30), 71, 24, 'PICOOC')
        self.write_export([self.r, other]); api = API([original, row(other, 101)])
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)

    def test_new_overlapping_source_cannot_make_neighbor_look_unique(self):
        other = h.Measurement(AT + timedelta(seconds=30), 71, 24, 'PICOOC')
        original = dict(row(self.r), bodyFat=None)
        neighbor = row(other, 101)
        path = self.plan([original, neighbor], [self.r, other], save=True)
        overlapping = h.Measurement(other.at + timedelta(milliseconds=500), 72, 25, 'PICOOC')
        self.write_export([self.r, other, overlapping]); api = API([original, neighbor])
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)

    def test_neighbor_appearing_after_delete_still_prevents_post(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.plan([original], save=True); api = API([original])
        delete = api.delete_weigh_in
        neighbor = row(h.Measurement(AT + timedelta(seconds=30), 75, None, 'PICOOC'), 101)
        def delete_and_add_neighbor(pk, day):
            delete(pk, day)
            api.rows.append(neighbor)
        with patch.object(api, 'delete_weigh_in', side_effect=delete_and_add_neighbor):
            with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertEqual(api.deletes, [('100', '2020-02-20')])
        self.assertFalse(api.posts)
        self.assertEqual(api.rows, [neighbor])
        self.assertEqual(json.loads(next((self.state / 'operations').glob('*.json')).read_text())['phase'], 'uncertain')

    def test_reviewed_repair_refuses_changed_remote(self):
        original = row(self.r); original['bodyFat'] = None
        path = self.plan([original], save=True)
        changed = dict(original, weight=71000); api = API([changed])
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertFalse(api.deletes)

    def test_reviewed_repair_refuses_changed_apple(self):
        original = row(self.r); original['bodyFat'] = None
        path = self.plan([original], save=True)
        self.write_export([h.Measurement(AT, 71, 23.5, 'PICOOC')]); api = API([original])
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertFalse(api.deletes)

    def test_delete_timeout_never_proceeds_to_upload_or_redelete(self):
        original = row(self.r); original['bodyFat'] = None
        path = self.plan([original], save=True); api = API([original]); api.delete_timeout = True
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertFalse(api.posts)
        self.assertEqual(h.get_status(self.db, self.r), 'repair_uncertain')
        journal = json.loads(next((self.state/'operations').glob('*.json')).read_text())
        self.assertNotIn('secret-ticket', json.dumps(journal))
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertEqual(len(api.deletes), 1)

    def test_delete_extra_requires_complete_export_confirmation(self):
        extra = h.Measurement(AT+timedelta(hours=1), 71, None, 'PICOOC')
        rows = [row(self.r, 101), row(extra, 100)]
        path = self.plan(rows, save=True); api = API(rows)
        with self.assertRaises(h.SafeError): self.reviewed(api, path, delete_extra=True, complete=False)
        self.assertFalse(api.deletes)

    def test_delete_extra_only_deletes_selected_snapshot_id(self):
        extra = h.Measurement(AT+timedelta(hours=1), 71, None, 'PICOOC')
        rows = [row(self.r, 101), row(extra, 100)]
        path = self.plan(rows, save=True); api = API(rows)
        self.reviewed(api, path, delete_extra=True)
        self.assertEqual(api.deletes, [('100', '2020-02-20')])
        self.assertEqual([r['samplePk'] for r in api.rows], [101])
        self.assertFalse(api.posts)

    def test_extra_that_now_matches_apple_cannot_be_deleted(self):
        extra = h.Measurement(AT+timedelta(hours=1), 71, None, 'PICOOC')
        rows = [row(self.r, 101), row(extra, 100)]
        path = self.plan(rows, save=True)
        self.write_export([self.r, extra]); api = API(rows)
        with self.assertRaises(h.SafeError): self.reviewed(api, path, delete_extra=True)
        self.assertFalse(api.deletes)

    def test_unsupported_optional_remote_metrics_prevent_loss(self):
        original = dict(row(self.r), bodyFat=None, muscleMass=40000)
        path = self.plan([original], save=True); api = API([original])
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertFalse(api.deletes)

    def test_invalid_preserved_fat_is_rejected_before_deletion(self):
        self.r = h.Measurement(AT, 71, None, 'PICOOC')
        self.write_export([self.r])
        original = dict(row(self.r), weight=70000, bodyFat='invalid')
        path = self.plan([original], save=True); api = API([original])
        with self.assertRaises(h.SafeError): self.reviewed(api, path)
        self.assertFalse(api.deletes)

    def test_valid_optional_remote_values_preserved_on_weight_correction(self):
        self.r = h.Measurement(AT, 71, None, 'PICOOC')
        self.write_export([self.r])
        original = dict(row(self.r), weight=70000, bodyFat=23.5, bmi=22)
        path = self.plan([original], save=True); api = API([original])
        self.reviewed(api, path)
        self.assertEqual(api.posts[0]['percent_fat'], 23.5)
        self.assertEqual(api.posts[0]['bmi'], 22)

    def test_operation_verification_is_read_only_and_recovers_delayed_result(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.state / 'operations' / 'test.json'
        h.write_json(path, {'key': self.r.key, 'phase': 'uncertain', 'original': original,
                           'measurement': self.r.payload(), 'replacement': self.r.payload()})
        api = API([row(self.r, 201)])
        args = argparse.Namespace(operation=str(path))
        with patch.object(h, 'api_client', return_value=api), contextlib.redirect_stdout(io.StringIO()):
            h.verify_operation(args)
        self.assertEqual(json.loads(path.read_text())['phase'], 'verified')
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_cancelled_repair_does_not_modify_remote(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.plan([original], save=True); api = API([original])
        args = argparse.Namespace(plan=str(path), record_id=self.r.key)
        with patch.object(h, 'api_client', return_value=api), patch('builtins.input', return_value='CANCEL'), contextlib.redirect_stdout(io.StringIO()):
            h.reviewed_operation(args, self.cfg)
        self.assertFalse(api.deletes)
        self.assertFalse(list((self.state / 'operations').glob('*.json')))

    def test_remote_change_during_confirmation_prevents_deletion(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.plan([original], save=True); api = API([original])
        args = argparse.Namespace(plan=str(path), record_id=self.r.key)
        def confirm(_):
            api.rows[0]['bodyFat'] = 24
            return 'REPLACE ' + self.r.key
        with patch.object(h, 'api_client', return_value=api), patch('builtins.input', side_effect=confirm), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(h.SafeError): h.reviewed_operation(args, self.cfg)
        self.assertFalse(api.deletes)

    def test_incomplete_journal_blocks_even_pending_automatic_upload(self):
        h.write_json(self.state / 'operations' / 'test.json', {'key': self.r.key, 'phase': 'uploading'})
        h.set_status(self.db, self.r, 'pending')
        api = API(); self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(h.get_status(self.db, self.r), 'repair_uncertain')

    def test_reviewed_resume_preserves_optional_values_and_never_deletes(self):
        self.r = h.Measurement(AT, 71, None, 'PICOOC')
        replacement = h.Measurement(AT, 71, 23.5, 'PICOOC', 22)
        original = dict(row(replacement), weight=70000)
        path = self.state / 'operations' / 'test.json'
        h.write_json(path, {'key': self.r.key, 'phase': 'uncertain', 'original': original,
                           'measurement': self.r.payload(), 'replacement': replacement.payload()})
        h.set_status(self.db, self.r, 'repair_uncertain'); api = API()
        args = argparse.Namespace(operation=str(path))
        with patch.object(h, 'api_client', return_value=api), patch('builtins.input', return_value='RESUME ' + self.r.key), contextlib.redirect_stdout(io.StringIO()):
            h.resume_operation(args)
        self.assertEqual(api.posts[0]['percent_fat'], 23.5)
        self.assertEqual(api.posts[0]['bmi'], 22)
        self.assertFalse(api.deletes)
        self.assertEqual(json.loads(path.read_text())['phase'], 'verified')

    def test_resume_refuses_if_original_record_still_exists(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.state / 'operations' / 'test.json'
        h.write_json(path, {'key': self.r.key, 'phase': 'uncertain', 'original': original,
                           'measurement': self.r.payload(), 'replacement': self.r.payload()})
        api = API([original]); args = argparse.Namespace(operation=str(path))
        with patch.object(h, 'api_client', return_value=api), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(h.SafeError): h.resume_operation(args)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_disabled_run_remains_offline(self):
        self.cfg['enabled'] = False
        with patch.object(h, 'api_client', side_effect=AssertionError('unexpected network')), contextlib.redirect_stdout(io.StringIO()):
            h.run_sync(self.cfg)

    def test_settling_and_limit_defer_new_writes(self):
        current = h.aware('2026-10-02T10:00:00+08:00')
        ignored = self.r
        missing = h.Measurement(AT + timedelta(hours=1), 71, 24, 'PICOOC')
        repair = h.Measurement(AT + timedelta(hours=2), 72, 25, 'PICOOC')
        pending = [h.Measurement(AT + timedelta(hours=3), 73, 26, 'PICOOC'),
                   h.Measurement(AT + timedelta(hours=4), 74, 27, 'PICOOC'),
                   h.Measurement(current - timedelta(days=1), 75, 28, 'PICOOC')]
        self.write_export([ignored, missing, repair, *pending])
        source = self.inbox / 'health.json'
        data = json.loads(source.read_text())
        data['exported_at'] = current.isoformat()
        source.write_text(json.dumps(data))
        original_source = source.read_bytes()
        self.cfg.update(settle_seconds=120, max_records_per_run=1)
        for record, status in ((ignored, 'ignored'), (missing, 'remote_missing'),
                               (repair, 'repair_uncertain')):
            h.set_status(self.db, record, status)
        journal = self.state / 'operations' / 'repair.json'
        h.write_json(journal, {'key': repair.key, 'phase': 'uncertain'})
        original_journal = journal.read_bytes()
        api = API()
        native_process = h.process_record

        def process_without_wait(*args, **kwargs):
            return native_process(*args, **kwargs, sleep=lambda _: None)

        for elapsed, total_posts in ((119, 0), (120, 1), (121, 2), (122, 3), (123, 3), (124, 3)):
            with self.subTest(elapsed=elapsed):
                api.visible = elapsed != 120
                if elapsed == 123:
                    api.rows.append(row(pending[0], 900))
                with patch.object(h, 'now', return_value=current + timedelta(seconds=elapsed)), \
                        patch.object(h, 'process_record', side_effect=process_without_wait):
                    self.sync(api)
                summary = json.loads((self.state / 'last_run.json').read_text())
                self.assertEqual(len(api.posts), total_posts)
                self.assertEqual(summary.get('deferred'), 'settling' if elapsed == 119 else None)
                for record, status in ((ignored, 'ignored'), (missing, 'remote_missing'),
                                       (repair, 'repair_uncertain')):
                    self.assertEqual(h.get_status(self.db, record), status)
                self.assertEqual(summary['results'][missing.key], 'remote_missing')
                self.assertEqual(summary['results'][repair.key], 'repair_uncertain')
                expected = ['pending', 'pending', 'pending'] if elapsed == 119 else [
                    'verified' if elapsed >= 123 else 'uncertain',
                    'verified' if elapsed >= 121 else 'pending',
                    'verified' if elapsed >= 122 else 'pending']
                for record, status in zip(pending, expected):
                    self.assertEqual(h.get_status(self.db, record), status)
                    if status != 'verified' or record.key in summary['results']:
                        self.assertEqual(summary['results'][record.key],
                                         'deferred' if status == 'pending' else status)
                self.assertEqual(source.read_bytes(), original_source)
                self.assertEqual(journal.read_bytes(), original_journal)
        self.assertEqual(len({post['timestamp'] for post in api.posts}), 3)
        self.assertFalse(api.deletes)

    def test_late_v1_fat_export_restarts_settling_before_single_complete_upload(self):
        current = h.aware('2026-10-02T10:00:00+08:00')
        self.r = h.Measurement(current - timedelta(minutes=5), 70, 23.5, 'PICOOC')
        self.cfg['settle_seconds'] = 120
        (self.inbox / 'health.json').unlink()
        weight = self.inbox / 'weight.json'
        weight.write_text(json.dumps({'schema_version': 1, 'metric': 'weight',
            'exported_at': current.isoformat(),
            'samples': [{'at': self.r.at.isoformat(), 'value': '70 kg', 'source': 'PICOOC'}]}))
        originals = {weight: weight.read_bytes()}
        api = API()
        for elapsed in (119, 120, 239, 240, 241):
            with self.subTest(elapsed=elapsed):
                if elapsed == 120:
                    fat = self.inbox / 'body_fat.json'
                    fat.write_text(json.dumps({'schema_version': 1, 'metric': 'body_fat',
                        'exported_at': (current + timedelta(seconds=120)).isoformat(),
                        'samples': [{'at': self.r.at.isoformat(), 'value': '23.5%', 'source': 'PICOOC'}]}))
                    originals[fat] = fat.read_bytes()
                with patch.object(h, 'now', return_value=current + timedelta(seconds=elapsed)):
                    self.sync(api)
                summary = json.loads((self.state / 'last_run.json').read_text())
                self.assertEqual(summary['complete_measurements'], 1)
                self.assertEqual(summary['waiting_pairs'], 0)
                self.assertEqual(summary['exports']['weight'], current.isoformat())
                if elapsed >= 120:
                    self.assertEqual(summary['exports']['body_fat'],
                                     (current + timedelta(seconds=120)).isoformat())
                else:
                    self.assertNotIn('body_fat', summary['exports'])
                if elapsed < 240:
                    self.assertEqual(summary['deferred'], 'settling')
                    self.assertEqual(summary['results'], {self.r.key: 'deferred'})
                    self.assertEqual(summary['local_status_counts'], {})
                    self.assertIsNone(h.state_row(self.db, self.r))
                    self.assertFalse(api.posts)
                else:
                    self.assertNotIn('deferred', summary)
                    self.assertEqual(summary['results'], {self.r.key: 'verified'})
                    self.assertEqual(summary['comparison'], {'missing': 1} if elapsed == 240 else {'matched': 1})
                    self.assertEqual(summary['local_status_counts'], {'verified': 1})
                    self.assertEqual(h.get_status(self.db, self.r), 'verified')
                    self.assertEqual(json.loads(h.state_row(self.db, self.r)['payload'])['fat'], 23.5)
                    self.assertEqual(len(api.posts), 1)
                    self.assertEqual(api.posts[0]['weight'], 70)
                    self.assertEqual(api.posts[0]['percent_fat'], 23.5)
                self.assertFalse(api.deletes)
                self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_zero_settling_allows_small_export_clock_skew(self):
        current = h.now()
        source = self.inbox / 'health.json'
        data = json.loads(source.read_text())
        data['exported_at'] = (current + timedelta(minutes=1)).isoformat()
        source.write_text(json.dumps(data))
        prior = source.read_bytes()
        api = API()
        with patch.object(h, 'now', return_value=current):
            self.sync(api)
        summary = json.loads((self.state / 'last_run.json').read_text())
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(summary['results'][self.r.key], 'verified')
        self.assertNotIn('deferred', summary)
        self.assertEqual(source.read_bytes(), prior)

    def test_positive_settling_waits_until_window_boundary(self):
        current = h.now()
        self.cfg['settle_seconds'] = 120
        source = self.inbox / 'health.json'
        data = json.loads(source.read_text())
        data['exported_at'] = current.isoformat()
        source.write_text(json.dumps(data))
        api = API()
        with patch.object(h, 'now', return_value=current + timedelta(seconds=119)):
            self.sync(api)
        summary = json.loads((self.state / 'last_run.json').read_text())
        self.assertEqual(summary['results'][self.r.key], 'deferred')
        self.assertEqual(summary['deferred'], 'settling')
        self.assertFalse(api.posts)
        with patch.object(h, 'now', return_value=current + timedelta(seconds=120)):
            self.sync(api)
        summary = json.loads((self.state / 'last_run.json').read_text())
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(summary['results'][self.r.key], 'verified')
        self.assertNotIn('deferred', summary)

    def test_confirmed_manual_upload_bypasses_positive_settling(self):
        current = h.now()
        self.cfg['settle_seconds'] = 120
        source = self.inbox / 'health.json'
        data = json.loads(source.read_text())
        data['exported_at'] = (current + timedelta(minutes=1)).isoformat()
        source.write_text(json.dumps(data))
        api = API()
        with patch.object(h, 'now', return_value=current), patch.object(h, 'api_client', return_value=api), \
                patch('builtins.input', return_value='UPLOAD'), contextlib.redirect_stdout(io.StringIO()):
            h.run_sync(self.cfg, self.r.key)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(h.get_status(self.db, self.r), 'verified')

    def test_ignored_never_uploaded_even_if_remote_missing(self):
        h.set_status(self.db, self.r, 'ignored')
        api = API(); self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(h.get_status(self.db, self.r), 'ignored')


if __name__ == '__main__': unittest.main()
