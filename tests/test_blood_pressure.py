"""Synthetic offline tests for Garmin blood pressure; no real health data or API."""

import copy
import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

from health_garmin_sync import blood_pressure as hg_blood_pressure
from health_garmin_sync import domain as hg_domain


def measurement(**changes):
    return {
        "version": 1001,
        "measurementTimestampGMT": "2026-10-14T16:30:00.123",
        "measurementTimestampLocal": "2026-10-15T00:30:00.123",
        "sourceType": "MANUAL",
        "systolic": 120,
        "diastolic": 80,
        "pulse": 65,
        "multiMeasurement": False,
        **changes,
    }


def envelope(*rows, **summary_fields):
    return {
        "measurementSummaries": [
            {"numOfMeasurements": len(rows), "measurements": list(rows), **summary_fields}
        ]
    }


class BloodPressureTests(unittest.TestCase):
    def test_parses_observed_envelope_without_using_local_timezone(self):
        response = envelope(measurement())
        original = copy.deepcopy(response)
        record = hg_blood_pressure.parse_blood_pressure(response)[0]
        self.assertEqual(record.source_id, "1001")
        self.assertEqual(record.at, datetime(2026, 10, 14, 16, 30, 0, 123000, UTC))
        self.assertIs(record.at.tzinfo, UTC)
        self.assertEqual(record.local_timestamp, "2026-10-15T00:30:00.123")
        self.assertEqual(record.source_type, "MANUAL")
        self.assertEqual((record.systolic, record.diastolic, record.pulse), (120.0, 80.0, 65.0))
        self.assertIsInstance(record.systolic, float)
        self.assertFalse(record.multi_measurement)
        self.assertEqual(response, original)

    def test_model_is_frozen(self):
        record = hg_blood_pressure.parse_blood_pressure(envelope(measurement()))[0]
        with self.assertRaises(FrozenInstanceError):
            record.systolic = 130

    def test_payload_is_json_safe_without_notes_or_account_fields(self):
        record = hg_blood_pressure.parse_blood_pressure(
            envelope(measurement(notes="private", account="private"))
        )[0]
        self.assertEqual(
            json.loads(json.dumps(record.payload(), allow_nan=False)),
            {
                "source_id": "1001",
                "at": "2026-10-14T16:30:00.123Z",
                "local_timestamp": "2026-10-15T00:30:00.123",
                "source_type": "MANUAL",
                "systolic": 120.0,
                "diastolic": 80.0,
                "pulse": 65.0,
                "multi_measurement": False,
            },
        )

    def test_explicit_offsets_convert_to_utc_across_day_boundary(self):
        for stamp in ("2026-10-15T00:30:00.123+08:00", "2026-10-14T16:30:00.123Z"):
            with self.subTest(stamp=stamp):
                record = hg_blood_pressure.parse_blood_pressure(
                    envelope(measurement(measurementTimestampGMT=stamp))
                )[0]
                self.assertEqual(record.payload()["at"], "2026-10-14T16:30:00.123Z")
        record = hg_blood_pressure.parse_blood_pressure(
            envelope(measurement(measurementTimestampGMT="2026-10-14T23:30:00-02:00"))
        )[0]
        self.assertEqual(record.payload()["at"], "2026-10-15T01:30:00.000Z")

    def test_payload_preserves_submillisecond_precision(self):
        record = hg_blood_pressure.parse_blood_pressure(
            envelope(measurement(measurementTimestampGMT="2026-10-14T16:30:00.123456Z"))
        )[0]
        self.assertEqual(record.payload()["at"], "2026-10-14T16:30:00.123456Z")

    def test_records_sort_by_utc_then_source_id(self):
        response = envelope(
            measurement(version=1003),
            measurement(version="1002"),
            measurement(version=1004, measurementTimestampGMT="2026-10-14T16:29:00Z"),
        )
        records = hg_blood_pressure.parse_blood_pressure(response)
        self.assertEqual([record.source_id for record in records], ["1004", "1002", "1003"])

    def test_original_multi_measurement_flag_is_preserved_without_averaging(self):
        records = hg_blood_pressure.parse_blood_pressure(
            envelope(
                measurement(multiMeasurement=True),
                measurement(version=1002, systolic=140, pulse=75, multiMeasurement=False),
            )
        )
        self.assertEqual([record.systolic for record in records], [120, 140])
        self.assertEqual([record.pulse for record in records], [65, 75])
        self.assertEqual([record.multi_measurement for record in records], [True, False])

    def test_count_is_optional_and_daily_count_does_not_imply_multi_measurement(self):
        response = envelope(measurement(), measurement(version=1002))
        del response["measurementSummaries"][0]["numOfMeasurements"]
        records = hg_blood_pressure.parse_blood_pressure(response)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(not record.multi_measurement for record in records))

    def test_missing_multi_measurement_defaults_false(self):
        row = measurement()
        del row["multiMeasurement"]
        record = hg_blood_pressure.parse_blood_pressure(envelope(row))[0]
        self.assertFalse(record.multi_measurement)

    def test_multi_measurement_rejects_nonboolean_values(self):
        for invalid in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(hg_domain.SafeError):
                hg_blood_pressure.parse_blood_pressure(
                    envelope(measurement(multiMeasurement=invalid))
                )

    def test_missing_or_null_pulse_stays_absent(self):
        for present in (False, True):
            with self.subTest(present=present):
                row = measurement(pulse=None)
                if not present:
                    del row["pulse"]
                record = hg_blood_pressure.parse_blood_pressure(envelope(row))[0]
                self.assertIsNone(record.pulse)
                self.assertIsNone(record.payload()["pulse"])

    def test_device_ranges_and_positive_values_do_not_use_sdk_write_limits(self):
        for systolic, diastolic in ((60, 40), (250, 180), (0.5, 0.25), (300, 200)):
            with self.subTest(systolic=systolic, diastolic=diastolic):
                record = hg_blood_pressure.parse_blood_pressure(
                    envelope(measurement(systolic=systolic, diastolic=diastolic, pulse=0.5))
                )[0]
                self.assertEqual((record.systolic, record.diastolic), (systolic, diastolic))

    def test_numbers_reject_bool_nonfinite_nonpositive_or_non_numeric(self):
        for field in ("systolic", "diastolic", "pulse"):
            for invalid in (
                True,
                False,
                float("nan"),
                float("inf"),
                -float("inf"),
                0,
                -1,
                "120",
                "NaN",
                [],
                {},
                10**1000,
            ):
                with self.subTest(field=field, invalid=type(invalid).__name__):
                    with self.assertRaises(hg_domain.SafeError):
                        hg_blood_pressure.parse_blood_pressure(
                            envelope(measurement(**{field: invalid}))
                        )

    def test_required_fields_cannot_be_missing_or_null(self):
        for field in (
            "version",
            "measurementTimestampGMT",
            "measurementTimestampLocal",
            "sourceType",
            "systolic",
            "diastolic",
        ):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    row = measurement(**{field: None})
                    if missing:
                        del row[field]
                    with self.assertRaises(hg_domain.SafeError):
                        hg_blood_pressure.parse_blood_pressure(envelope(row))

    def test_version_accepts_positive_integer_or_canonical_decimal(self):
        for version in (1, 1001, "1", "1001", "123456789012345678901234567890"):
            with self.subTest(version=version):
                record = hg_blood_pressure.parse_blood_pressure(
                    envelope(measurement(version=version))
                )[0]
                self.assertEqual(record.source_id, str(version))

    def test_version_rejects_noncanonical_or_nonpositive_values(self):
        for version in (
            True,
            False,
            0,
            -1,
            1.0,
            "0",
            "01",
            "+1",
            "-1",
            "1.0",
            "1e3",
            " 1",
            "1 ",
            "١",
            "",
            None,
            [],
            {},
        ):
            with self.subTest(version=version):
                with self.assertRaises(hg_domain.SafeError):
                    hg_blood_pressure.parse_blood_pressure(envelope(measurement(version=version)))

    def test_invalid_timestamp_formats_and_calendar_values_are_rejected(self):
        for field in ("measurementTimestampGMT", "measurementTimestampLocal"):
            for invalid in (
                "2026-10-15",
                "20261015T003000",
                "2026-10-15 00:30:00",
                "2026-10-15T00:30",
                "2026-02-30T00:30:00",
                "2026-10-15T25:30:00",
                "2026-10-15T00:30:00+25:00",
                "2026-10-15T00:30:00+01:60",
                "2026-10-15T00:30:00.1234567",
                "",
                True,
                1,
            ):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(hg_domain.SafeError):
                        hg_blood_pressure.parse_blood_pressure(
                            envelope(measurement(**{field: invalid}))
                        )

    def test_utc_conversion_overflow_is_safe_error(self):
        for stamp in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(stamp=stamp), self.assertRaises(hg_domain.SafeError):
                hg_blood_pressure.parse_blood_pressure(
                    envelope(measurement(measurementTimestampGMT=stamp))
                )

    def test_empty_arrays_are_valid(self):
        for response in (
            {"measurementSummaries": []},
            envelope(),
            {"measurementSummaries": [{"measurements": []}]},
        ):
            with self.subTest(response=response):
                self.assertEqual(hg_blood_pressure.parse_blood_pressure(response), [])

    def test_unknown_or_incomplete_envelope_is_not_empty_success(self):
        for response in (
            None,
            [],
            "{}",
            {},
            {"measurements": []},
            {"measurementSummaries": None},
            {"measurementSummaries": {}},
            {"measurementSummaries": [None]},
            {"measurementSummaries": [[]]},
            {"measurementSummaries": [{}]},
            {"measurementSummaries": [{"measurements": None}]},
            {"measurementSummaries": [{"measurements": {}}]},
            envelope(None),
            envelope([]),
            envelope({}),
        ):
            with self.subTest(response=response), self.assertRaises(hg_domain.SafeError):
                hg_blood_pressure.parse_blood_pressure(response)

    def test_summary_count_requires_exact_nonbool_integer_count(self):
        for invalid in (None, True, False, "1", 1.0, -1, 0, 2):
            with self.subTest(invalid=invalid), self.assertRaises(hg_domain.SafeError):
                hg_blood_pressure.parse_blood_pressure(
                    envelope(measurement(), numOfMeasurements=invalid)
                )

    def test_exact_repeats_deduplicate_after_count_validation(self):
        response = envelope(measurement(), measurement(version="1001"))
        records = hg_blood_pressure.parse_blood_pressure(response)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].multi_measurement)
        response["measurementSummaries"][0]["numOfMeasurements"] = 1
        with self.assertRaises(hg_domain.SafeError):
            hg_blood_pressure.parse_blood_pressure(response)

    def test_exact_repeat_between_summaries_deduplicates(self):
        summary = envelope(measurement())["measurementSummaries"][0]
        records = hg_blood_pressure.parse_blood_pressure(
            {"measurementSummaries": [summary, copy.deepcopy(summary)]}
        )
        self.assertEqual(len(records), 1)

    def test_shared_version_with_any_different_payload_is_ambiguous(self):
        for change in (
            {"measurementTimestampGMT": "2026-10-14T16:31:00"},
            {"measurementTimestampLocal": "2026-10-15T00:31:00"},
            {"sourceType": "DEVICE"},
            {"systolic": 121},
            {"diastolic": 81},
            {"pulse": 66},
            {"pulse": None},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(hg_domain.SafeError, "歧义"):
                hg_blood_pressure.parse_blood_pressure(
                    envelope(measurement(), measurement(**change))
                )

    def test_shared_version_with_different_original_multi_measurement_flag_is_ambiguous(self):
        summaries = envelope(measurement())["measurementSummaries"]
        summaries += envelope(measurement(multiMeasurement=True))["measurementSummaries"]
        with self.assertRaisesRegex(hg_domain.SafeError, "歧义"):
            hg_blood_pressure.parse_blood_pressure({"measurementSummaries": summaries})

    def test_distinct_versions_at_same_instant_remain_distinct(self):
        records = hg_blood_pressure.parse_blood_pressure(
            envelope(measurement(), measurement(version=1002))
        )
        self.assertEqual(len(records), 2)

    def test_bad_source_type_is_rejected_and_errors_do_not_echo_private_fields(self):
        for source_type in ("", " ", True, 1, []):
            with self.subTest(source_type=source_type):
                with self.assertRaises(hg_domain.SafeError) as raised:
                    hg_blood_pressure.parse_blood_pressure(
                        envelope(measurement(sourceType=source_type, notes="PRIVATE_ACCOUNT"))
                    )
                self.assertNotIn("PRIVATE_ACCOUNT", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
