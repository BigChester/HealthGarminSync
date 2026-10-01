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

    def test_optional_remote_values_not_erased_when_apple_has_none(self):
        self.assertEqual(self.action([row(self.r)], h.Measurement(AT, 70, None, 'PICOOC')), 'matched')

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

                # A corrected export removes the ambiguity, but an empty read
                # still cannot prove that the previous request did not succeed.
                self.write_export([self.r])
                self.sync(api)
                self.assertFalse(api.posts)
                self.assertEqual(h.get_status(self.db, self.r), 'uncertain')

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

    def test_extra_retained_in_automatic_run(self):
        extra = h.Measurement(AT+timedelta(hours=1), 71, None, 'PICOOC')
        api = API([row(self.r), row(extra, 101)])
        self.sync(api)
        self.assertFalse(api.deletes)
        self.assertEqual(len(api.rows), 2)

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
        records = [self.r, h.Measurement(AT+timedelta(hours=1), 71, 24, 'PICOOC')]
        self.write_export(records); self.cfg['max_records_per_run'] = 1
        api = API(); self.sync(api)
        self.assertEqual(len(api.posts), 1)

    def test_ignored_never_uploaded_even_if_remote_missing(self):
        h.set_status(self.db, self.r, 'ignored')
        api = API(); self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(h.get_status(self.db, self.r), 'ignored')


if __name__ == '__main__': unittest.main()
