"""Exact local source preservation, isolated cleanup and offline recovery."""
import argparse
import contextlib
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import health_garmin as h
import source_archive as a
import replace_history as bulk
import test_reconciliation as fixtures
from test_reconciliation import API, AT, row


class SourceArchiveTests(unittest.TestCase):
    setUp = fixtures.ReconciliationTests.setUp
    tearDown = fixtures.ReconciliationTests.tearDown
    write_export = fixtures.ReconciliationTests.write_export
    sync = fixtures.ReconciliationTests.sync
    plan = fixtures.ReconciliationTests.plan

    def enable(self):
        self.cfg['archive_after_sync'] = True

    def capture(self):
        snapshots = []
        samples, records, _, _ = h.load_measurements(self.cfg, source_snapshot=snapshots)
        return snapshots, samples, records

    def cleanup(self):
        snapshots, samples, records = self.capture()
        return a.archive_completed(self.cfg, snapshots, samples, records, self.db)

    def test_verified_run_preserves_exact_bytes_and_history_without_reposting(self):
        self.enable()
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22, 53.55)
        self.write_export([self.r])
        source = self.inbox / 'health.json'
        obj = json.loads(source.read_text())
        obj['unrecognized_metadata'] = {'keep': ['all', 'original']}
        raw = b'\xef\xbb\xbf  ' + json.dumps(obj, ensure_ascii=False, indent=4).encode() + b'\r\n'
        source.write_bytes(raw)
        api = API([row(self.r)])
        self.sync(api)
        self.assertFalse(source.exists())
        archived = a.read_archive(self.cfg)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].raw, raw)
        self.assertEqual(archived[0].name, 'health.json')
        self.assertEqual(archived[0].sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(h.load_measurements(self.cfg)[1], [self.r])
        self.assertEqual(json.loads(h.state_row(self.db, self.r)['payload'])['lbm'], 53.55)
        self.sync(api)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)
        self.assertEqual(stat.S_IMODE(a.archive_path().stat().st_mode), 0o600)
        destination = self.root / 'restore'
        with contextlib.redirect_stdout(io.StringIO()):
            a.restore_exports(self.cfg, destination)
        self.assertEqual((destination / 'health.json').read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / 'health.json').stat().st_mode), 0o600)
        with self.assertRaises(h.SafeError):
            a.restore_exports(self.cfg, destination)
        self.cfg['archive_after_sync'] = False
        self.assertEqual(h.load_measurements(self.cfg)[1], [self.r])

    def test_legacy_disabled_cancelled_failed_and_settling_runs_keep_source(self):
        source = self.inbox / 'health.json'
        original = source.read_bytes()
        self.sync(API([row(self.r)]))
        self.assertEqual(source.read_bytes(), original)
        self.enable()
        self.cfg['enabled'] = False
        with patch.object(h, 'api_client', side_effect=AssertionError('must stay offline')):
            self.sync(API())
        self.cfg['enabled'] = True
        with patch('builtins.input', return_value='CANCEL'), contextlib.redirect_stdout(io.StringIO()):
            h.run_sync(self.cfg, self.r.key)
        h.set_status(self.db, self.r, 'pending')
        with patch.object(h, 'api_client', side_effect=TimeoutError), self.assertRaises(TimeoutError):
            h.run_sync(self.cfg)
        obj = json.loads(original)
        obj['exported_at'] = h.now().isoformat()
        source.write_text(json.dumps(obj))
        latest = source.read_bytes()
        self.cfg['settle_seconds'] = 120
        self.sync(API([row(self.r)]))
        self.assertEqual(source.read_bytes(), latest)
        self.assertFalse(a.archive_path().exists())

    def test_v1_split_metrics_archive_together_and_orphans_remain(self):
        self.enable()
        (self.inbox / 'health.json').unlink()
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22, 53.55)
        raw = {}
        for metric, value in [('weight', 70), ('body_fat', '23.5%'), ('bmi', 22), ('lbm', '53.55 kg')]:
            path = self.inbox / (metric + '.json')
            path.write_text(json.dumps({'schema_version': 1, 'metric': metric,
                'exported_at': '2020-02-20T09:00:00+08:00',
                'samples': [{'at': AT.isoformat(), 'value': value, 'source': 'PICOOC'}]}))
            raw[path.name] = path.read_bytes()
        orphan = self.inbox / 'orphan.json'
        orphan.write_text(json.dumps({'schema_version': 1, 'metric': 'body_fat',
            'exported_at': '2020-02-20T09:00:00+08:00',
            'samples': [{'at': (AT + timedelta(days=1)).isoformat(), 'value': '24%', 'source': 'PICOOC'}]}))
        self.sync(API([row(self.r)]))
        self.assertEqual({s.name: s.raw for s in a.read_archive(self.cfg)}, raw)
        self.assertEqual(list(self.inbox.glob('*.json')), [orphan])
        self.assertEqual(h.load_measurements(self.cfg)[1], [self.r])

    def test_filtered_and_unverified_files_are_not_deleted(self):
        self.enable()
        source = self.inbox / 'health.json'
        original = source.read_bytes()
        for status in ('pending', 'ignored', 'uncertain', 'inflight', 'verified_conflict', 'remote_missing'):
            with self.subTest(status=status):
                h.set_status(self.db, self.r, status)
                self.assertEqual(self.cleanup()['removed'], 0)
                self.assertEqual(source.read_bytes(), original)
        h.set_status(self.db, self.r, 'verified')
        self.cfg['allowed_sources'] = ['Other']
        self.assertEqual(self.cleanup()['removed'], 0)
        self.cfg['allowed_sources'] = ['PICOOC']
        self.cfg['since'] = '2021-01-01'
        self.assertEqual(self.cleanup()['removed'], 0)
        self.assertFalse(a.archive_path().exists())

    def test_unknown_ignored_request_and_active_operation_block_all_cleanup(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        other = h.Measurement(AT + timedelta(days=2), 71, None, 'PICOOC')
        h.set_status(self.db, other, 'inflight')
        h.set_status(self.db, other, 'ignored')
        self.assertEqual(h.unknown_request_count(self.db), 1)
        self.assertEqual(self.cleanup()['removed'], 0)
        h.set_status(self.db, other, 'verified_manual')
        operation = self.state / 'operations' / 'active.json'
        h.write_json(operation, {'key': 'operation', 'phase': 'uncertain'})
        self.assertEqual(self.cleanup()['removed'], 0)
        operation.unlink()
        self.assertEqual(self.cleanup()['removed'], 1)

    def test_bmi_lbm_mismatch_and_ambiguous_pairing_keep_source(self):
        self.enable()
        self.r = h.Measurement(AT, 70, 23.5, 'PICOOC', 22, 53.55)
        self.write_export([self.r])
        for wrong in (h.Measurement(AT, 70, 23.5, 'PICOOC', 23, 53.55),
                      h.Measurement(AT, 70, 23.5, 'PICOOC', 22, 54)):
            with self.subTest(wrong=wrong):
                h.set_status(self.db, wrong, 'verified')
                self.assertEqual(self.cleanup()['removed'], 0)
        obj = json.loads((self.inbox / 'health.json').read_text())
        obj['metrics']['body_fat'].append({'at': (AT + timedelta(seconds=1)).isoformat(),
                                         'value': '24%', 'source': 'PICOOC'})
        # The exact pair remains valid but the extra fat sample is unowned.
        (self.inbox / 'health.json').write_text(json.dumps(obj))
        h.set_status(self.db, self.r, 'verified')
        self.assertEqual(self.cleanup()['removed'], 0)

    def test_archive_inbox_scope_and_canonical_alias_are_isolated(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        self.cleanup()
        alias = self.root / 'alias'
        alias.symlink_to(self.inbox, target_is_directory=True)
        self.assertEqual(h.load_measurements(dict(self.cfg, inbox=str(alias)))[1], [self.r])
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(h.SafeError):
            h.load_measurements(dict(self.cfg, inbox=str(other)))
        other_cfg = dict(self.cfg, inbox=str(other))
        self.assertEqual(a.read_archive(other_cfg), [])

    def test_corruption_rejected_before_cleanup_or_restore_creation(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        snapshots, _, _ = self.capture()
        a.persist(snapshots)
        with contextlib.closing(sqlite3.connect(a.archive_path())) as db, db:
            db.execute("UPDATE source_exports SET raw=?", (b'corrupted',))
        destination = self.root / 'never-created'
        with self.assertRaises(h.SafeError):
            a.restore_exports(self.cfg, destination)
        self.assertFalse(destination.exists())
        with self.assertRaises(h.SafeError):
            self.cleanup()
        self.assertTrue((self.inbox / 'health.json').exists())

    def test_invalid_archive_filename_schema_and_nonregular_database_rejected(self):
        snapshots, _, _ = self.capture()
        a.persist(snapshots)
        with contextlib.closing(sqlite3.connect(a.archive_path())) as db, db:
            db.execute("UPDATE source_exports SET filename='../outside.json'")
        with self.assertRaises(h.SafeError):
            a.read_archive(self.cfg)
        a.archive_path().unlink()
        with contextlib.closing(sqlite3.connect(a.archive_path())) as db, db:
            db.execute('CREATE TABLE unrelated (x)')
        with self.assertRaises(h.SafeError):
            a.read_archive(self.cfg)
        a.archive_path().unlink()
        a.archive_path().symlink_to(self.state / 'sync.sqlite3')
        with self.assertRaises(h.SafeError):
            a.read_archive(self.cfg)
        a.archive_path().unlink()
        os.mkfifo(a.archive_path())
        with self.assertRaises(h.SafeError):
            a.read_archive(self.cfg)

    def test_persist_failure_and_independent_readback_failure_keep_live_source(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        source = self.inbox / 'health.json'
        original = source.read_bytes()
        with patch.object(a, 'persist', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.cleanup()
        connect = sqlite3.connect
        def fail_readback(path, *args, **kwargs):
            if str(path).endswith('?mode=ro'):
                raise sqlite3.OperationalError('readback failed')
            return connect(path, *args, **kwargs)
        with patch.object(a.sqlite3, 'connect', side_effect=fail_readback):
            with self.assertRaises(h.SafeError):
                self.cleanup()
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(h.get_status(self.db, self.r), 'verified')
        self.assertEqual(self.cleanup()['removed'], 1)

    def test_post_commit_interrupt_is_idempotent_and_does_not_duplicate_bytes(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        source = self.inbox / 'health.json'
        original = source.read_bytes()
        with patch.object(a, 'remove_snapshot', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cleanup()
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.cleanup()['removed'], 1)
        self.assertEqual([s.raw for s in a.read_archive(self.cfg)], [original])

    def test_source_change_during_api_read_is_retained(self):
        self.enable()
        source = self.inbox / 'health.json'
        api = API([row(self.r)])
        original_read = api.get_body_composition
        replacement = source.read_bytes() + b'\n'
        def read(*args):
            source.write_bytes(replacement)
            return original_read(*args)
        api.get_body_composition = read
        self.sync(api)
        self.assertEqual(source.read_bytes(), replacement)
        summary = json.loads((self.state / 'last_run.json').read_text())
        self.assertEqual(summary['source_archive']['changed'], 1)

    def test_atomic_move_does_not_delete_last_moment_icloud_replacement(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        source = self.inbox / 'health.json'
        replacement = source.read_bytes() + b'\n'
        real_replace = os.replace
        def replace(old, new):
            concurrent = source.with_name('concurrent.tmp')
            concurrent.write_bytes(replacement)
            real_replace(concurrent, source)
            return real_replace(old, new)
        with patch.object(a.os, 'replace', side_effect=replace):
            result = self.cleanup()
        self.assertEqual(result['removed'], 0)
        self.assertEqual(result['changed'], 1)
        self.assertEqual(source.read_bytes(), replacement)

    def test_quarantine_keeps_both_concurrent_files_without_overwrite(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        source = self.inbox / 'health.json'
        original = source.read_bytes()
        newer = original + b'\n'
        newest = original + b'\n\n'
        real_replace = os.replace
        def replace(old, new):
            replacement = source.with_name('concurrent.tmp')
            replacement.write_bytes(newer)
            real_replace(replacement, source)
            real_replace(old, new)
            source.write_bytes(newest)
        with patch.object(a.os, 'replace', side_effect=replace):
            result = self.cleanup()
        self.assertEqual(result['changed'], 1)
        self.assertEqual(source.read_bytes(), newest)
        preserved = [p.read_bytes() for p in self.inbox.glob('.hg-archive-*/*.json')]
        self.assertEqual(preserved, [newer])
        self.assertEqual(len(h.load_measurements(self.cfg)[1]), 1)

    def test_unlink_failure_restores_source_and_verified_state(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        unlink = Path.unlink
        def failed(path, *args, **kwargs):
            if path.parent.name.startswith('.hg-archive-'):
                raise PermissionError('cannot remove')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', failed):
            result = self.cleanup()
        self.assertEqual(result['failed'], 1)
        self.assertTrue((self.inbox / 'health.json').exists())
        self.assertEqual(h.get_status(self.db, self.r), 'verified')

    def test_same_name_different_exports_restore_without_overwriting(self):
        first, _, _ = self.capture()
        a.persist(first)
        second = h.Measurement(AT + timedelta(days=1), 71, 24, 'PICOOC')
        self.write_export([second])
        last, _, _ = self.capture()
        a.persist([s for s in last if s.path is not None])
        destination = self.root / 'restore'
        with contextlib.redirect_stdout(io.StringIO()):
            a.restore_exports(self.cfg, destination)
        exported = list(destination.glob('*.json'))
        self.assertEqual(len(exported), 2)
        self.assertEqual({p.read_bytes() for p in exported}, {first[0].raw, last[0].raw})
        records = h.load_measurements(dict(self.cfg, inbox=str(destination)))[1]
        self.assertEqual(records, [self.r, second])

    def test_restore_preserves_existing_parent_permissions(self):
        snapshots, _, _ = self.capture()
        a.persist(snapshots)
        shared = self.root / 'shared'
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        destination = shared / 'restored'
        with contextlib.redirect_stdout(io.StringIO()):
            a.restore_exports(self.cfg, destination)
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        before = (destination / 'health.json').read_bytes()
        with self.assertRaises(h.SafeError):
            a.restore_exports(self.cfg, destination)
        self.assertEqual((destination / 'health.json').read_bytes(), before)

    def test_long_ascii_and_unicode_filename_collisions_restore_all_bytes(self):
        original = self.inbox / 'health.json'
        for name in ('x' * 240 + '.json', '称' * 80 + '.json'):
            with self.subTest(name=name), patch.object(h, 'STATE', self.state / hashlib.sha256(name.encode()).hexdigest()):
                h.private_dir(h.STATE)
                if original.exists():
                    original.unlink()
                self.write_export([self.r])
                source = self.inbox / name
                original.rename(source)
                first, _, _ = self.capture()
                a.persist(first)
                source.write_bytes(source.read_bytes() + b'\n')
                second, _, _ = self.capture()
                a.persist([snapshot for snapshot in second if snapshot.path is not None])
                destination = self.root / h.STATE.name
                with contextlib.redirect_stdout(io.StringIO()):
                    a.restore_exports(self.cfg, destination)
                exports = list(destination.glob('*.json'))
                self.assertEqual({path.read_bytes() for path in exports}, {first[0].raw, second[0].raw})
                self.assertTrue(all(len(path.name.encode('utf-8')) <= 255 for path in exports))
                source.unlink()

    def test_case_and_unicode_equivalent_names_restore_all_exports(self):
        snapshots, _, _ = self.capture()
        raw = snapshots[0].raw
        for names in (('Health.json', 'health.json'), ('café.json', 'cafe\u0301.json')):
            with self.subTest(names=names), patch.object(h, 'STATE', self.state / hashlib.sha256(names[0].encode()).hexdigest()):
                h.private_dir(h.STATE)
                values = [a.SourceSnapshot(snapshots[0].inbox, name, raw + b'\n' * number,
                                           snapshots[0].exported_at) for number, name in enumerate(names)]
                a.persist(values)
                destination = self.root / h.STATE.name
                with contextlib.redirect_stdout(io.StringIO()):
                    a.restore_exports(self.cfg, destination)
                self.assertEqual({path.read_bytes() for path in destination.glob('*.json')},
                                 {raw, raw + b'\n'})

    def test_insert_failure_rolls_back_whole_batch_before_any_unlink(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        source = self.inbox / 'health.json'
        (self.inbox / 'copy.json').write_bytes(source.read_bytes())
        connect = sqlite3.connect
        class FailingBatch:
            def __init__(self, database):
                self.database = database
                self.inserts = 0
            def __enter__(self):
                self.database.__enter__()
                return self
            def __exit__(self, *args):
                return self.database.__exit__(*args)
            def execute(self, statement, *args):
                if statement.startswith('INSERT'):
                    self.inserts += 1
                    if self.inserts == 2:
                        raise sqlite3.OperationalError('simulated disk failure')
                return self.database.execute(statement, *args)
            def close(self):
                self.database.close()
        def connection(path, *args, **kwargs):
            return FailingBatch(connect(path, *args, **kwargs))
        with patch.object(a.sqlite3, 'connect', side_effect=connection), self.assertRaises(h.SafeError):
            self.cleanup()
        self.assertEqual(len(list(self.inbox.glob('*.json'))), 2)
        self.assertEqual(a.read_archive(self.cfg), [])
        self.assertEqual(self.cleanup()['removed'], 2)

    def test_archive_history_still_protects_new_source_correspondence(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        self.cleanup()
        new = h.Measurement(AT, 70, 23.5, 'Second Scale')
        self.cfg['allowed_sources'].append(new.source)
        self.write_export([new])
        api = API([row(self.r)])
        self.sync(api)
        self.assertFalse(api.posts)
        self.assertEqual(h.get_status(self.db, new), 'conflict')
        self.assertEqual(h.load_measurements(self.cfg)[1], [new, self.r])

    def test_archive_only_full_history_backup_is_independently_readable(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        self.cleanup()
        api = API([row(self.r)])
        path = self.plan([row(self.r)], save=True)
        with patch('builtins.input', return_value='REPLACE ALL'), contextlib.redirect_stdout(io.StringIO()):
            bulk.execute(argparse.Namespace(plan=str(path), complete_export=True), self.cfg, api)
        journal = next((self.state / 'operations').glob('*.json'))
        backup = Path(h.read_operation(journal)['backup'])
        restored_cfg = dict(self.cfg, inbox=str(backup / 'exports'))
        self.assertEqual(h.load_measurements(restored_cfg, include_archive=False)[1], [self.r])
        self.assertEqual((backup / 'exports' / 'health.json').read_bytes(), a.read_archive(self.cfg)[0].raw)
        self.assertFalse(api.posts)
        self.assertFalse(api.deletes)

    def test_doctor_archive_metadata_never_opens_sqlite(self):
        import test_diagnostics
        fixture = test_diagnostics.DoctorTests('runTest')
        fixture.setUp()
        try:
            with patch.object(h, 'STATE', fixture.state):
                archive = fixture.state / 'source_archive.sqlite3'
                archive.write_bytes(b'only metadata is needed')
                for live in (True, False):
                    if not live:
                        fixture.export.unlink()
                    with patch.object(h.sqlite3, 'connect', side_effect=AssertionError('doctor opened SQLite')):
                        code, report, _ = fixture.report()
                    self.assertEqual(code, 0)
                    checks = {c['id']: c for c in report['checks']}
                    self.assertEqual(checks['inbox']['status'], 'ok' if live else 'info')
        finally:
            fixture.doCleanups()

    def test_optional_setting_validation_and_cli_settings_toggle(self):
        cfg = dict(self.cfg, is_cn=True)
        for value in ('true', 1, None):
            with self.assertRaises(h.SafeError):
                h.validate_config(dict(cfg, archive_after_sync=value))
        self.assertNotIn('archive_after_sync', h.validate_config(cfg))
        self.assertIs(h.validate_config(dict(cfg, archive_after_sync=True))['archive_after_sync'], True)
        cfg['enabled'] = False
        arguments = argparse.Namespace(fat_unit=None, weight_unit=None, pair_window=None,
            since=None, inbox=None, source=None, archive_after_sync='on')
        destination = self.root / 'config.json'
        with patch.object(h, 'CONFIG', destination), contextlib.redirect_stdout(io.StringIO()):
            h.update_settings(arguments, cfg)
        self.assertTrue(cfg['archive_after_sync'])
        self.assertTrue(json.loads(destination.read_text())['archive_after_sync'])

    def test_missing_upload_and_manual_upload_archive_only_after_verified_readback(self):
        for manual in (False, True):
            with self.subTest(manual=manual), patch.object(h, 'STATE', self.state / str(manual)):
                h.private_dir(h.STATE)
                self.enable()
                self.write_export([self.r])
                api = API()
                with (patch.object(h, 'api_client', return_value=api),
                      patch('builtins.input', return_value='UPLOAD'),
                      contextlib.redirect_stdout(io.StringIO())):
                    h.run_sync(self.cfg, self.r.key if manual else None)
                    h.run_sync(self.cfg)
                self.assertEqual(len(api.posts), 1)
                self.assertFalse((self.inbox / 'health.json').exists())
                self.assertEqual(h.load_measurements(self.cfg)[1], [self.r])
                with contextlib.closing(h.database()) as db:
                    self.assertEqual(h.get_status(db, self.r), 'verified')

    def test_upload_limit_retains_mixed_file_until_all_measurements_verified(self):
        self.enable()
        other = h.Measurement(AT + timedelta(days=1), 71, 24, 'PICOOC')
        self.write_export([self.r, other])
        self.cfg['max_records_per_run'] = 1
        api = API()
        self.sync(api)
        self.assertEqual(len(api.posts), 1)
        self.assertTrue((self.inbox / 'health.json').exists())
        self.assertFalse(a.archive_path().exists())
        self.sync(api)
        self.assertEqual(len(api.posts), 2)
        self.assertFalse((self.inbox / 'health.json').exists())
        self.sync(api)
        self.assertEqual(len(api.posts), 2)

    def test_quarantine_crash_before_and_after_move_is_recoverable(self):
        self.enable()
        h.set_status(self.db, self.r, 'verified')
        real_replace = os.replace
        for moved in (False, True):
            self.write_export([self.r])
            original = (self.inbox / 'health.json').read_bytes()
            def crash(old, new):
                if moved:
                    real_replace(old, new)
                raise KeyboardInterrupt
            with self.subTest(moved=moved), patch.object(a.os, 'replace', side_effect=crash):
                with self.assertRaises(KeyboardInterrupt):
                    self.cleanup()
            self.assertEqual(h.load_measurements(self.cfg)[1], [self.r])
            paths = a.live_paths(self.inbox)
            self.assertEqual([path.read_bytes() for path in paths], [original])
            self.assertEqual(self.cleanup()['removed'], 1)

    def test_real_cli_preview_restore_errors_and_init_share_module_state(self):
        project = self.root / 'cli-project'
        project.mkdir()
        for name in ('health_garmin.py', 'source_archive.py'):
            shutil.copyfile(Path(h.__file__).resolve().parent / name, project / name)
        config = dict(self.cfg, is_cn=True, enabled=False)
        (project / 'config.json').write_text(json.dumps(config))
        def invoke(*arguments, input_text=None):
            return subprocess.run([sys.executable, str(project / 'health_garmin.py'), *arguments],
                                  input=input_text, text=True, capture_output=True, timeout=10, cwd=project)
        h.set_status(self.db, self.r, 'verified')
        self.enable()
        self.cleanup()
        (project / 'state').mkdir()
        shutil.copyfile(a.archive_path(), project / 'state' / 'source_archive.sqlite3')
        result = invoke('preview')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('完整测量 1', result.stdout)
        destination = self.root / 'cli-restored'
        result = invoke('restore-exports', '--to', str(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = invoke('restore-exports', '--to', str(destination))
        self.assertEqual(result.returncode, 2)
        self.assertIn('停止：', result.stderr)
        self.assertIn('恢复目标已存在', result.stderr)
        (project / 'config.json').unlink()
        result = invoke('init', '--inbox', str(self.inbox), '--since', '2020-01-01', input_text='PICOOC\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        initialized = json.loads((project / 'config.json').read_text())
        self.assertTrue(initialized['archive_after_sync'])
        self.assertFalse(initialized['enabled'])

    def test_doctor_reports_cleanup_failures_without_hiding_unknown_requests(self):
        import test_diagnostics
        fixture = test_diagnostics.DoctorTests('runTest')
        fixture.setUp()
        try:
            counts = {'archived': 1, 'removed': 0, 'changed': 0, 'failed': 1, 'missing': 0}
            summary = {'run_at': h.now().isoformat(), 'results': {}, 'source_archive': counts}
            fixture.last.write_text(json.dumps(summary))
            _, _, checks = fixture.report()
            self.assertEqual(checks['source_archive']['status'], 'warn')
            summary['unknown_request_count'] = 1
            fixture.last.write_text(json.dumps(summary))
            _, _, checks = fixture.report()
            self.assertEqual(checks['last_run']['status'], 'error')
            self.assertEqual(checks['source_archive']['status'], 'warn')
            for invalid in (True, -1, '1'):
                summary['source_archive'] = dict(counts, failed=invalid)
                fixture.last.write_text(json.dumps(summary))
                _, _, checks = fixture.report()
                self.assertEqual(checks['last_run']['status'], 'error')
                self.assertNotIn('source_archive', checks)
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
