"""Pinned SDK contracts with real requests/urllib3 and a sealed fake transport."""
import argparse
import contextlib
from datetime import timedelta
from importlib.metadata import PackageNotFoundError, version
import io
import json
import logging
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import health_garmin as h
import test_reconciliation as fixtures


try:
    PINNED_SDK = version('garminconnect') == h.REQUIRED_GARMIN_VERSION
except PackageNotFoundError:
    PINNED_SDK = False


def forbidden(*args, **kwargs):
    raise AssertionError('Real network, login or user-home access is forbidden')


def fit_weight_timestamp(data):
    """Decode the actual FIT weight message's uint32 timestamp, including definitions."""
    cursor = data[0]
    end = cursor + struct.unpack_from('<I', data, 4)[0]
    definitions = {}
    while cursor < end:
        header = data[cursor]
        cursor += 1
        if header & 0x80:
            raise AssertionError('Unexpected compressed FIT timestamp')
        local = header & 0x0f
        if header & 0x40:
            endian = '<' if data[cursor + 1] == 0 else '>'
            global_message = struct.unpack_from(endian + 'H', data, cursor + 2)[0]
            count = data[cursor + 4]
            cursor += 5
            fields = [tuple(data[cursor + i * 3:cursor + i * 3 + 3]) for i in range(count)]
            cursor += count * 3
            definitions[local] = global_message, endian, fields
        else:
            global_message, endian, fields = definitions[local]
            for number, size, base_type in fields:
                value = data[cursor:cursor + size]
                cursor += size
                if global_message == 30 and number == 253:
                    if size != 4 or base_type & 0x1f != 6:
                        raise AssertionError('Unexpected FIT weight timestamp type')
                    seconds = struct.unpack(endian + 'I', value)[0] + 631065600
                    return h.datetime.fromtimestamp(seconds, h.UTC)
    raise AssertionError('Missing FIT weight timestamp')


@unittest.skipUnless(PINNED_SDK, 'Requires the installed, pinned Garmin SDK')
class SDKTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state, self.inbox = self.root / 'state', self.root / 'inbox'
        self.state.mkdir()
        self.inbox.mkdir()
        for name, value in (('BASE', self.root), ('STATE', self.state), ('CONFIG', self.root / 'config.json')):
            self.enterContext(patch.object(h, name, value))
        self.enterContext(patch.object(Path, 'home', forbidden))
        for name in ('create_connection', 'getaddrinfo'):
            self.enterContext(patch.object(socket, name, forbidden))
        for name in ('connect', 'connect_ex'):
            self.enterContext(patch.object(socket.socket, name, forbidden))
        try:
            import requests
            import urllib3
            import garminconnect
        except ImportError:
            self.skipTest('Pinned SDK transport dependencies are unavailable')
        self.requests, self.urllib3, self.garminconnect = requests, urllib3, garminconnect
        for name in ('request', 'get', 'post', 'put', 'patch', 'delete', 'head', 'options'):
            self.enterContext(patch.object(requests, name, forbidden))
            self.enterContext(patch.object(requests.api, name, forbidden))
        self.enterContext(patch.object(requests.sessions, 'get_netrc_auth', forbidden))
        try:
            import curl_cffi
            from curl_cffi import requests as curl_requests
        except ImportError:
            pass
        else:
            for name in ('request', 'get', 'post', 'put', 'patch', 'delete', 'head', 'options'):
                if hasattr(curl_requests, name):
                    self.enterContext(patch.object(curl_requests, name, forbidden))
            for cls in (curl_requests.Session, curl_requests.AsyncSession):
                self.enterContext(patch.object(cls, 'request', forbidden))
            self.enterContext(patch.object(curl_cffi.Curl, 'perform', forbidden))
        self.enterContext(patch.object(garminconnect.Garmin, 'login', forbidden))
        for logger in (garminconnect.logger, garminconnect.client._LOGGER):
            self.addCleanup(logger.setLevel, logger.level)
            logger.setLevel(logging.CRITICAL)
        self.enterContext(patch.object(garminconnect.time, 'sleep', lambda seconds: None))

        # Permit only this instance's API session, delegating to real preparation,
        # adapter and urllib3 retry handling. All other requests paths stay sealed.
        allowed_sessions, allowed_adapters = set(), set()
        original_request = requests.Session.request
        original_send = requests.Session.send
        original_adapter_send = requests.adapters.HTTPAdapter.send

        def request(session, *args, **kwargs):
            self.assertIn(id(session), allowed_sessions)
            self.assertFalse(session.trust_env)
            return original_request(session, *args, **kwargs)

        def send(session, *args, **kwargs):
            self.assertIn(id(session), allowed_sessions)
            return original_send(session, *args, **kwargs)

        def adapter_send(adapter, *args, **kwargs):
            self.assertIn(id(adapter), allowed_adapters)
            return original_adapter_send(adapter, *args, **kwargs)

        self.enterContext(patch.object(requests.Session, 'request', request))
        self.enterContext(patch.object(requests.Session, 'send', send))
        self.enterContext(patch.object(requests.adapters.HTTPAdapter, 'send', adapter_send))
        self.sent, self.fit_files, self.fit_values = [], [], []
        self.rows = []
        self.handler = lambda method, path, pool: (200, self.read_payload(path))

        def transport(pool, conn, method, url, **kwargs):
            self.assertEqual(pool.host, 'connectapi.garmin.cn')
            self.assertEqual(kwargs['timeout'].read_timeout, 15)
            path = urlsplit(url).path
            self.sent.append({'method': method, 'path': path, 'body': kwargs.get('body')})
            status, payload = self.handler(method, path, pool)
            return urllib3.response.HTTPResponse(
                body=io.BytesIO(json.dumps(payload).encode()), status=status,
                headers={'Content-Type': 'application/json'}, preload_content=False)

        self.enterContext(patch.object(urllib3.connectionpool.HTTPConnectionPool, '_make_request', transport))
        encoder = garminconnect.FitEncoderWeight
        original_value, original_scale = encoder.getvalue, encoder.write_weight_scale

        def fit_value(instance):
            value = original_value(instance)
            self.fit_files.append(value)
            return value

        def fit_scale(instance, at, **kwargs):
            self.fit_values.append((at, kwargs))
            return original_scale(instance, at, **kwargs)

        self.enterContext(patch.object(encoder, 'getvalue', fit_value))
        self.enterContext(patch.object(encoder, 'write_weight_scale', fit_scale))
        self.sdk = garminconnect.Garmin(is_cn=True)
        self.addCleanup(self.sdk.client.cs.close)
        self.addCleanup(self.sdk.client._api_session.close)
        self.sdk.client.di_token = 'synthetic-memory-token'
        self.sdk.client.cs.trust_env = self.sdk.client._api_session.trust_env = False
        allowed_sessions.add(id(self.sdk.client._api_session))
        allowed_adapters.update(id(adapter) for adapter in self.sdk.client._api_session.adapters.values())
        for adapter in self.sdk.client._api_session.adapters.values():
            self.assertEqual(adapter.max_retries.total, 0)
        self.assertIsNone(self.sdk.client._tokenstore_path)
        self.refresh_guard = self.enterContext(patch.object(self.sdk.client, '_refresh_session', side_effect=forbidden))
        self.check_saved = h.guard_token_persistence(self.sdk.client)
        self.api = h.GarminMeasurementAPI(self.sdk, self.check_saved)
        self.r = h.Measurement(fixtures.AT, 70, 23.5, 'PICOOC', 22, 53.55)
        self.cfg = {'allowed_sources': ['PICOOC'], 'since': '2020-01-01',
                    'weight_bare_unit': 'kg', 'fat_bare_unit': 'percent', 'pair_window_seconds': 0,
                    'inbox': str(self.inbox), 'enabled': True, 'settle_seconds': 0, 'max_records_per_run': 10}
        fixtures.ReconciliationTests.write_export(self, [self.r])
        self.source_bytes = (self.inbox / 'health.json').read_bytes()
        self.db = h.database()
        self.addCleanup(self.db.close)

    def read_payload(self, path):
        if '/weight/dayview/' in path:
            day = path.rsplit('/', 1)[1]
            return {'dateWeightList': [row for row in self.rows if row['calendarDate'] == day]}
        if '/weight/range/' in path:
            start, end = path.rsplit('/', 2)[1:]
            days = sorted({row['calendarDate'] for row in self.rows if start <= row['calendarDate'] <= end})
            return {'dailyWeightSummaries': [
                {'summaryDate': day, 'numOfWeightEntries': len(items), 'allWeightMetrics': items}
                for day in days for items in [[row for row in self.rows if row['calendarDate'] == day]]]}
        return {'synthetic': True}

    def operations(self):
        return {'FIT POST': lambda: self.sdk.add_body_composition(self.r.at.isoformat(), self.r.weight,
                                                                 percent_fat=self.r.fat, bmi=self.r.bmi),
                'weight POST': lambda: self.sdk.add_weigh_in(self.r.weight, timestamp=self.r.at.isoformat()),
                'DELETE': lambda: self.sdk.delete_weigh_in('100', self.r.at.date().isoformat())}

    def assert_source_untouched(self):
        self.assertEqual(list(self.inbox.iterdir()), [self.inbox / 'health.json'])
        self.assertEqual((self.inbox / 'health.json').read_bytes(), self.source_bytes)

    def assert_durable_status(self, status):
        with contextlib.closing(h.database()) as independent:
            self.assertEqual(h.get_status(independent, self.r), status)

    def test_mutating_methods_send_once_on_transient_failure(self):
        for name, operation in self.operations().items():
            for failure in ('timeout', 500, 503):
                with self.subTest(method=name, failure=failure):
                    self.sent.clear()
                    def fail(method, path, pool):
                        if failure == 'timeout':
                            raise self.urllib3.exceptions.ReadTimeoutError(pool, path, 'synthetic read timeout')
                        return failure, {'message': 'synthetic failure'}
                    self.handler = fail
                    expected = self.requests.Timeout if failure == 'timeout' else self.garminconnect.GarminConnectConnectionError
                    with self.assertRaises(expected):
                        operation()
                    self.assertEqual([request['method'] for request in self.sent], ['DELETE' if name == 'DELETE' else 'POST'])
                    if name == 'FIT POST':
                        self.assertEqual(self.sent[0]['path'], '/upload-service/upload')
                        self.assertEqual(self.fit_files[-1][8:12], b'.FIT')
                        self.assertIn(self.fit_files[-1], self.sent[0]['body'])
                    elif name == 'weight POST':
                        self.assertEqual(self.sent[0]['path'], '/weight-service/user-weight')
                        payload = json.loads(self.sent[0]['body'])
                        self.assertEqual((payload['value'], payload['unitKey']), (self.r.weight, 'kg'))
                    else:
                        self.assertEqual(self.sent[0]['path'], '/weight-service/weight/2020-02-20/byversion/100')
                    self.refresh_guard.assert_not_called()
        self.assert_source_untouched()

    def test_readonly_get_retries_recover_and_default_is_three_retries(self):
        statuses = [503, 200]
        self.handler = lambda method, path, pool: (statuses.pop(0), self.read_payload(path))
        self.assertEqual(self.sdk.get_daily_weigh_ins('2020-02-20'), {'dateWeightList': []})
        self.assertEqual([request['method'] for request in self.sent], ['GET', 'GET'])
        self.sent.clear()
        self.handler = lambda method, path, pool: (503, {'message': 'synthetic failure'})
        self.assertEqual(self.sdk.retry_attempts, 3)
        with self.assertRaises(self.garminconnect.GarminConnectConnectionError):
            self.sdk.get_weigh_ins('2020-02-19', '2020-02-21')
        self.assertEqual([request['method'] for request in self.sent], ['GET'] * 4)
        self.refresh_guard.assert_not_called()

    def test_explicit_401_rejection_allows_one_refresh_and_resend(self):
        for name, operation in self.operations().items():
            with self.subTest(method=name):
                self.sent.clear()
                statuses = [401, 200]
                self.handler = lambda method, path, pool: (statuses.pop(0), {'synthetic': True})
                def refresh():
                    self.sdk.client.di_token = 'synthetic-refreshed-token'
                with patch.object(self.sdk.client, '_refresh_session', side_effect=refresh) as refreshed:
                    operation()
                refreshed.assert_called_once_with()
                self.assertEqual([request['method'] for request in self.sent], ['DELETE' if name == 'DELETE' else 'POST'] * 2)
                if name == 'FIT POST':
                    for request in self.sent:
                        self.assertIn(self.fit_files[-1], request['body'])
                else:
                    self.assertEqual(self.sent[0]['body'], self.sent[1]['body'])

    def assert_refresh_save_failure_blocks_further_requests(self, unauthorized):
        client = self.sdk.client
        token_dir = (self.state / 'tokens').resolve()
        client.di_refresh_token, client.di_client_id = 'synthetic-refresh', 'synthetic-client'
        client._tokenstore_path = str(token_dir)
        client.dump(str(token_dir))
        cache = token_dir / 'garmin_tokens.json'
        before = cache.read_bytes()
        response = self.requests.Response()
        response.status_code = 200
        response._content = json.dumps({'access_token': 'synthetic-renewed-access',
                                        'refresh_token': 'synthetic-renewed-refresh'}).encode()
        self.addCleanup(response.close)

        def renew(*args, **kwargs):
            self.assert_durable_status('inflight')
            return response

        def server(method, path, pool):
            if method == 'POST':
                self.assert_durable_status('inflight')
                if unauthorized and sum(request['method'] == 'POST' for request in self.sent) == 1:
                    return 401, {'message': 'synthetic explicit authentication rejection'}
                self.rows = [fixtures.row(self.r)]
            return 200, self.read_payload(path)

        self.handler = server
        native_refresh = self.garminconnect.client.Client._refresh_session.__get__(client)
        expires = lambda: not unauthorized and bool(self.sent) and client.di_token == 'synthetic-memory-token'
        secret = 'synthetic-token-save-error-never-output'
        original_replace = Path.replace

        def replace(path, target):
            if Path(target) == cache:
                raise OSError(secret)
            return original_replace(path, target)

        with patch.object(client, '_refresh_session', native_refresh), \
                patch.object(client, '_http_post', side_effect=renew) as refresh, \
                patch.object(client, '_token_expires_soon', side_effect=expires), \
                patch.object(Path, 'replace', replace), \
                patch.object(client, 'dump', wraps=client.dump) as dump:
            self.assertEqual(h.process_record(self.api, self.db, self.r, sleep=lambda delay: None), 'uncertain')
            self.assert_durable_status('uncertain')
            self.assertEqual(h.state_row(self.db, self.r)['detail'], 'TokenPersistenceError')
            sent = list(self.sent)
            self.assertEqual(sum(request['method'] == 'POST' for request in sent), int(unauthorized))
            self.assertFalse(any(request['method'] == 'DELETE' for request in sent))
            # A successful refresh changed the in-memory token, so the next
            # request would not refresh again. The recorded save failure must
            # still stop this instance before a new or repeated measurement.
            self.assertEqual(client.di_token, 'synthetic-renewed-access')
            later = h.Measurement(self.r.at + timedelta(minutes=1), 71, 24, 'PICOOC')
            for record in (self.r, later):
                with self.assertRaises(h.TokenPersistenceError):
                    h.process_record(self.api, self.db, record)
            self.assertIsNone(h.state_row(self.db, later))
            for operation in self.operations().values():
                with self.assertRaises(h.TokenPersistenceError):
                    operation()
            self.assertEqual(self.sent, sent)
            refresh.assert_called_once()
            dump.assert_called_once_with(str(token_dir))
        self.assertEqual(cache.read_bytes(), before)
        self.assert_durable_status('uncertain')
        self.assert_source_untouched()

    def test_proactive_refresh_save_failure_blocks_post_and_later_records(self):
        self.assert_refresh_save_failure_blocks_further_requests(unauthorized=False)

    def test_401_refresh_save_failure_does_not_replay_or_start_later_records(self):
        self.assert_refresh_save_failure_blocks_further_requests(unauthorized=True)

    def test_saved_post_timeout_stays_uncertain_until_visible_without_repost(self):
        saved, visible = [], False
        def server(method, path, pool):
            if method == 'POST':
                self.assert_durable_status('inflight')
                saved.append(fixtures.row(self.r, pk=200))
                raise self.urllib3.exceptions.ReadTimeoutError(pool, path, 'synthetic saved POST response timeout')
            self.rows = saved if visible else []
            return 200, self.read_payload(path)
        self.handler = server
        self.assertEqual(h.process_record(self.api, self.db, self.r, sleep=lambda delay: None), 'uncertain')
        self.assert_durable_status('uncertain')
        self.assertEqual(h.process_record(self.api, self.db, self.r, sleep=lambda delay: None), 'uncertain')
        self.assertEqual(sum(request['method'] == 'POST' for request in self.sent), 1)
        visible = True
        self.assertEqual(h.process_record(self.api, self.db, self.r, sleep=lambda delay: None), 'verified')
        self.assert_durable_status('verified')
        self.assertEqual(sum(request['method'] == 'POST' for request in self.sent), 1)
        self.assertFalse(any(request['method'] == 'DELETE' for request in self.sent))
        at, values = self.fit_values[-1]
        self.assertEqual(at.timestamp(), self.r.at.timestamp())
        self.assertEqual((values['weight'], values['percent_fat'], values['bmi']), (70, 23.5, 22))
        self.assertIsNone(values['muscle_mass'])
        self.assertNotIn('lbm', values)
        self.assertEqual(json.loads(h.state_row(self.db, self.r)['payload'])['lbm'], self.r.lbm)
        post = next(request for request in self.sent if request['method'] == 'POST')
        self.assertIn(self.fit_files[-1], post['body'])
        self.assert_source_untouched()

    def test_unknown_requests_protect_actual_fit_and_json_precision_overlap(self):
        token = self.state / 'synthetic-token'
        token.write_bytes(b'unchanged local token')
        cases = (
            ('fit_forward', '.999888', '1.999889', True, True, False, True),
            ('fit_raw_boundary', '.999888', '1.999888', True, True, False, True),
            ('fit_same_source', '.999888', '1.999889', True, True, True, True),
            ('fit_reverse_boundary', '1.000001', '0', True, True, False, True),
            ('fit_reverse_outside', '1.000001', '-.000001', True, True, False, False),
            ('fit_two_seconds', '.999888', '2.999888', True, True, False, False),
            ('json_forward', '.999888', '1.999889', False, False, False, True),
            ('json_reverse_boundary', '1.000001', '0', False, False, False, True),
            ('json_reverse_outside', '1.000001', '-.000001', False, False, False, False),
            ('old_fit_new_json', '1.000001', '0', True, False, False, True),
            ('old_json_new_fit', '.999888', '1.999889', False, True, False, True),
        )
        epoch = h.aware('2020-02-20T00:00:00+00:00')
        for name, old_offset, new_offset, old_fit, new_fit, same_source, blocked in cases:
            with self.subTest(case=name):
                self.db.execute('DELETE FROM uploads'); self.db.commit()
                self.sent.clear(); self.rows = []
                old = h.Measurement(epoch + timedelta(seconds=float(old_offset)), 70,
                                    23.5 if old_fit else None, 'PICOOC')
                candidate = h.Measurement(epoch + timedelta(seconds=float(new_offset)), 71,
                                          24 if new_fit else None, 'PICOOC' if same_source else 'NEW')
                wire_times = []
                def server(method, path, pool):
                    if method == 'POST':
                        if '/upload' in path:
                            wire_times.append(fit_weight_timestamp(self.fit_files[-1]))
                        else:
                            payload = json.loads(self.sent[-1]['body'])
                            wire_times.append(h.aware(payload['gmtTimestamp'] + '+00:00'))
                        return 200, {'detailedImportResult': {'successes': [{}], 'failures': []}}
                    return 200, self.read_payload(path)
                self.handler = server
                self.assertEqual(h.process_record(self.api, self.db, old, sleep=lambda _: None), 'uncertain')
                previous = dict(h.state_row(self.db, old))
                expected_old = old.at.replace(microsecond=0 if old_fit else old.at.microsecond // 1000 * 1000)
                self.assertEqual(wire_times, [expected_old])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(h.process_record(self.api, self.db, candidate, sleep=lambda _: None), 'uncertain')
                self.assertEqual(dict(h.state_row(self.db, old)), previous)
                self.assertEqual(sum(request['method'] == 'POST' for request in self.sent), 1 if blocked else 2)
                if blocked:
                    self.assertIsNone(h.state_row(self.db, candidate))
                else:
                    self.assertEqual(h.state_row(self.db, candidate)['status'], 'uncertain')
                    self.assertGreater(abs((wire_times[1] - old.at).total_seconds()), 1)
                    self.assertGreater(abs((wire_times[0] - candidate.at).total_seconds()), 1)
                self.assertFalse(any(request['method'] == 'DELETE' for request in self.sent))
                self.assertEqual(token.read_bytes(), b'unchanged local token')
                self.assert_source_untouched()

    def test_precision_overlap_does_not_change_existing_readonly_matched_contract(self):
        old = h.Measurement(h.aware('2020-02-20T00:00:00.200000+00:00'), 70, 23.5, 'PICOOC')
        self.assertEqual(h.process_record(self.api, self.db, old, sleep=lambda _: None), 'uncertain')
        previous = dict(h.state_row(self.db, old))
        actual = fit_weight_timestamp(self.fit_files[-1])
        renamed = h.Measurement(old.at + timedelta(milliseconds=200), 70, 23.5, 'NEW')
        self.rows = [fixtures.row(h.Measurement(actual, 70, 23.5, 'REMOTE'), 200)]
        self.assertEqual(h.process_record(self.api, self.db, renamed), 'verified')
        self.assertEqual(dict(h.state_row(self.db, old)), previous)
        self.assertEqual(sum(request['method'] == 'POST' for request in self.sent), 1)
        self.assert_source_untouched()

    def test_same_key_optional_revision_preserves_unknown_request_wire_mode(self):
        token = self.state / 'synthetic-token'
        token.write_bytes(b'unchanged local token')
        old = h.Measurement(h.aware('2020-02-20T00:00:01.500001+00:00'), 70, 23.5, 'PICOOC')
        middle = h.Measurement(old.at, 70, None, 'PICOOC')
        candidate = h.Measurement(h.aware('2020-02-20T00:00:00+00:00'), 71, None, 'NEW')
        self.cfg['allowed_sources'] = ['PICOOC', 'NEW']
        for mode in ('process_missing', 'process_conflict', 'sync_missing', 'sync_conflict', 'sync_repair'):
            with self.subTest(mode=mode):
                self.db.execute('DELETE FROM uploads'); self.db.commit()
                self.sent.clear(); self.rows = []
                (self.state / 'last_sync_comparison.json').unlink(missing_ok=True)
                def server(method, path, pool):
                    return 200, ({'detailedImportResult': {'successes': [{}], 'failures': []}}
                                 if method == 'POST' else self.read_payload(path))
                self.handler = server
                fixtures.ReconciliationTests.write_export(self, [old])
                self.source_bytes = (self.inbox / 'health.json').read_bytes()
                self.assertEqual(h.load_measurements(self.cfg)[1], [old])
                self.assertEqual(h.process_record(self.api, self.db, old, sleep=lambda _: None), 'uncertain')
                wire = fit_weight_timestamp(self.fit_files[-1])
                self.assertEqual(wire.isoformat(), '2020-02-20T00:00:01+00:00')
                self.assertEqual(abs((wire - candidate.at).total_seconds()), 1)
                self.assert_source_untouched()
                current = h.Measurement(old.at, 70, None, 'PICOOC', 22) if mode == 'sync_repair' else middle
                fixtures.ReconciliationTests.write_export(self, [current])
                self.source_bytes = (self.inbox / 'health.json').read_bytes()
                self.assertEqual(h.load_measurements(self.cfg)[1], [current])
                if mode.endswith('conflict'):
                    self.rows = [fixtures.row(h.Measurement(wire, 72, 25, 'REMOTE'), 200)]
                elif mode == 'sync_repair':
                    self.rows = [fixtures.row(h.Measurement(wire, 70, 23.5, 'REMOTE'), 200)]
                with contextlib.redirect_stdout(io.StringIO()):
                    if mode.startswith('sync'):
                        with patch.object(h, 'api_client', return_value=self.api):
                            h.run_sync(self.cfg)
                    else:
                        self.assertEqual(h.process_record(self.api, self.db, current), 'uncertain')
                persisted = h.state_row(self.db, old)
                self.assertEqual(persisted['core'], current.core)
                self.assertEqual(json.loads(persisted['payload']), current.payload())
                self.assertIn(persisted['status'], ('inflight', 'uncertain', 'repair_uncertain'))
                self.assert_source_untouched()
                self.rows = []
                fixtures.ReconciliationTests.write_export(self, [candidate])
                self.source_bytes = (self.inbox / 'health.json').read_bytes()
                self.assertEqual(h.load_measurements(self.cfg)[1], [candidate])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(h.process_record(self.api, self.db, candidate, sleep=lambda _: None), 'uncertain')
                self.assertIsNone(h.state_row(self.db, candidate))
                self.assertEqual(sum(request['method'] == 'POST' for request in self.sent), 1)
                self.assertEqual(h.state_row(self.db, old)['core'], current.core)
                self.assertEqual(json.loads(h.state_row(self.db, old)['payload']), current.payload())
                with patch('builtins.input', return_value='ABSENT'), contextlib.redirect_stdout(io.StringIO()):
                    h.resolve_record(argparse.Namespace(record_id=old.key, present=False, absent=True, ignore=False))
                self.assertEqual(h.state_row(self.db, old)['status'], 'pending')
                self.assertEqual(h.process_record(self.api, self.db, candidate, sleep=lambda _: None), 'uncertain')
                self.assertEqual(sum(request['method'] == 'POST' for request in self.sent), 2)
                self.assertFalse(any(request['method'] == 'DELETE' for request in self.sent))
                self.assertEqual(token.read_bytes(), b'unchanged local token')
                self.assert_source_untouched()

    def test_deleted_but_timed_out_review_keeps_journal_and_verify_never_redeletes(self):
        original = fixtures.row(self.r)
        original['bodyFat'] = None
        self.rows = [original]
        plan = h.comparison_plan(self.db, [self.r], {'dateWeightList': self.rows}, self.r.at.date(), self.r.at.date())
        plan_path = h.save_comparison(plan, self.cfg)
        def server(method, path, pool):
            if method == 'DELETE':
                self.assert_durable_status('repair_uncertain')
                journal = json.loads(next((self.state / 'operations').glob('*.json')).read_text())
                self.assertEqual(journal['phase'], 'deleting')
                self.assertEqual(journal['original'], original)
                self.rows = []
                raise self.urllib3.exceptions.ReadTimeoutError(pool, path, 'synthetic deleted response timeout')
            self.assertEqual(method, 'GET')
            return 200, self.read_payload(path)
        self.handler = server
        args = argparse.Namespace(plan=str(plan_path), record_id=self.r.key, remote_id='100', complete_export=True)
        with patch.object(h, 'api_client', return_value=self.api), \
                patch('builtins.input', return_value='REPLACE ' + self.r.key), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(h.SafeError, '结果未确定'):
                h.reviewed_operation(args, self.cfg)
            journal_path = next((self.state / 'operations').glob('*.json'))
            journal = json.loads(journal_path.read_text())
            self.assertEqual(journal['phase'], 'uncertain')
            self.assertEqual(journal['replacement']['lbm'], self.r.lbm)
            self.assertFalse(h.verify_operation(argparse.Namespace(operation=str(journal_path))))
        self.assert_durable_status('repair_uncertain')
        checked = json.loads(journal_path.read_text())
        self.assertEqual(checked['phase'], 'uncertain')
        self.assertIs(checked['last_check_passed'], False)
        self.assertIn('last_checked_at', checked)
        self.assertEqual(sum(request['method'] == 'DELETE' for request in self.sent), 1)
        self.assertFalse(any(request['method'] == 'POST' for request in self.sent))
        self.assert_source_untouched()


if __name__ == '__main__':
    unittest.main()
