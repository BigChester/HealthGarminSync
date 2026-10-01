"""Offline regression tests for combined exports; no real health or login data."""
import contextlib
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import health_garmin as h

CFG = {"allowed_sources": ["PICOOC"], "since": "2020-01-01",
       "weight_bare_unit": "kg", "fat_bare_unit": "percent"}
AT = "2020-02-20T08:00:00+08:00"
EXPORTED = "2020-02-20T09:00:00+08:00"


def sample(value):
    return {"at": AT, "value": value, "source": "PICOOC"}


def bundle():
    return {"schema_version": 2, "exported_at": EXPORTED,
            "metrics": {"weight": [sample("70 kg")], "body_fat": [sample("23.5%") ]}}


def old(metric, samples):
    return {"schema_version": 1, "metric": metric, "exported_at": EXPORTED, "samples": samples}


class ExportBundleTests(unittest.TestCase):
    def test_two_metrics(self):
        data, at = h.decode_export(bundle(), CFG)
        self.assertEqual([s.metric for s in data], ["weight", "body_fat"])
        self.assertEqual([s.value for s in data], [70, 23.5])
        self.assertEqual(at, h.aware(EXPORTED))

    def test_export_time_uses_ten_minute_clock_tolerance_in_both_schemas(self):
        current = h.aware('2026-10-02T10:00:00+08:00')
        for original in (bundle(), old('weight', [sample('70 kg')])):
            for skew in (timedelta(days=-30), timedelta(0), timedelta(minutes=10),
                         timedelta(minutes=10, microseconds=1), timedelta(days=30)):
                for zone in (current.tzinfo, h.UTC):
                    with self.subTest(schema=original['schema_version'], skew=skew, zone=zone):
                        data = copy.deepcopy(original)
                        stamp = (current + skew).astimezone(zone)
                        data['exported_at'] = stamp.isoformat()
                        with patch.object(h, 'now', return_value=current):
                            if skew > timedelta(minutes=10):
                                with self.assertRaisesRegex(h.SafeError, '未来导出时间'):
                                    h.decode_export(data, CFG)
                            else:
                                self.assertEqual(h.decode_export(data, CFG)[1], stamp)

    def test_future_export_metadata_is_rejected_even_without_selected_samples(self):
        current = h.now()
        for selected in (False, True):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as temp:
                data = bundle()
                if not selected:
                    data['metrics'] = {'weight': [], 'body_fat': []}
                data['exported_at'] = (current + timedelta(days=1)).isoformat()
                source = Path(temp) / 'future.json'
                source.write_text(json.dumps(data))
                previous = source.read_bytes()
                with patch.object(h, 'now', return_value=current):
                    with self.assertRaisesRegex(h.SafeError, 'future.json:.*未来导出时间'):
                        h.load_exports(dict(CFG, inbox=temp))
                self.assertEqual(source.read_bytes(), previous)

    def test_v1_still_reads(self):
        x = bundle(); data = []
        for metric, samples in x["metrics"].items():
            data.extend(h.decode_export(old(metric, samples), CFG)[0])
        self.assertEqual(h.decode_export(x, CFG)[0], data)

    def test_singleton_object_each_metric(self):
        x = bundle()
        for metric in x['metrics']:
            x['metrics'][metric] = x['metrics'][metric][0]
        self.assertEqual(len(h.decode_export(x, CFG)[0]), 2)

    def test_one_object_other_list(self):
        x = bundle(); x['metrics']['weight'] = x['metrics']['weight'][0]
        self.assertEqual(len(h.decode_export(x, CFG)[0]), 2)

    def test_optional_bmi_missing(self):
        records, _ = h.pair_samples(h.decode_export(bundle(), CFG)[0])
        self.assertIsNone(records[0].bmi)

    def test_optional_bmi_empty(self):
        x = bundle(); x['metrics']['bmi'] = []
        self.assertEqual(len(h.decode_export(x, CFG)[0]), 2)

    def test_optional_bmi_present(self):
        x = bundle(); x['metrics']['bmi'] = [sample('22.5')]
        records, _ = h.pair_samples(h.decode_export(x, CFG)[0])
        self.assertEqual(records[0].bmi, 22.5)

    def test_four_singleton_metrics_preserve_lbm_and_identity(self):
        x = bundle()
        before, _ = h.pair_samples(h.decode_export(x, CFG)[0])
        x['metrics']['bmi'] = [sample('22.5')]
        x['metrics']['lbm'] = [sample('53.55')]
        x['metrics'] = {key: items[0] for key, items in x['metrics'].items()}
        records, waiting = h.pair_samples(h.decode_export(x, CFG)[0])
        self.assertFalse(waiting)
        self.assertEqual(records[0].lbm, 53.55)
        self.assertEqual(records[0].payload()['lbm'], 53.55)
        self.assertEqual(records[0].key, before[0].key)
        self.assertEqual(records[0].core, before[0].core)

    def test_lbm_mass_units_do_not_accept_percent(self):
        self.assertEqual(h.parse_quantity('53550 g', 'lbm', CFG), 53.55)
        with self.assertRaises(h.SafeError):
            h.parse_quantity('53.55%', 'lbm', CFG)

    def test_lbm_greater_than_weight_rejected(self):
        x = bundle(); x['metrics']['lbm'] = [sample('71 kg')]
        with self.assertRaisesRegex(h.SafeError, '瘦体重大于体重'):
            h.pair_samples(h.decode_export(x, CFG)[0])

    def test_one_lbm_cannot_be_shared_by_two_weigh_ins(self):
        x = bundle(); del x['metrics']['body_fat']
        later = h.aware(AT) + timedelta(seconds=10)
        x['metrics']['weight'].append(dict(sample('71 kg'), at=later.isoformat()))
        x['metrics']['lbm'] = [dict(sample('53.55 kg'), at=(h.aware(AT) + timedelta(seconds=5)).isoformat())]
        records, waiting = h.pair_samples(h.decode_export(x, CFG)[0], 10, allow_weight_only=True)
        self.assertEqual(records, [])
        self.assertEqual(len(waiting), 2)
        self.assertTrue(all('瘦体重' in message for message in waiting))

    def test_one_bmi_cannot_be_shared_by_two_weigh_ins(self):
        x = bundle()
        del x['metrics']['body_fat']
        later = h.aware(AT) + timedelta(seconds=10)
        x['metrics']['weight'].append(dict(sample('71 kg'), at=later.isoformat()))
        x['metrics']['bmi'] = [dict(sample('22.5'), at=(h.aware(AT) + timedelta(seconds=5)).isoformat())]
        records, waiting = h.pair_samples(h.decode_export(x, CFG)[0], 10, allow_weight_only=True)
        self.assertEqual(records, [])
        self.assertEqual(len(waiting), 2)
        self.assertTrue(all('BMI' in message for message in waiting))

    def test_exact_bmi_not_reused_for_nearby_weigh_in(self):
        x = bundle()
        del x['metrics']['body_fat']
        later = h.aware(AT) + timedelta(seconds=10)
        x['metrics']['weight'].append(dict(sample('71 kg'), at=later.isoformat()))
        x['metrics']['bmi'] = [sample('22.5')]
        records, waiting = h.pair_samples(h.decode_export(x, CFG)[0], 10, allow_weight_only=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].at, h.aware(AT))
        self.assertEqual(records[0].bmi, 22.5)
        self.assertEqual(len(waiting), 1)

    def test_empty_lists_valid_no_fake_samples(self):
        x = bundle(); x['metrics'] = {'weight': [], 'body_fat': []}
        self.assertEqual(h.decode_export(x, CFG)[0], [])

    def test_only_weight_waits(self):
        x = bundle(); del x['metrics']['body_fat']
        records, waiting = h.pair_samples(h.decode_export(x, CFG)[0])
        self.assertEqual(records, []); self.assertEqual(len(waiting), 1)

    def test_missing_metrics_rejected(self):
        x = bundle(); del x['metrics']
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_empty_metrics_rejected(self):
        x = bundle(); x['metrics'] = {}
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_stringified_metrics_rejected(self):
        x = bundle(); x['metrics'] = json.dumps(x['metrics'])
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_stringified_samples_rejected(self):
        x = bundle(); x['metrics']['weight'] = json.dumps(x['metrics']['weight'])
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_whole_child_payload_rejected(self):
        x = bundle(); x['metrics']['weight'] = old('weight', x['metrics']['weight'])
        with self.assertRaisesRegex(h.SafeError, 'samples'): h.decode_export(x, CFG)

    def test_nested_array_rejected(self):
        x = bundle(); x['metrics']['weight'] = [x['metrics']['weight']]
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_missing_source_rejected(self):
        x = bundle(); del x['metrics']['body_fat'][0]['source']
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_metric_typo_rejected(self):
        x = bundle(); x['metrics']['bodyfat'] = x['metrics'].pop('body_fat')
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_null_metric_rejected(self):
        x = bundle(); x['metrics']['body_fat'] = None
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_bad_version_rejected(self):
        for value in (0, 3, True, None, 'two'):
            with self.subTest(value=value):
                x = bundle(); x['schema_version'] = value
                with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_version_alias_not_silently_assumed(self):
        x = bundle(); x['version'] = x.pop('schema_version')
        with self.assertRaisesRegex(h.SafeError, '字段名不是 version'): h.decode_export(x, CFG)

    def test_mixed_schema_fields_rejected(self):
        x = bundle(); x['metric'] = 'weight'
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)
        x = old('weight', [sample('70 kg')]); x['metrics'] = bundle()['metrics']
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_demo_file_rejected(self):
        x = bundle(); x['demo'] = True
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_demo_sample_rejected(self):
        x = bundle(); x['metrics']['weight'][0]['demo'] = True
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)

    def test_wrong_source_is_filtered(self):
        x = bundle(); x['metrics']['weight'][0]['source'] = 'Garmin Connect'
        data, _ = h.decode_export(x, CFG)
        self.assertEqual([s.metric for s in data], ['body_fat'])

    def test_bad_unit_still_rejected(self):
        x = bundle(); x['metrics']['body_fat'][0]['value'] = '0.235'
        with self.assertRaises(h.SafeError): h.decode_export(x, CFG)
        data, _ = h.decode_export(x, dict(CFG, fat_bare_unit='fraction'))
        self.assertEqual(data[1].value, 23.5)

    def test_grouped_export_does_not_pair_by_position(self):
        x = bundle(); x['metrics']['body_fat'][0]['at'] = '2020-02-21T08:00:00+08:00'
        records, waiting = h.pair_samples(h.decode_export(x, CFG)[0])
        self.assertEqual(records, []); self.assertEqual(len(waiting), 1)

    def test_combined_limit_covers_all_groups(self):
        x = bundle(); x['metrics']['weight'] *= 10001; x['metrics']['body_fat'] *= 10000
        with self.assertRaisesRegex(h.SafeError, '合计过多'): h.decode_export(x, CFG)

    def test_v1_v2_mixed_files_deduplicate_and_record_id_unchanged(self):
        x = bundle()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for metric, samples in x['metrics'].items():
                (root / f'{metric}-old.json').write_text(json.dumps(old(metric, samples)))
            old_samples, _ = h.load_exports(dict(CFG, inbox=temp))
            old_records, _ = h.pair_samples(old_samples)
            (root / 'health-new.json').write_text(json.dumps(x))
            new_samples, exports = h.load_exports(dict(CFG, inbox=temp))
            records, _ = h.pair_samples(new_samples)
            self.assertEqual(len(new_samples), 2)
            self.assertEqual(records[0].key, old_records[0].key)
            self.assertEqual(records[0].core, old_records[0].core)
            self.assertEqual(set(exports), {'weight', 'body_fat'})

    def test_latest_export_timestamp_per_metric(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); x = bundle()
            (p/'old.json').write_text(json.dumps(old('weight', [sample('70 kg')])))
            x['exported_at'] = '2020-02-21T09:00:00+08:00'
            (p/'new.json').write_text(json.dumps(x))
            _, exports = h.load_exports(dict(CFG, inbox=temp))
            self.assertEqual(exports['weight'], x['exported_at'])
            self.assertEqual(exports['body_fat'], x['exported_at'])

    def test_conflicting_value_in_mixed_files_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); x = bundle()
            (p/'old.json').write_text(json.dumps(old('weight', [sample('71 kg')])))
            (p/'new.json').write_text(json.dumps(x))
            with self.assertRaises(h.SafeError): h.load_exports(dict(CFG, inbox=temp))

    def test_inspect_reports_both_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp)/'health.json').write_text(json.dumps(bundle()))
            output = io.StringIO()
            with contextlib.redirect_stdout(output): h.inspect_files(dict(CFG, inbox=temp))
            self.assertIn('schema_version= 2', output.getvalue())
            self.assertIn('weight', output.getvalue()); self.assertIn('body_fat', output.getvalue())

    def test_db_status_survives_transition(self):
        x = bundle(); records, _ = h.pair_samples(h.decode_export(x, CFG)[0]); r = records[0]
        with tempfile.TemporaryDirectory() as temp, patch.object(h, 'STATE', Path(temp)):
            with contextlib.closing(h.database()) as db, db:
                h.set_status(db, r, 'verified')
                combined, _ = h.pair_samples(h.decode_export(bundle(), CFG)[0])
                self.assertEqual(h.get_status(db, combined[0]), 'verified')

if __name__ == '__main__':
    unittest.main()
