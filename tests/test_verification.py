"""Current verification results stay separate from completed operation history."""
import argparse
import contextlib
from datetime import timedelta
import io
import json
import logging
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import health_garmin as h
import replace_history as bulk
import test_diagnostics as doctor_fixtures
import test_reconciliation as fixtures
from test_reconciliation import API, row


class OperationVerificationTests(unittest.TestCase):
    setUp = fixtures.ReconciliationTests.setUp
    tearDown = fixtures.ReconciliationTests.tearDown
    write_export = fixtures.ReconciliationTests.write_export

    def journal(self, phase='verified', deletion=False, full=False):
        path = self.state / 'operations' / 'verification.json'
        data = {'key': self.r.key, 'phase': phase,
                'last_checked_at': '2020-02-20T00:00:00+00:00', 'last_check_passed': True}
        if full:
            backup = self.state / 'backups' / 'verification'
            h.private_dir(backup)
            data.update(type='full_replacement', key='full-test',
                        start=self.r.at.date().isoformat(), end=self.r.at.date().isoformat(),
                        targets=[self.r.payload()], record_ids=[self.r.key], backup=str(backup))
        else:
            data['original'] = row(self.r, 100)
            if not deletion:
                data['measurement'] = self.r.payload()
        h.write_json(path, data)
        return path

    def check_single(self, path, api):
        output = io.StringIO()
        with patch.object(h, 'api_client', return_value=api), contextlib.redirect_stdout(output):
            result = h.verify_operation(argparse.Namespace(operation=str(path)))
        return result, output.getvalue()

    def check_bulk(self, path, api):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = bulk.verify(path, api)
        return result, output.getvalue()

    def assert_current(self, path, passed, phase, api):
        journal = json.loads(path.read_text())
        self.assertIs(journal['last_check_passed'], passed)
        self.assertEqual(journal['phase'], phase)
        self.assertNotEqual(journal['last_checked_at'], '2020-02-20T00:00:00+00:00')
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        return journal

    def test_completed_repair_reports_missing_or_conflict_then_current_match(self):
        for rows, diagnosis in (([], '目标缺失'), ([dict(row(self.r), weight=71000)], '目标冲突')):
            with self.subTest(diagnosis=diagnosis):
                path = self.journal()
                api = API(rows)
                result, output = self.check_single(path, api)
                self.assertIs(result, False)
                self.assertIn('本次只读核对： ' + diagnosis, output)
                self.assertIn('操作历史阶段： verified', output)
                self.assert_current(path, False, 'verified', api)
                self.assertNotIn(self.r.key, h.active_operations())
                api.rows = [row(self.r, 201)]
                result, output = self.check_single(path, api)
                self.assertIs(result, True)
                self.assertIn('本次只读核对： 匹配', output)
                self.assert_current(path, True, 'verified', api)

    def test_completed_deletion_reports_reappeared_original_then_absence(self):
        path = self.journal(phase='deleted_verified', deletion=True)
        api = API([row(self.r, 100)])
        result, output = self.check_single(path, api)
        self.assertIs(result, False)
        self.assertIn('本次只读核对： 原记录仍存在', output)
        self.assert_current(path, False, 'deleted_verified', api)
        self.assertNotIn(self.r.key, h.active_operations())
        api.rows = []
        result, output = self.check_single(path, api)
        self.assertIs(result, True)
        self.assertIn('本次只读核对： 原记录已不存在', output)
        self.assert_current(path, True, 'deleted_verified', api)

    def test_uncertain_repair_still_completes_on_current_match(self):
        path = self.journal(phase='uncertain')
        api = API([row(self.r, 201)])
        result, _ = self.check_single(path, api)
        self.assertIs(result, True)
        self.assert_current(path, True, 'verified', api)
        self.assertNotIn(self.r.key, h.active_operations())
        self.assertEqual(h.get_status(self.db, self.r), 'verified')

    def test_completed_full_replacement_reports_current_mismatch_then_match(self):
        for rows in ([], [dict(row(self.r), weight=71000)], [row(self.r, 100), row(self.r, 101)]):
            with self.subTest(rows=rows):
                path = self.journal(full=True)
                api = API(rows)
                result, output = self.check_bulk(path, api)
                self.assertIs(result, False)
                self.assertIn('本次完整历史只读核验： 未匹配', output)
                self.assertIn('操作历史阶段： verified', output)
                self.assert_current(path, False, 'verified', api)
                self.assertNotIn(self.r.key, h.active_operations())
                api.rows = [row(self.r, 201)]
                result, output = self.check_bulk(path, api)
                self.assertIs(result, True)
                self.assertIn('本次完整历史只读核验： 匹配', output)
                self.assert_current(path, True, 'verified', api)

    def test_closed_partial_history_is_not_reopened_on_full_mismatch(self):
        path = self.journal(phase='closed_partial', full=True)
        api = API()
        result, _ = self.check_bulk(path, api)
        self.assertIs(result, False)
        self.assert_current(path, False, 'closed_partial', api)
        self.assertNotIn(self.r.key, h.active_operations())

    def test_uncertain_full_replacement_still_completes_on_match(self):
        path = self.journal(phase='uncertain', full=True)
        api = API([row(self.r)])
        result, _ = self.check_bulk(path, api)
        self.assertIs(result, True)
        self.assert_current(path, True, 'verified', api)
        self.assertNotIn(self.r.key, h.active_operations())

    def test_failed_read_propagates_without_reporting_historical_success(self):
        for full in (False, True):
            with self.subTest(full=full):
                path = self.journal(full=full)
                before = path.read_bytes()
                api = API()
                api.fail_chunk = 1
                output = io.StringIO()
                with patch.object(h, 'api_client', return_value=api), contextlib.redirect_stdout(output):
                    with self.assertRaises(h.SafeError):
                        h.verify_operation(argparse.Namespace(operation=str(path)))
                self.assertEqual(output.getvalue(), '')
                after = json.loads(path.read_text())
                previous = json.loads(before)
                self.assertIs(after.pop('last_check_passed'), False)
                self.assertNotEqual(after.pop('last_checked_at'), previous.pop('last_checked_at'))
                previous.pop('last_check_passed')
                self.assertEqual(after, previous)
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)

    def invoke_cli(self, path, api, login_error=None):
        output, errors = io.StringIO(), io.StringIO()
        previous_umask = os.umask(0o077)
        try:
            with patch.object(sys, 'argv', ['health_garmin.py', 'verify-operation', str(path)]), \
                    patch.object(h, 'config', return_value=self.cfg), \
                    patch.object(h, 'api_client', return_value=api, side_effect=login_error), \
                    patch.object(h, 'PrivateRotatingFileHandler', return_value=logging.NullHandler()), \
                    patch.object(h.logging, 'basicConfig'), patch.object(h.LOG, 'error'), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = h.main()
        finally:
            os.umask(previous_umask)
        return result, output.getvalue(), errors.getvalue()

    def test_cli_exit_code_tracks_current_result_for_single_and_full_checks(self):
        for full in (False, True):
            with self.subTest(full=full):
                path = self.journal(full=full)
                api = API()
                result, output, errors = self.invoke_cli(path, api)
                self.assertEqual(result, 2)
                self.assertIn('未匹配' if full else '目标缺失', output)
                self.assertEqual(errors, '')
                self.assert_current(path, False, 'verified', api)
                api.rows = [row(self.r, 201)]
                result, output, errors = self.invoke_cli(path, api)
                self.assertEqual(result, 0)
                self.assertIn('匹配', output)
                self.assertEqual(errors, '')
                self.assert_current(path, True, 'verified', api)

    def test_cli_read_failure_is_nonzero_without_success_output(self):
        path = self.journal()
        api = API()
        api.fail_chunk = 1
        result, output, errors = self.invoke_cli(path, api)
        self.assertEqual(result, 2)
        self.assertEqual(output, '')
        self.assertIn('停止', errors)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_final_batch_mismatch_preserves_saved_verification_and_blocks_batch(self):
        class ChangedAtFinalCheck(API):
            def get_body_composition(self, start, end):
                # Plan review, confirmation, daily and target checks match;
                # the final full snapshot observes the target disappearing.
                if len(self.reads) == 4:
                    self.rows = []
                return super().get_body_composition(start, end)

        api = ChangedAtFinalCheck([row(self.r)])
        plan = fixtures.ReconciliationTests.plan(self, api.rows, save=True)
        args = argparse.Namespace(plan=str(plan), complete_export=True)
        with patch('builtins.input', return_value='REPLACE ALL'), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(h.SafeError, '完整替换已暂停'):
                bulk.execute(args, self.cfg, api)
        path = next((self.state / 'operations').glob('full-*.json'))
        job = json.loads(path.read_text())
        self.assertIs(job['last_check_passed'], False)
        self.assertIsNotNone(h.aware(job['last_checked_at']))
        self.assertEqual(job['verification'],
                         {'matched': 0, 'target_count': 1, 'remote_count': 0, 'extra': 0})
        self.assertEqual(job['phase'], 'uncertain')
        self.assertIn(self.r.key, h.active_operations())
        self.assertIn('最终完整历史未匹配', job['reason'])
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)


class OperationDiagnosticTests(unittest.TestCase):
    save_config = doctor_fixtures.DoctorTests.save_config
    snapshot = doctor_fixtures.DoctorTests.snapshot
    invoke = doctor_fixtures.DoctorTests.invoke
    report = doctor_fixtures.DoctorTests.report
    journal = OperationVerificationTests.journal
    check_single = OperationVerificationTests.check_single
    check_bulk = OperationVerificationTests.check_bulk
    invoke_cli = OperationVerificationTests.invoke_cli

    def setUp(self):
        doctor_fixtures.DoctorTests.setUp(self)
        self.r = h.load_measurements(self.cfg)[1][0]

    def assert_diagnosis(self, path, phase, failed):
        before = path.read_bytes()
        code, _, checks = self.report()
        unfinished = int(phase not in ('verified', 'deleted_verified', 'closed_partial'))
        self.assertEqual(code, 2 if failed else 0)
        self.assertEqual(checks['operations']['status'], 'error' if failed else 'warn' if unfinished else 'ok')
        self.assertEqual(checks['operations']['data'], {'total': 1, 'unfinished': unfinished,
                                                      'failed_checks': int(failed)})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(before)['phase'], phase)
        self.assertEqual(self.r.key in h.active_operations(), bool(unfinished))

    def test_completed_deletion_failure_and_recovery_reach_offline_doctor(self):
        path = self.journal(phase='deleted_verified', deletion=True)
        api = API([row(self.r, 100)])
        self.assertIs(self.check_single(path, api)[0], False)
        self.assert_diagnosis(path, 'deleted_verified', failed=True)
        api.rows = []
        self.assertIs(self.check_single(path, api)[0], True)
        self.assert_diagnosis(path, 'deleted_verified', failed=False)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_generated_operation_checks_share_future_tolerance_after_clock_rollback(self):
        reference = h.now()
        source, token = self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes()
        for kind in ('deletion', 'repair', 'full'):
            for offset, expected in ((timedelta(seconds=600), 0),
                                     (timedelta(seconds=600, microseconds=1), 2),
                                     (timedelta(days=30), 2)):
                with self.subTest(kind=kind, rollback=offset):
                    path = self.journal(phase='uncertain', deletion=kind == 'deletion', full=kind == 'full')
                    api = API([] if kind == 'deletion' else [row(self.r, 201)])
                    with patch.object(h, 'now', return_value=reference + offset):
                        self.assertIs(self.check_single(path, api)[0], True)
                    before = path.read_bytes()
                    with patch.object(h, 'now', return_value=reference):
                        code, _, checks = self.report()
                        text_code, output = self.invoke('doctor')
                    self.assertEqual((code, text_code), (expected, expected))
                    self.assertEqual(checks['operations']['status'], 'error' if expected else 'ok')
                    if expected:
                        self.assertIn('Mac 时间', checks['operations']['hint'])
                        self.assertIn('state/operations', checks['operations']['hint'])
                        self.assertIn('Mac 时间', output)
                        self.assertNotIn(path.name, output)
                        self.assertNotIn(json.loads(before)['last_checked_at'], output)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse(api.posts)
                    self.assertFalse(api.deletes)
        self.assertEqual(self.export.read_bytes(), source)
        self.assertEqual((self.tokens / 'oauth.json').read_bytes(), token)

    def test_deletion_identity_uncertainty_survives_cli_and_doctor_until_known_absence(self):
        path = self.journal(phase='uncertain', deletion=True)
        original = json.loads(path.read_text())['original']
        raw = Mock()
        api = h.GarminMeasurementAPI(raw)
        day = original['calendarDate']

        def rows(items):
            raw.get_weigh_ins.return_value = {'dailyWeightSummaries': [{
                'summaryDate': day, 'numOfWeightEntries': len(items), 'allWeightMetrics': items}]}

        source, token = self.export.read_bytes(), (self.tokens / 'oauth.json').read_bytes()
        for invalid in (None, '', 0, -1, True, 100.0, 'invalid'):
            with self.subTest(samplePk=invalid):
                rows([dict(original, samplePk=invalid)])
                code, output, errors = self.invoke_cli(path, api)
                self.assertEqual(code, 2)
                self.assertNotIn('原记录已不存在', output)
                self.assertNotIn('invalid', errors)
                self.assert_diagnosis(path, 'uncertain', failed=True)
        anonymous = dict(original)
        anonymous.pop('samplePk')
        rows([anonymous])
        self.assertEqual(self.invoke_cli(path, api)[0], 2)
        self.assert_diagnosis(path, 'uncertain', failed=True)
        rows([anonymous, dict(original, samplePk='00100')])
        code, output, _ = self.invoke_cli(path, api)
        self.assertEqual(code, 2)
        self.assertIn('原记录仍存在', output)
        self.assert_diagnosis(path, 'uncertain', failed=True)
        rows([dict(original, samplePk=200)])
        self.assertEqual(self.invoke_cli(path, api)[0], 0)
        self.assert_diagnosis(path, 'deleted_verified', failed=False)
        self.assertFalse(h.original_id_absent([original], '00100'))
        self.assertTrue(h.original_id_absent([], '00100'))
        with self.assertRaises(h.SafeError):
            h.original_id_absent([], True)
        raw.delete_weigh_in.assert_not_called()
        raw.add_weigh_in.assert_not_called()
        raw.add_body_composition.assert_not_called()
        self.assertEqual(self.export.read_bytes(), source)
        self.assertEqual((self.tokens / 'oauth.json').read_bytes(), token)

    def test_completed_repair_missing_or_conflicting_target_reaches_offline_doctor(self):
        for rows in ([], [dict(row(self.r), weight=71000)]):
            with self.subTest(rows=rows):
                path = self.journal()
                api = API(rows)
                self.assertIs(self.check_single(path, api)[0], False)
                self.assert_diagnosis(path, 'verified', failed=True)
                api.rows = [row(self.r, 201)]
                self.assertIs(self.check_single(path, api)[0], True)
                self.assert_diagnosis(path, 'verified', failed=False)
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)

    def test_full_replacement_failure_reaches_doctor_without_reopening_history(self):
        for phase in ('verified', 'closed_partial', 'uncertain'):
            with self.subTest(phase=phase):
                path = self.journal(phase=phase, full=True)
                api = API()
                self.assertIs(self.check_bulk(path, api)[0], False)
                self.assert_diagnosis(path, phase, failed=True)
                api.rows = [row(self.r, 201)]
                self.assertIs(self.check_bulk(path, api)[0], True)
                self.assert_diagnosis(path, 'verified', failed=False)
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)

    def test_query_failures_reach_doctor_and_recovery_clears_them_without_reopening_history(self):
        for kind, phase in (('single', 'verified'), ('deletion', 'deleted_verified'),
                            ('full', 'verified'), ('full', 'closed_partial')):
            for fault in ('login', 'timeout', 'incomplete'):
                with self.subTest(kind=kind, phase=phase, fault=fault):
                    full = kind == 'full'
                    path = self.journal(phase=phase, deletion=kind == 'deletion', full=full)
                    job = json.loads(path.read_text())
                    backup = None
                    if full:
                        job['verification'] = {'matched': 1, 'target_count': 1, 'remote_count': 1, 'extra': 0}
                        backup = Path(job['backup']) / 'remote-after.json'
                        h.write_json(backup, {'dateWeightList': [row(self.r, 201)]})
                        h.write_json(path, job)
                    before = path.read_bytes()
                    backup_bytes = backup.read_bytes() if backup else None
                    source = self.export.read_bytes()
                    api = API()
                    primary = h.SafeError('模拟登录失败。') if fault == 'login' else TimeoutError(doctor_fixtures.SECRET)
                    if fault == 'timeout':
                        api.get_body_composition = lambda *args: (_ for _ in ()).throw(primary)
                    elif fault == 'incomplete':
                        api.fail_chunk = 1
                    output = io.StringIO()
                    login_error = primary if fault == 'login' else None
                    with patch.object(h, 'api_client', return_value=api, side_effect=login_error), \
                            contextlib.redirect_stdout(output), self.assertRaises(Exception) as caught:
                        h.verify_operation(argparse.Namespace(operation=str(path)))
                    if fault != 'incomplete':
                        self.assertIs(caught.exception, primary)
                    else:
                        self.assertIsInstance(caught.exception, h.SafeError)
                    self.assertEqual(output.getvalue(), '')
                    after = json.loads(path.read_text())
                    self.assertIs(after.pop('last_check_passed'), False)
                    h.aware(after.pop('last_checked_at'))
                    historical = json.loads(before)
                    historical.pop('last_check_passed')
                    historical.pop('last_checked_at')
                    self.assertEqual(after, historical)
                    if backup:
                        self.assertEqual(backup.read_bytes(), backup_bytes)
                    self.assert_diagnosis(path, phase, failed=True)
                    api.reads.clear()
                    code, output, errors = self.invoke_cli(path, api, login_error)
                    self.assertEqual(code, 2)
                    self.assertEqual(output, '')
                    self.assertNotIn(doctor_fixtures.SECRET, errors)
                    self.assertFalse(api.posts)
                    self.assertFalse(api.deletes)
                    restored = API([] if kind == 'deletion' else [row(self.r, 201)])
                    code, output, errors = self.invoke_cli(path, restored)
                    self.assertEqual(code, 0)
                    self.assertEqual(errors, '')
                    self.assertIn('匹配' if kind != 'deletion' else '原记录已不存在', output)
                    self.assert_diagnosis(path, 'deleted_verified' if kind == 'deletion' else 'verified', failed=False)
                    self.assertEqual(self.export.read_bytes(), source)
                    self.assertFalse(restored.posts)
                    self.assertFalse(restored.deletes)

    def test_failure_evidence_save_error_does_not_replace_the_query_exception(self):
        for full in (False, True):
            with self.subTest(full=full):
                path = self.journal(full=full)
                before = path.read_bytes()
                primary = TimeoutError(doctor_fixtures.SECRET)
                api = API()
                api.get_body_composition = lambda *args: (_ for _ in ()).throw(primary)
                errors = io.StringIO()
                with patch.object(h, 'api_client', return_value=api), \
                        patch.object(h, 'write_json', side_effect=OSError(doctor_fixtures.SECRET)), \
                        patch.object(h.LOG, 'error'), contextlib.redirect_stderr(errors), \
                        self.assertRaises(TimeoutError) as caught:
                    h.verify_operation(argparse.Namespace(operation=str(path)))
                self.assertIs(caught.exception, primary)
                self.assertEqual(path.read_bytes(), before)
                self.assertIn('核对失败证据未保存', errors.getvalue())
                self.assertIn('OSError', errors.getvalue())
                self.assertNotIn(doctor_fixtures.SECRET, errors.getvalue())
                self.assertFalse(api.posts)
                self.assertFalse(api.deletes)

    def test_invalid_local_journal_is_not_rewritten_or_queried(self):
        for kind in ('single', 'deletion', 'full'):
            with self.subTest(kind=kind):
                path = self.journal(deletion=kind == 'deletion', full=kind == 'full')
                job = json.loads(path.read_text())
                if kind == 'single':
                    job['measurement']['at'] = 'invalid time'
                elif kind == 'deletion':
                    job['original'].pop('samplePk')
                else:
                    job['targets'][0]['at'] = 'invalid time'
                h.write_json(path, job)
                before = path.read_bytes()
                with patch.object(h, 'api_client') as api, patch.object(h, 'record_failed_check') as failed, \
                        self.assertRaises((h.SafeError, KeyError)):
                    h.verify_operation(argparse.Namespace(operation=str(path)))
                api.assert_not_called()
                failed.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

    def test_successful_read_local_save_errors_are_not_recorded_as_query_failures(self):
        for full in (False, True):
            for stage in ('database', 'journal', 'backup') if full else ('database', 'journal'):
                with self.subTest(full=full, stage=stage):
                    path = self.journal(full=full)
                    job = json.loads(path.read_text())
                    backup = Path(job['backup']) / 'remote-after.json' if full else None
                    primary = OSError(doctor_fixtures.SECRET)
                    original_write = h.write_json

                    def write(target, value):
                        if target == (backup if stage == 'backup' else path):
                            raise primary
                        return original_write(target, value)

                    with contextlib.ExitStack() as stack:
                        stack.enter_context(patch.object(h, 'api_client', return_value=API([row(self.r, 201)])))
                        failed = stack.enter_context(patch.object(h, 'record_failed_check'))
                        if stage == 'database':
                            stack.enter_context(patch.object(h, 'database', side_effect=primary))
                        else:
                            stack.enter_context(patch.object(h, 'write_json', side_effect=write))
                        with self.assertRaises(OSError) as caught:
                            h.verify_operation(argparse.Namespace(operation=str(path)))
                    self.assertIs(caught.exception, primary)
                    failed.assert_not_called()
                    self.assertIs(json.loads(path.read_text())['last_check_passed'], True)


if __name__ == '__main__':
    unittest.main()
