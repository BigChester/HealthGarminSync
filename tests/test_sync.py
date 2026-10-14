"""Offline tests only. No credentials, Garmin requests, iCloud or HealthKit access."""

import json
import os
import plistlib
import sys
import tempfile
import time
import unittest
from datetime import UTC, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from health_garmin_sync import domain as hg_domain
from health_garmin_sync import exports as hg_exports
from health_garmin_sync import garmin as hg_garmin
from health_garmin_sync import launchagent as hg_launchagent
from health_garmin_sync import processing as hg_processing
from health_garmin_sync import remote as hg_remote
from health_garmin_sync import runtime as hg_runtime
from health_garmin_sync import sources as hg_sources
from health_garmin_sync import state as hg_state

CFG = {
    "allowed_sources": ["PICOOC"],
    "since": "2020-01-01",
    "weight_bare_unit": "kg",
    "fat_bare_unit": "percent",
}
T = hg_domain.aware("2020-02-20T08:00:00+08:00")


def export(metric="weight", value="70.0 kg", stamp=None, source="PICOOC"):
    return {
        "schema_version": 1,
        "metric": metric,
        "exported_at": "2020-02-20T09:00:00+08:00",
        "samples": [{"at": stamp or T.isoformat(), "value": value, "source": source}],
    }


def remote(r, weight=None, fat=None):
    return {
        "dateWeightList": [
            {
                "date": int(r.at.timestamp() * 1000),
                "weight": r.weight * 1000 if weight is None else weight,
                "bodyFat": r.fat if fat is None else fat,
                "bmi": r.bmi,
            }
        ]
    }


class UnitTests(unittest.TestCase):
    def test_kg(self):
        self.assertEqual(hg_exports.parse_quantity("70.5 kg", "weight", CFG), 70.5)

    def test_chinese_kg(self):
        self.assertEqual(hg_exports.parse_quantity("70.5千克", "weight", CFG), 70.5)

    def test_lb(self):
        self.assertAlmostEqual(
            hg_exports.parse_quantity("154.323584 lb", "weight", CFG), 70, places=5
        )

    def test_gram(self):
        self.assertEqual(hg_exports.parse_quantity("70000 g", "weight", CFG), 70)

    def test_percent_suffix(self):
        self.assertEqual(hg_exports.parse_quantity("23.5%", "body_fat", CFG), 23.5)

    def test_fraction(self):
        c = dict(CFG, fat_bare_unit="fraction")
        self.assertEqual(hg_exports.parse_quantity("0.235", "body_fat", c), 23.5)

    def test_explicit_percent_overrides_bare_config(self):
        c = dict(CFG, fat_bare_unit="fraction")
        self.assertEqual(hg_exports.parse_quantity("23.5 %", "body_fat", c), 23.5)

    def test_small_bare_fat_rejected(self):
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.parse_quantity("0.235", "body_fat", CFG)

    def test_comma_rejected(self):
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.parse_quantity("70,5", "weight", CFG)

    def test_nan_rejected(self):
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.parse_quantity(float("nan"), "weight", CFG)

    def test_boolean_rejected(self):
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.parse_quantity(True, "weight", CFG)

    def test_unknown_unit_rejected(self):
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.parse_quantity("140 斤", "weight", CFG)


class ExportTests(unittest.TestCase):
    def test_export_parse(self):
        ss, _ = hg_exports.decode_export(export(), CFG)
        self.assertEqual(ss[0].value, 70)

    def test_unwrapped_single_object(self):
        x = export()
        x["samples"] = x["samples"][0]
        ss, _ = hg_exports.decode_export(x, CFG)
        self.assertEqual(len(ss), 1)

    def test_empty_array(self):
        x = export()
        x["samples"] = []
        self.assertEqual(hg_exports.decode_export(x, CFG)[0], [])

    def test_no_source_rejected(self):
        x = export()
        del x["samples"][0]["source"]
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.decode_export(x, CFG)

    def test_wrong_source_excluded(self):
        self.assertEqual(hg_exports.decode_export(export(source="Garmin Connect"), CFG)[0], [])

    def test_demo_rejected(self):
        x = export()
        x["demo"] = True
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.decode_export(x, CFG)

    def test_json_string_array_rejected(self):
        x = export()
        x["samples"] = json.dumps(x["samples"])
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.decode_export(x, CFG)

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.decode_export(export(stamp="2020-02-20T08:00:00"), CFG)

    def test_old_samples_excluded(self):
        self.assertEqual(hg_exports.decode_export(export(), dict(CFG, since="2020-02-21"))[0], [])

    def test_future_sample_rejected(self):
        stamp = (hg_runtime.now() + timedelta(days=1)).isoformat()
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.decode_export(export(stamp=stamp), CFG)

    def test_real_file_loading(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            (p / "one.json").write_text(json.dumps(export()))
            ss, ts = hg_sources.load_exports(dict(CFG, inbox=temp))
            self.assertEqual(len(ss), 1)
            self.assertIn("weight", ts)


class PairTests(unittest.TestCase):
    def w(self, t=T):
        return hg_domain.Sample("weight", t, 70, "PICOOC")

    def f(self, t=T):
        return hg_domain.Sample("body_fat", t, 23.5, "PICOOC")

    def test_exact_pair(self):
        rr, wait = hg_exports.pair_samples([self.w(), self.f()])
        self.assertEqual((len(rr), len(wait)), (1, 0))

    def test_fat_missing(self):
        rr, wait = hg_exports.pair_samples([self.w()])
        self.assertEqual((len(rr), len(wait)), (0, 1))

    def test_different_day_not_paired(self):
        rr, _ = hg_exports.pair_samples([self.w(), self.f(T + timedelta(days=1))], 60)
        self.assertEqual(rr, [])

    def test_window_off_by_default(self):
        rr, _ = hg_exports.pair_samples([self.w(), self.f(T + timedelta(seconds=5))])
        self.assertEqual(rr, [])

    def test_explicit_window(self):
        rr, _ = hg_exports.pair_samples([self.w(), self.f(T + timedelta(seconds=5))], 10)
        self.assertEqual(len(rr), 1)

    def test_ambiguous_fat_rejected(self):
        rr, _ = hg_exports.pair_samples(
            [self.w(), self.f(T - timedelta(seconds=5)), self.f(T + timedelta(seconds=5))], 10
        )
        self.assertEqual(rr, [])

    def test_one_fat_not_reused(self):
        rr, _ = hg_exports.pair_samples(
            [self.w(T - timedelta(seconds=5)), self.w(T + timedelta(seconds=5)), self.f()], 10
        )
        self.assertEqual(rr, [])

    def test_exact_preferred(self):
        rr, _ = hg_exports.pair_samples([self.w(), self.f(), self.f(T + timedelta(seconds=5))], 10)
        self.assertEqual(len(rr), 1)

    def test_identical_exports_deduplicate(self):
        self.assertEqual(len(hg_exports.deduplicate([self.w(), self.w()])), 1)

    def test_conflicting_revision_stops(self):
        with self.assertRaises(hg_domain.SafeError):
            hg_exports.deduplicate([self.w(), hg_domain.Sample("weight", T, 71, "PICOOC")])

    def test_same_instant_different_offset_same_id(self):
        a = hg_domain.Measurement(T, 70, 23.5, "PICOOC")
        b = hg_domain.Measurement(T.astimezone(UTC), 70, 23.5, "picooc")
        self.assertEqual(a.key, b.key)

    def test_bmi_optional(self):
        rr, _ = hg_exports.pair_samples([self.w(), self.f()])
        self.assertIsNone(rr[0].bmi)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.r = hg_domain.Measurement(T, 70, 23.5, "PICOOC")

    def test_matched_grams(self):
        self.assertEqual(hg_remote.classify_remote(self.r, remote(self.r)), "matched")

    def test_absent(self):
        self.assertEqual(hg_remote.classify_remote(self.r, {"dateWeightList": []}), "absent")

    def test_unknown_shape_fails(self):
        with self.assertRaises(hg_domain.SafeError):
            hg_remote.classify_remote(self.r, {"unexpected": []})

    def test_wrong_fat_conflict(self):
        self.assertEqual(hg_remote.classify_remote(self.r, remote(self.r, fat=0.235)), "conflict")

    def test_wrong_weight_unit_conflict(self):
        self.assertEqual(hg_remote.classify_remote(self.r, remote(self.r, weight=70)), "conflict")

    def test_missing_time_conflict(self):
        x = remote(self.r)
        x["dateWeightList"][0]["date"] = "2020-02-20"
        self.assertEqual(hg_remote.classify_remote(self.r, x), "conflict")

    def test_naive_gmt_field(self):
        x = remote(self.r)
        x["dateWeightList"][0].pop("date")
        x["dateWeightList"][0]["timestampGMT"] = "2020-02-20T00:00:00"
        self.assertEqual(hg_remote.classify_remote(self.r, x), "matched")

    def test_existing_weight_without_fat_not_overwritten(self):
        x = remote(self.r)
        x["dateWeightList"][0]["bodyFat"] = None
        self.assertEqual(hg_remote.classify_remote(self.r, x), "conflict")

    def test_two_remote_matches_conflict(self):
        x = remote(self.r)
        x["dateWeightList"] *= 2
        self.assertEqual(hg_remote.classify_remote(self.r, x), "conflict")


class FakeAPI:
    def __init__(self, r, fail=False, visible=True):
        self.r = r
        self.fail = fail
        self.visible = visible
        self.posts = 0
        self.existing = False

    def get_body_composition(self, *args):
        return remote(self.r) if self.existing else {"dateWeightList": []}

    def add_body_composition(self, **kwargs):
        self.posts += 1
        self.payload = kwargs
        if self.fail:
            raise TimeoutError("private-token-must-not-leak")
        self.existing = self.visible
        return {"detailedImportResult": {"successes": [{}], "failures": []}}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(hg_runtime, "STATE", Path(self.tmp.name))
        self.patch.start()
        self.db = hg_state.database()
        self.r = hg_domain.Measurement(T, 70, 23.5, "PICOOC")

    def tearDown(self):
        self.db.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_upload_and_readback_verified(self):
        api = FakeAPI(self.r)
        self.assertEqual(
            hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None), "verified"
        )
        self.assertEqual(api.posts, 1)
        self.assertEqual(api.payload["timestamp"], "2020-02-20T00:00:00+00:00")

    def test_second_run_no_reupload(self):
        api = FakeAPI(self.r)
        hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None)
        hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None)
        self.assertEqual(api.posts, 1)

    def test_existing_remote_no_upload(self):
        api = FakeAPI(self.r)
        api.existing = True
        self.assertEqual(hg_processing.process_record(api, self.db, self.r), "verified")
        self.assertEqual(api.posts, 0)

    def test_timeout_uncertain_no_blind_retry(self):
        api = FakeAPI(self.r, fail=True)
        hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None)
        hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None)
        self.assertEqual(api.posts, 1)
        self.assertEqual(hg_state.get_status(self.db, self.r), "uncertain")

    def test_delayed_visibility_recovers_without_write(self):
        api = FakeAPI(self.r, visible=False)
        hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None)
        api.existing = True
        self.assertEqual(hg_processing.process_record(api, self.db, self.r), "verified")
        self.assertEqual(api.posts, 1)

    def test_inflight_marker_blocks_repeat_after_crash(self):
        hg_state.set_status(self.db, self.r, "inflight")
        api = FakeAPI(self.r)
        self.assertEqual(hg_processing.process_record(api, self.db, self.r), "uncertain")
        self.assertEqual(api.posts, 0)

    def test_marker_durable_before_post(self):
        api = FakeAPI(self.r)
        original = api.add_body_composition

        def check(**kwargs):
            self.assertEqual(hg_state.get_status(self.db, self.r), "inflight")
            return original(**kwargs)

        api.add_body_composition = check
        hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None)

    def test_core_revision_rejected(self):
        hg_state.set_status(self.db, self.r, "verified")
        with self.assertRaises(hg_domain.SafeError):
            hg_state.get_status(self.db, hg_domain.Measurement(T, 71, 23.5, "PICOOC"))

    def test_optional_late_bmi_does_not_duplicate(self):
        hg_state.set_status(self.db, self.r, "verified")
        self.assertEqual(
            hg_state.get_status(self.db, hg_domain.Measurement(T, 70, 23.5, "PICOOC", 22.5)),
            "verified",
        )

    def test_exception_does_not_store_secret(self):
        api = FakeAPI(self.r, fail=True)
        hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None)
        detail = self.db.execute("SELECT detail FROM uploads").fetchone()[0]
        self.assertNotIn("private-token", detail)


class PlatformTests(unittest.TestCase):
    def test_utc_mktime_compatibility_and_restore(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            with hg_garmin.utc_fit_clock():
                utc = T.astimezone(UTC)
                self.assertEqual(time.mktime(utc.timetuple()), T.timestamp())
            self.assertEqual(os.environ["TZ"], "America/Los_Angeles")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_launchd_plist_valid_and_absolute(self):
        doc = plistlib.loads(plistlib.dumps(hg_launchagent.agent_plist()))
        self.assertEqual(doc["StartInterval"], 1800)
        self.assertTrue(Path(doc["ProgramArguments"][0]).is_absolute())
        self.assertEqual(doc["ProgramArguments"][-1], "health_garmin_sync.background")
        self.assertEqual(doc["StandardOutPath"], "/dev/null")
        self.assertEqual(doc["StandardErrorPath"], "/dev/null")


if __name__ == "__main__":
    unittest.main()
