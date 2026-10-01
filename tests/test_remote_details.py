"""Full measurements must not be confused with daily latest-value summaries."""
import copy
import unittest
from unittest.mock import Mock

import health_garmin as h
from test_reconciliation import AT, row


class RemoteDetailTests(unittest.TestCase):
    def setUp(self):
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22)
        first = row(self.r, 100)
        second = dict(first, samplePk=101, timestampGMT=first['timestampGMT'] + 30000, weight=71000)
        self.obj = {'dailyWeightSummaries': [{'summaryDate': '2020-02-20',
            'numOfWeightEntries': 2, 'latestWeight': second, 'allWeightMetrics': [first, second]}]}
        self.raw = Mock()
        self.raw.get_weigh_ins.return_value = self.obj
        self.api = h.GarminMeasurementAPI(self.raw)

    def test_range_returns_both_measurements_instead_of_daily_latest(self):
        obj = self.api.get_body_composition('2020-02-19', '2020-02-21')
        self.assertEqual(len(obj['dateWeightList']), 2)
        self.assertEqual(h.classify_remote(self.r, obj), 'matched')
        self.raw.get_body_composition.assert_not_called()

    def test_day_uses_complete_daily_endpoint(self):
        self.raw.get_daily_weigh_ins.return_value = {'dateWeightList': self.obj['dailyWeightSummaries'][0]['allWeightMetrics']}
        self.assertEqual(len(self.api.get_body_composition('2020-02-20', '2020-02-20')['dateWeightList']), 2)
        self.raw.get_daily_weigh_ins.assert_called_once_with('2020-02-20')
        self.raw.get_weigh_ins.assert_not_called()

    def test_missing_details_never_falls_back_to_latest(self):
        del self.obj['dailyWeightSummaries'][0]['allWeightMetrics']
        with self.assertRaises(h.SafeError): self.api.get_body_composition('2020-02-19', '2020-02-21')

    def test_count_mismatch_stops_before_assuming_complete(self):
        self.obj['dailyWeightSummaries'][0]['numOfWeightEntries'] = 3
        with self.assertRaises(h.SafeError): self.api.get_body_composition('2020-02-19', '2020-02-21')

    def test_wrong_calendar_day_rejected(self):
        self.obj['dailyWeightSummaries'][0]['allWeightMetrics'][0]['calendarDate'] = '2020-02-19'
        with self.assertRaises(h.SafeError): self.api.get_body_composition('2020-02-19', '2020-02-21')

    def test_unknown_range_shape_rejected(self):
        self.raw.get_weigh_ins.return_value = {'dateWeightList': []}
        with self.assertRaises(h.SafeError): self.api.get_body_composition('2020-02-19', '2020-02-21')
