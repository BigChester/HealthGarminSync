"""Full measurements must not be confused with daily latest-value summaries."""

import unittest
from unittest.mock import Mock

from test_reconciliation import AT, row

from health_garmin_sync import domain as hg_domain
from health_garmin_sync import garmin as hg_garmin
from health_garmin_sync import remote as hg_remote


class RemoteDetailTests(unittest.TestCase):
    def setUp(self):
        self.r = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22)
        first = row(self.r, 100)
        second = dict(first, samplePk=101, timestampGMT=first["timestampGMT"] + 30000, weight=71000)
        self.obj = {
            "dailyWeightSummaries": [
                {
                    "summaryDate": "2020-02-20",
                    "numOfWeightEntries": 2,
                    "latestWeight": second,
                    "allWeightMetrics": [first, second],
                }
            ]
        }
        self.raw = Mock()
        self.raw.get_weigh_ins.return_value = self.obj
        self.api = hg_garmin.GarminMeasurementAPI(self.raw)

    def test_range_returns_both_measurements_instead_of_daily_latest(self):
        obj = self.api.get_body_composition("2020-02-19", "2020-02-21")
        self.assertEqual(len(obj["dateWeightList"]), 2)
        self.assertEqual(hg_remote.classify_remote(self.r, obj), "matched")
        self.raw.get_body_composition.assert_not_called()

    def test_day_uses_complete_daily_endpoint(self):
        self.raw.get_daily_weigh_ins.return_value = {
            "dateWeightList": self.obj["dailyWeightSummaries"][0]["allWeightMetrics"]
        }
        self.assertEqual(
            len(self.api.get_body_composition("2020-02-20", "2020-02-20")["dateWeightList"]), 2
        )
        self.raw.get_daily_weigh_ins.assert_called_once_with("2020-02-20")
        self.raw.get_weigh_ins.assert_not_called()

    def test_missing_details_never_falls_back_to_latest(self):
        del self.obj["dailyWeightSummaries"][0]["allWeightMetrics"]
        with self.assertRaises(hg_domain.SafeError):
            self.api.get_body_composition("2020-02-19", "2020-02-21")

    def test_count_mismatch_stops_before_assuming_complete(self):
        self.obj["dailyWeightSummaries"][0]["numOfWeightEntries"] = 3
        with self.assertRaises(hg_domain.SafeError):
            self.api.get_body_composition("2020-02-19", "2020-02-21")

    def test_repeated_known_identity_cannot_satisfy_daily_count(self):
        first = self.obj["dailyWeightSummaries"][0]["allWeightMetrics"][0]
        for duplicate in (
            dict(first),
            dict(first, samplePk=str(first["samplePk"])),
            dict(first, samplePk="00100"),
            dict(first, weight=71000),
        ):
            with self.subTest(duplicate=duplicate):
                self.obj["dailyWeightSummaries"][0]["allWeightMetrics"] = [first, duplicate]
                with self.assertRaises(hg_domain.SafeError) as raised:
                    self.api.get_body_composition("2020-02-19", "2020-02-21")
                self.assertNotIn(str(first["samplePk"]), str(raised.exception))
                self.assertNotIn("71000", str(raised.exception))

    def test_daily_details_reject_known_aliases_and_existing_raw_duplicates(self):
        first = self.obj["dailyWeightSummaries"][0]["allWeightMetrics"][0]
        for pk in (100, "100", "00100", "opaque"):
            with self.subTest(identity=pk):
                original = dict(first, samplePk="opaque") if pk == "opaque" else first
                self.raw.get_daily_weigh_ins.return_value = {
                    "dateWeightList": [original, dict(original, samplePk=pk)]
                }
                with self.assertRaises(hg_domain.SafeError) as raised:
                    self.api.get_body_composition("2020-02-20", "2020-02-20")
                self.assertNotIn(str(pk), str(raised.exception))
                self.assertNotIn(str(first["weight"]), str(raised.exception))

    def test_distinct_known_ids_and_anonymous_details_keep_existing_contract(self):
        first, second = self.obj["dailyWeightSummaries"][0]["allWeightMetrics"]
        for items in (
            [first, second],
            [dict(first, samplePk=None), dict(first, samplePk=None)],
            [
                {key: value for key, value in first.items() if key != "samplePk"},
                {key: value for key, value in first.items() if key != "samplePk"},
            ],
        ):
            with self.subTest(items=items):
                self.obj["dailyWeightSummaries"][0]["allWeightMetrics"] = items
                result = self.api.get_body_composition("2020-02-19", "2020-02-21")
                self.assertEqual(result["dateWeightList"], items)

    def test_wrong_calendar_day_rejected(self):
        self.obj["dailyWeightSummaries"][0]["allWeightMetrics"][0]["calendarDate"] = "2020-02-19"
        with self.assertRaises(hg_domain.SafeError):
            self.api.get_body_composition("2020-02-19", "2020-02-21")

    def test_unknown_range_shape_rejected(self):
        self.raw.get_weigh_ins.return_value = {"dateWeightList": []}
        with self.assertRaises(hg_domain.SafeError):
            self.api.get_body_composition("2020-02-19", "2020-02-21")
