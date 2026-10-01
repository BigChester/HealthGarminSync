"""Doctor checks synthetic files without writes, authentication or real launchctl."""
import builtins
import contextlib
from datetime import timedelta
import fcntl
from importlib.metadata import PackageNotFoundError
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import diagnostics as d
import health_garmin as h


SECRET = 'private-ticket-never-output'


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.base = self.root / 'project'
        self.base.mkdir()
        self.state = self.base / 'state'
        self.state.mkdir()
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        self.config_path = self.base / 'config.json'
        for name, value in (('BASE', self.base), ('STATE', self.state), ('CONFIG', self.config_path)):
            self.enterContext(patch.object(h, name, value))
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.enterContext(patch.object(sys, 'platform', 'darwin'))
        self.enterContext(patch.object(d, 'version', return_value=h.REQUIRED_GARMIN_VERSION))
        self.service = self.enterContext(patch.object(d.subprocess, 'run',
                             return_value=subprocess.CompletedProcess([], 0, SECRET, SECRET)))
        self.cfg = {'is_cn': True, 'enabled': True, 'inbox': str(self.inbox),
                    'since': '2020-01-01', 'allowed_sources': [SECRET],
                    'weight_bare_unit': 'kg', 'fat_bare_unit': 'percent',
                    'pair_window_seconds': 0, 'settle_seconds': 120, 'max_records_per_run': 10}
        self.save_config()
        self.python = self.base / '.venv/bin/python'
        self.python.parent.mkdir(parents=True)
        self.python.write_text('synthetic executable')
        self.python.chmod(0o700)
        self.entry = self.base / 'agent_runner.py'
        self.entry.write_text('# synthetic background entry\n')
        self.export = self.inbox / (SECRET + '.json')
        self.export.write_text(json.dumps({'schema_version': 2,
            'exported_at': '2020-02-20T09:00:00+08:00', 'metrics': {
                'weight': [{'at': '2020-02-20T08:00:00+08:00', 'value': '73.123 kg', 'source': SECRET}],
                'body_fat': [{'at': '2020-02-20T08:00:00+08:00', 'value': '26.789%', 'source': SECRET}]}}))
        self.tokens = self.state / 'tokens'
        self.tokens.mkdir()
        (self.tokens / 'oauth.json').write_text(SECRET)
        self.last = self.state / 'last_run.json'
        self.last.write_text(json.dumps({'run_at': h.now().isoformat(),
            'results': {SECRET: 'verified'}, 'exports': {SECRET: SECRET}, 'payload': SECRET}))
        stamp = h.now().isoformat()
        self.agent_attempt = self.state / 'last_agent_attempt.json'
        self.agent_attempt.write_text(json.dumps({'outcome': 'completed', 'started_at': stamp,
            'finished_at': stamp, 'stage': 'run', 'error_type': None}))
        self.agent_attempt.chmod(0o600)
        self.plist = self.home / 'Library/LaunchAgents' / (h.LABEL + '.plist')
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps(h.agent_plist()))

    def save_config(self):
        self.config_path.write_text(json.dumps(self.cfg))

    def snapshot(self):
        return {str(path.relative_to(self.root)): (stat.S_IMODE(path.stat().st_mode),
                path.read_bytes() if path.is_file() else None) for path in [self.root, *self.root.rglob('*')]}

    def invoke(self, *arguments):
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        original_import = builtins.__import__
        original_read = Path.read_text

        def offline_import(name, *args, **kwargs):
            if name.split('.')[0] in ('garminconnect', 'requests', 'curl_cffi', 'garth'):
                raise AssertionError('network/auth module imported')
            return original_import(name, *args, **kwargs)

        def read_without_tokens(path, *args, **kwargs):
            if self.tokens in path.parents:
                raise AssertionError('token content read')
            return original_read(path, *args, **kwargs)

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', ['health_garmin.py', *arguments]))
            for name in ('private_dir', 'write_json', 'database', 'api_client', 'manage_agent', 'RotatingFileHandler'):
                stack.enter_context(patch.object(h, name, side_effect=AssertionError('mutating helper called')))
            stack.enter_context(patch.object(h.logging, 'basicConfig', side_effect=AssertionError('log setup')))
            stack.enter_context(patch.object(h.os, 'umask', side_effect=AssertionError('umask changed')))
            stack.enter_context(patch.object(h.fcntl, 'flock', side_effect=AssertionError('lock acquired')))
            stack.enter_context(patch.object(h.sqlite3, 'connect', side_effect=AssertionError('SQLite opened')))
            for name in ('mkdir', 'chmod', 'write_text', 'write_bytes', 'replace', 'touch', 'unlink'):
                stack.enter_context(patch.object(Path, name, side_effect=AssertionError('filesystem mutation')))
            stack.enter_context(patch.object(Path, 'read_text', read_without_tokens))
            stack.enter_context(patch('builtins.__import__', side_effect=offline_import))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            try:
                result = h.main()
            except SystemExit as exc:
                result = exc.code
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(stderr.getvalue(), '')
        self.assertNotIn(SECRET, stdout.getvalue())
        self.assertNotIn('73.123', stdout.getvalue())
        self.assertNotIn('26.789', stdout.getvalue())
        return result, stdout.getvalue()

    def report(self):
        result, output = self.invoke('doctor', '--json')
        report = json.loads(output)
        self.assertEqual(result, 2 if report['summary']['error'] else 0)
        return result, report, {item['id']: item for item in report['checks']}

    def test_healthy_setup_returns_parseable_safe_report_and_only_queries_service(self):
        result, report, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(checks['inbox']['data']['measurements'], 1)
        self.assertEqual(checks['last_run']['data']['results'], {'verified': 1})
        self.assertIn('当前进程', report['scope'])
        self.service.assert_called_once_with(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{h.LABEL}'],
                                             capture_output=True, timeout=5)

    def test_plain_output_is_readable_and_contains_current_process_scope(self):
        result, output = self.invoke('doctor')
        self.assertEqual(result, 0)
        self.assertIn('离线诊断', output)
        self.assertIn('当前进程可读取导出', output)
        self.assertIn('后台执行', output)

    def test_disabled_uninstalled_with_missing_optional_state_is_warning(self):
        self.cfg['enabled'] = False
        self.save_config()
        self.plist.unlink()
        self.last.unlink()
        self.service.return_value.returncode = 1
        result, report, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(report['status'], 'warning')
        for identifier in ('enabled', 'agent_plist', 'agent_service', 'last_run'):
            self.assertEqual(checks[identifier]['status'], 'warn')

    def test_missing_or_corrupt_config_and_absent_state_do_not_initialize_files(self):
        import shutil
        shutil.rmtree(self.state)
        for content in (None, '{"secret":"' + SECRET + '",', json.dumps(dict(self.cfg, enabled='false'))):
            with self.subTest(content=content):
                if content is None:
                    self.config_path.unlink()
                else:
                    self.config_path.write_text(content)
                result, _, checks = self.report()
                self.assertEqual(result, 2)
                self.assertEqual(checks['config']['status'], 'error')
                self.assertEqual(checks['state']['status'], 'warn')
                self.assertFalse(self.state.exists())

    def test_real_busy_lock_does_not_block_doctor(self):
        with (self.state / 'run.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, _, _ = self.report()
        self.assertEqual(result, 0)

    def test_help_does_not_create_runtime_files(self):
        for args in (('--help',), ('doctor', '--help')):
            with self.subTest(args=args):
                result, output = self.invoke(*args)
                self.assertEqual(result, 0)
                self.assertIn('doctor', output)
        self.service.assert_not_called()

    def test_missing_dependency_wrong_version_and_missing_interpreter_are_errors(self):
        for version_result in (PackageNotFoundError('garminconnect'), '0.0.1'):
            with self.subTest(version_result=version_result):
                with patch.object(d, 'version', side_effect=version_result if isinstance(version_result, Exception) else None,
                                  return_value=version_result):
                    result, _, checks = self.report()
                self.assertEqual(result, 2)
                self.assertEqual(checks['dependency']['status'], 'error')
        self.python.unlink()
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['interpreter']['status'], 'error')

    def test_missing_or_unreadable_background_entry_is_error(self):
        original = d.os.access
        with patch.object(d.os, 'access', side_effect=lambda path, mode: False if path == self.entry else original(path, mode)):
            result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['agent_entry']['status'], 'error')
        self.entry.unlink()
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['agent_entry']['status'], 'error')

    def test_old_direct_output_plist_requires_update(self):
        doc = h.agent_plist()
        doc['ProgramArguments'] = [str(self.python), str(self.base / 'health_garmin.py'), 'run']
        doc['StandardOutPath'] = str(self.state / 'agent.out.log')
        doc['StandardErrorPath'] = str(self.state / 'agent.err.log')
        self.plist.write_bytes(plistlib.dumps(doc))
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['agent_plist']['status'], 'error')
        self.assertIn('./hg install-agent', checks['agent_plist']['hint'])

    def test_old_python_and_nonexecutable_interpreter_are_errors(self):
        self.python.chmod(0o600)
        with patch.object(sys, 'version_info', (3, 11)):
            result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['python']['status'], 'error')
        self.assertEqual(checks['interpreter']['status'], 'error')

    def test_unreadable_inbox_error_does_not_echo_exception(self):
        original = Path.iterdir
        def unreadable(path):
            if path == self.inbox:
                raise PermissionError(SECRET)
            return original(path)
        with patch.object(Path, 'iterdir', unreadable):
            result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['inbox']['status'], 'error')

    def test_unknown_home_in_inbox_is_reported_as_json_error(self):
        original_expand = Path.expanduser
        def unavailable_home(path):
            if path == Path(self.cfg['inbox']):
                raise RuntimeError(SECRET)
            return original_expand(path)
        with patch.object(Path, 'expanduser', unavailable_home):
            result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['inbox']['status'], 'error')

    def test_unreadable_state_reports_error_without_initializing_evidence(self):
        original_access = os.access
        def denied(path, mode):
            return False if path == self.state else original_access(path, mode)
        with patch.object(d.os, 'access', denied):
            result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['state']['status'], 'error')
        self.assertNotIn('last_run', checks)

    def test_absent_state_when_disabled_remains_warning(self):
        import shutil
        self.cfg['enabled'] = False
        self.save_config()
        shutil.rmtree(self.state)
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks['state']['status'], 'warn')
        self.assertEqual(checks['tokens']['status'], 'warn')

    def test_malformed_export_and_pairing_ambiguity_are_errors(self):
        self.export.write_text('{"secret":"' + SECRET + '",')
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['inbox']['status'], 'error')
        doc = {'schema_version': 2, 'exported_at': '2020-02-20T09:00:00+08:00', 'metrics': {
            'weight': [{'at': '2020-02-20T08:00:00+08:00', 'value': 70, 'source': SECRET}],
            'body_fat': [{'at': at, 'value': '25%', 'source': SECRET}
                         for at in ('2020-02-20T07:59:55+08:00', '2020-02-20T08:00:05+08:00')]}}
        self.cfg['pair_window_seconds'] = 10
        self.save_config()
        self.export.write_text(json.dumps(doc))
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertGreater(checks['inbox']['data']['waiting_pairs'], 0)

    def test_empty_inbox_and_missing_tokens_are_warnings(self):
        self.cfg['enabled'] = False
        self.save_config()
        self.export.unlink()
        (self.tokens / 'oauth.json').unlink()
        self.tokens.rmdir()
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks['inbox']['status'], 'warn')
        self.assertEqual(checks['tokens']['status'], 'warn')

    def test_enabled_missing_or_empty_token_directory_is_error(self):
        (self.tokens / 'oauth.json').unlink()
        for present in (True, False):
            with self.subTest(directory_exists=present):
                if not present:
                    self.tokens.rmdir()
                result, _, checks = self.report()
                self.assertEqual(result, 2)
                self.assertEqual(checks['tokens']['status'], 'error')
                self.assertIn('./hg login', checks['tokens']['hint'])

    def test_all_emitted_single_and_full_operation_phases_and_run_results_are_readable(self):
        phases = ('verified', 'deleted_verified', 'closed_partial', 'uncertain',
                  'deleting', 'deleted', 'uploading', 'replacing')
        operations = self.state / 'operations'
        operations.mkdir()
        for index, phase in enumerate(phases):
            (operations / f'job-{index}.json').write_text(json.dumps({
                'phase': phase, 'key': SECRET + str(index),
                'type': 'full_replacement' if phase in ('replacing', 'closed_partial') else 'repair',
                'record_ids': [SECRET], 'payload': SECRET}))
        results = ('pending', 'verified', 'verified_manual', 'verified_conflict', 'remote_missing',
                   'repair_uncertain', 'uncertain', 'inflight', 'ignored', 'repair', 'replace', 'conflict', 'deferred')
        self.last.write_text(json.dumps({'run_at': h.now().isoformat(),
            'results': {str(index): result for index, result in enumerate(results)}}))
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['operations']['data'], {'total': 8, 'unfinished': 5, 'failed_checks': 0})
        self.assertEqual(checks['last_run']['data']['results'], {result: 1 for result in results})

    def test_recent_run_result_severity_and_safe_counts(self):
        groups = {'error': ('inflight', 'uncertain', 'repair_uncertain'),
                  'warn': ('repair', 'replace', 'conflict', 'verified_conflict', 'remote_missing'),
                  'info': ('verified', 'verified_manual', 'ignored', 'deferred', 'pending')}
        for severity, outcomes in groups.items():
            for outcome in outcomes:
                with self.subTest(outcome=outcome):
                    self.last.write_text(json.dumps({'run_at': h.now().isoformat(),
                        'results': {SECRET: outcome}, 'error': SECRET}))
                    code, report, checks = self.report()
                    check = checks['last_run']
                    self.assertEqual(code, 2 if severity == 'error' else 0)
                    self.assertEqual(report['status'],
                                     {'error': 'error', 'warn': 'warning', 'info': 'ok'}[severity])
                    self.assertEqual(check['status'], severity)
                    self.assertIn('最近运行摘要', check['message'])
                    self.assertEqual(check['data']['results'], {outcome: 1})
                    if severity != 'info':
                        self.assertIn('核对', check['hint'])

    def test_mixed_recent_results_keep_highest_severity(self):
        for outcomes, severity in ((('verified', 'repair', 'uncertain'), 'error'),
                                   (('pending', 'conflict'), 'warn'),
                                   (('ignored', 'deferred', 'verified_manual'), 'info')):
            with self.subTest(outcomes=outcomes):
                self.last.write_text(json.dumps({'run_at': h.now().isoformat(),
                    'results': {str(index): value for index, value in enumerate(outcomes)}}))
                code, _, checks = self.report()
                self.assertEqual(code, 2 if severity == 'error' else 0)
                self.assertEqual(checks['last_run']['status'], severity)
                self.assertEqual(checks['last_run']['data']['results'], {value: 1 for value in outcomes})

    def test_local_state_snapshot_severity_includes_records_outside_latest_results(self):
        for status in sorted(d.RESULTS):
            with self.subTest(status=status):
                self.last.write_text(json.dumps({'run_at': h.now().isoformat(),
                    'results': {SECRET: 'verified'}, 'local_status_counts': {status: 2}}))
                code, _, checks = self.report()
                expected = 'error' if status in d.UNCERTAIN_RESULTS else 'warn' if status in d.REVIEW_RESULTS else 'info'
                self.assertEqual(code, 2 if expected == 'error' else 0)
                check = checks['last_run']
                self.assertEqual(check['status'], expected)
                self.assertEqual(check['data']['local_status_counts'], {status: 2})
                self.assertEqual(check['data']['results'], {'verified': 1})
                if expected != 'info':
                    self.assertIn('快照', check['hint'])
                    self.assertIn('./hg status', check['hint'])

    def test_zero_local_counts_do_not_report_unresolved_records(self):
        self.last.write_text(json.dumps({'run_at': h.now().isoformat(), 'results': {},
            'local_status_counts': {'uncertain': 0, 'verified_conflict': 0}}))
        code, _, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(checks['last_run']['status'], 'info')

    def test_invalid_local_status_counts_are_errors_without_private_details(self):
        for counts in (None, [], {SECRET: 1}, {'uncertain': -1}, {'uncertain': True},
                       {'verified': '1'}, {'verified': 1.5}, {'verified': {}}, {'verified': None}):
            with self.subTest(counts=counts):
                self.last.write_text(json.dumps({'run_at': h.now().isoformat(),
                    'results': {}, 'local_status_counts': counts}))
                code, _, checks = self.report()
                self.assertEqual(code, 2)
                self.assertEqual(checks['last_run']['status'], 'error')
                self.assertNotIn('data', checks['last_run'])

    def test_stale_summary_keeps_uncertain_error_and_task_hint(self):
        self.last.write_text(json.dumps({'run_at': (h.now() - timedelta(hours=3)).isoformat(),
                                        'results': {SECRET: 'uncertain'}}))
        code, _, checks = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(checks['last_run']['status'], 'error')
        self.assertIn('超过两小时', checks['last_run']['hint'])
        self.assertIn('核对', checks['last_run']['hint'])
        self.assertEqual(checks['last_run']['data']['results'], {'uncertain': 1})

    def test_disabled_sync_keeps_recent_uncertain_summary_visible(self):
        self.cfg['enabled'] = False
        self.save_config()
        for outcome in ('inflight', 'uncertain', 'repair_uncertain'):
            with self.subTest(outcome=outcome):
                self.last.write_text(json.dumps({'run_at': h.now().isoformat(), 'results': {SECRET: outcome}}))
                code, report, checks = self.report()
                self.assertEqual(code, 2)
                self.assertEqual(report['status'], 'error')
                self.assertEqual(checks['last_run']['status'], 'error')

    def test_fresh_manual_run_does_not_hide_stale_background_entry(self):
        current = h.now()
        self.last.write_text(json.dumps({'run_at': current.isoformat(), 'results': {SECRET: 'verified'}}))
        for age, severity in ((timedelta(hours=2), 'info'),
                              (timedelta(hours=2, seconds=1), 'warn')):
            with self.subTest(age=age):
                stamp = (current - age).isoformat()
                self.agent_attempt.write_text(json.dumps({'outcome': 'completed', 'started_at': stamp,
                    'finished_at': stamp, 'stage': 'run', 'error_type': None}))
                with patch.object(h, 'now', return_value=current):
                    code, report, checks = self.report()
                self.assertEqual(code, 0)
                self.assertEqual(checks['agent_service']['status'], 'ok')
                self.assertEqual(checks['last_run']['status'], 'info')
                self.assertEqual(checks['last_agent_attempt']['status'], severity)
                self.assertEqual(report['status'], 'warning' if severity == 'warn' else 'ok')
                if severity == 'warn':
                    self.assertIn('超过两小时', checks['last_agent_attempt']['hint'])

    def test_stale_background_failure_keeps_error_and_disabled_entry_has_no_age_reminder(self):
        stamp = (h.now() - timedelta(days=2)).isoformat()
        for enabled in (True, False):
            self.cfg['enabled'] = enabled
            self.save_config()
            for outcome in ('completed', 'failed', 'interrupted'):
                with self.subTest(enabled=enabled, outcome=outcome):
                    self.agent_attempt.write_text(json.dumps({'outcome': outcome, 'started_at': stamp,
                        'finished_at': stamp, 'stage': 'run',
                        'error_type': {'completed': None, 'failed': 'OSError',
                                       'interrupted': 'KeyboardInterrupt'}[outcome]}))
                    code, _, checks = self.report()
                    check = checks['last_agent_attempt']
                    self.assertEqual(code, 0 if outcome == 'completed' else 2)
                    self.assertEqual(check['status'], 'warn' if enabled and outcome == 'completed'
                                     else 'info' if outcome == 'completed' else 'error')
                    self.assertEqual('hint' in check, enabled)

    def test_stale_unfinished_entry_only_reminds_without_claiming_it_stopped(self):
        stamp = (h.now() - timedelta(hours=3)).isoformat()
        self.agent_attempt.write_text(json.dumps({'outcome': 'running', 'started_at': stamp,
            'finished_at': None, 'stage': 'run', 'error_type': None}))
        code, _, checks = self.report()
        check = checks['last_agent_attempt']
        self.assertEqual(code, 0)
        self.assertEqual(check['status'], 'warn')
        self.assertIn('可能仍在运行或曾被中断', check['message'])
        self.assertIn('超过两小时', check['hint'])

    def test_recent_entry_finish_is_fresh_even_when_it_started_hours_ago(self):
        current = h.now()
        self.agent_attempt.write_text(json.dumps({'outcome': 'completed',
            'started_at': (current - timedelta(hours=3)).isoformat(),
            'finished_at': current.isoformat(), 'stage': 'run', 'error_type': None}))
        with patch.object(h, 'now', return_value=current):
            code, report, checks = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(checks['last_agent_attempt']['status'], 'info')
        self.assertNotIn('hint', checks['last_agent_attempt'])

    def test_excessively_nested_local_json_is_reported_without_traceback(self):
        nested = '[' * 2000 + '0' + ']' * 2000
        for path, identifier in ((self.config_path, 'config'), (self.last, 'last_run'), (self.export, 'inbox')):
            with self.subTest(identifier=identifier):
                original = path.read_bytes()
                path.write_text(nested)
                result, _, checks = self.report()
                self.assertEqual(result, 2)
                self.assertEqual(checks[identifier]['status'], 'error')
                path.write_bytes(original)

    def test_journals_count_unfinished_without_printing_ids_and_malformed_evidence_errors(self):
        operations = self.state / 'operations'
        operations.mkdir()
        path = operations / 'job.json'
        path.write_text(json.dumps({'phase': 'uncertain', 'key': SECRET, 'payload': SECRET}))
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks['operations']['data'], {'total': 1, 'unfinished': 1, 'failed_checks': 0})
        for content in ('{"private":"' + SECRET + '",', json.dumps({'phase': [], 'key': SECRET})):
            path.write_text(content)
            result, _, checks = self.report()
            self.assertEqual(result, 2)
            self.assertEqual(checks['operations']['status'], 'error')

    def test_journal_verification_severity_keeps_historical_phase_and_unfinished_count(self):
        operations = self.state / 'operations'
        operations.mkdir()
        path = operations / 'job.json'
        for phase in sorted(d.PHASES):
            for passed in (False, True):
                with self.subTest(phase=phase, passed=passed):
                    path.write_text(json.dumps({'phase': phase, 'key': SECRET,
                        'last_checked_at': h.now().isoformat(), 'last_check_passed': passed}))
                    previous = path.read_bytes()
                    code, _, checks = self.report()
                    unfinished = int(phase not in d.TERMINAL_PHASES)
                    severity = 'error' if not passed else 'warn' if unfinished else 'ok'
                    self.assertEqual(code, 0 if passed else 2)
                    self.assertEqual(checks['operations']['status'], severity)
                    self.assertEqual(checks['operations']['data'], {'total': 1,
                        'unfinished': unfinished, 'failed_checks': int(not passed)})
                    self.assertEqual(path.read_bytes(), previous)

    def test_invalid_journal_verification_is_reported_without_raw_values(self):
        operations = self.state / 'operations'
        operations.mkdir()
        path = operations / 'job.json'
        stamp = h.now().isoformat()
        cases = [{'last_checked_at': stamp}, {'last_check_passed': False},
                 {'last_checked_at': None, 'last_check_passed': False},
                 {'last_checked_at': [], 'last_check_passed': False},
                 {'last_checked_at': SECRET, 'last_check_passed': False},
                 {'last_checked_at': '2026-10-02T12:00:00', 'last_check_passed': True}]
        cases += [{'last_checked_at': stamp, 'last_check_passed': value}
                  for value in (None, 0, 1, 'false', SECRET, [], {})]
        for fields in cases:
            with self.subTest(fields=fields):
                path.write_text(json.dumps({'phase': 'verified', 'key': SECRET, **fields}))
                code, _, checks = self.report()
                self.assertEqual(code, 2)
                self.assertEqual(checks['operations']['status'], 'error')
                self.assertNotIn('data', checks['operations'])

    def test_invalid_last_run_does_not_emit_raw_result_and_old_heartbeat_only_warns(self):
        for content in ('not JSON ' + SECRET, json.dumps({'run_at': h.now().isoformat(), 'results': {'id': SECRET}})):
            self.last.write_text(content)
            result, _, checks = self.report()
            self.assertEqual(result, 2)
            self.assertEqual(checks['last_run']['status'], 'error')
        self.last.write_text(json.dumps({'run_at': (h.now() - timedelta(hours=3)).isoformat(), 'results': {}}))
        result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks['last_run']['status'], 'warn')

    def test_malformed_plist_and_runtime_paths_are_errors_without_value_dump(self):
        self.plist.write_text('<plist><dict>' + SECRET)
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['agent_plist']['status'], 'error')
        for field, value in (('ProgramArguments', [SECRET]), ('WorkingDirectory', SECRET),
                             ('StartInterval', 10), ('StandardOutPath', SECRET)):
            with self.subTest(field=field):
                doc = h.agent_plist()
                doc[field] = value
                self.plist.write_bytes(plistlib.dumps(doc))
                result, _, checks = self.report()
                self.assertEqual(result, 2)
                self.assertIn(field, checks['agent_plist']['message'])

    def test_enabled_missing_plist_or_unloaded_service_is_error_with_install_hint(self):
        self.plist.unlink()
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertIn('./hg install-agent', checks['agent_plist']['hint'])
        self.plist.write_bytes(plistlib.dumps(h.agent_plist()))
        self.service.return_value.returncode = 1
        result, _, checks = self.report()
        self.assertEqual(result, 2)
        self.assertEqual(checks['agent_plist']['status'], 'ok')
        self.assertEqual(checks['agent_service']['status'], 'error')

    def test_service_timeout_and_unavailable_are_distinct_from_not_loaded(self):
        for exception, diagnosis in ((subprocess.TimeoutExpired('launchctl', 5, SECRET, SECRET), '超时'),
                                     (FileNotFoundError(SECRET), '不可用')):
            with self.subTest(exception=exception):
                self.service.side_effect = exception
                result, _, checks = self.report()
                self.assertEqual(result, 2)
                self.assertIn(diagnosis, checks['agent_service']['message'])

    def test_nonmacos_skips_service_query(self):
        with patch.object(sys, 'platform', 'linux'):
            result, _, checks = self.report()
        self.assertEqual(result, 0)
        self.assertEqual(checks['agent']['status'], 'info')
        self.service.assert_not_called()


if __name__ == '__main__':
    unittest.main()
