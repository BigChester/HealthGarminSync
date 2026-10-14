"""Remote comparison and reviewed mutation tests, using no accounts or network."""

import argparse
import contextlib
import copy
import io
import json
import sqlite3 as hg_std_sqlite3
import tempfile
import unittest
from datetime import UTC as hg_std_UTC
from datetime import timedelta
from importlib.metadata import version as hg_std_version
from pathlib import Path
from unittest.mock import Mock, patch, seal

from health_garmin_sync import domain as hg_domain
from health_garmin_sync import exports as hg_exports
from health_garmin_sync import file_io as hg_file_io
from health_garmin_sync import garmin as hg_garmin
from health_garmin_sync import operations as hg_operations
from health_garmin_sync import processing as hg_processing
from health_garmin_sync import reconciliation as hg_reconciliation
from health_garmin_sync import remote as hg_remote
from health_garmin_sync import runtime as hg_runtime
from health_garmin_sync import sources as hg_sources
from health_garmin_sync import state as hg_state
from health_garmin_sync import sync as hg_sync

AT = hg_domain.aware("2020-02-20T08:00:00+08:00")


def row(r, pk=100):
    return {
        "samplePk": pk,
        "calendarDate": r.at.date().isoformat(),
        "timestampGMT": int(r.at.timestamp() * 1000),
        "weight": r.weight * 1000,
        "bodyFat": r.fat,
        "bmi": r.bmi,
        "sourceType": "INDEX_SCALE",
    }


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
            return {"unknown": []}
        return {
            "dateWeightList": copy.deepcopy(
                [r for r in self.rows if start <= r["calendarDate"] <= end]
            )
        }

    def add_body_composition(self, **kw):
        self.posts.append(kw)
        if self.visible:
            r = hg_domain.Measurement(
                hg_domain.aware(kw["timestamp"]),
                kw["weight"],
                kw["percent_fat"],
                "PICOOC",
                kw.get("bmi"),
            )
            item = row(r, pk=200 + len(self.posts))
            # Garmin's calendar date follows account local time.
            item["calendarDate"] = r.at.astimezone(AT.tzinfo).date().isoformat()
            self.rows.append(item)
        return {"detailedImportResult": {"successes": [{}], "failures": []}}

    def add_weigh_in(self, **kw):
        self.posts.append(kw)
        if self.visible:
            self.rows.append(
                row(
                    hg_domain.Measurement(
                        hg_domain.aware(kw["timestamp"]), kw["weight"], None, "PICOOC"
                    ),
                    200 + len(self.posts),
                )
            )
        return {"ok": True}

    def delete_weigh_in(self, pk, day):
        self.deletes.append((pk, day))
        self.rows = [r for r in self.rows if str(r["samplePk"]) != pk]
        if self.delete_timeout:
            raise TimeoutError("secret-ticket")


class ReconciliationTests(unittest.TestCase):
    def assert_native_review_id_change(
        self, kind, phase, *, before_id=100, after_id="00100", change="alias"
    ):
        import test_sdk_transport as transport

        from health_garmin_sync import history as bulk

        if not transport.PINNED_SDK:
            self.skipTest("Requires the installed, pinned Garmin SDK")
        sdk = transport.SDKTransportTests("runTest")
        try:
            sdk.setUp()
            sdk.enterContext(patch("subprocess.run", side_effect=transport.forbidden))
            records = [sdk.r]
            original = row(sdk.r, before_id)
            if kind == "bulk":
                records.append(
                    hg_domain.Measurement(sdk.r.at + timedelta(hours=2), 72, 24, "PICOOC", 23, 54)
                )
            elif kind == "repair":
                original["bodyFat"] = None
            else:
                original = row(
                    hg_domain.Measurement(sdk.r.at + timedelta(hours=2), 75, None, "PICOOC"),
                    before_id,
                )
            self.write_export.__func__(sdk, records)
            sdk.rows = [original] if kind != "extra" else [row(sdk.r, 300), original]
            source = (sdk.inbox / "health.json").read_bytes()
            token = sdk.state / "synthetic-token"
            token.write_bytes(b"unchanged token")
            active = False

            def server(method, path, pool):
                if method == "DELETE":
                    day, pk = path.split("/")[-3], int(path.rsplit("/", 1)[-1])
                    self.assertEqual((day, pk), (original["calendarDate"], 100))
                    sdk.rows = [
                        r
                        for r in sdk.rows
                        if (hg_remote.canonical_remote_id(r.get("samplePk")), r["calendarDate"])
                        != (pk, day)
                    ]
                    return 200, {"ok": True}
                if method == "POST":
                    values = sdk.fit_values[-1][1]
                    at = transport.fit_weight_timestamp(sdk.fit_files[-1]).astimezone(AT.tzinfo)
                    sdk.rows.append(
                        row(
                            hg_domain.Measurement(
                                at, values["weight"], values["percent_fat"], "PICOOC", values["bmi"]
                            ),
                            201,
                        )
                    )
                    return 200, {"detailedImportResult": {"successes": [{}], "failures": []}}
                self.assertEqual(method, "GET")
                obj = copy.deepcopy(sdk.read_payload(path))
                if active or (phase == "day" and "/dayview/" in path):
                    items = (
                        obj["dateWeightList"]
                        if "/dayview/" in path
                        else [r for s in obj["dailyWeightSummaries"] for r in s["allWeightMetrics"]]
                    )
                    for item in items:
                        if item.get("samplePk") != before_id:
                            continue
                        if change in ("alias", "unknown_alias"):
                            item["samplePk"] = after_id
                        elif change == "weight":
                            item["weight"] += 1000
                        elif change == "fat":
                            item["bodyFat"] = 25
                        elif change == "bmi":
                            item["bmi"] = 24
                        elif change == "date":
                            item["calendarDate"] = "2020-02-21"
                        elif change == "new_id":
                            item["samplePk"] = 101
                        elif change == "metadata":
                            item["sourceType"] = "MANUAL"
                    if change == "ambiguous":
                        items.append(dict(original, samplePk=after_id, calendarDate="2020-02-21"))
                    if change in ("date", "ambiguous") and "/range/" in path:
                        days = sorted({r["calendarDate"] for r in items})
                        obj["dailyWeightSummaries"] = [
                            {
                                "summaryDate": day,
                                "numOfWeightEntries": len(day_rows),
                                "allWeightMetrics": day_rows,
                            }
                            for day in days
                            for day_rows in [[r for r in items if r["calendarDate"] == day]]
                        ]
                return 200, obj

            sdk.handler = server
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                saved = hg_reconciliation.compare_exports(
                    argparse.Namespace(inbox=None, since="2020-02-20", until="2020-02-21"), sdk.cfg
                )
            path = next((sdk.state / "plans").glob("compare-*.json"))
            saved_bytes = path.read_bytes()
            if phase == "initial":
                active = True
            prompts = []

            def confirm(text):
                nonlocal active
                prompts.append(text)
                if phase == "confirmation":
                    active = True
                return (
                    "REPLACE ALL"
                    if kind == "bulk"
                    else "REPLACE " + sdk.r.key
                    if kind == "repair"
                    else "DELETE " + str(after_id if phase == "initial" else before_id)
                )

            args = argparse.Namespace(
                plan=str(path), complete_export=True, record_id=sdk.r.key, remote_id=str(before_id)
            )
            output = io.StringIO()
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                patch("builtins.input", side_effect=confirm),
                contextlib.redirect_stdout(output),
            ):

                def invoke():
                    if kind == "bulk":
                        bulk.execute(args, sdk.cfg, sdk.api)
                    else:
                        hg_operations.reviewed_operation(
                            args, sdk.cfg, delete_extra=kind == "extra"
                        )

                if change == "alias":
                    invoke()
                else:
                    with self.assertRaises(hg_domain.SafeError):
                        invoke()
            mutations = [r["method"] for r in sdk.sent if r["method"] != "GET"]
            journals = list((sdk.state / "operations").glob("*.json"))
            if change == "alias":
                self.assertEqual(
                    mutations,
                    ["POST"]
                    if kind == "bulk"
                    else ["DELETE", "POST"]
                    if kind == "repair"
                    else ["DELETE"],
                )
                job = hg_state.read_operation(journals[0])
                self.assertEqual(
                    job["phase"], "deleted_verified" if kind == "extra" else "verified"
                )
                if kind == "bulk":
                    self.assertIn(
                        "保留匹配 1 条；删除旧记录 0 条；重建/新增 1 条", output.getvalue()
                    )
                    self.assertEqual(sdk.rows[0], original)
                    self.assertEqual(job["verification"]["matched"], 2)
                    self.assertEqual(
                        [hg_state.get_status(sdk.db, r) for r in records], ["verified", "verified"]
                    )
                else:
                    self.assertEqual(job["original"]["samplePk"], after_id)
                    self.assertEqual(
                        json.loads((Path(job["backup"]) / "remote.json").read_text())[
                            "dateWeightList"
                        ][0 if kind == "repair" else 1]["samplePk"],
                        after_id,
                    )
                self.assertEqual(
                    saved["remote_snapshot"]["dateWeightList"][-1]["samplePk"], before_id
                )
            else:
                self.assertEqual(mutations, [])
                self.assertFalse(journals)
                self.assertEqual(len(prompts), phase == "confirmation")
                self.assertEqual(
                    sdk.rows, [original] if kind != "extra" else [row(sdk.r, 300), original]
                )
                self.assertIsNone(hg_state.state_row(sdk.db, sdk.r))
            self.assertEqual(path.read_bytes(), saved_bytes)
            self.assertEqual((sdk.inbox / "health.json").read_bytes(), source)
            self.assertEqual(token.read_bytes(), b"unchanged token")
        finally:
            sdk.doCleanups()

    def test_native_full_replacement_keeps_canonical_matches_across_range_and_daily_ids(self):
        for phase in ("initial", "confirmation", "day"):
            for before, after in ((100, "100"), (100, "00100"), ("00100", 100)):
                with self.subTest(phase=phase, before=before, after=after):
                    self.assert_native_review_id_change(
                        "bulk", phase, before_id=before, after_id=after
                    )

    def test_native_single_review_accepts_only_equivalent_record_ids(self):
        for kind in ("repair", "extra"):
            for phase in ("initial", "confirmation"):
                with self.subTest(kind=kind, phase=phase):
                    self.assert_native_review_id_change(kind, phase)
        # Canonical selection must not pick one of the same-ID records on different days.
        for phase in ("initial", "confirmation"):
            with self.subTest(kind="extra", phase=phase, change="ambiguous"):
                self.assert_native_review_id_change("extra", phase, change="ambiguous")

    def test_native_review_still_rejects_changed_remote_fields(self):
        for kind in ("bulk", "repair", "extra"):
            for change in ("weight", "fat", "bmi", "date", "new_id", "metadata"):
                with self.subTest(kind=kind, change=change):
                    self.assert_native_review_id_change(kind, "confirmation", change=change)
        for old, new in ((100.0, "00100"), (True, "001")):
            with self.subTest(unknown_id=old):
                self.assert_native_review_id_change(
                    "repair", "initial", before_id=old, after_id=new, change="unknown_alias"
                )
        for old, new in ((100, 100.0), (1, True)):
            with self.subTest(unknown_id=new, phase="confirmation"):
                self.assert_native_review_id_change(
                    "repair", "confirmation", before_id=old, after_id=new, change="unknown_alias"
                )

    def test_native_timestamp_collision_preserves_uncertain_replacement_evidence(self):
        import test_sdk_transport as transport

        if not transport.PINNED_SDK:
            self.skipTest("Requires the installed, pinned Garmin SDK")
        sdk = transport.SDKTransportTests("runTest")
        try:
            sdk.setUp()
            sdk.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
            records = [
                hg_domain.Measurement(AT, 70, None, "PICOOC"),
                hg_domain.Measurement(AT + timedelta(days=2), 74, None, "OTHER"),
            ]
            self.write_export.__func__(sdk, records)
            sdk.rows = [
                row(hg_domain.Measurement(r.at, r.weight + 1, 23.5 + n, r.source, 22 + n), 100 + n)
                for n, r in enumerate(records)
            ]
            plan = hg_reconciliation.save_comparison(
                hg_reconciliation.comparison_plan(
                    sdk.db,
                    records,
                    {"dateWeightList": copy.deepcopy(sdk.rows)},
                    records[0].at.date(),
                    records[1].at.date(),
                ),
                sdk.cfg,
            )

            def server(method, path, pool):
                if method == "DELETE":
                    pk = int(path.rsplit("/", 1)[-1])
                    sdk.rows = [item for item in sdk.rows if item["samplePk"] != pk]
                    if pk == 100:
                        raise sdk.urllib3.exceptions.ReadTimeoutError(
                            pool, path, "synthetic accepted DELETE"
                        )
                    return 200, {"ok": True}
                if method == "POST":
                    values = sdk.fit_values[-1][1]
                    at = transport.fit_weight_timestamp(sdk.fit_files[-1]).astimezone(AT.tzinfo)
                    sdk.rows.append(
                        row(
                            hg_domain.Measurement(
                                at, values["weight"], values["percent_fat"], "OTHER", values["bmi"]
                            ),
                            200,
                        )
                    )
                    return 200, {"detailedImportResult": {"successes": [{}], "failures": []}}
                self.assertEqual(method, "GET")
                return 200, sdk.read_payload(path)

            sdk.handler = server
            token = sdk.state / "synthetic-token"
            token.write_bytes(b"unchanged token")
            source = (sdk.inbox / "health.json").read_bytes()
            with (
                patch.object(hg_runtime, "now", return_value=AT + timedelta(days=10)),
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):

                def invoke(record):
                    with patch("builtins.input", return_value="REPLACE " + record.key):
                        hg_operations.reviewed_operation(
                            argparse.Namespace(
                                plan=str(plan),
                                record_id=record.key,
                                remote_id=None,
                                complete_export=True,
                            ),
                            sdk.cfg,
                        )

                with self.assertRaises(hg_domain.SafeError):
                    invoke(records[0])
                journal = next((sdk.state / "operations").glob("*.json"))
                evidence = hg_state.read_operation(journal)
                self.assertEqual(evidence["phase"], "uncertain")
                self.assertEqual(
                    (evidence["replacement"]["fat"], evidence["replacement"]["bmi"]), (23.5, 22)
                )
                saved_journal = journal.read_bytes()
                backup = Path(evidence["backup"])
                saved_backup = {
                    p.relative_to(backup): p.read_bytes() for p in backup.rglob("*") if p.is_file()
                }
                saved_rows = [
                    dict(hg_state.state_row(sdk.db, r)) if hg_state.state_row(sdk.db, r) else None
                    for r in records
                ]
                with self.assertRaisesRegex(hg_domain.SafeError, "已存在"):
                    invoke(records[1])
            self.assertEqual(sum(r["method"] == "DELETE" for r in sdk.sent), 1)
            self.assertEqual(sum(r["method"] == "POST" for r in sdk.sent), 0)
            self.assertEqual(journal.read_bytes(), saved_journal)
            self.assertEqual(
                {p.relative_to(backup): p.read_bytes() for p in backup.rglob("*") if p.is_file()},
                saved_backup,
            )
            self.assertEqual(
                [
                    dict(hg_state.state_row(sdk.db, r)) if hg_state.state_row(sdk.db, r) else None
                    for r in records
                ],
                saved_rows,
            )
            self.assertEqual((sdk.inbox / "health.json").read_bytes(), source)
            self.assertEqual(token.read_bytes(), b"unchanged token")
        finally:
            sdk.doCleanups()

    def test_single_and_full_replacement_reject_occupied_evidence_paths_before_delete(self):
        from health_garmin_sync import history as bulk

        for full in (False, True):
            for occupied in ("backup", "journal"):
                with (
                    self.subTest(full=full, occupied=occupied),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    state = Path(temp) / "state"
                    hg_file_io.private_dir(state)
                    with (
                        patch.object(hg_runtime, "STATE", state),
                        contextlib.closing(hg_state.database()) as db,
                    ):
                        api = API([dict(row(self.r), weight=71000)])
                        plan = hg_reconciliation.save_comparison(
                            hg_reconciliation.comparison_plan(
                                db, [self.r], {"dateWeightList": api.rows}, AT.date(), AT.date()
                            ),
                            self.cfg,
                        )
                        fixed_time = AT + timedelta(days=10)
                        stamp = fixed_time.strftime("%Y%m%dT%H%M%S%fZ")
                        name = ("full-" if full else "") + stamp
                        backup = state / "backups" / name
                        journal = state / "operations" / (name + ".json")
                        if occupied == "backup":
                            hg_file_io.private_dir(backup)
                            old_path = backup / "remote.json"
                            old_path.write_bytes(b"previous recovery evidence")
                        else:
                            old_path = journal
                            hg_file_io.write_json(
                                journal, {"key": "previous-operation", "phase": "verified"}
                            )
                        old_bytes = old_path.read_bytes()
                        source = (self.inbox / "health.json").read_bytes()
                        with (
                            patch.object(hg_runtime, "now", return_value=fixed_time),
                            patch.object(hg_garmin, "api_client", return_value=api),
                            patch(
                                "builtins.input",
                                return_value="REPLACE ALL" if full else "REPLACE " + self.r.key,
                            ),
                            contextlib.redirect_stdout(io.StringIO()),
                        ):
                            with self.assertRaisesRegex(hg_domain.SafeError, "已存在"):
                                args = argparse.Namespace(
                                    plan=str(plan),
                                    complete_export=True,
                                    record_id=self.r.key,
                                    remote_id=None,
                                )
                                if full:
                                    bulk.execute(args, self.cfg, api)
                                else:
                                    hg_operations.reviewed_operation(args, self.cfg)
                        self.assertEqual(old_path.read_bytes(), old_bytes)
                        self.assertEqual(api.deletes, [])
                        self.assertEqual(api.posts, [])
                        self.assertIsNone(hg_state.state_row(db, self.r))
                        self.assertEqual((self.inbox / "health.json").read_bytes(), source)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        hg_file_io.private_dir(self.state)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.state_patch = patch.object(hg_runtime, "STATE", self.state)
        self.state_patch.start()
        self.r = hg_domain.Measurement(AT, 70, 23.5, "PICOOC")
        self.cfg = {
            "allowed_sources": ["PICOOC"],
            "since": "2020-01-01",
            "weight_bare_unit": "kg",
            "fat_bare_unit": "percent",
            "pair_window_seconds": 0,
            "inbox": str(self.inbox),
            "enabled": True,
            "settle_seconds": 0,
            "max_records_per_run": 10,
        }
        self.db = hg_state.database()
        self.write_export([self.r])

    def tearDown(self):
        self.db.close()
        self.state_patch.stop()
        self.temp.cleanup()

    def write_export(self, records):
        metrics = {"weight": [], "body_fat": [], "bmi": [], "lbm": []}
        for r in records:

            def sample(value):
                return {"at": r.at.isoformat(), "value": value, "source": r.source}

            metrics["weight"].append(sample(r.weight))
            if r.fat is not None:
                metrics["body_fat"].append(sample(f"{r.fat}%"))
            if r.bmi is not None:
                metrics["bmi"].append(sample(r.bmi))
            if r.lbm is not None:
                metrics["lbm"].append(sample(f"{r.lbm} kg"))
        (self.inbox / "health.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "exported_at": "2020-02-20T09:00:00+08:00",
                    "metrics": metrics,
                }
            )
        )

    def plan(self, rows, records=None, save=False):
        records = records if records is not None else [self.r]
        obj = {"dateWeightList": copy.deepcopy(rows)}
        plan = hg_reconciliation.comparison_plan(self.db, records, obj, AT.date(), AT.date())
        if save:
            return hg_reconciliation.save_comparison(plan, self.cfg)
        return plan

    def action(self, rows, r=None):
        return self.plan(rows, [r or self.r])["entries"][0]["action"]

    def sync(self, api):
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_sync.run_sync(self.cfg)

    def reviewed(self, api, path, delete_extra=False, complete=True):
        args = argparse.Namespace(
            plan=str(path), record_id=self.r.key, remote_id="100", complete_export=complete
        )
        confirm = "DELETE 100" if delete_extra else "REPLACE " + self.r.key
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            patch("builtins.input", return_value=confirm),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.reviewed_operation(args, self.cfg, delete_extra)

    def detailed_api(self, backend):
        raw = Mock(
            spec_set=[
                "get_weigh_ins",
                "get_daily_weigh_ins",
                "add_body_composition",
                "add_weigh_in",
                "delete_weigh_in",
            ]
        )

        def summaries(start, end):
            days = {}
            for item in backend.get_body_composition(start, end)["dateWeightList"]:
                days.setdefault(item["calendarDate"], []).append(item)
            return {
                "dailyWeightSummaries": [
                    {
                        "summaryDate": day,
                        "numOfWeightEntries": len(items),
                        "allWeightMetrics": items,
                    }
                    for day, items in sorted(days.items())
                ]
            }

        raw.get_weigh_ins.side_effect = summaries
        raw.get_daily_weigh_ins.side_effect = lambda day: backend.get_body_composition(day, day)
        raw.add_body_composition.side_effect = backend.add_body_composition
        raw.add_weigh_in.side_effect = backend.add_weigh_in
        raw.delete_weigh_in.side_effect = backend.delete_weigh_in
        seal(raw)
        return hg_garmin.GarminMeasurementAPI(raw)

    def native_deletion_api(self, backend, *, fail_at=1):
        from importlib.metadata import PackageNotFoundError

        try:
            if hg_std_version("garminconnect") != hg_runtime.REQUIRED_GARMIN_VERSION:
                self.skipTest("Requires the pinned Garmin SDK")
        except PackageNotFoundError:
            self.skipTest("Requires the pinned Garmin SDK")
        import socket

        import garminconnect
        import requests

        def forbidden(*args, **kwargs):
            raise AssertionError("No real network/home/login/service")

        for target, name in (
            (Path, "home"),
            (garminconnect.Garmin, "login"),
            (requests.Session, "request"),
            (requests.Session, "send"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
        ):
            self.enterContext(patch.object(target, name, forbidden))
        self.enterContext(patch("subprocess.run", forbidden))
        try:
            import curl_cffi
        except ImportError:
            pass
        else:
            self.enterContext(patch.object(curl_cffi.Curl, "perform", forbidden))
        sdk = garminconnect.Garmin(is_cn=True)
        self.addCleanup(sdk.client.cs.close)
        self.addCleanup(sdk.client._api_session.close)
        sdk.client.cs.trust_env = sdk.client._api_session.trust_env = False

        def request(method, domain, url, **kwargs):
            self.assertEqual((method, domain), ("DELETE", "connectapi"))
            before, pk = url.rsplit("/byversion/", 1)
            day = before.rsplit("/", 1)[1]
            backend.deletes.append((pk, day))
            if len(backend.deletes) == fail_at:
                raise TimeoutError("synthetic unknown deletion")
            backend.rows = [
                item
                for item in backend.rows
                if int(item["samplePk"]) != int(pk) or item["calendarDate"] != day
            ]
            return {"ok": True}

        sdk.client.request = Mock(side_effect=request)
        api = self.detailed_api(backend)
        api.api.delete_weigh_in.side_effect = sdk.delete_weigh_in
        return api

    def deletion_plan(self, api, records=None):
        records = [self.r] if records is None else records
        start, end = min(r.at.date() for r in records), max(r.at.date() for r in records)
        obj = hg_remote.fetch_remote_snapshot(api, start, end)
        return hg_reconciliation.save_comparison(
            hg_reconciliation.comparison_plan(self.db, records, obj, start, end), self.cfg
        )

    def incomplete_deletion(self, api, *, full=False, remote_id="100"):
        plan = self.deletion_plan(api)
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
            patch("builtins.input", return_value="REPLACE ALL" if full else "DELETE " + remote_id),
        ):
            with self.assertRaises(hg_domain.SafeError):
                if full:
                    from health_garmin_sync import history as replace_history

                    replace_history.execute(
                        argparse.Namespace(plan=str(plan), complete_export=True), self.cfg, api
                    )
                else:
                    hg_operations.reviewed_operation(
                        argparse.Namespace(
                            plan=str(plan), remote_id=remote_id, complete_export=True
                        ),
                        self.cfg,
                        True,
                    )
        path = next((hg_runtime.STATE / "operations").glob("*.json"))
        self.assertEqual(hg_state.read_operation(path)["phase"], "uncertain")
        return path

    def paused_reviewed_neighbor(self, *, alternate=False, legacy=False):
        other = hg_domain.Measurement(AT + timedelta(seconds=30), 71, 24, "PICOOC")
        source_inbox = self.inbox
        if alternate:
            self.write_export([self.r])
            source_inbox = self.root / ("alternate-" + hg_runtime.STATE.name)
            source_inbox.mkdir()
        original_inbox = self.inbox
        try:
            self.inbox = source_inbox
            self.write_export([self.r, other])
        finally:
            self.inbox = original_inbox
        backend = API([dict(row(self.r), bodyFat=None), row(other, 101)])
        backend.delete_timeout = True
        api = self.detailed_api(backend)
        plan = hg_reconciliation.save_comparison(
            self.plan(backend.rows, [self.r, other]), dict(self.cfg, inbox=str(source_inbox))
        )
        native_write = hg_file_io.write_json

        def save_old_plan(path, data, **kwargs):
            if legacy and path.name == "plan.json" and path.parent.parent.name == "backups":
                data = {key: value for key, value in data.items() if key != "config"}
            native_write(path, data, **kwargs)

        with patch.object(hg_file_io, "write_json", side_effect=save_old_plan):
            with self.assertRaises(hg_domain.SafeError):
                self.reviewed(api, plan)
        journal = next((hg_runtime.STATE / "operations").glob("*.json"))
        self.assertEqual(hg_state.read_operation(journal)["phase"], "uncertain")
        self.assertEqual(len(backend.deletes), 1)
        self.assertFalse(backend.posts)
        backend.delete_timeout = False
        return api, backend, journal, other, source_inbox / "health.json"

    def test_classifies_missing_match_repair_replace(self):
        self.assertEqual(self.action([]), "missing")
        self.assertEqual(self.action([row(self.r)]), "matched")
        missing = row(self.r)
        missing["bodyFat"] = None
        self.assertEqual(self.action([missing]), "repair")
        wrong = row(self.r)
        wrong["weight"] = 71000
        self.assertEqual(self.action([wrong]), "replace")

    def test_boolean_remote_numbers_are_conflicts_for_valid_exported_measurement(self):
        self.r = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22)
        self.write_export([self.r])
        _, records, waiting, _ = hg_sources.load_measurements(self.cfg)
        self.assertFalse(waiting)
        self.assertEqual(records, [self.r])
        for field in ("weight", "bodyFat", "bmi"):
            for value in (True, False):
                with self.subTest(field=field, value=value):
                    remote = dict(row(self.r), **{field: value})
                    entry = self.plan([remote], records)["entries"][0]
                    self.assertEqual(entry["action"], "conflict")
                    self.assertEqual(entry["reason"], "远端数值无法可靠解析")

    def test_legacy_replace_plan_with_boolean_remote_cannot_authorize_mutation(self):
        self.r = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22)
        self.write_export([self.r])
        source = (self.inbox / "health.json").read_bytes()
        api = API([dict(row(self.r), bodyFat=True)])
        legacy = self.plan(api.rows)
        legacy["entries"][0].update(action="replace", reason="同次测量数值不同")
        legacy["counts"] = {"replace": 1}
        path = hg_reconciliation.save_comparison(legacy, self.cfg)
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertFalse((self.state / "operations").exists())
        self.assertIsNone(hg_state.state_row(self.db, self.r))
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)

    def test_boolean_optional_values_cannot_verify_uncertain_but_numeric_strings_can(self):
        self.r = hg_domain.Measurement(AT, 70, 1, "PICOOC", 1)
        self.write_export([self.r])
        _, records, waiting, _ = hg_sources.load_measurements(self.cfg)
        self.assertFalse(waiting)
        self.assertEqual(records, [self.r])
        for status in ("uncertain", "inflight"):
            with self.subTest(status=status):
                hg_state.set_status(self.db, self.r, status, "awaiting visibility")
                api = API([dict(row(self.r), bodyFat=True, bmi=True)])
                self.assertEqual(
                    hg_processing.process_record(api, self.db, records[0]), "uncertain"
                )
                self.assertEqual(hg_state.get_status(self.db, self.r), "uncertain")
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)
        api = API([dict(row(self.r), weight="70000", bodyFat="1", bmi="1")])
        self.assertEqual(hg_processing.process_record(api, self.db, records[0]), "verified")
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_optional_remote_values_not_erased_when_apple_has_none(self):
        self.assertEqual(
            self.action([row(self.r)], hg_domain.Measurement(AT, 70, None, "PICOOC")), "matched"
        )
        self.assertEqual(
            self.action(
                [dict(row(self.r), bodyFat=True, bmi=True)],
                hg_domain.Measurement(AT, 70, None, "PICOOC"),
            ),
            "matched",
        )

    def test_missing_bmi_becomes_repair(self):
        self.assertEqual(
            self.action([row(self.r)], hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22)), "repair"
        )

    def test_lbm_is_local_evidence_and_not_garmin_muscle_mass(self):
        self.r = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22, 53.55)
        self.write_export([self.r])
        api = API()
        self.sync(api)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(set(api.posts[0]), {"timestamp", "weight", "percent_fat", "bmi"})
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        payload = json.loads(hg_state.state_row(self.db, self.r)["payload"])
        self.assertEqual(payload["lbm"], 53.55)
        self.assertEqual(self.action(api.rows), "matched")

    def test_bmi_repair_preserves_local_lbm_in_journal_and_database(self):
        original = self.r
        self.r = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22, 53.55)
        self.write_export([self.r])
        api = API([row(original)])
        path = self.plan(api.rows, save=True)
        self.reviewed(api, path)
        journal_path = next((self.state / "operations").glob("*.json"))
        journal = json.loads(journal_path.read_text())
        self.assertEqual(journal["replacement"]["lbm"], 53.55)
        self.assertEqual(json.loads(hg_state.state_row(self.db, self.r)["payload"])["lbm"], 53.55)
        self.assertNotIn("muscle_mass", api.posts[0])
        self.assertEqual(journal["phase"], "verified")

    def test_reviewed_optional_preservation_retains_only_same_core_local_lbm(self):
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        save = hg_state.set_status
        for weight, explicit, expected in ((70, None, 53.55), (70, 54, 54), (71, None, None)):
            with self.subTest(weight=weight, explicit_lbm=explicit):
                self.db.execute("DELETE FROM uploads")
                self.db.commit()
                for journal in (self.state / "operations").glob("*.json"):
                    journal.unlink()
                hg_state.set_status(
                    self.db, hg_domain.Measurement(AT, 70, None, "PICOOC", lbm=53.55), "verified"
                )
                self.r = hg_domain.Measurement(AT, weight, None, "PICOOC", 22, explicit)
                self.write_export([self.r])
                source = (self.inbox / "health.json").read_bytes()
                _, records, waiting, _ = hg_sources.load_measurements(self.cfg)
                self.assertEqual(records, [self.r])
                self.assertFalse(waiting)
                backend = API([row(hg_domain.Measurement(AT, 70, 23.5, "PICOOC"))])
                api = self.detailed_api(backend)
                plan = hg_reconciliation.comparison_plan(
                    self.db,
                    records,
                    hg_remote.fetch_remote_snapshot(api, AT.date(), AT.date()),
                    AT.date(),
                    AT.date(),
                )
                path = hg_reconciliation.save_comparison(plan, self.cfg)
                durable = []

                def inspect_commit(db, record, status, detail="", **kwargs):
                    save(db, record, status, detail, **kwargs)
                    with contextlib.closing(
                        hg_std_sqlite3.connect(self.state / "sync.sqlite3")
                    ) as reader:
                        core, payload, stored_status = reader.execute(
                            "SELECT core,payload,status FROM uploads WHERE record_id=?",
                            (self.r.key,),
                        ).fetchone()
                    self.assertEqual((core, stored_status), (self.r.core, status))
                    self.assertEqual(json.loads(payload)["fat"], None)
                    self.assertEqual(json.loads(payload).get("lbm"), expected)
                    durable.append(status)

                with patch.object(hg_state, "set_status", side_effect=inspect_commit):
                    self.reviewed(api, path)
                self.assertEqual(
                    durable, ["repair_uncertain", "pending", "inflight", "verified", "verified"]
                )
                journal_path = next((self.state / "operations").glob("*.json"))
                journal = json.loads(journal_path.read_text())
                backup = Path(journal["backup"]) / "plan.json"
                backup_bytes = backup.read_bytes()
                persisted = hg_state.state_row(self.db, self.r)
                payload = json.loads(persisted["payload"])
                self.assertEqual(
                    (persisted["core"], persisted["status"]), (self.r.core, "verified")
                )
                self.assertEqual(payload.get("lbm"), expected)
                self.assertEqual(journal["replacement"].get("lbm"), expected)
                self.assertEqual(journal["measurement"], self.r.payload())
                self.assertEqual(payload["fat"], None)
                before = (len(backend.posts), len(backend.deletes))
                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertTrue(
                        hg_operations.verify_operation(
                            argparse.Namespace(operation=str(journal_path))
                        )
                    )
                self.sync(api)
                persisted = hg_state.state_row(self.db, self.r)
                self.assertEqual(
                    (persisted["core"], persisted["status"]), (self.r.core, "verified")
                )
                self.assertEqual(json.loads(persisted["payload"]).get("lbm"), expected)
                self.assertEqual((len(backend.posts), len(backend.deletes)), before)
                self.assertEqual(backend.deletes, [("100", AT.date().isoformat())])
                self.assertEqual(len(backend.posts), 1)
                self.assertEqual(
                    set(backend.posts[0]), {"timestamp", "weight", "percent_fat", "bmi"}
                )
                self.assertEqual(
                    (backend.posts[0]["percent_fat"], backend.posts[0]["bmi"]), (23.5, 22)
                )
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(backup.read_bytes(), backup_bytes)
                self.assertEqual(token.read_bytes(), b"unchanged local token")
        other = hg_domain.Measurement(AT + timedelta(hours=1), 70, None, "PICOOC")
        untouched = dict(hg_state.state_row(self.db, self.r))
        reads = len(backend.reads)
        with self.assertRaises(hg_domain.SafeError):
            hg_processing.process_record(api, self.db, self.r, local_record=other)
        self.assertEqual(dict(hg_state.state_row(self.db, self.r)), untouched)
        self.assertEqual(len(backend.reads), reads)
        self.assertEqual((len(backend.posts), len(backend.deletes)), before)

    def test_reviewed_retained_lbm_survives_resume_and_delayed_verification(self):
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        process, save = hg_processing.process_record, hg_state.set_status
        for mode, explicit in (
            (mode, lbm)
            for mode in ("timeout_resume", "delayed_verify", "auto_matched")
            for lbm in (None, 53.55)
        ):
            with self.subTest(mode=mode, source_lbm=explicit):
                self.db.execute("DELETE FROM uploads")
                self.db.commit()
                for journal in (self.state / "operations").glob("*.json"):
                    journal.unlink()
                (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                hg_state.set_status(
                    self.db, hg_domain.Measurement(AT, 70, None, "PICOOC", lbm=53.55), "verified"
                )
                self.r = hg_domain.Measurement(AT, 70, None, "PICOOC", 22, explicit)
                self.write_export([self.r])
                source = (self.inbox / "health.json").read_bytes()
                backend = API([row(hg_domain.Measurement(AT, 70, 23.5, "PICOOC"))])
                backend.visible = False
                post = backend.add_body_composition

                def first_post(**kwargs):
                    post(**kwargs)
                    if mode == "timeout_resume":
                        raise TimeoutError("synthetic uncertain submission")

                backend.add_body_composition = first_post
                api = self.detailed_api(backend)
                plan = self.deletion_plan(api)
                durable = []

                def inspect_commit(db, record, status, detail="", **kwargs):
                    save(db, record, status, detail, **kwargs)
                    with contextlib.closing(
                        hg_std_sqlite3.connect(self.state / "sync.sqlite3")
                    ) as reader:
                        core, payload, stored_status = reader.execute(
                            "SELECT core,payload,status FROM uploads WHERE record_id=?",
                            (self.r.key,),
                        ).fetchone()
                    self.assertEqual((core, stored_status), (self.r.core, status))
                    self.assertEqual(json.loads(payload)["fat"], None)
                    self.assertEqual(json.loads(payload)["lbm"], 53.55)
                    durable.append(status)

                with (
                    patch.object(
                        hg_processing,
                        "process_record",
                        side_effect=lambda *a, **kw: process(*a, **kw, sleep=lambda _: None),
                    ),
                    patch.object(hg_state, "set_status", side_effect=inspect_commit),
                ):
                    with self.assertRaises(hg_domain.SafeError):
                        self.reviewed(api, plan)
                self.assertEqual(durable, ["repair_uncertain", "pending", "inflight", "uncertain"])
                journal_path = next((self.state / "operations").glob("*.json"))
                journal = json.loads(journal_path.read_text())
                original_measurement = journal["measurement"]
                self.assertEqual(journal["phase"], "uncertain")
                self.assertEqual(original_measurement, self.r.payload())
                self.assertEqual(journal["replacement"]["lbm"], 53.55)
                persisted = hg_state.state_row(self.db, self.r)
                self.assertEqual(
                    (persisted["core"], persisted["status"]), (self.r.core, "uncertain")
                )
                self.assertEqual(json.loads(persisted["payload"])["lbm"], 53.55)
                self.assertEqual(len(backend.posts), 1)
                backup = Path(journal["backup"])
                evidence = {path.name: path.read_bytes() for path in backup.glob("*.json")}
                args = argparse.Namespace(operation=str(journal_path))
                if mode == "timeout_resume":
                    backend.add_body_composition = post
                    backend.visible = True
                    api = self.detailed_api(backend)
                    with (
                        patch.object(hg_garmin, "api_client", return_value=api),
                        patch("builtins.input", return_value="RESUME " + self.r.key),
                        contextlib.redirect_stdout(io.StringIO()),
                        patch.object(hg_state, "set_status", side_effect=inspect_commit),
                    ):
                        hg_operations.resume_operation(args)
                    self.assertEqual(durable[-3:], ["pending", "inflight", "verified"])
                else:
                    backend.rows = [row(hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22), 201)]
                    if mode == "auto_matched":
                        self.sync(api)
                        persisted = hg_state.state_row(self.db, self.r)
                        self.assertEqual(
                            (persisted["core"], persisted["status"]), (self.r.core, "verified")
                        )
                        self.assertEqual(json.loads(persisted["payload"])["lbm"], 53.55)
                        self.assertEqual(json.loads(journal_path.read_text())["phase"], "uncertain")
                before = (len(backend.posts), len(backend.deletes))
                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertTrue(hg_operations.verify_operation(args))
                self.sync(api)
                self.assertEqual(before, (2 if mode == "timeout_resume" else 1, 1))
                self.assertEqual(json.loads(journal_path.read_text())["phase"], "verified")
                persisted = hg_state.state_row(self.db, self.r)
                self.assertEqual(
                    (persisted["core"], persisted["status"]), (self.r.core, "verified")
                )
                self.assertEqual(json.loads(persisted["payload"])["lbm"], 53.55)
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                # A later explicit export wins over both kinds of historical journal measurement.
                self.write_export([hg_domain.Measurement(AT, 70, None, "PICOOC", 22, 54)])
                source = (self.inbox / "health.json").read_bytes()
                self.sync(api)
                for _ in range(2):
                    with (
                        patch.object(hg_garmin, "api_client", return_value=api),
                        patch(
                            "builtins.input",
                            side_effect=AssertionError("Matched resume must be read-only"),
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        self.assertTrue(hg_operations.verify_operation(args))
                        hg_operations.resume_operation(args)
                    self.sync(api)
                    persisted = hg_state.state_row(self.db, self.r)
                    self.assertEqual(
                        (persisted["core"], persisted["status"]), (self.r.core, "verified")
                    )
                    self.assertEqual(json.loads(persisted["payload"])["lbm"], 54)
                self.assertEqual((len(backend.posts), len(backend.deletes)), before)
                self.assertEqual(
                    json.loads(journal_path.read_text())["measurement"], original_measurement
                )
                self.assertEqual(
                    {path.name: path.read_bytes() for path in backup.glob("*.json")}, evidence
                )
                for submitted in backend.posts:
                    self.assertEqual(set(submitted), {"timestamp", "weight", "percent_fat", "bmi"})
                    self.assertEqual((submitted["percent_fat"], submitted["bmi"]), (23.5, 22))
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(token.read_bytes(), b"unchanged local token")

    def completed_optional_repair(self):
        self.r = hg_domain.Measurement(AT, 70, None, "PICOOC", lbm=53.55)
        self.write_export([self.r])
        records = hg_sources.load_measurements(self.cfg)[1]
        self.assertEqual(records, [self.r])
        backend = API([row(hg_domain.Measurement(AT, 71, 23.5, "PICOOC"))])
        api = self.native_deletion_api(backend, fail_at=None)
        plan = self.deletion_plan(api)
        self.reviewed(api, plan)
        journal = next((hg_runtime.STATE / "operations").glob("*.json"))
        self.assertEqual(hg_state.read_operation(journal)["phase"], "verified")
        self.assertEqual((len(backend.posts), len(backend.deletes)), (1, 1))
        return api, backend, journal

    def test_unknown_automatic_upload_survives_source_and_value_revisions(self):
        self.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        for error, offset, changed, allowed, resolved in (
            (TimeoutError, 0, False, False, None),
            (KeyboardInterrupt, 1, True, False, None),
            (TimeoutError, 2.000001, True, True, None),
            (TimeoutError, 0, True, True, "pending"),
            (TimeoutError, 0, False, True, "verified_manual"),
            (TimeoutError, 0, False, True, "verified"),
        ):
            with self.subTest(error=error.__name__, offset=offset, resolved=resolved):
                self.db.execute("DELETE FROM uploads")
                self.db.commit()
                (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                self.write_export([self.r])
                backend = API()
                backend.visible = False
                api = self.native_deletion_api(backend, fail_at=None)
                native_post = backend.add_body_composition

                def accepted_timeout(**kw):
                    native_post(**kw)
                    raise error("synthetic accepted upload")

                api.api.add_body_composition.side_effect = accepted_timeout
                if error is KeyboardInterrupt:
                    with self.assertRaises(KeyboardInterrupt):
                        self.sync(api)
                else:
                    self.sync(api)
                self.assertEqual(
                    hg_state.get_status(self.db, self.r),
                    "inflight" if error is KeyboardInterrupt else "uncertain",
                )
                if resolved == "verified":
                    backend.rows = [row(self.r, 201)]
                    self.sync(api)
                elif resolved:
                    with (
                        patch(
                            "builtins.input",
                            return_value="ABSENT" if resolved == "pending" else "PRESENT",
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_operations.resolve_record(
                            argparse.Namespace(
                                record_id=self.r.key,
                                present=resolved == "verified_manual",
                                absent=resolved == "pending",
                                ignore=False,
                            )
                        )
                    if resolved == "verified_manual":
                        backend.rows = [row(self.r, 201)]
                old = dict(hg_state.state_row(self.db, self.r))
                if resolved:
                    self.assertEqual(old["status"], resolved)
                current = hg_domain.Measurement(
                    (AT + timedelta(seconds=offset)).astimezone(hg_std_UTC),
                    72 if changed else 70,
                    24 if changed else 23.5,
                    "OTHER",
                    22 if changed else None,
                )
                self.write_export([current])
                source = (self.inbox / "health.json").read_bytes()
                self.assertEqual(hg_sources.load_measurements(self.cfg)[1], [current])
                api.api.add_body_composition.side_effect = native_post
                backend.visible = True
                self.sync(api)
                summary = json.loads((self.state / "last_run.json").read_text())
                readonly = resolved in ("verified", "verified_manual")
                self.assertEqual(len(backend.posts), 2 if allowed and not readonly else 1)
                self.assertFalse(backend.deletes)
                self.assertEqual(dict(hg_state.state_row(self.db, self.r)), old)
                if allowed:
                    self.assertEqual(hg_state.get_status(self.db, current), "verified")
                else:
                    self.assertIsNone(hg_state.state_row(self.db, current))
                    self.assertEqual(summary["results"][current.key], "uncertain")
                    plan = json.loads((self.state / "plans" / "sync-latest.json").read_text())
                    self.assertIn(self.r.key, plan["entries"][0]["reason"])
                    # A delayed matching read may complete the current alias without another POST.
                    backend.rows = [row(current, 201)]
                    self.sync(api)
                    self.assertEqual(hg_state.get_status(self.db, current), "verified")
                    self.assertEqual(len(backend.posts), 1)
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(token.read_bytes(), b"unchanged local token")

    def test_reviewed_weight_only_identity_uses_actual_retained_fit_timestamp_for_unknown_guard(
        self,
    ):
        import test_sdk_transport as transport

        if not transport.PINNED_SDK:
            self.skipTest("Requires the installed, pinned Garmin SDK")
        sdk = transport.SDKTransportTests("runTest")
        try:
            sdk.setUp()
            sdk.enterContext(patch("subprocess.run", side_effect=transport.forbidden))
            sdk.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
            old = hg_domain.Measurement(
                AT + timedelta(seconds=1.500001), 70, None, "PICOOC", lbm=53.55
            )
            candidate = hg_domain.Measurement(AT, 72, None, "OTHER", lbm=54)
            original = row(hg_domain.Measurement(old.at, 71, 23.5, "PICOOC", 22))
            sdk.rows = [original]
            # Establish an explicit permission for a different source before the older request becomes unknown.
            self.write_export.__func__(sdk, [candidate])
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_sync.run_sync(sdk.cfg)
            self.assertEqual(hg_state.get_status(sdk.db, candidate), "conflict")
            with (
                patch("builtins.input", return_value="ABSENT"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_operations.resolve_record(
                    argparse.Namespace(
                        record_id=candidate.key, present=False, absent=True, ignore=False
                    )
                )
            candidate_before = dict(hg_state.state_row(sdk.db, candidate))
            self.assertEqual(candidate_before["status"], "pending")
            self.write_export.__func__(sdk, [old])
            self.assertEqual(hg_sources.load_measurements(sdk.cfg)[1], [old])
            saved_source = (sdk.inbox / "health.json").read_bytes()
            plan = hg_reconciliation.save_comparison(
                hg_reconciliation.comparison_plan(
                    sdk.db, [old], {"dateWeightList": [original]}, old.at.date(), old.at.date()
                ),
                sdk.cfg,
            )
            wire_times = []

            def server(method, path, pool):
                if method == "DELETE":
                    sdk.rows = []
                    return 200, {"ok": True}
                if method == "POST":
                    self.assertEqual(path, "/upload-service/upload")
                    wire_times.append(transport.fit_weight_timestamp(sdk.fit_files[-1]))
                    with contextlib.closing(hg_state.database()) as independent:
                        self.assertEqual(hg_state.get_status(independent, old), "inflight")
                        wire_at = independent.execute(
                            "SELECT wire_at FROM request_times WHERE record_id=?", (old.key,)
                        ).fetchone()[0]
                        self.assertEqual(hg_domain.aware(wire_at), wire_times[-1])
                    raise sdk.urllib3.exceptions.ReadTimeoutError(
                        pool, path, "synthetic accepted hidden FIT upload"
                    )
                self.assertEqual(method, "GET")
                return 200, sdk.read_payload(path)

            sdk.handler = server
            args = argparse.Namespace(
                plan=str(plan), record_id=old.key, remote_id="100", complete_export=True
            )
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                patch("builtins.input", return_value="REPLACE " + old.key),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(hg_domain.SafeError):
                    hg_operations.reviewed_operation(args, sdk.cfg)
            self.assertEqual(wire_times, [AT.astimezone(hg_std_UTC) + timedelta(seconds=1)])
            self.assertGreater(abs((old.at - candidate.at).total_seconds()), 1)
            self.assertEqual(
                (sdk.fit_values[-1][1]["percent_fat"], sdk.fit_values[-1][1]["bmi"]), (23.5, 22)
            )
            self.assertIsNone(sdk.fit_values[-1][1]["muscle_mass"])
            journal_path = next((sdk.state / "operations").glob("*.json"))
            journal = hg_state.read_operation(journal_path)
            self.assertEqual(journal["measurement"], old.payload())
            self.assertEqual(
                (journal["replacement"]["fat"], journal["replacement"]["bmi"]), (23.5, 22)
            )
            self.assertEqual(journal["phase"], "uncertain")
            old_before = dict(hg_state.state_row(sdk.db, old))
            self.assertEqual(old_before["status"], "uncertain")
            self.assertEqual(json.loads(old_before["payload"]), old.payload())
            self.assertEqual((sdk.inbox / "health.json").read_bytes(), saved_source)
            # Resolve the local marker only: the journal's actual FIT mode must still protect the pending alias.
            with (
                patch("builtins.input", return_value="PRESENT"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_operations.resolve_record(
                    argparse.Namespace(record_id=old.key, present=True, absent=False, ignore=False)
                )
            old_before = dict(hg_state.state_row(sdk.db, old))
            self.assertEqual(old_before["status"], "verified_manual")
            journal_bytes = journal_path.read_bytes()
            backup = Path(journal["backup"])
            evidence = {p.name: p.read_bytes() for p in backup.glob("*.json")}
            token = sdk.state / "synthetic-token"
            token.write_bytes(b"unchanged local token")
            self.write_export.__func__(sdk, [candidate])
            source = (sdk.inbox / "health.json").read_bytes()
            self.assertEqual(hg_sources.load_measurements(sdk.cfg)[1], [candidate])
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_sync.run_sync(sdk.cfg)
                self.assertEqual(
                    hg_processing.process_record(sdk.api, sdk.db, candidate), "uncertain"
                )
            latest = json.loads((sdk.state / "plans" / "sync-latest.json").read_text())
            self.assertEqual(latest["entries"][0]["action"], "uncertain")
            self.assertIn(journal_path.name, latest["entries"][0]["reason"])
            with patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(hg_domain.SafeError, journal_path.name):
                    hg_operations.resolve_record(
                        argparse.Namespace(
                            record_id=candidate.key, present=False, absent=True, ignore=False
                        )
                    )
            prompt.assert_not_called()
            self.assertEqual(dict(hg_state.state_row(sdk.db, candidate)), candidate_before)
            self.assertEqual(dict(hg_state.state_row(sdk.db, old)), old_before)
            self.assertEqual(journal_path.read_bytes(), journal_bytes)
            self.assertEqual({p.name: p.read_bytes() for p in backup.glob("*.json")}, evidence)
            self.assertEqual(sum(request["method"] == "POST" for request in sdk.sent), 1)
            self.assertEqual(sum(request["method"] == "DELETE" for request in sdk.sent), 1)
            self.assertEqual((sdk.inbox / "health.json").read_bytes(), source)
            self.assertEqual(token.read_bytes(), b"unchanged local token")
        finally:
            sdk.doCleanups()

    def test_new_unknown_request_keeps_latest_source_revision_and_blocks_old_completed_resume(self):
        import test_sdk_transport as transport

        if not transport.PINNED_SDK:
            self.skipTest("Requires the installed, pinned Garmin SDK")
        sdk = transport.SDKTransportTests("runTest")
        try:
            sdk.setUp()
            sdk.enterContext(patch("subprocess.run", side_effect=transport.forbidden))
            old = hg_domain.Measurement(
                AT + timedelta(seconds=1.500001), 70, 23.5, "PICOOC", 22, 53.55
            )
            self.write_export.__func__(sdk, [old])
            self.assertEqual(hg_sources.load_measurements(sdk.cfg)[1], [old])
            original = row(hg_domain.Measurement(old.at, 71, 23.5, "PICOOC", 22))
            sdk.rows = [original]

            def server(method, path, pool):
                if method == "DELETE":
                    sdk.rows = []
                    return 200, {"ok": True}
                if method == "POST":
                    self.assertEqual(path, "/upload-service/upload")
                    wire_at = transport.fit_weight_timestamp(sdk.fit_files[-1])
                    with contextlib.closing(hg_state.database()) as independent:
                        self.assertEqual(hg_state.get_status(independent, old), "inflight")
                        saved = independent.execute(
                            "SELECT wire_at FROM request_times WHERE record_id=?", (old.key,)
                        ).fetchone()[0]
                        self.assertEqual(hg_domain.aware(saved), wire_at)
                    if sum(request["method"] == "POST" for request in sdk.sent) == 1:
                        sdk.rows = [
                            row(
                                hg_domain.Measurement(
                                    wire_at, old.weight, old.fat, old.source, old.bmi
                                ),
                                201,
                            )
                        ]
                        return 200, {"detailedImportResult": {"successes": [{}], "failures": []}}
                    raise sdk.urllib3.exceptions.ReadTimeoutError(
                        pool, path, "synthetic accepted hidden FIT upload"
                    )
                self.assertEqual(method, "GET")
                return 200, sdk.read_payload(path)

            sdk.handler = server
            plan = hg_reconciliation.save_comparison(
                hg_reconciliation.comparison_plan(
                    sdk.db, [old], {"dateWeightList": [original]}, old.at.date(), old.at.date()
                ),
                sdk.cfg,
            )
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                patch("builtins.input", return_value="REPLACE " + old.key),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_operations.reviewed_operation(
                    argparse.Namespace(
                        plan=str(plan), record_id=old.key, remote_id="100", complete_export=True
                    ),
                    sdk.cfg,
                )
            journal = next((sdk.state / "operations").glob("*.json"))
            self.assertEqual(hg_state.read_operation(journal)["phase"], "verified")
            journal_bytes = journal.read_bytes()
            backup = Path(hg_state.read_operation(journal)["backup"])
            evidence = {p.name: p.read_bytes() for p in backup.glob("*.json")}
            sdk.rows = []
            with (
                patch.object(hg_runtime, "now", return_value=AT + timedelta(hours=2)),
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_sync.run_sync(sdk.cfg)
            self.assertEqual(hg_state.get_status(sdk.db, old), "remote_missing")
            with (
                patch("builtins.input", return_value="ABSENT"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_operations.resolve_record(
                    argparse.Namespace(record_id=old.key, present=False, absent=True, ignore=False)
                )
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_sync.run_sync(sdk.cfg)
            self.assertEqual(hg_state.get_status(sdk.db, old), "uncertain")
            latest = hg_domain.Measurement(old.at, 72, old.fat, old.source, old.bmi, 54)
            self.write_export.__func__(sdk, [latest])
            self.assertEqual(hg_sources.load_measurements(sdk.cfg)[1], [latest])
            source = (sdk.inbox / "health.json").read_bytes()
            token = sdk.state / "synthetic-token"
            token.write_bytes(b"unchanged local token")
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_sync.run_sync(sdk.cfg)
            current = dict(hg_state.state_row(sdk.db, latest))
            self.assertEqual(current["core"], latest.core)
            self.assertEqual(json.loads(current["payload"]), latest.payload())
            self.assertEqual(current["status"], "uncertain")
            wire = sdk.db.execute(
                "SELECT wire_at FROM request_times WHERE record_id=?", (old.key,)
            ).fetchone()[0]
            self.assertEqual(
                hg_domain.aware(wire), AT.astimezone(hg_std_UTC) + timedelta(seconds=1)
            )
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                patch("builtins.input") as prompt,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(hg_domain.SafeError, "本地测量已修订"):
                    hg_operations.resume_operation(argparse.Namespace(operation=str(journal)))
            prompt.assert_not_called()
            self.assertEqual(dict(hg_state.state_row(sdk.db, latest)), current)
            self.assertEqual(journal.read_bytes(), journal_bytes)
            # Late visibility can confirm the historical operation without clearing the newer unknown request.
            sdk.rows = [
                row(
                    hg_domain.Measurement(
                        hg_domain.aware(wire), old.weight, old.fat, old.source, old.bmi
                    ),
                    202,
                )
            ]
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                patch("builtins.input") as prompt,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertTrue(
                    hg_operations.verify_operation(argparse.Namespace(operation=str(journal)))
                )
                hg_operations.resume_operation(argparse.Namespace(operation=str(journal)))
            prompt.assert_not_called()
            self.assertEqual(dict(hg_state.state_row(sdk.db, latest)), current)
            self.assertEqual(
                sdk.db.execute(
                    "SELECT wire_at FROM request_times WHERE record_id=?", (old.key,)
                ).fetchone()[0],
                wire,
            )
            for field in ("measurement", "replacement", "original", "backup"):
                self.assertEqual(
                    hg_state.read_operation(journal)[field], json.loads(journal_bytes)[field]
                )
            self.assertEqual({p.name: p.read_bytes() for p in backup.glob("*.json")}, evidence)
            self.assertEqual(sum(request["method"] == "POST" for request in sdk.sent), 2)
            self.assertEqual(sum(request["method"] == "DELETE" for request in sdk.sent), 1)
            self.assertEqual((sdk.inbox / "health.json").read_bytes(), source)
            self.assertEqual(token.read_bytes(), b"unchanged local token")
        finally:
            sdk.doCleanups()

    def test_legacy_unknown_database_keeps_original_fit_time_across_optional_source_revision(self):
        import test_sdk_transport as transport

        if not transport.PINNED_SDK:
            self.skipTest("Requires the installed, pinned Garmin SDK")
        sdk = transport.SDKTransportTests("runTest")
        try:
            sdk.setUp()
            sdk.enterContext(patch("subprocess.run", side_effect=transport.forbidden))
            sdk.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
            old = hg_domain.Measurement(
                AT + timedelta(seconds=1.500001), 70, 23.5, "PICOOC", 22, 53.55
            )
            self.write_export.__func__(sdk, [old])
            wire_times = []

            def server(method, path, pool):
                if method == "POST":
                    self.assertEqual(path, "/upload-service/upload")
                    wire_times.append(transport.fit_weight_timestamp(sdk.fit_files[-1]))
                    raise sdk.urllib3.exceptions.ReadTimeoutError(
                        pool, path, "synthetic accepted hidden FIT upload"
                    )
                self.assertEqual(method, "GET")
                return 200, sdk.read_payload(path)

            sdk.handler = server
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_sync.run_sync(sdk.cfg)
            self.assertEqual(hg_state.get_status(sdk.db, old), "uncertain")
            self.assertEqual(wire_times, [AT.astimezone(hg_std_UTC) + timedelta(seconds=1)])
            # This is exactly the previous six-column database format, with no separate wire evidence.
            sdk.db.execute("DROP TABLE request_times")
            sdk.db.commit()
            self.assertEqual(len(sdk.db.execute("PRAGMA table_info(uploads)").fetchall()), 6)
            middle = hg_domain.Measurement(old.at, old.weight, None, old.source, lbm=54)
            self.write_export.__func__(sdk, [middle])
            self.assertEqual(hg_sources.load_measurements(sdk.cfg)[1], [middle])
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_sync.run_sync(sdk.cfg)
            current = dict(hg_state.state_row(sdk.db, middle))
            self.assertEqual(current["core"], middle.core)
            self.assertEqual(json.loads(current["payload"]), middle.payload())
            self.assertEqual(current["status"], "uncertain")
            wire = sdk.db.execute(
                "SELECT wire_at FROM request_times WHERE record_id=?", (old.key,)
            ).fetchone()[0]
            self.assertEqual(hg_domain.aware(wire), wire_times[0])
            candidate = hg_domain.Measurement(AT, 72, None, "OTHER")
            self.write_export.__func__(sdk, [candidate])
            source = (sdk.inbox / "health.json").read_bytes()
            self.assertEqual(hg_sources.load_measurements(sdk.cfg)[1], [candidate])
            token = sdk.state / "synthetic-token"
            token.write_bytes(b"unchanged local token")
            with (
                patch.object(hg_garmin, "api_client", return_value=sdk.api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                hg_sync.run_sync(sdk.cfg)
                self.assertEqual(
                    hg_processing.process_record(sdk.api, sdk.db, candidate), "uncertain"
                )
            self.assertIsNone(hg_state.state_row(sdk.db, candidate))
            self.assertEqual(dict(hg_state.state_row(sdk.db, middle)), current)
            self.assertEqual(
                sdk.db.execute(
                    "SELECT wire_at FROM request_times WHERE record_id=?", (old.key,)
                ).fetchone()[0],
                wire,
            )
            self.assertEqual(sum(request["method"] == "POST" for request in sdk.sent), 1)
            self.assertFalse(any(request["method"] == "DELETE" for request in sdk.sent))
            self.assertFalse(list((sdk.state / "operations").glob("*.json")))
            self.assertEqual((sdk.inbox / "health.json").read_bytes(), source)
            self.assertEqual(token.read_bytes(), b"unchanged local token")
        finally:
            sdk.doCleanups()

    def test_unfinished_reviewed_targets_protect_source_aliases_without_unknown_database_rows(self):
        from health_garmin_sync import history as bulk

        self.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        for kind in ("single", "single_marker", "legacy_replacement", "full"):
            with self.subTest(kind=kind):
                self.db.execute("DELETE FROM uploads")
                self.db.commit()
                for journal in (self.state / "operations").glob("*.json"):
                    journal.unlink()
                (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                self.write_export([self.r])
                backend = API([dict(row(self.r), bodyFat=None)])
                api = self.native_deletion_api(backend, fail_at=1)
                plan = self.deletion_plan(api)
                native_write = hg_file_io.write_json

                def legacy_write(path, value, **kwargs):
                    if kind == "legacy_replacement" and path.parent.name == "operations":
                        value = {key: item for key, item in value.items() if key != "measurement"}
                    native_write(path, value, **kwargs)

                with (
                    patch.object(hg_file_io, "write_json", side_effect=legacy_write),
                    self.assertRaises(hg_domain.SafeError),
                ):
                    if kind == "full":
                        with (
                            patch("builtins.input", return_value="REPLACE ALL"),
                            contextlib.redirect_stdout(io.StringIO()),
                        ):
                            bulk.execute(
                                argparse.Namespace(plan=str(plan), complete_export=True),
                                self.cfg,
                                api,
                            )
                    else:
                        self.reviewed(api, plan)
                journal = next((self.state / "operations").glob("*.json"))
                self.assertEqual(hg_state.read_operation(journal)["phase"], "uncertain")
                if kind == "single_marker":
                    self.assertEqual(hg_state.get_status(self.db, self.r), "repair_uncertain")
                elif kind != "full":
                    # An explicit local PRESENT decision does not finish a reviewed journal.
                    with (
                        patch("builtins.input", return_value="PRESENT"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_operations.resolve_record(
                            argparse.Namespace(
                                record_id=self.r.key, present=True, absent=False, ignore=False
                            )
                        )
                    self.assertEqual(hg_state.get_status(self.db, self.r), "verified_manual")
                else:
                    self.assertIsNone(hg_state.state_row(self.db, self.r))
                journal_bytes = journal.read_bytes()
                backup = Path(hg_state.read_operation(journal)["backup"])
                backup_bytes = {p.name: p.read_bytes() for p in backup.glob("*.json")}
                backend.rows = []  # A delayed DELETE became visible; its unknown operation remains active.
                current = hg_domain.Measurement(AT, 72, 24, "OTHER", 22)
                self.write_export([current])
                source = (self.inbox / "health.json").read_bytes()
                self.assertEqual(hg_sources.load_measurements(self.cfg)[1], [current])
                for _ in range(2):
                    self.sync(api)
                    summary = json.loads((self.state / "last_run.json").read_text())
                    self.assertEqual(summary["results"][current.key], "uncertain")
                    self.assertIsNone(hg_state.state_row(self.db, current))
                plan_obj = json.loads((self.state / "plans" / "sync-latest.json").read_text())
                self.assertIn(
                    self.r.key if kind == "single_marker" else journal.name,
                    plan_obj["entries"][0]["reason"],
                )
                self.assertEqual((len(backend.posts), len(backend.deletes)), (0, 1))
                backend.rows = [row(current, 201)]
                self.sync(api)
                self.assertEqual(hg_state.get_status(self.db, current), "verified")
                self.assertEqual((len(backend.posts), len(backend.deletes)), (0, 1))
                self.assertEqual(journal.read_bytes(), journal_bytes)
                self.assertEqual(
                    {p.name: p.read_bytes() for p in backup.glob("*.json")}, backup_bytes
                )
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(token.read_bytes(), b"unchanged local token")

    def test_alias_absence_resolution_waits_for_original_unknown_request(self):
        self.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
        current = hg_domain.Measurement(AT, 72, 24, "OTHER", 22)
        self.write_export([current])
        backend = API([row(hg_domain.Measurement(AT, 71, 24, "PICOOC"))])
        api = self.native_deletion_api(backend, fail_at=None)
        self.sync(api)
        self.assertEqual(hg_state.get_status(self.db, current), "conflict")
        current_row = dict(hg_state.state_row(self.db, current))
        self.write_export([self.r])
        backend.rows = [dict(row(self.r), bodyFat=None)]
        backend.visible = False
        native_post = backend.add_body_composition

        def accepted_timeout(**kw):
            native_post(**kw)
            raise TimeoutError("synthetic accepted upload")

        api.api.add_body_composition.side_effect = accepted_timeout
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, self.deletion_plan(api))
        journal = next((self.state / "operations").glob("*.json"))
        journal_bytes = journal.read_bytes()
        self.write_export([current])
        source = (self.inbox / "health.json").read_bytes()
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        self.sync(api)
        self.assertEqual(dict(hg_state.state_row(self.db, current)), current_row)
        args = argparse.Namespace(record_id=current.key, present=False, absent=True, ignore=False)
        with patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(hg_domain.SafeError, self.r.key):
                hg_operations.resolve_record(args)
        prompt.assert_not_called()
        self.assertEqual(dict(hg_state.state_row(self.db, current)), current_row)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertEqual((len(backend.posts), len(backend.deletes)), (1, 1))
        # A successful historical check releases the active protection, without changing this alias.
        backend.rows = [row(self.r, 201)]
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(
                hg_operations.verify_operation(argparse.Namespace(operation=str(journal)))
            )
        backend.rows = []
        with (
            patch("builtins.input", return_value="ABSENT"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.resolve_record(args)
        self.assertEqual(hg_state.get_status(self.db, current), "pending")
        backend.visible = True
        api.api.add_body_composition.side_effect = native_post
        self.sync(api)
        self.assertEqual(hg_state.get_status(self.db, current), "verified")
        self.assertEqual((len(backend.posts), len(backend.deletes)), (2, 1))
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)
        self.assertEqual(token.read_bytes(), b"unchanged local token")

    def test_verified_source_alias_missing_requires_explicit_absence_confirmation(self):
        self.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        for prior in ("verified", "verified_manual", "remote_missing"):
            for changed in (False, True):
                with (
                    self.subTest(prior=prior, changed=changed),
                    patch.object(hg_runtime, "now", return_value=AT + timedelta(hours=2)),
                ):
                    self.db.execute("DELETE FROM uploads")
                    self.db.commit()
                    (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                    self.write_export([self.r])
                    backend = API()
                    api = self.native_deletion_api(backend, fail_at=None)
                    self.sync(api)
                    self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
                    if prior == "verified_manual":
                        with (
                            patch("builtins.input", return_value="PRESENT"),
                            contextlib.redirect_stdout(io.StringIO()),
                        ):
                            hg_operations.resolve_record(
                                argparse.Namespace(
                                    record_id=self.r.key, present=True, absent=False, ignore=False
                                )
                            )
                    backend.rows = []
                    if prior == "remote_missing":
                        self.sync(api)
                    original = dict(hg_state.state_row(self.db, self.r))
                    self.assertEqual(original["status"], prior)
                    current = hg_domain.Measurement(
                        AT.astimezone(hg_std_UTC),
                        72 if changed else 70,
                        24 if changed else 23.5,
                        "OTHER",
                        22 if changed else None,
                        54,
                    )
                    self.write_export([current])
                    source = (self.inbox / "health.json").read_bytes()
                    self.assertEqual(hg_sources.load_measurements(self.cfg)[1], [current])
                    for _ in range(2):
                        self.sync(api)
                        self.assertEqual(hg_state.get_status(self.db, current), "remote_missing")
                        self.assertEqual(
                            json.loads(hg_state.state_row(self.db, current)["payload"]),
                            current.payload(),
                        )
                        summary = json.loads((self.state / "last_run.json").read_text())
                        self.assertEqual(summary["results"][current.key], "remote_missing")
                        plan = json.loads((self.state / "plans" / "sync-latest.json").read_text())
                        self.assertEqual(plan["entries"][0]["action"], "review_missing")
                        self.assertIn(self.r.key, plan["entries"][0]["reason"])
                        self.assertEqual((len(backend.posts), len(backend.deletes)), (1, 0))
                        self.assertEqual(dict(hg_state.state_row(self.db, self.r)), original)
                    with (
                        patch("builtins.input", return_value="ABSENT"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_operations.resolve_record(
                            argparse.Namespace(
                                record_id=current.key, present=False, absent=True, ignore=False
                            )
                        )
                    self.assertEqual(hg_state.get_status(self.db, current), "pending")
                    self.sync(api)
                    self.sync(api)
                    self.assertEqual(hg_state.get_status(self.db, current), "verified")
                    self.assertEqual((len(backend.posts), len(backend.deletes)), (2, 0))
                    self.assertEqual(dict(hg_state.state_row(self.db, self.r)), original)
                    self.assertFalse(list((self.state / "operations").glob("*.json")))
                    self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                    self.assertEqual(token.read_bytes(), b"unchanged local token")

    def test_verified_alias_controls_keep_matching_conflicting_and_unrelated_measurements(self):
        self.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
        for case in ("matched", "conflict", "different_time", "verified_conflict"):
            with (
                self.subTest(case=case),
                patch.object(hg_runtime, "now", return_value=AT + timedelta(hours=2)),
            ):
                self.db.execute("DELETE FROM uploads")
                self.db.commit()
                (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                self.write_export([self.r])
                backend = API()
                api = self.native_deletion_api(backend, fail_at=None)
                self.sync(api)
                if case == "verified_conflict":
                    backend.rows[0]["weight"] = 71000
                    self.sync(api)
                    self.assertEqual(hg_state.get_status(self.db, self.r), "verified_conflict")
                if case in ("different_time", "verified_conflict"):
                    backend.rows = []
                original = dict(hg_state.state_row(self.db, self.r))
                current = hg_domain.Measurement(
                    AT + timedelta(minutes=2 if case == "different_time" else 0),
                    72 if case == "conflict" else 70,
                    23.5,
                    "OTHER",
                )
                self.write_export([current])
                source = (self.inbox / "health.json").read_bytes()
                self.assertEqual(hg_sources.load_measurements(self.cfg)[1], [current])
                self.sync(api)
                self.assertEqual(
                    hg_state.get_status(self.db, current),
                    "conflict"
                    if case == "conflict"
                    else "remote_missing"
                    if case == "verified_conflict"
                    else "verified",
                )
                self.assertEqual(
                    (len(backend.posts), len(backend.deletes)),
                    (2 if case == "different_time" else 1, 0),
                )
                self.assertEqual(dict(hg_state.state_row(self.db, self.r)), original)
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)

    def test_direct_process_rechecks_verified_alias_history_without_overriding_unknown_protection(
        self,
    ):
        self.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
        backend = API()
        api = self.native_deletion_api(backend, fail_at=None)
        with patch.object(hg_runtime, "now", return_value=AT + timedelta(hours=2)):
            self.sync(api)
            backend.rows = []
            self.sync(api)
        with (
            patch("builtins.input", return_value="ABSENT"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.resolve_record(
                argparse.Namespace(record_id=self.r.key, present=False, absent=True, ignore=False)
            )
        current = hg_domain.Measurement(AT, 72, 24, "OTHER", 22)
        self.write_export([current])
        source = (self.inbox / "health.json").read_bytes()
        self.assertEqual(hg_sources.load_measurements(self.cfg)[1], [current])
        plan = hg_reconciliation.comparison_plan(
            self.db, [current], {"dateWeightList": []}, AT.date(), AT.date()
        )
        self.assertEqual(plan["entries"][0]["action"], "missing")
        # A user's newer PRESENT decision invalidates the previously permissive plan.
        with (
            patch("builtins.input", return_value="PRESENT"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.resolve_record(
                argparse.Namespace(record_id=self.r.key, present=True, absent=False, ignore=False)
            )
        original = dict(hg_state.state_row(self.db, self.r))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hg_processing.process_record(api, self.db, current), "remote_missing")
        self.assertIn(self.r.key, hg_state.state_row(self.db, current)["detail"])
        self.assertEqual((len(backend.posts), len(backend.deletes)), (1, 0))
        self.assertEqual(dict(hg_state.state_row(self.db, self.r)), original)
        with (
            patch("builtins.input", return_value="ABSENT"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.resolve_record(
                argparse.Namespace(record_id=current.key, present=False, absent=True, ignore=False)
            )
        # Even an explicitly pending alias cannot supersede a new unknown request at this time.
        self.write_export([self.r])
        backend.visible = False
        native_post = backend.add_body_composition

        def accepted_timeout(**kw):
            native_post(**kw)
            raise TimeoutError("synthetic accepted request")

        api.api.add_body_composition.side_effect = accepted_timeout
        with patch.object(hg_runtime, "now", return_value=AT + timedelta(hours=2)):
            self.sync(api)
        with (
            patch("builtins.input", return_value="ABSENT"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.resolve_record(
                argparse.Namespace(record_id=self.r.key, present=False, absent=True, ignore=False)
            )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hg_processing.process_record(api, self.db, self.r), "uncertain")
        original = dict(hg_state.state_row(self.db, self.r))
        current_row = dict(hg_state.state_row(self.db, current))
        self.write_export([current])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hg_processing.process_record(api, self.db, current), "uncertain")
        self.assertEqual(dict(hg_state.state_row(self.db, current)), current_row)
        self.assertEqual(dict(hg_state.state_row(self.db, self.r)), original)
        self.assertEqual((len(backend.posts), len(backend.deletes)), (2, 0))
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)

    def test_old_matched_operations_preserve_current_export_revisions_and_ignore_choice(self):
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        for case in ("fat", "bmi", "weight", "ignored", "source"):
            with self.subTest(case=case):
                self.db.execute("DELETE FROM uploads")
                self.db.commit()
                for journal in (self.state / "operations").glob("*.json"):
                    journal.unlink()
                (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                self.cfg["allowed_sources"] = ["PICOOC", "OTHER"]
                api, backend, journal = self.completed_optional_repair()
                historical = hg_state.read_operation(journal)
                backup = Path(historical["backup"])
                evidence = {p.name: p.read_bytes() for p in backup.glob("*.json")}
                current = hg_domain.Measurement(
                    AT,
                    72 if case == "weight" else 70,
                    23.5 if case == "fat" else None,
                    "OTHER" if case == "source" else "PICOOC",
                    22 if case == "bmi" else None,
                    54,
                )
                if case == "fat":
                    self.assertEqual(
                        current.core,
                        hg_domain.Measurement(
                            **{
                                **historical["replacement"],
                                "at": hg_domain.aware(historical["replacement"]["at"]),
                            }
                        ).core,
                    )
                self.write_export([current])
                source = (self.inbox / "health.json").read_bytes()
                self.assertEqual(hg_sources.load_measurements(self.cfg)[1], [current])
                self.sync(api)
                if case == "ignored":
                    with (
                        patch("builtins.input", return_value="IGNORE"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_operations.resolve_record(
                            argparse.Namespace(
                                record_id=current.key, present=False, absent=False, ignore=True
                            )
                        )
                latest = dict(hg_state.state_row(self.db, current))
                status = (
                    "ignored"
                    if case == "ignored"
                    else "verified_conflict"
                    if case in ("bmi", "weight")
                    else "verified"
                )
                self.assertEqual((latest["core"], latest["status"]), (current.core, status))
                self.assertEqual(json.loads(latest["payload"]), current.payload())
                args = argparse.Namespace(operation=str(journal))
                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    patch(
                        "builtins.input",
                        side_effect=AssertionError("Matched old operation is read-only"),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertTrue(hg_operations.verify_operation(args))
                    self.assertEqual(dict(hg_state.state_row(self.db, current)), latest)
                    hg_operations.resume_operation(args)
                self.assertEqual(dict(hg_state.state_row(self.db, current)), latest)
                self.assertEqual(hg_state.read_operation(journal)["phase"], "verified")
                self.assertTrue(hg_state.read_operation(journal)["last_check_passed"])
                self.assertEqual(
                    hg_state.read_operation(journal)["measurement"], historical["measurement"]
                )
                self.sync(api)
                refreshed = hg_state.state_row(self.db, current)
                self.assertEqual((refreshed["core"], refreshed["status"]), (current.core, status))
                self.assertEqual(json.loads(refreshed["payload"]), current.payload())
                self.assertEqual((len(backend.posts), len(backend.deletes)), (1, 1))
                self.assertEqual({p.name: p.read_bytes() for p in backup.glob("*.json")}, evidence)
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(token.read_bytes(), b"unchanged local token")

    def test_resume_rechecks_local_revision_before_confirmation_and_each_upload_read(self):
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        for timing in (
            "initial",
            "ignored",
            "confirmation",
            "pre_post",
            "pre_post_matched",
            "pre_post_lbm",
            "confirmation_matched",
        ):
            with self.subTest(timing=timing):
                self.db.execute("DELETE FROM uploads")
                self.db.commit()
                for journal in (self.state / "operations").glob("*.json"):
                    journal.unlink()
                (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                api, backend, journal = self.completed_optional_repair()
                historical, old_journal = hg_state.read_operation(journal), journal.read_bytes()
                backup = Path(historical["backup"])
                evidence = {p.name: p.read_bytes() for p in backup.glob("*.json")}
                backend.rows = []  # The previously completed target has subsequently gone missing.
                source = (self.inbox / "health.json").read_bytes()
                current, latest = self.r, None
                reads = 0
                native_get = backend.get_body_composition

                def revise(*, visible=False, old_matched=False, lbm_only=False):
                    nonlocal current, latest, source
                    current = hg_domain.Measurement(
                        AT,
                        70 if timing == "confirmation_matched" or lbm_only else 72,
                        23.5 if timing == "confirmation_matched" else None,
                        "PICOOC",
                        lbm=54,
                    )
                    self.write_export([current])
                    source = (self.inbox / "health.json").read_bytes()
                    self.assertEqual(hg_sources.load_measurements(self.cfg)[1], [current])
                    if visible:
                        backend.rows = [row(current, 301)]
                    elif old_matched:
                        payload = historical["replacement"]
                        backend.rows = [
                            row(
                                hg_domain.Measurement(
                                    **{**payload, "at": hg_domain.aware(payload["at"])}
                                ),
                                303,
                            )
                        ]
                    self.sync(api)
                    latest = dict(hg_state.state_row(self.db, current))
                    self.assertEqual(latest["core"], current.core)
                    self.assertEqual(
                        latest["status"],
                        "verified"
                        if visible or lbm_only
                        else "repair_uncertain"
                        if old_matched
                        else "remote_missing",
                    )

                def get(start, end):
                    nonlocal reads
                    reads += 1
                    if reads == 3 and timing in ("pre_post", "pre_post_matched", "pre_post_lbm"):
                        revise(
                            visible=timing == "pre_post",
                            old_matched=timing != "pre_post",
                            lbm_only=timing == "pre_post_lbm",
                        )
                    return native_get(start, end)

                backend.get_body_composition = get
                if timing == "initial":
                    revise()
                elif timing == "ignored":
                    with (
                        patch("builtins.input", return_value="IGNORE"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_operations.resolve_record(
                            argparse.Namespace(
                                record_id=self.r.key, present=False, absent=False, ignore=True
                            )
                        )
                    latest = dict(hg_state.state_row(self.db, self.r))

                def confirm(_):
                    self.assertNotIn(timing, ("initial", "ignored"))
                    if timing in ("confirmation", "confirmation_matched"):
                        revise(visible=timing == "confirmation_matched")
                    return "RESUME " + self.r.key

                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    patch("builtins.input", side_effect=confirm) as prompt,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    if timing in ("confirmation_matched", "pre_post_matched", "pre_post_lbm"):
                        hg_operations.resume_operation(argparse.Namespace(operation=str(journal)))
                    else:
                        with self.assertRaisesRegex(hg_domain.SafeError, "本地测量已修订或忽略"):
                            hg_operations.resume_operation(
                                argparse.Namespace(operation=str(journal))
                            )
                self.assertEqual(prompt.call_count, 0 if timing in ("initial", "ignored") else 1)
                if timing == "pre_post_lbm":
                    self.assertEqual(
                        json.loads(hg_state.state_row(self.db, current)["payload"]),
                        current.payload(),
                    )
                else:
                    self.assertEqual(dict(hg_state.state_row(self.db, current)), latest)
                if timing == "pre_post":
                    self.assertEqual(hg_state.read_operation(journal)["phase"], "uncertain")
                elif timing in ("confirmation_matched", "pre_post_matched", "pre_post_lbm"):
                    self.assertEqual(hg_state.read_operation(journal)["phase"], "verified")
                else:
                    self.assertEqual(journal.read_bytes(), old_journal)
                self.assertEqual(
                    hg_state.read_operation(journal)["measurement"], historical["measurement"]
                )
                self.assertEqual((len(backend.posts), len(backend.deletes)), (1, 1))
                self.sync(api)
                self.assertEqual((len(backend.posts), len(backend.deletes)), (1, 1))
                refreshed = hg_state.state_row(self.db, current)
                self.assertEqual(refreshed["core"], current.core)
                self.assertEqual(json.loads(refreshed["payload"]), current.payload())
                self.assertEqual(
                    refreshed["status"],
                    "uncertain" if timing == "pre_post_matched" else latest["status"],
                )
                self.assertEqual({p.name: p.read_bytes() for p in backup.glob("*.json")}, evidence)
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(token.read_bytes(), b"unchanged local token")

    def test_reviewed_persistence_protects_updates_during_post_and_vetoes_stale_inflight(self):
        token = self.state / "synthetic-token"
        token.write_bytes(b"unchanged local token")
        for mode in ("matched_core", "matched_lbm", "timeout", "stale_absence"):
            with self.subTest(mode=mode):
                self.db.execute("DELETE FROM uploads")
                self.db.commit()
                for journal in (self.state / "operations").glob("*.json"):
                    journal.unlink()
                (self.state / "last_sync_comparison.json").unlink(missing_ok=True)
                self.r = hg_domain.Measurement(AT, 70, None, "PICOOC", lbm=53.55)
                self.write_export([self.r])
                backend = API([row(hg_domain.Measurement(AT, 71, 23.5, "PICOOC"))])
                api = self.native_deletion_api(backend, fail_at=None)
                plan = self.deletion_plan(api)
                native_post, native_get = backend.add_body_composition, backend.get_body_composition
                current, latest, source, changed = self.r, None, None, False

                def revise():
                    nonlocal current, latest, source, changed
                    changed = True
                    current = hg_domain.Measurement(
                        AT, 70 if mode == "matched_lbm" else 72, None, "PICOOC", lbm=54
                    )
                    self.write_export([current])
                    source = (self.inbox / "health.json").read_bytes()
                    self.assertEqual(hg_sources.load_measurements(self.cfg)[1], [current])
                    if mode == "stale_absence":
                        backend.rows = [row(current, 301)]
                    self.sync(api)
                    latest = dict(hg_state.state_row(self.db, current))
                    self.assertEqual(latest["core"], current.core)
                    self.assertEqual(json.loads(latest["payload"]), current.payload())

                def post(**kwargs):
                    result = native_post(**kwargs)
                    revise()
                    if mode == "timeout":
                        raise TimeoutError("synthetic accepted upload response lost")
                    return result

                def get(start, end):
                    snapshot = native_get(start, end)
                    paths = list((self.state / "operations").glob("*.json"))
                    if (
                        mode == "stale_absence"
                        and not changed
                        and paths
                        and hg_state.read_operation(paths[0])["phase"] == "uploading"
                    ):
                        # This complete read precedes a newer export/sync; veto at the durable intent gate.
                        revise()
                    return snapshot

                backend.add_body_composition = post
                backend.get_body_composition = get
                # Refresh the sealed adapter bindings; only the remote API boundary is replaced.
                api.api.add_body_composition.side_effect = post
                with contextlib.redirect_stdout(io.StringIO()):
                    if mode in ("timeout", "stale_absence"):
                        with self.assertRaises(hg_domain.SafeError):
                            self.reviewed(api, plan)
                    else:
                        self.reviewed(api, plan)
                self.assertTrue(changed)
                journal = next((self.state / "operations").glob("*.json"))
                job = hg_state.read_operation(journal)
                self.assertEqual(job["measurement"], self.r.payload())
                self.assertEqual(
                    job["phase"],
                    "uncertain" if mode in ("timeout", "stale_absence") else "verified",
                )
                stored = dict(hg_state.state_row(self.db, current))
                if mode == "matched_lbm":
                    self.assertEqual(json.loads(stored["payload"]), current.payload())
                    self.assertEqual(stored["status"], "verified")
                else:
                    self.assertEqual(stored, latest)
                self.assertEqual(
                    (len(backend.posts), len(backend.deletes)),
                    (0 if mode == "stale_absence" else 1, 1),
                )
                for submitted in backend.posts:
                    self.assertEqual(set(submitted), {"timestamp", "weight", "percent_fat", "bmi"})
                    self.assertEqual(submitted["percent_fat"], 23.5)
                before = (len(backend.posts), len(backend.deletes))
                self.sync(api)
                self.assertEqual((len(backend.posts), len(backend.deletes)), before)
                self.assertEqual(hg_state.state_row(self.db, current)["core"], current.core)
                self.assertEqual(
                    json.loads(hg_state.state_row(self.db, current)["payload"]), current.payload()
                )
                backup = Path(job["backup"])
                self.assertEqual(
                    json.loads((backup / "plan.json").read_text())["entries"][0]["measurement"],
                    self.r.payload(),
                )
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(token.read_bytes(), b"unchanged local token")

    def test_bmi_enrichment_repairs_existing_record_and_verifies_all_fields(self):
        original = self.r
        hg_state.set_status(self.db, original, "verified")
        self.r = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22)
        self.assertEqual(self.r.key, original.key)
        self.write_export([self.r])
        api = API([row(original)])
        path = self.plan(api.rows, save=True)
        self.reviewed(api, path)
        self.assertEqual(api.deletes, [("100", "2020-02-20")])
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]["weight"], 70)
        self.assertEqual(api.posts[0]["percent_fat"], 23.5)
        self.assertEqual(api.posts[0]["bmi"], 22)
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        self.assertEqual(len(api.rows), 1)
        self.assertEqual(self.action(api.rows), "matched")

    def test_old_verified_record_with_late_bmi_is_rechecked(self):
        hg_state.set_status(self.db, self.r, "verified")
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        enriched = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22)
        self.write_export([enriched])
        api = API([row(self.r)])
        self.sync(api)
        self.assertTrue(api.reads)
        self.assertFalse(api.posts)
        summary = json.loads((self.state / "last_run.json").read_text())
        self.assertEqual(summary["results"][self.r.key], "repair")

    def test_old_completed_record_retains_late_lbm_without_changing_verification(self):
        original = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22)
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        for status in ("verified", "verified_manual", "ignored"):
            with self.subTest(status=status):
                hg_state.set_status(self.db, original, status, "previous decision")
                before = dict(hg_state.state_row(self.db, original))
                payload = json.loads(before["payload"])
                for lbm in (53.55, 54.0):
                    enriched = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", lbm=lbm)
                    self.write_export([enriched])
                    source = (self.inbox / "health.json").read_bytes()
                    with (
                        patch.object(
                            hg_garmin,
                            "api_client",
                            side_effect=AssertionError("No remote work expected"),
                        ) as api,
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_sync.run_sync(self.cfg)
                    payload["lbm"] = lbm
                    after = dict(hg_state.state_row(self.db, original))
                    self.assertEqual(json.loads(after.pop("payload")), payload)
                    self.assertEqual(
                        after, {key: value for key, value in before.items() if key != "payload"}
                    )
                    api.assert_not_called()
                    self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                    summary = json.loads((self.state / "last_run.json").read_text())
                    self.assertEqual(summary["results"], {})
                    self.assertEqual(summary["local_status_counts"], {status: 1})

    def test_missing_or_unchanged_lbm_preserves_existing_local_evidence(self):
        original = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22, 53.55)
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        for status in ("verified", "verified_manual", "ignored"):
            for lbm in (None, 53.55):
                with self.subTest(status=status, lbm=lbm):
                    hg_state.set_status(self.db, original, status, "previous decision")
                    before = dict(hg_state.state_row(self.db, original))
                    self.write_export([hg_domain.Measurement(AT, 70, 23.5, "PICOOC", lbm=lbm)])
                    api = API([row(original)])
                    self.sync(api)
                    self.assertEqual(dict(hg_state.state_row(self.db, original)), before)
                    self.assertFalse(api.reads)
                    self.assertFalse(api.posts)
                    self.assertFalse(api.deletes)

    def test_late_lbm_with_remote_field_change_still_requires_comparison(self):
        for weight, bmi, action in ((71, None, "replace"), (70, 22, "repair")):
            with self.subTest(weight=weight, bmi=bmi):
                hg_state.set_status(self.db, self.r, "verified")
                hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
                changed = hg_domain.Measurement(AT, weight, 23.5, "PICOOC", bmi, 53.55)
                self.write_export([changed])
                api = API([row(self.r)])
                self.sync(api)
                self.assertTrue(api.reads)
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)
                self.assertEqual(hg_state.get_status(self.db, changed), "verified_conflict")
                summary = json.loads((self.state / "last_run.json").read_text())
                self.assertEqual(summary["results"], {self.r.key: action})

    def test_matched_sync_retains_missing_lbm_and_updates_explicit_values(self):
        original = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", lbm=53.55)
        cases = [
            ("first", self.r, 53.55),
            ("recent", self.r, 53.55),
            ("manual", self.r, 53.55),
            ("new_lbm", hg_domain.Measurement(AT, 70, 23.5, "PICOOC", lbm=54), 54),
            ("weight_changed", hg_domain.Measurement(AT, 40, 23.5, "PICOOC"), None),
            ("fat_changed", hg_domain.Measurement(AT, 70, 24, "PICOOC"), None),
        ]
        for mode, current, expected in cases:
            with (
                self.subTest(mode=mode),
                patch.object(hg_runtime, "STATE", self.state / mode),
                patch.object(hg_runtime, "now", return_value=AT + timedelta(days=7)),
            ):
                hg_file_io.private_dir(hg_runtime.STATE)
                with contextlib.closing(hg_state.database()) as db:
                    hg_state.set_status(db, original, "verified", "previous decision")
                    self.write_export([current])
                    source = (self.inbox / "health.json").read_bytes()
                    token = hg_runtime.STATE / "tokens" / "synthetic"
                    hg_file_io.private_dir(token.parent)
                    token.write_bytes(b"synthetic private token")
                    if mode in ("recent", "manual"):
                        hg_file_io.write_json(hg_runtime.STATE / "last_sync_comparison.json", {})
                    backend = API([row(current)])
                    api = self.detailed_api(backend)
                    with (
                        patch.object(hg_garmin, "api_client", return_value=api),
                        patch("builtins.input", return_value="UPLOAD"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        hg_sync.run_sync(self.cfg, current.key if mode == "manual" else None)
                    stored = dict(hg_state.state_row(db, current))
                    payload = current.payload()
                    if expected is not None:
                        payload["lbm"] = expected
                    self.assertEqual(json.loads(stored["payload"]), payload)
                    self.assertEqual(stored["core"], current.core)
                    self.assertEqual(stored["status"], "verified")
                    self.assertTrue(backend.reads)
                    self.assertFalse(backend.posts)
                    self.assertFalse(backend.deletes)
                    self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                    self.assertEqual(token.read_bytes(), b"synthetic private token")
                    self.assertEqual(current.lbm, 54 if mode == "new_lbm" else None)

    def test_missing_lbm_survives_durable_upload_and_delayed_verification(self):
        original = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", lbm=53.55)
        hg_state.set_status(self.db, original, "pending")
        backend = API()
        backend.visible = False
        post = backend.add_body_composition

        def observe_post(**kwargs):
            with contextlib.closing(hg_std_sqlite3.connect(self.state / "sync.sqlite3")) as reader:
                payload, status = reader.execute(
                    "SELECT payload,status FROM uploads WHERE record_id=?", (self.r.key,)
                ).fetchone()
            self.assertEqual(status, "inflight")
            self.assertEqual(json.loads(payload)["lbm"], 53.55)
            self.assertEqual(set(kwargs), {"timestamp", "weight", "percent_fat", "bmi"})
            return post(**kwargs)

        backend.add_body_composition = observe_post
        api = self.detailed_api(backend)
        self.assertEqual(
            hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None), "uncertain"
        )
        self.assertEqual(json.loads(hg_state.state_row(self.db, self.r)["payload"])["lbm"], 53.55)
        backend.rows = [row(self.r)]
        self.assertEqual(
            hg_processing.process_record(api, self.db, self.r, sleep=lambda _: None), "verified"
        )
        self.assertEqual(json.loads(hg_state.state_row(self.db, self.r)["payload"])["lbm"], 53.55)
        self.assertEqual(len(backend.posts), 1)
        self.assertFalse(backend.deletes)
        self.assertIsNone(self.r.lbm)

    def test_idle_historical_sync_finishes_without_login_or_overwriting_review_plan(self):
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        for status in ("verified", "verified_manual", "ignored"):
            with self.subTest(status=status):
                hg_state.set_status(self.db, self.r, status)
                plan = self.plan([row(self.r)], save=True)
                previous = plan.read_bytes(), (self.state / "last_compare.json").read_bytes()
                with (
                    patch.object(
                        hg_garmin, "api_client", side_effect=TimeoutError("Unavailable login")
                    ) as api,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    hg_sync.run_sync(self.cfg)
                api.assert_not_called()
                summary = json.loads((self.state / "last_run.json").read_text())
                self.assertEqual(summary["results"], {})
                self.assertEqual(summary["comparison"], {})
                self.assertEqual(summary["local_status_counts"], {status: 1})
                self.assertEqual(
                    (plan.read_bytes(), (self.state / "last_compare.json").read_bytes()), previous
                )

    def test_skipped_completed_sources_still_protect_selected_record_correspondence(self):
        for status in ("verified", "verified_manual", "ignored"):
            for offset, source in ((0, "Second Scale"), (0.5, "PICOOC"), (2, "PICOOC")):
                for visible in (True,) if offset == 2 else (False, True):
                    with self.subTest(status=status, offset=offset, visible=visible):
                        old = self.r
                        new = hg_domain.Measurement(
                            AT + timedelta(seconds=offset), 70, 23.5, source
                        )
                        self.cfg["allowed_sources"] = ["PICOOC", "Second Scale"]
                        hg_state.set_status(self.db, old, status, "Retained completed decision")
                        self.db.execute("DELETE FROM uploads WHERE record_id!=?", (old.key,))
                        self.db.commit()
                        before = dict(hg_state.state_row(self.db, old))
                        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
                        self.write_export([old, new])
                        source_bytes = (self.inbox / "health.json").read_bytes()
                        remote = hg_domain.Measurement(
                            AT + timedelta(seconds=1 if offset == 2 else 0), 70, 23.5, "PICOOC"
                        )
                        api = API([row(remote)] if visible else [])
                        self.sync(api)
                        self.assertFalse(api.posts)
                        self.assertFalse(api.deletes)
                        self.assertEqual(dict(hg_state.state_row(self.db, old)), before)
                        self.assertEqual(hg_state.get_status(self.db, new), "conflict")
                        summary = json.loads((self.state / "last_run.json").read_text())
                        self.assertEqual(summary["comparison"], {"conflict": 1})
                        self.assertEqual(summary["results"], {new.key: "conflict"})
                        self.assertEqual(summary["local_status_counts"], {status: 1, "conflict": 1})
                        plan = json.loads((self.state / "plans" / "sync-latest.json").read_text())
                        self.assertEqual([entry["id"] for entry in plan["entries"]], [new.key])
                        self.assertEqual(plan["counts"], {"conflict": 1})
                        self.assertEqual(plan["extra_remote"], [])
                        self.assertEqual((self.inbox / "health.json").read_bytes(), source_bytes)

    def test_unrelated_completed_context_does_not_expand_queries_or_change_status(self):
        old = self.r
        new = hg_domain.Measurement(AT + timedelta(days=40), 71, 24, "PICOOC")
        later = hg_domain.Measurement(AT + timedelta(days=80), 72, 25, "PICOOC")
        neighbor = hg_domain.Measurement(new.at - timedelta(minutes=10), 73, 26, "PICOOC")
        retained = (old, neighbor, later)
        for record in retained:
            hg_state.set_status(self.db, record, "verified", "Keep existing evidence")
        before = [dict(hg_state.state_row(self.db, record)) for record in retained]
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        self.write_export([old, neighbor, new, later])
        source = (self.inbox / "health.json").read_bytes()
        api = API([row(record, pk) for pk, record in enumerate((old, neighbor, new, later), 100)])
        self.sync(api)
        day = new.at.date()
        self.assertEqual(
            api.reads,
            [((day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat())],
        )
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertEqual([dict(hg_state.state_row(self.db, record)) for record in retained], before)
        summary = json.loads((self.state / "last_run.json").read_text())
        self.assertEqual(summary["comparison"], {"matched": 1})
        self.assertEqual(summary["results"], {new.key: "verified"})
        self.assertEqual(summary["local_status_counts"], {"verified": 4})
        plan = json.loads((self.state / "plans" / "sync-latest.json").read_text())
        self.assertEqual([entry["id"] for entry in plan["entries"]], [new.key])
        self.assertEqual(plan["start"], day.isoformat())
        self.assertEqual(plan["end"], day.isoformat())
        self.assertEqual(plan["extra_remote"], [])
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)

    def test_explicit_single_upload_keeps_other_source_correspondence_context(self):
        new = hg_domain.Measurement(AT, 70, 23.5, "Second Scale")
        self.cfg["allowed_sources"].append(new.source)
        self.write_export([self.r, new])
        source = (self.inbox / "health.json").read_bytes()
        for visible in (False, True):
            with self.subTest(visible=visible):
                hg_state.set_status(self.db, self.r, "verified", "Original verification")
                self.db.execute("DELETE FROM uploads WHERE record_id=?", (new.key,))
                self.db.commit()
                before = dict(hg_state.state_row(self.db, self.r))
                api = API([row(self.r)] if visible else [])
                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    patch("builtins.input", return_value="UPLOAD"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    hg_sync.run_sync(self.cfg, new.key)
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)
                self.assertEqual(dict(hg_state.state_row(self.db, self.r)), before)
                self.assertEqual(hg_state.get_status(self.db, new), "conflict")
                summary = json.loads((self.state / "last_run.json").read_text())
                self.assertEqual(summary["comparison"], {"conflict": 1})
                self.assertEqual(summary["results"], {new.key: "conflict"})
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)

    def test_required_comparison_still_logs_in_for_initial_recent_changed_or_pending_records(self):
        recent = hg_domain.Measurement(hg_runtime.now() - timedelta(days=1), 70, 23.5, "PICOOC")
        cases = [
            ("first", self.r, self.r, "verified", True),
            ("recent", recent, recent, "verified", False),
            ("BMI", self.r, hg_domain.Measurement(AT, 70, 23.5, "PICOOC", 22), "verified", False),
            ("core", self.r, hg_domain.Measurement(AT, 71, 23.5, "PICOOC"), "verified", False),
        ]
        cases += [
            (status, self.r, self.r, status, False)
            for status in (
                "pending",
                "inflight",
                "uncertain",
                "repair_uncertain",
                "verified_conflict",
                "remote_missing",
            )
        ]
        for name, previous, current, status, first in cases:
            with self.subTest(name=name):
                hg_state.set_status(self.db, previous, status)
                marker = self.state / "last_sync_comparison.json"
                if first:
                    marker.unlink(missing_ok=True)
                else:
                    hg_file_io.write_json(marker, {})
                self.write_export([current])
                remote = API([row(previous)])
                with (
                    patch.object(hg_garmin, "api_client", return_value=remote) as api,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    hg_sync.run_sync(self.cfg)
                api.assert_called_once_with()
                self.assertTrue(remote.reads)
                self.assertFalse(remote.posts)
                self.assertFalse(remote.deletes)

    def test_explicit_upload_of_old_verified_record_still_checks_remote(self):
        hg_state.set_status(self.db, self.r, "verified")
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        remote = API([row(self.r)])
        with (
            patch.object(hg_garmin, "api_client", return_value=remote) as api,
            patch("builtins.input", return_value="UPLOAD"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_sync.run_sync(self.cfg, self.r.key)
        api.assert_called_once_with()
        self.assertTrue(remote.reads)
        self.assertFalse(remote.posts)
        self.assertFalse(remote.deletes)
        summary = json.loads((self.state / "last_run.json").read_text())
        self.assertEqual(summary["results"], {self.r.key: "verified"})

    def test_login_failure_rolls_back_local_lbm_merge_and_allows_later_recovery(self):
        hg_state.set_status(self.db, self.r, "verified", "previous verification")
        before = dict(hg_state.state_row(self.db, self.r))
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        hg_file_io.write_json(self.state / "last_run.json", {"previous": "summary"})
        old_summary = (self.state / "last_run.json").read_bytes()
        enriched = hg_domain.Measurement(AT, 70, 23.5, "PICOOC", lbm=53.55)
        other = hg_domain.Measurement(AT + timedelta(hours=1), 71, 24, "PICOOC")
        self.write_export([enriched, other])
        source = (self.inbox / "health.json").read_bytes()
        with (
            patch.object(
                hg_garmin, "api_client", side_effect=TimeoutError("Unavailable login")
            ) as api,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(TimeoutError),
        ):
            hg_sync.run_sync(self.cfg)
        api.assert_called_once_with()
        self.assertEqual(dict(hg_state.state_row(self.db, self.r)), before)
        self.assertEqual((self.state / "last_run.json").read_bytes(), old_summary)
        remote = API([row(other)])
        self.sync(remote)
        self.assertEqual(json.loads(hg_state.state_row(self.db, self.r)["payload"])["lbm"], 53.55)
        self.assertEqual(hg_state.get_status(self.db, other), "verified")
        self.assertFalse(remote.posts)
        self.assertFalse(remote.deletes)
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)

    def test_previously_verified_is_queried_and_detects_change(self):
        hg_state.set_status(self.db, self.r, "verified")
        wrong = row(self.r)
        wrong["bodyFat"] = 24
        api = API([wrong])
        self.sync(api)
        self.assertTrue(api.reads)
        self.assertFalse(api.posts)
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified_conflict")

    def test_verified_conflict_does_not_lose_duplicate_protection(self):
        hg_state.set_status(self.db, self.r, "verified")
        wrong = dict(row(self.r), bodyFat=24)
        api = API([wrong])
        self.sync(api)
        api.rows.clear()
        self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(hg_state.get_status(self.db, self.r), "remote_missing")

    def test_previously_verified_missing_is_not_blindly_reuploaded(self):
        hg_state.set_status(self.db, self.r, "verified")
        api = API()
        self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(hg_state.get_status(self.db, self.r), "remote_missing")

    def test_recent_verified_rechecked_after_initial_run(self):
        self.r = hg_domain.Measurement(hg_runtime.now() - timedelta(days=1), 70, 23.5, "PICOOC")
        self.write_export([self.r])
        hg_state.set_status(self.db, self.r, "verified")
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        api = API([row(self.r)])
        self.sync(api)
        self.assertTrue(api.reads)
        self.assertFalse(api.posts)

    def test_repeated_sync_reuses_latest_plan_and_updates_snapshot(self):
        self.r = hg_domain.Measurement(hg_runtime.now() - timedelta(days=1), 70, 23.5, "PICOOC")
        self.write_export([self.r])
        source = (self.inbox / "health.json").read_bytes()
        api = API([row(self.r)])
        self.sync(api)
        path = self.state / "plans" / "sync-latest.json"
        first = json.loads(path.read_text())
        self.assertEqual(first["counts"], {"matched": 1})
        api.rows[0]["bodyFat"] = 24
        self.sync(api)
        latest = json.loads(path.read_text())
        self.assertEqual(list((self.state / "plans").iterdir()), [path])
        self.assertEqual(latest["counts"], {"replace": 1})
        self.assertEqual(latest["remote_snapshot"]["dateWeightList"], api.rows)
        summary = json.loads((self.state / "last_compare.json").read_text())
        self.assertEqual(summary["path"], str(path))
        self.assertEqual(summary["created_at"], latest["created_at"])
        self.assertEqual(summary["counts"], latest["counts"])
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified_conflict")
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)

    def test_manual_comparisons_survive_later_automatic_sync(self):
        api = API([row(self.r)])
        args = argparse.Namespace(since="2020-02-20", until="2020-02-20", inbox=None)
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_reconciliation.compare_exports(args, self.cfg)
            hg_reconciliation.compare_exports(args, self.cfg)
        saved = {p: p.read_bytes() for p in (self.state / "plans").glob("compare-*.json")}
        self.assertEqual(len(saved), 2)
        api.rows[0]["bodyFat"] = None
        self.sync(api)
        self.assertEqual({p: p.read_bytes() for p in saved}, saved)
        summary = json.loads((self.state / "last_compare.json").read_text())
        self.assertEqual(Path(summary["path"]), self.state / "plans" / "sync-latest.json")
        self.reviewed(api, Path(summary["path"]))
        self.assertEqual(len(api.deletes), 1)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        self.assertEqual({p: p.read_bytes() for p in saved}, saved)

    def test_latest_plan_replace_failure_preserves_evidence_and_stops_upload(self):
        api = API([row(self.r)])
        self.sync(api)
        paths = [
            self.state / "plans" / "sync-latest.json",
            self.state / "last_compare.json",
            self.state / "last_sync_comparison.json",
        ]
        saved = {p: p.read_bytes() for p in paths}
        other = hg_domain.Measurement(AT + timedelta(hours=1), 71, 24, "PICOOC")
        self.write_export([self.r, other])
        original_replace = Path.replace

        def fail_latest(path, target):
            if Path(target) == paths[0]:
                raise OSError("simulated disk failure")
            return original_replace(path, target)

        with patch.object(Path, "replace", fail_latest), self.assertRaises(OSError):
            self.sync(api)
        self.assertEqual({p: p.read_bytes() for p in paths}, saved)
        self.assertIsNone(hg_state.state_row(self.db, other))
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_explicit_historical_compare_ignores_incremental_marker(self):
        hg_state.set_status(self.db, self.r, "verified")
        hg_file_io.write_json(self.state / "last_sync_comparison.json", {})
        api = API([row(self.r)])
        args = argparse.Namespace(since="2020-02-20", until="2020-02-20", inbox=None)
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            plan = hg_reconciliation.compare_exports(args, self.cfg)
        self.assertEqual(plan["counts"], {"matched": 1})
        self.assertTrue(api.reads)
        self.assertFalse(api.posts)

    def test_one_batch_reads_many_existing_records(self):
        records = [
            hg_domain.Measurement(AT + timedelta(minutes=i * 10), 70 + i, 23.5, "PICOOC")
            for i in range(6)
        ]
        self.write_export(records)
        for r in records:
            hg_state.set_status(self.db, r, "verified")
        api = API([row(r, i + 1) for i, r in enumerate(records)])
        self.sync(api)
        self.assertEqual(len(api.reads), 1)
        self.assertFalse(api.posts)

    def test_missing_only_is_uploaded(self):
        other = hg_domain.Measurement(AT + timedelta(hours=1), 71, 24, "PICOOC")
        self.write_export([self.r, other])
        api = API([row(self.r)])
        self.sync(api)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]["weight"], 71)

    def test_uncertain_recovers_from_batch_without_post(self):
        hg_state.set_status(self.db, self.r, "uncertain")
        api = API([row(self.r)])
        self.sync(api)
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        self.assertFalse(api.posts)

    def test_uncertain_absent_does_not_post(self):
        hg_state.set_status(self.db, self.r, "uncertain")
        api = API()
        self.sync(api)
        self.assertEqual(hg_state.get_status(self.db, self.r), "uncertain")
        self.assertFalse(api.posts)

    def test_source_overlap_cannot_clear_uncertain_upload_protection(self):
        overlapping = hg_domain.Measurement(AT + timedelta(milliseconds=500), 71, 24, "PICOOC")
        for status in ("uncertain", "inflight", "repair_uncertain"):
            with self.subTest(status=status):
                hg_state.set_status(self.db, self.r, status)
                self.write_export([self.r, overlapping])
                api = API()
                self.sync(api)
                self.assertFalse(api.posts)
                self.assertEqual(hg_state.get_status(self.db, self.r), status)
                summary = json.loads((self.state / "last_run.json").read_text())
                self.assertEqual(summary["results"][self.r.key], status)
                self.assertEqual(summary["comparison"], {"conflict": 2})

                # A corrected export removes the ambiguity, but an empty read
                # still cannot prove that the previous request did not succeed.
                self.write_export([self.r])
                self.sync(api)
                self.assertFalse(api.posts)
                self.assertEqual(hg_state.get_status(self.db, self.r), "uncertain")
                summary = json.loads((self.state / "last_run.json").read_text())
                self.assertEqual(summary["results"][self.r.key], "uncertain")
                self.assertEqual(summary["comparison"], {"uncertain": 1})

    def test_review_actions_report_persisted_uncertain_markers_without_submission(self):
        for marker in ("inflight", "uncertain", "repair_uncertain"):
            for action in ("repair", "replace", "conflict"):
                with self.subTest(marker=marker, action=action):
                    hg_state.set_status(self.db, self.r, marker)
                    plan = self.plan([])
                    plan["entries"][0]["action"] = action
                    plan["counts"] = {action: 1}
                    api = API()
                    with patch.object(
                        hg_reconciliation, "load_local_comparison", return_value=plan
                    ):
                        self.sync(api)
                    summary = json.loads((self.state / "last_run.json").read_text())
                    self.assertEqual(summary["results"][self.r.key], marker)
                    self.assertEqual(summary["comparison"], {action: 1})
                    self.assertEqual(hg_state.get_status(self.db, self.r), marker)
                    self.assertFalse(api.posts)
                    self.assertFalse(api.deletes)

    def test_every_chunk_is_validated_before_first_write(self):
        other = hg_domain.Measurement(AT + timedelta(days=60), 71, 24, "PICOOC")
        self.write_export([self.r, other])
        api = API()
        api.fail_chunk = 2
        with self.assertRaises(hg_domain.SafeError):
            self.sync(api)
        self.assertFalse(api.posts)

    def test_snapshot_preserves_anonymous_multiplicity_and_only_deduplicates_known_ids(self):
        known = row(self.r)
        for missing_key in (False, True):
            with self.subTest(missing_key=missing_key):
                anonymous = dict(known)
                if missing_key:
                    del anonymous["samplePk"]
                else:
                    anonymous["samplePk"] = None
                api = Mock(spec_set=["get_body_composition"])
                api.get_body_composition.return_value = {
                    "dateWeightList": [anonymous, dict(anonymous), known]
                }
                # Exercise the generic merge contract over two returned chunks;
                # the validated SDK range endpoint uses disjoint calendar days.
                snapshot = hg_remote.fetch_remote_snapshot(
                    api, AT.date(), (AT + timedelta(days=45)).date()
                )
                self.assertEqual(api.get_body_composition.call_count, 2)
                self.assertEqual(len(snapshot["dateWeightList"]), 5)
                self.assertEqual(
                    sum(item.get("samplePk") is None for item in snapshot["dateWeightList"]), 4
                )
                self.assertEqual(snapshot["dateWeightList"].count(known), 1)
                self.assertEqual(hg_remote.classify_remote(self.r, snapshot), "conflict")

    def test_snapshot_single_anonymous_matches_and_changed_known_identity_still_stops(self):
        known = row(self.r)
        for missing_key in (False, True):
            with self.subTest(missing_key=missing_key):
                anonymous = dict(known)
                if missing_key:
                    del anonymous["samplePk"]
                else:
                    anonymous["samplePk"] = None
                api = Mock(spec_set=["get_body_composition"])
                api.get_body_composition.return_value = {"dateWeightList": [anonymous]}
                snapshot = hg_remote.fetch_remote_snapshot(api, AT.date(), AT.date())
                self.assertEqual(snapshot["dateWeightList"], [anonymous])
                self.assertEqual(hg_remote.classify_remote(self.r, snapshot), "matched")
        api.get_body_composition.side_effect = [
            {"dateWeightList": [known]},
            {"dateWeightList": [dict(known, weight=71000)]},
        ]
        with self.assertRaises(hg_domain.SafeError):
            hg_remote.fetch_remote_snapshot(api, AT.date(), (AT + timedelta(days=45)).date())

    def test_unknown_remote_timestamp_stops_entire_comparison(self):
        item = row(self.r)
        item["timestampGMT"] = "unparseable"
        with self.assertRaises(hg_domain.SafeError):
            self.plan([item])

    def test_multiple_candidates_block_matching(self):
        self.assertEqual(self.action([row(self.r, 1), row(self.r, 2)]), "conflict")

    def test_time_drift_never_matches_only_equal_weight(self):
        item = row(self.r)
        item["timestampGMT"] += 30000
        self.assertEqual(self.action([item]), "conflict")

    def test_two_apple_records_cannot_claim_same_remote_record(self):
        other = hg_domain.Measurement(AT, 70, 23.5, "Other")
        plan = self.plan([row(self.r)], [self.r, other])
        self.assertEqual([e["action"] for e in plan["entries"]], ["conflict", "conflict"])

    def test_overlapping_source_records_not_both_uploaded_into_empty_remote(self):
        other = hg_domain.Measurement(AT, 71, 24, "Other")
        plan = self.plan([], [self.r, other])
        self.assertEqual([e["action"] for e in plan["entries"]], ["conflict", "conflict"])

    def test_extras_exclude_padding_and_ambiguous_candidates(self):
        adjacent = hg_domain.Measurement(AT - timedelta(days=1), 69, 23, "PICOOC")
        ambiguous = row(self.r)
        ambiguous["timestampGMT"] += 30000
        plan = self.plan([row(adjacent), ambiguous])
        self.assertEqual(plan["extra_remote"], [])

    def test_exact_match_does_not_hide_unclaimed_nearby_orphan(self):
        orphan = row(hg_domain.Measurement(AT + timedelta(seconds=30), 75, None, "PICOOC"), 101)
        plan = self.plan([row(self.r), orphan])
        self.assertEqual(plan["counts"], {"matched": 1})
        self.assertEqual(plan["extra_remote"], [orphan])

    def test_legitimate_close_source_targets_claim_their_own_rows(self):
        other = hg_domain.Measurement(AT + timedelta(seconds=30), 71, 24, "PICOOC")
        plan = self.plan([row(self.r), row(other, 101)], [self.r, other])
        self.assertEqual(plan["counts"], {"matched": 2})
        self.assertEqual(plan["extra_remote"], [])

    def test_ambiguous_exact_rows_keep_all_nearby_candidates_protected(self):
        orphan = row(hg_domain.Measurement(AT + timedelta(seconds=30), 75, None, "PICOOC"), 102)
        plan = self.plan([row(self.r, 100), row(self.r, 101), orphan])
        self.assertEqual(plan["counts"], {"conflict": 1})
        self.assertEqual(plan["extra_remote"], [])

    def test_unknown_exact_values_keep_nearby_rows_protected(self):
        unknown = dict(row(self.r), weight="unknown")
        nearby = row(hg_domain.Measurement(AT + timedelta(seconds=30), 75, None, "PICOOC"), 101)
        plan = self.plan([unknown, nearby])
        self.assertEqual(plan["counts"], {"conflict": 1})
        self.assertEqual(plan["extra_remote"], [])

    def test_overlapping_source_targets_do_not_make_nearby_rows_deletable(self):
        other = hg_domain.Measurement(AT + timedelta(milliseconds=500), 71, 24, "Other")
        nearby = row(hg_domain.Measurement(AT + timedelta(seconds=30), 75, None, "PICOOC"), 101)
        plan = self.plan([row(self.r), nearby], [self.r, other])
        self.assertEqual(plan["counts"], {"conflict": 2})
        self.assertEqual(plan["extra_remote"], [])

    def test_extra_retained_in_automatic_run(self):
        extra = hg_domain.Measurement(AT + timedelta(hours=1), 71, None, "PICOOC")
        api = API([row(self.r), row(extra, 101)])
        self.sync(api)
        self.assertFalse(api.deletes)
        self.assertEqual(len(api.rows), 2)

    def test_nearby_extra_retained_in_automatic_run(self):
        extra = hg_domain.Measurement(AT + timedelta(seconds=30), 71, None, "PICOOC")
        api = API([row(self.r), row(extra, 101)])
        self.sync(api)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)
        self.assertEqual(len(api.rows), 2)
        path = json.loads((self.state / "last_compare.json").read_text())["path"]
        self.assertEqual(len(json.loads(Path(path).read_text())["extra_remote"]), 1)

    def test_automatic_missing_target_with_known_neighbor_still_requires_review(self):
        other = hg_domain.Measurement(AT + timedelta(seconds=30), 71, 24, "PICOOC")
        self.write_export([self.r, other])
        api = API([row(other, 101)])
        self.sync(api)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertEqual(hg_state.get_status(self.db, self.r), "conflict")

    def test_weight_only_history_pairs_and_uploads(self):
        self.r = hg_domain.Measurement(AT, 70, None, "PICOOC")
        self.write_export([self.r])
        api = API()
        self.sync(api)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]["unitKey"], "kg")
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")

    def test_weight_and_bmi_without_fat_uploads_available_fields(self):
        self.r = hg_domain.Measurement(AT, 70, None, "PICOOC", 22)
        self.write_export([self.r])
        api = API()
        self.sync(api)
        self.assertEqual(api.posts[0]["bmi"], 22)
        self.assertIsNone(api.posts[0]["percent_fat"])
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")

    def test_weight_only_does_not_fallback_on_ambiguous_fat(self):
        samples = [
            hg_domain.Sample("weight", AT, 70, "PICOOC"),
            hg_domain.Sample("body_fat", AT - timedelta(seconds=5), 23, "PICOOC"),
            hg_domain.Sample("body_fat", AT + timedelta(seconds=5), 24, "PICOOC"),
        ]
        records, waiting = hg_exports.pair_samples(samples, 10, allow_weight_only=True)
        self.assertFalse(records)
        self.assertTrue(waiting)

    def test_revised_local_core_can_be_compared(self):
        old = hg_domain.Measurement(AT, 69, 23.5, "PICOOC")
        hg_state.set_status(self.db, old, "verified")
        self.assertEqual(self.action([row(old)]), "replace")
        with contextlib.redirect_stdout(io.StringIO()):
            hg_sync.show_preview(self.cfg)

    def test_reviewed_repair_backs_up_and_replaces_exact_record(self):
        original = row(self.r)
        original["bodyFat"] = None
        path = self.plan([original], save=True)
        api = API([original])
        self.reviewed(api, path)
        self.assertEqual(api.deletes, [("100", "2020-02-20")])
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        backups = list((self.state / "backups").glob("*/remote.json"))
        self.assertEqual(json.loads(backups[0].read_text())["dateWeightList"], [original])
        self.assertTrue(list((self.state / "backups").glob("*/sync.sqlite3")))

    def test_reviewed_repair_rejects_unknown_neighbor_before_deleting_original(self):
        original = dict(row(self.r), bodyFat=None)
        neighbor = row(hg_domain.Measurement(AT + timedelta(seconds=30), 75, None, "PICOOC"), 101)
        path = self.plan([original, neighbor], save=True)
        api = API([original, neighbor])
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)
        self.assertEqual(api.rows, [original, neighbor])
        self.assertFalse(list((self.state / "operations").glob("*.json")))

    def test_reviewed_repair_preserves_uniquely_matched_reviewed_neighbor(self):
        other = hg_domain.Measurement(AT + timedelta(seconds=30), 71, 24, "PICOOC")
        self.write_export([self.r, other])
        original = dict(row(self.r), bodyFat=None)
        neighbor = row(other, 101)
        path = self.plan([original, neighbor], [self.r, other], save=True)
        api = API([original, neighbor])
        self.reviewed(api, path)
        self.assertEqual(api.deletes, [("100", "2020-02-20")])
        self.assertEqual(len(api.posts), 1)
        self.assertIn(neighbor, api.rows)
        self.assertEqual(len(api.rows), 2)
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")

    def test_confirmation_time_new_neighbor_prevents_deletion(self):
        original = dict(row(self.r), bodyFat=None)
        neighbor = row(hg_domain.Measurement(AT + timedelta(seconds=30), 75, None, "PICOOC"), 101)
        path = self.plan([original], save=True)
        api = API([original])
        args = argparse.Namespace(plan=str(path), record_id=self.r.key)

        def confirm(_):
            api.rows.append(neighbor)
            return "REPLACE " + self.r.key

        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            patch("builtins.input", side_effect=confirm),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(hg_domain.SafeError):
                hg_operations.reviewed_operation(args, self.cfg)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)
        self.assertEqual(api.rows, [original, neighbor])

    def test_reviewed_repair_rejects_ambiguous_or_unknown_source_neighbor(self):
        other = hg_domain.Measurement(AT + timedelta(seconds=30), 71, 24, "PICOOC")
        self.write_export([self.r, other])
        original = dict(row(self.r), bodyFat=None)
        for neighbors in (
            [row(other, 101), row(other, 102)],
            [dict(row(other, 101), weight="unknown")],
        ):
            with self.subTest(neighbors=neighbors):
                rows = [original, *neighbors]
                path = self.plan(rows, [self.r, other], save=True)
                api = API(rows)
                with self.assertRaises(hg_domain.SafeError):
                    self.reviewed(api, path)
                self.assertFalse(api.deletes)
                self.assertFalse(api.posts)
                self.assertEqual(api.rows, rows)

    def test_new_source_neighbor_not_in_reviewed_plan_cannot_allow_replacement(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.plan([original], save=True)
        other = hg_domain.Measurement(AT + timedelta(seconds=30), 71, 24, "PICOOC")
        self.write_export([self.r, other])
        api = API([original, row(other, 101)])
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)

    def test_new_overlapping_source_cannot_make_neighbor_look_unique(self):
        other = hg_domain.Measurement(AT + timedelta(seconds=30), 71, 24, "PICOOC")
        original = dict(row(self.r), bodyFat=None)
        neighbor = row(other, 101)
        path = self.plan([original, neighbor], [self.r, other], save=True)
        overlapping = hg_domain.Measurement(
            other.at + timedelta(milliseconds=500), 72, 25, "PICOOC"
        )
        self.write_export([self.r, other, overlapping])
        api = API([original, neighbor])
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.deletes)
        self.assertFalse(api.posts)

    def test_neighbor_appearing_after_delete_still_prevents_post(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.plan([original], save=True)
        api = API([original])
        delete = api.delete_weigh_in
        neighbor = row(hg_domain.Measurement(AT + timedelta(seconds=30), 75, None, "PICOOC"), 101)

        def delete_and_add_neighbor(pk, day):
            delete(pk, day)
            api.rows.append(neighbor)

        with patch.object(api, "delete_weigh_in", side_effect=delete_and_add_neighbor):
            with self.assertRaises(hg_domain.SafeError):
                self.reviewed(api, path)
        self.assertEqual(api.deletes, [("100", "2020-02-20")])
        self.assertFalse(api.posts)
        self.assertEqual(api.rows, [neighbor])
        self.assertEqual(
            json.loads(next((self.state / "operations").glob("*.json")).read_text())["phase"],
            "uncertain",
        )

    def test_reviewed_repair_refuses_changed_remote(self):
        original = row(self.r)
        original["bodyFat"] = None
        path = self.plan([original], save=True)
        changed = dict(original, weight=71000)
        api = API([changed])
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.deletes)

    def test_reviewed_repair_refuses_changed_apple(self):
        original = row(self.r)
        original["bodyFat"] = None
        path = self.plan([original], save=True)
        self.write_export([hg_domain.Measurement(AT, 71, 23.5, "PICOOC")])
        api = API([original])
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.deletes)

    def test_delete_timeout_never_proceeds_to_upload_or_redelete(self):
        original = row(self.r)
        original["bodyFat"] = None
        path = self.plan([original], save=True)
        api = API([original])
        api.delete_timeout = True
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.posts)
        self.assertEqual(hg_state.get_status(self.db, self.r), "repair_uncertain")
        journal = json.loads(next((self.state / "operations").glob("*.json")).read_text())
        self.assertNotIn("secret-ticket", json.dumps(journal))
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertEqual(len(api.deletes), 1)

    def test_delete_extra_blocks_issued_full_deletes_and_single_canonical_aliases(self):
        for mode in ("full", "full_verified_delete", "single_alias", "unavailable_backup"):
            with self.subTest(mode=mode), patch.object(hg_runtime, "STATE", self.state / mode):
                hg_file_io.private_dir(hg_runtime.STATE)
                self.write_export([self.r])
                extra = row(
                    hg_domain.Measurement(AT + timedelta(hours=1), 74, None, "PICOOC"),
                    "00100" if mode == "single_alias" else 100,
                )
                untouched = row(
                    hg_domain.Measurement(AT + timedelta(hours=2), 75, None, "PICOOC"), 101
                )
                backend = API(
                    [row(self.r, 300), extra, untouched]
                    if mode == "single_alias"
                    else [extra, untouched]
                )
                api = self.native_deletion_api(
                    backend, fail_at=2 if mode == "full_verified_delete" else 1
                )
                journal = self.incomplete_deletion(
                    api,
                    full=mode != "single_alias",
                    remote_id="00100" if mode == "single_alias" else "100",
                )
                if mode == "full_verified_delete":
                    self.assertIn(
                        {"kind": "delete_verified", "id": "100"},
                        [
                            {key: event[key] for key in ("kind", "id")}
                            for event in hg_state.read_operation(journal)["events"]
                        ],
                    )
                    backend.rows.append(extra)  # A completed DELETE can still be visible again.
                elif mode == "single_alias":
                    backend.rows[1]["samplePk"] = 100
                plan = self.deletion_plan(api)
                original_journal, original_rows = journal.read_bytes(), copy.deepcopy(backend.rows)
                source = (self.inbox / "health.json").read_bytes()
                token = hg_runtime.STATE / "tokens" / "synthetic"
                hg_file_io.private_dir(token.parent)
                token.write_bytes(b"synthetic private token")
                deletes = list(backend.deletes)
                journals = list((hg_runtime.STATE / "operations").glob("*.json"))
                native_read = hg_file_io.read_regular_text

                def unavailable_backup(path, **kwargs):
                    if mode == "unavailable_backup" and path.name == "remote-before.json":
                        raise FileNotFoundError("private unavailable path")
                    return native_read(path, **kwargs)

                args = argparse.Namespace(plan=str(plan), remote_id="100", complete_export=True)
                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    patch.object(hg_file_io, "read_regular_text", side_effect=unavailable_backup),
                    contextlib.redirect_stdout(io.StringIO()),
                    patch(
                        "builtins.input",
                        side_effect=AssertionError("Must stop before confirmation"),
                    ) as prompt,
                ):
                    with self.assertRaises(hg_domain.SafeError):
                        hg_operations.reviewed_operation(args, self.cfg, True)
                prompt.assert_not_called()
                self.assertEqual(backend.deletes, deletes)
                self.assertFalse(backend.posts)
                self.assertEqual(backend.rows, original_rows)
                self.assertEqual(journal.read_bytes(), original_journal)
                self.assertEqual(list((hg_runtime.STATE / "operations").glob("*.json")), journals)
                with contextlib.closing(hg_state.database()) as db:
                    self.assertIsNone(hg_state.state_row(db, self.r))
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(token.read_bytes(), b"synthetic private token")

    def test_deletion_guards_allow_unrelated_dates_ids_and_closed_partial_batch(self):
        for mode in ("other_id", "full_other_date", "single_other_date", "closed_partial"):
            with self.subTest(mode=mode), patch.object(hg_runtime, "STATE", self.state / mode):
                from health_garmin_sync import history as replace_history

                hg_file_io.private_dir(hg_runtime.STATE)
                self.write_export([self.r])
                extra = row(
                    hg_domain.Measurement(AT + timedelta(hours=1), 74, None, "PICOOC"),
                    "00100" if mode == "single_other_date" else 100,
                )
                second_extra = row(
                    hg_domain.Measurement(AT + timedelta(hours=2), 75, None, "PICOOC"), 101
                )
                backend = API(
                    [row(self.r, 300), extra]
                    if mode == "single_other_date"
                    else [extra]
                    if mode == "closed_partial"
                    else [extra, second_extra]
                )
                api = self.native_deletion_api(
                    backend, fail_at=None if mode == "closed_partial" else 1
                )
                if mode == "closed_partial":
                    native_get, after_delete_reads = backend.get_body_composition, 0

                    def fail_before_upload(start, end):
                        nonlocal after_delete_reads
                        if backend.deletes:
                            after_delete_reads += 1
                            if after_delete_reads == 2:
                                raise TimeoutError("synthetic pre-upload query failure")
                        return native_get(start, end)

                    backend.get_body_composition = fail_before_upload
                journal = self.incomplete_deletion(
                    api,
                    full=mode != "single_other_date",
                    remote_id="00100" if mode == "single_other_date" else "100",
                )
                records = [self.r]
                if mode == "closed_partial":
                    with contextlib.redirect_stdout(io.StringIO()):
                        replace_history.close_verified_partial(
                            journal, dict(self.cfg, enabled=False), api
                        )
                    self.assertEqual(hg_state.read_operation(journal)["phase"], "closed_partial")
                    backend.rows.append(extra)
                elif mode.endswith("other_date"):
                    other = hg_domain.Measurement(AT + timedelta(days=1), 72, 25, "PICOOC")
                    records.append(other)
                    self.write_export(records)
                    backend.rows = [item for item in backend.rows if int(item["samplePk"]) != 100]
                    backend.rows.append(
                        row(
                            hg_domain.Measurement(
                                other.at + timedelta(hours=1), 74, None, "PICOOC"
                            ),
                            100,
                        )
                    )
                original_journal = journal.read_bytes()
                source = (self.inbox / "health.json").read_bytes()
                token = hg_runtime.STATE / "tokens" / "synthetic"
                hg_file_io.private_dir(token.parent)
                token.write_bytes(b"synthetic private token")
                remote_id = "101" if mode == "other_id" else "100"
                plan = self.deletion_plan(api, records)
                native_read = hg_file_io.read_regular_text

                def forbid_unrelated_backup(path, **kwargs):
                    if mode in ("other_id", "closed_partial") and path.name == "remote-before.json":
                        raise AssertionError("Unrelated or terminal backup must not be read")
                    return native_read(path, **kwargs)

                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    patch.object(
                        hg_file_io, "read_regular_text", side_effect=forbid_unrelated_backup
                    ),
                    patch("builtins.input", return_value="DELETE " + remote_id),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    hg_operations.reviewed_operation(
                        argparse.Namespace(
                            plan=str(plan), remote_id=remote_id, complete_export=True
                        ),
                        self.cfg,
                        True,
                    )
                self.assertEqual(len(backend.deletes), 2)
                expected_day = records[-1].at.date().isoformat()
                self.assertEqual(backend.deletes[-1], (remote_id, expected_day))
                self.assertFalse(backend.posts)
                self.assertEqual(journal.read_bytes(), original_journal)
                self.assertEqual((self.inbox / "health.json").read_bytes(), source)
                self.assertEqual(token.read_bytes(), b"synthetic private token")

    def test_delete_extra_requires_complete_export_confirmation(self):
        extra = hg_domain.Measurement(AT + timedelta(hours=1), 71, None, "PICOOC")
        rows = [row(self.r, 101), row(extra, 100)]
        path = self.plan(rows, save=True)
        api = API(rows)
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path, delete_extra=True, complete=False)
        self.assertFalse(api.deletes)

    def test_delete_extra_only_deletes_selected_snapshot_id(self):
        extra = hg_domain.Measurement(AT + timedelta(hours=1), 71, None, "PICOOC")
        rows = [row(self.r, 101), row(extra, 100)]
        path = self.plan(rows, save=True)
        api = API(rows)
        self.reviewed(api, path, delete_extra=True)
        self.assertEqual(api.deletes, [("100", "2020-02-20")])
        self.assertEqual([r["samplePk"] for r in api.rows], [101])
        self.assertFalse(api.posts)

    def test_extra_that_now_matches_apple_cannot_be_deleted(self):
        extra = hg_domain.Measurement(AT + timedelta(hours=1), 71, None, "PICOOC")
        rows = [row(self.r, 101), row(extra, 100)]
        path = self.plan(rows, save=True)
        self.write_export([self.r, extra])
        api = API(rows)
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path, delete_extra=True)
        self.assertFalse(api.deletes)

    def test_unsupported_optional_remote_metrics_prevent_loss(self):
        original = dict(row(self.r), bodyFat=None, muscleMass=40000)
        path = self.plan([original], save=True)
        api = API([original])
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.deletes)

    def test_invalid_preserved_fat_is_rejected_before_deletion(self):
        self.r = hg_domain.Measurement(AT, 71, None, "PICOOC")
        self.write_export([self.r])
        original = dict(row(self.r), weight=70000, bodyFat="invalid")
        path = self.plan([original], save=True)
        api = API([original])
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, path)
        self.assertFalse(api.deletes)

    def test_valid_optional_remote_values_preserved_on_weight_correction(self):
        self.r = hg_domain.Measurement(AT, 71, None, "PICOOC")
        self.write_export([self.r])
        original = dict(row(self.r), weight=70000, bodyFat=23.5, bmi=22)
        path = self.plan([original], save=True)
        api = API([original])
        self.reviewed(api, path)
        self.assertEqual(api.posts[0]["percent_fat"], 23.5)
        self.assertEqual(api.posts[0]["bmi"], 22)

    def test_operation_verification_is_read_only_and_recovers_delayed_result(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.state / "operations" / "test.json"
        hg_file_io.write_json(
            path,
            {
                "key": self.r.key,
                "phase": "uncertain",
                "original": original,
                "measurement": self.r.payload(),
                "replacement": self.r.payload(),
            },
        )
        api = API([row(self.r, 201)])
        args = argparse.Namespace(operation=str(path))
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.verify_operation(args)
        self.assertEqual(json.loads(path.read_text())["phase"], "verified")
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_delete_extra_stays_uncertain_when_post_delete_original_has_no_id(self):
        extra = hg_domain.Measurement(AT + timedelta(hours=1), 71, 24, "PICOOC")
        original = row(extra, 100)

        class AnonymousReadback(API):
            def delete_weigh_in(api, pk, day):
                super().delete_weigh_in(pk, day)
                api.rows.append(dict(original, samplePk=None))

        api = AnonymousReadback([row(self.r, 101), original])
        plan = self.plan(api.rows, save=True)
        source = (self.inbox / "health.json").read_bytes()
        tokens = self.state / "tokens" / "synthetic"
        hg_file_io.private_dir(tokens.parent)
        tokens.write_bytes(b"synthetic token")
        with self.assertRaisesRegex(hg_domain.SafeError, "操作结果未确定"):
            self.reviewed(api, plan, delete_extra=True)
        journal = next((self.state / "operations").glob("*.json"))
        self.assertEqual(json.loads(journal.read_text())["phase"], "uncertain")
        self.assertEqual(len(api.deletes), 1)
        self.assertFalse(api.posts)
        self.assertIsNone(hg_state.state_row(self.db, self.r))
        args = argparse.Namespace(operation=str(journal))
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(hg_domain.SafeError):
                hg_operations.verify_operation(args)
            self.assertIs(json.loads(journal.read_text())["last_check_passed"], False)
            api.rows = [row(self.r, 101), dict(original, samplePk="00100")]
            self.assertIs(hg_operations.verify_operation(args), False)
            api.rows = [row(self.r, 101)]
            self.assertIs(hg_operations.verify_operation(args), True)
        self.assertEqual(json.loads(journal.read_text())["phase"], "deleted_verified")
        self.assertEqual(len(api.deletes), 1)
        self.assertFalse(api.posts)
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)
        self.assertEqual(tokens.read_bytes(), b"synthetic token")

    def test_repair_does_not_post_after_delete_readback_loses_original_id(self):
        original = dict(row(self.r), bodyFat=None)
        anonymous = dict(original)
        anonymous.pop("samplePk")

        class AnonymousReadback(API):
            def delete_weigh_in(api, pk, day):
                super().delete_weigh_in(pk, day)
                api.rows.append(anonymous)

        api = AnonymousReadback([original])
        plan = self.plan(api.rows, save=True)
        source = (self.inbox / "health.json").read_bytes()
        tokens = self.state / "tokens" / "synthetic"
        hg_file_io.private_dir(tokens.parent)
        tokens.write_bytes(b"synthetic token")
        with self.assertRaisesRegex(hg_domain.SafeError, "操作结果未确定"):
            self.reviewed(api, plan)
        journal = next((self.state / "operations").glob("*.json"))
        self.assertEqual(json.loads(journal.read_text())["phase"], "uncertain")
        self.assertEqual(hg_state.get_status(self.db, self.r), "repair_uncertain")
        self.assertEqual(len(api.deletes), 1)
        self.assertFalse(api.posts)
        args = argparse.Namespace(operation=str(journal))
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertIs(hg_operations.verify_operation(args), False)
            self.assertEqual(hg_state.get_status(self.db, self.r), "repair_uncertain")
            api.rows = [row(self.r, 201)]
            self.assertIs(hg_operations.verify_operation(args), True)
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        self.assertEqual(json.loads(journal.read_text())["phase"], "verified")
        self.assertEqual(len(api.deletes), 1)
        self.assertFalse(api.posts)
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)
        self.assertEqual(tokens.read_bytes(), b"synthetic token")

    def test_cancelled_repair_does_not_modify_remote(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.plan([original], save=True)
        api = API([original])
        args = argparse.Namespace(plan=str(path), record_id=self.r.key)
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            patch("builtins.input", return_value="CANCEL"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.reviewed_operation(args, self.cfg)
        self.assertFalse(api.deletes)
        self.assertFalse(list((self.state / "operations").glob("*.json")))

    def test_remote_change_during_confirmation_prevents_deletion(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.plan([original], save=True)
        api = API([original])
        args = argparse.Namespace(plan=str(path), record_id=self.r.key)

        def confirm(_):
            api.rows[0]["bodyFat"] = 24
            return "REPLACE " + self.r.key

        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            patch("builtins.input", side_effect=confirm),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(hg_domain.SafeError):
                hg_operations.reviewed_operation(args, self.cfg)
        self.assertFalse(api.deletes)

    def test_incomplete_journal_blocks_even_pending_automatic_upload(self):
        hg_file_io.write_json(
            self.state / "operations" / "test.json", {"key": self.r.key, "phase": "uploading"}
        )
        hg_state.set_status(self.db, self.r, "pending")
        api = API()
        self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(hg_state.get_status(self.db, self.r), "repair_uncertain")

    def test_reviewed_resume_preserves_optional_values_and_never_deletes(self):
        self.r = hg_domain.Measurement(AT, 71, None, "PICOOC")
        replacement = hg_domain.Measurement(AT, 71, 23.5, "PICOOC", 22)
        original = dict(row(replacement), weight=70000)
        path = self.state / "operations" / "test.json"
        hg_file_io.write_json(
            path,
            {
                "key": self.r.key,
                "phase": "uncertain",
                "original": original,
                "measurement": self.r.payload(),
                "replacement": replacement.payload(),
            },
        )
        hg_state.set_status(self.db, self.r, "repair_uncertain")
        api = API()
        args = argparse.Namespace(operation=str(path))
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            patch("builtins.input", return_value="RESUME " + self.r.key),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_operations.resume_operation(args)
        self.assertEqual(api.posts[0]["percent_fat"], 23.5)
        self.assertEqual(api.posts[0]["bmi"], 22)
        self.assertFalse(api.deletes)
        self.assertEqual(json.loads(path.read_text())["phase"], "verified")

    def test_resume_refuses_if_original_record_still_exists(self):
        original = dict(row(self.r), bodyFat=None)
        path = self.state / "operations" / "test.json"
        hg_file_io.write_json(
            path,
            {
                "key": self.r.key,
                "phase": "uncertain",
                "original": original,
                "measurement": self.r.payload(),
                "replacement": self.r.payload(),
            },
        )
        api = API([original])
        args = argparse.Namespace(operation=str(path))
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(hg_domain.SafeError):
                hg_operations.resume_operation(args)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_resume_requires_identifiable_rows_before_and_after_confirmation(self):
        original = dict(row(self.r), bodyFat=None)
        extra = hg_domain.Measurement(self.r.at + timedelta(hours=2), 73, None, "PICOOC")

        class UnknownAfterDelete(API):
            def delete_weigh_in(api, pk, day):
                super().delete_weigh_in(pk, day)
                api.rows[0]["samplePk"] = None

        api = UnknownAfterDelete([original, row(extra, 300)])
        plan = self.plan(api.rows, save=True)
        source = (self.inbox / "health.json").read_bytes()
        token = self.state / "tokens" / "synthetic"
        hg_file_io.private_dir(token.parent)
        token.write_bytes(b"synthetic token")
        with self.assertRaises(hg_domain.SafeError):
            self.reviewed(api, plan)
        journal = next((self.state / "operations").glob("*.json"))
        before = journal.read_bytes()
        local_before = dict(hg_state.state_row(self.db, self.r))
        self.assertEqual(json.loads(before)["phase"], "uncertain")
        self.assertEqual(hg_state.get_status(self.db, self.r), "repair_uncertain")
        self.assertEqual(len(api.deletes), 1)
        self.assertFalse(api.posts)
        args = argparse.Namespace(operation=str(journal))
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with patch(
                "builtins.input",
                side_effect=AssertionError("unknown identity must stop before confirmation"),
            ):
                with self.assertRaises(hg_domain.SafeError):
                    hg_operations.resume_operation(args)
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(dict(hg_state.state_row(self.db, self.r)), local_before)
            self.assertFalse(api.posts)
            api.rows = [row(extra, 300)]

            def confirm(_):
                api.rows[0].pop("samplePk")
                return "RESUME " + self.r.key

            with patch("builtins.input", side_effect=confirm) as prompt:
                with self.assertRaises(hg_domain.SafeError):
                    hg_operations.resume_operation(args)
                prompt.assert_called_once()
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(dict(hg_state.state_row(self.db, self.r)), local_before)
            self.assertFalse(api.posts)
            api.rows = [row(extra, 300)]
            with patch("builtins.input", return_value="RESUME " + self.r.key):
                hg_operations.resume_operation(args)
        self.assertEqual(json.loads(journal.read_text())["phase"], "verified")
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
        self.assertEqual(len(api.deletes), 1)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual((self.inbox / "health.json").read_bytes(), source)
        self.assertEqual(token.read_bytes(), b"synthetic token")

    def test_reviewed_neighbor_resume_reloads_actual_sources_and_preserves_legacy_plan(self):
        for name, alternate, legacy in (
            ("normal", False, False),
            ("alternate", True, False),
            ("legacy", False, True),
        ):
            with self.subTest(case=name), patch.object(hg_runtime, "STATE", self.state / name):
                hg_file_io.private_dir(hg_runtime.STATE)
                api, backend, journal, other, source = self.paused_reviewed_neighbor(
                    alternate=alternate, legacy=legacy
                )
                source_bytes = source.read_bytes()
                default_bytes = (self.inbox / "health.json").read_bytes()
                token = hg_runtime.STATE / "tokens" / "synthetic"
                hg_file_io.private_dir(token.parent)
                token.write_bytes(b"private synthetic token")
                backup = Path(hg_state.read_operation(journal)["backup"])
                saved = json.loads((backup / "plan.json").read_text())
                if legacy:
                    self.assertNotIn("config", saved)
                else:
                    self.assertEqual(saved["config"]["inbox"], str(source.parent))
                self.assertEqual(
                    json.loads((backup / "config.json").read_text())["inbox"], self.cfg["inbox"]
                )
                args = argparse.Namespace(operation=str(journal))
                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    contextlib.redirect_stdout(io.StringIO()),
                    patch("builtins.input", return_value="RESUME " + self.r.key),
                    patch.object(Path, "home", side_effect=AssertionError("No real home")),
                    patch("socket.create_connection", side_effect=AssertionError("No network")),
                    patch("subprocess.run", side_effect=AssertionError("No service")),
                ):
                    hg_operations.resume_operation(args)
                    native_read = hg_file_io.read_regular_text

                    def no_backup_read(path, **kwargs):
                        if path.parent == backup:
                            raise AssertionError("Matched needs no backup")
                        return native_read(path, **kwargs)

                    with (
                        patch.object(
                            hg_sources,
                            "load_measurements",
                            side_effect=AssertionError("Matched needs no source"),
                        ),
                        patch.object(hg_file_io, "read_regular_text", side_effect=no_backup_read),
                    ):
                        hg_operations.resume_operation(args)
                        self.assertTrue(hg_operations.verify_operation(args))
                self.assertEqual(len(backend.deletes), 1)
                self.assertEqual(len(backend.posts), 1)
                self.assertIn(row(other, 101), backend.rows)
                self.assertEqual(hg_state.read_operation(journal)["phase"], "verified")
                with contextlib.closing(hg_state.database()) as db:
                    self.assertEqual(hg_state.get_status(db, self.r), "verified")
                self.assertEqual(source.read_bytes(), source_bytes)
                self.assertEqual((self.inbox / "health.json").read_bytes(), default_bytes)
                self.assertEqual(token.read_bytes(), b"private synthetic token")

    def test_neighbor_resume_revalidates_initial_confirmation_and_post_preflight(self):
        changes = (
            "target",
            "target_overlap",
            "peer_source",
            "peer_overlap",
            "peer_remote",
            "unknown_id",
            "original",
        )
        scenarios = [(stage, change) for stage in (1, 2, 3) for change in changes]
        scenarios.append((3, "peer_disappears_target_changes"))
        for number, (stage, change) in enumerate(scenarios):
            with (
                self.subTest(read=stage, change=change),
                patch.object(hg_runtime, "STATE", self.state / str(number)),
            ):
                hg_file_io.private_dir(hg_runtime.STATE)
                api, backend, journal, other, source = self.paused_reviewed_neighbor()
                journal_before = journal.read_bytes()
                with contextlib.closing(hg_state.database()) as db:
                    local_before = dict(hg_state.state_row(db, self.r))
                token = hg_runtime.STATE / "tokens" / "synthetic"
                hg_file_io.private_dir(token.parent)
                token.write_bytes(b"private synthetic token")
                reads = 0
                expected_source = source.read_bytes()
                native_read = backend.get_body_composition

                def changing_read(start, end):
                    nonlocal reads, expected_source
                    reads += 1
                    if reads == stage:
                        if change in ("target", "peer_disappears_target_changes"):
                            self.write_export(
                                [hg_domain.Measurement(AT, 72, 23.5, "PICOOC"), other]
                            )
                            if change == "peer_disappears_target_changes":
                                backend.rows = []
                        elif change == "target_overlap":
                            overlap = hg_domain.Measurement(
                                AT + timedelta(milliseconds=500), 72, 25, "PICOOC"
                            )
                            self.write_export([self.r, other, overlap])
                        elif change == "peer_source":
                            self.write_export(
                                [self.r, hg_domain.Measurement(other.at, 72, 24, "PICOOC")]
                            )
                        elif change == "peer_overlap":
                            overlap = hg_domain.Measurement(
                                other.at + timedelta(milliseconds=500), 72, 25, "PICOOC"
                            )
                            self.write_export([self.r, other, overlap])
                        elif change == "peer_remote":
                            backend.rows[0]["weight"] = 72000
                        elif change == "unknown_id":
                            backend.rows[0]["samplePk"] = None
                        elif change == "original":
                            backend.rows.append(dict(row(self.r), bodyFat=None))
                        expected_source = source.read_bytes()
                    return native_read(start, end)

                backend.get_body_composition = changing_read
                args = argparse.Namespace(operation=str(journal))
                with (
                    patch.object(hg_garmin, "api_client", return_value=api),
                    contextlib.redirect_stdout(io.StringIO()),
                    patch("builtins.input", return_value="RESUME " + self.r.key) as prompt,
                    patch.object(Path, "home", side_effect=AssertionError("No real home")),
                    patch("socket.create_connection", side_effect=AssertionError("No network")),
                    patch("subprocess.run", side_effect=AssertionError("No service")),
                ):
                    with self.assertRaises(hg_domain.SafeError):
                        hg_operations.resume_operation(args)
                self.assertEqual(reads, stage)
                self.assertEqual(prompt.call_count, 0 if stage == 1 else 1)
                self.assertEqual(len(backend.deletes), 1)
                self.assertFalse(backend.posts)
                self.assertEqual(hg_state.read_operation(journal)["phase"], "uncertain")
                with contextlib.closing(hg_state.database()) as db:
                    self.assertEqual(hg_state.get_status(db, self.r), "repair_uncertain")
                    if stage < 3:
                        self.assertEqual(dict(hg_state.state_row(db, self.r)), local_before)
                        self.assertEqual(journal.read_bytes(), journal_before)
                self.assertEqual(source.read_bytes(), expected_source)
                self.assertEqual(token.read_bytes(), b"private synthetic token")

    def test_resume_preflight_error_survives_failure_journal_save_error(self):
        api, backend, journal, _, source = self.paused_reviewed_neighbor()
        source_bytes = source.read_bytes()
        primary = TimeoutError("private network detail")
        native_read, native_write = backend.get_body_composition, hg_file_io.write_json
        reads = 0

        def interrupted_read(start, end):
            nonlocal reads
            reads += 1
            if reads == 3:
                raise primary
            return native_read(start, end)

        backend.get_body_composition = interrupted_read

        def unavailable_journal(path, data):
            if path == journal and data.get("phase") == "uncertain":
                raise OSError("private storage detail")
            native_write(path, data)

        errors = io.StringIO()
        args = argparse.Namespace(operation=str(journal))
        with (
            patch.object(hg_garmin, "api_client", return_value=api),
            patch.object(hg_file_io, "write_json", side_effect=unavailable_journal),
            patch("builtins.input", return_value="RESUME " + self.r.key),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(errors),
        ):
            with self.assertRaises(TimeoutError) as failure:
                hg_operations.resume_operation(args)
        self.assertIs(failure.exception, primary)
        self.assertEqual(hg_state.get_status(self.db, self.r), "repair_uncertain")
        self.assertEqual(hg_state.read_operation(journal)["phase"], "uploading")
        self.assertIn(self.r.key, hg_state.active_operations())
        self.assertEqual(len(backend.deletes), 1)
        self.assertFalse(backend.posts)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertIn("OSError", errors.getvalue())
        self.assertNotIn("private", errors.getvalue())

    def test_disabled_run_remains_offline(self):
        self.cfg["enabled"] = False
        with (
            patch.object(hg_garmin, "api_client", side_effect=AssertionError("unexpected network")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_sync.run_sync(self.cfg)

    def test_settling_and_limit_defer_new_writes(self):
        current = hg_domain.aware("2026-10-02T10:00:00+08:00")
        ignored = self.r
        missing = hg_domain.Measurement(AT + timedelta(hours=1), 71, 24, "PICOOC")
        repair = hg_domain.Measurement(AT + timedelta(hours=2), 72, 25, "PICOOC")
        pending = [
            hg_domain.Measurement(AT + timedelta(hours=3), 73, 26, "PICOOC"),
            hg_domain.Measurement(AT + timedelta(hours=4), 74, 27, "PICOOC"),
            hg_domain.Measurement(current - timedelta(days=1), 75, 28, "PICOOC"),
        ]
        self.write_export([ignored, missing, repair, *pending])
        source = self.inbox / "health.json"
        data = json.loads(source.read_text())
        data["exported_at"] = current.isoformat()
        source.write_text(json.dumps(data))
        original_source = source.read_bytes()
        self.cfg.update(settle_seconds=120, max_records_per_run=1)
        for record, status in (
            (ignored, "ignored"),
            (missing, "remote_missing"),
            (repair, "repair_uncertain"),
        ):
            hg_state.set_status(self.db, record, status)
        journal = self.state / "operations" / "repair.json"
        hg_file_io.write_json(journal, {"key": repair.key, "phase": "uncertain"})
        original_journal = journal.read_bytes()
        api = API()
        native_process = hg_processing.process_record

        def process_without_wait(*args, **kwargs):
            return native_process(*args, **kwargs, sleep=lambda _: None)

        for elapsed, total_posts in ((119, 0), (120, 1), (121, 2), (122, 3), (123, 3), (124, 3)):
            with self.subTest(elapsed=elapsed):
                api.visible = elapsed != 120
                if elapsed == 123:
                    api.rows.append(row(pending[0], 900))
                with (
                    patch.object(
                        hg_runtime, "now", return_value=current + timedelta(seconds=elapsed)
                    ),
                    patch.object(hg_processing, "process_record", side_effect=process_without_wait),
                ):
                    self.sync(api)
                summary = json.loads((self.state / "last_run.json").read_text())
                self.assertEqual(len(api.posts), total_posts)
                self.assertEqual(summary.get("deferred"), "settling" if elapsed == 119 else None)
                for record, status in (
                    (ignored, "ignored"),
                    (missing, "remote_missing"),
                    (repair, "repair_uncertain"),
                ):
                    self.assertEqual(hg_state.get_status(self.db, record), status)
                self.assertEqual(summary["results"][missing.key], "remote_missing")
                self.assertEqual(summary["results"][repair.key], "repair_uncertain")
                expected = (
                    ["pending", "pending", "pending"]
                    if elapsed == 119
                    else [
                        "verified" if elapsed >= 123 else "uncertain",
                        "verified" if elapsed >= 121 else "pending",
                        "verified" if elapsed >= 122 else "pending",
                    ]
                )
                for record, status in zip(pending, expected):
                    self.assertEqual(hg_state.get_status(self.db, record), status)
                    if status != "verified" or record.key in summary["results"]:
                        self.assertEqual(
                            summary["results"][record.key],
                            "deferred" if status == "pending" else status,
                        )
                self.assertEqual(source.read_bytes(), original_source)
                self.assertEqual(journal.read_bytes(), original_journal)
        self.assertEqual(len({post["timestamp"] for post in api.posts}), 3)
        self.assertFalse(api.deletes)

    def test_late_v1_fat_export_restarts_settling_before_single_complete_upload(self):
        current = hg_domain.aware("2026-10-02T10:00:00+08:00")
        self.r = hg_domain.Measurement(current - timedelta(minutes=5), 70, 23.5, "PICOOC")
        self.cfg["settle_seconds"] = 120
        (self.inbox / "health.json").unlink()
        weight = self.inbox / "weight.json"
        weight.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "metric": "weight",
                    "exported_at": current.isoformat(),
                    "samples": [
                        {"at": self.r.at.isoformat(), "value": "70 kg", "source": "PICOOC"}
                    ],
                }
            )
        )
        originals = {weight: weight.read_bytes()}
        api = API()
        for elapsed in (119, 120, 239, 240, 241):
            with self.subTest(elapsed=elapsed):
                if elapsed == 120:
                    fat = self.inbox / "body_fat.json"
                    fat.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "metric": "body_fat",
                                "exported_at": (current + timedelta(seconds=120)).isoformat(),
                                "samples": [
                                    {
                                        "at": self.r.at.isoformat(),
                                        "value": "23.5%",
                                        "source": "PICOOC",
                                    }
                                ],
                            }
                        )
                    )
                    originals[fat] = fat.read_bytes()
                with patch.object(
                    hg_runtime, "now", return_value=current + timedelta(seconds=elapsed)
                ):
                    self.sync(api)
                summary = json.loads((self.state / "last_run.json").read_text())
                self.assertEqual(summary["complete_measurements"], 1)
                self.assertEqual(summary["waiting_pairs"], 0)
                self.assertEqual(summary["exports"]["weight"], current.isoformat())
                if elapsed >= 120:
                    self.assertEqual(
                        summary["exports"]["body_fat"],
                        (current + timedelta(seconds=120)).isoformat(),
                    )
                else:
                    self.assertNotIn("body_fat", summary["exports"])
                if elapsed < 240:
                    self.assertEqual(summary["deferred"], "settling")
                    self.assertEqual(summary["results"], {self.r.key: "deferred"})
                    self.assertEqual(summary["local_status_counts"], {})
                    self.assertIsNone(hg_state.state_row(self.db, self.r))
                    self.assertFalse(api.posts)
                else:
                    self.assertNotIn("deferred", summary)
                    self.assertEqual(summary["results"], {self.r.key: "verified"})
                    self.assertEqual(
                        summary["comparison"], {"missing": 1} if elapsed == 240 else {"matched": 1}
                    )
                    self.assertEqual(summary["local_status_counts"], {"verified": 1})
                    self.assertEqual(hg_state.get_status(self.db, self.r), "verified")
                    self.assertEqual(
                        json.loads(hg_state.state_row(self.db, self.r)["payload"])["fat"], 23.5
                    )
                    self.assertEqual(len(api.posts), 1)
                    self.assertEqual(api.posts[0]["weight"], 70)
                    self.assertEqual(api.posts[0]["percent_fat"], 23.5)
                self.assertFalse(api.deletes)
                self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_zero_settling_allows_small_export_clock_skew(self):
        current = hg_runtime.now()
        source = self.inbox / "health.json"
        data = json.loads(source.read_text())
        data["exported_at"] = (current + timedelta(minutes=1)).isoformat()
        source.write_text(json.dumps(data))
        prior = source.read_bytes()
        api = API()
        with patch.object(hg_runtime, "now", return_value=current):
            self.sync(api)
        summary = json.loads((self.state / "last_run.json").read_text())
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(summary["results"][self.r.key], "verified")
        self.assertNotIn("deferred", summary)
        self.assertEqual(source.read_bytes(), prior)

    def test_positive_settling_waits_until_window_boundary(self):
        current = hg_runtime.now()
        self.cfg["settle_seconds"] = 120
        source = self.inbox / "health.json"
        data = json.loads(source.read_text())
        data["exported_at"] = current.isoformat()
        source.write_text(json.dumps(data))
        api = API()
        with patch.object(hg_runtime, "now", return_value=current + timedelta(seconds=119)):
            self.sync(api)
        summary = json.loads((self.state / "last_run.json").read_text())
        self.assertEqual(summary["results"][self.r.key], "deferred")
        self.assertEqual(summary["deferred"], "settling")
        self.assertFalse(api.posts)
        with patch.object(hg_runtime, "now", return_value=current + timedelta(seconds=120)):
            self.sync(api)
        summary = json.loads((self.state / "last_run.json").read_text())
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(summary["results"][self.r.key], "verified")
        self.assertNotIn("deferred", summary)

    def test_confirmed_manual_upload_bypasses_positive_settling(self):
        current = hg_runtime.now()
        self.cfg["settle_seconds"] = 120
        source = self.inbox / "health.json"
        data = json.loads(source.read_text())
        data["exported_at"] = (current + timedelta(minutes=1)).isoformat()
        source.write_text(json.dumps(data))
        api = API()
        with (
            patch.object(hg_runtime, "now", return_value=current),
            patch.object(hg_garmin, "api_client", return_value=api),
            patch("builtins.input", return_value="UPLOAD"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            hg_sync.run_sync(self.cfg, self.r.key)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(hg_state.get_status(self.db, self.r), "verified")

    def test_ignored_never_uploaded_even_if_remote_missing(self):
        hg_state.set_status(self.db, self.r, "ignored")
        api = API()
        self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(hg_state.get_status(self.db, self.r), "ignored")


if __name__ == "__main__":
    unittest.main()
