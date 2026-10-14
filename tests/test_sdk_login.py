"""Pinned SDK login contracts using synthetic credentials and sealed networking."""

import base64
import contextlib
import getpass as hg_std_getpass
import io
import json
import logging
import os
import socket
import stat
import sys
import tempfile
import time
import unittest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from health_garmin_sync import cli as hg_cli
from health_garmin_sync import domain as hg_domain
from health_garmin_sync import garmin as hg_garmin
from health_garmin_sync import runtime as hg_runtime

try:
    PINNED_SDK = version("garminconnect") == hg_runtime.REQUIRED_GARMIN_VERSION
except PackageNotFoundError:
    PINNED_SDK = False

SECRET = "synthetic-private-error-never-output"
TOKENS = {
    "di_token": "synthetic-access",
    "di_refresh_token": "synthetic-refresh",
    "di_client_id": "synthetic-client",
}


def forbidden(*args, **kwargs):
    raise AssertionError("Real network or user-home access is forbidden")


@unittest.skipUnless(PINNED_SDK, "Requires the installed, pinned Garmin SDK")
class SDKLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.tokens = self.state / "tokens"
        self.token_file = self.tokens / "garmin_tokens.json"
        for name, value in (
            ("BASE", self.root),
            ("STATE", self.state),
            ("CONFIG", self.root / "config.json"),
        ):
            self.enterContext(patch.object(hg_runtime, name, value))
        self.enterContext(patch.object(Path, "home", forbidden))
        for name in ("create_connection", "getaddrinfo"):
            self.enterContext(patch.object(socket, name, forbidden))
        for name in ("connect", "connect_ex"):
            self.enterContext(patch.object(socket.socket, name, forbidden))
        try:
            import garminconnect
            import requests
        except ImportError:
            self.skipTest("Pinned SDK login dependencies are unavailable")
        self.sdk = garminconnect
        self.client_type = garminconnect.client.Client
        for name in ("request", "get", "post", "put", "patch", "delete", "head", "options"):
            self.enterContext(patch.object(requests, name, forbidden))
            self.enterContext(patch.object(requests.api, name, forbidden))
        self.enterContext(patch.object(requests.Session, "request", forbidden))
        self.enterContext(patch.object(requests.Session, "send", forbidden))
        self.enterContext(patch.object(requests.adapters.HTTPAdapter, "send", forbidden))
        self.enterContext(patch.object(requests.sessions, "get_netrc_auth", forbidden))
        try:
            import curl_cffi
            from curl_cffi import requests as curl_requests
        except ImportError:
            pass
        else:
            for name in ("request", "get", "post", "put", "patch", "delete", "head", "options"):
                if hasattr(curl_requests, name):
                    self.enterContext(patch.object(curl_requests, name, forbidden))
            for cls in (curl_requests.Session, curl_requests.AsyncSession):
                self.enterContext(patch.object(cls, "request", forbidden))
            self.enterContext(patch.object(curl_cffi.Curl, "perform", forbidden))
        for name in ("garminconnect", "garminconnect.client", "urllib3"):
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
            logger.setLevel(logging.CRITICAL)
        self.instances = []
        original_init = garminconnect.Garmin.__init__

        def construct(instance, *args, **kwargs):
            original_init(instance, *args, **kwargs)
            instance.client.cs.trust_env = instance.client._api_session.trust_env = False
            self.addCleanup(instance.client.cs.close)
            self.addCleanup(instance.client._api_session.close)
            self.instances.append(instance)

        self.enterContext(patch.object(garminconnect.Garmin, "__init__", construct))
        self.native_refresh = self.client_type._refresh_session
        self.refresh = self.enterContext(
            patch.object(self.client_type, "_refresh_session", side_effect=forbidden)
        )
        self.profile = self.enterContext(
            patch.object(garminconnect.Garmin, "_load_profile_and_settings", return_value=None)
        )
        self.credential_login = self.enterContext(
            patch.object(self.client_type, "login", autospec=True, side_effect=forbidden)
        )
        self.read_email = self.enterContext(patch("builtins.input", side_effect=forbidden))
        self.read_password = self.enterContext(
            patch.object(hg_std_getpass, "getpass", side_effect=forbidden)
        )

    def allow_interactive(self):
        self.read_email.side_effect = None
        self.read_email.return_value = "synthetic@example.invalid"
        self.read_password.side_effect = None
        self.read_password.return_value = "synthetic-password"

        def credential_login(client, email, password, **kwargs):
            self.assertEqual(email, "synthetic@example.invalid")
            self.assertEqual(password, "synthetic-password")
            for key, value in TOKENS.items():
                setattr(client, key, value)
            return None, None

        self.credential_login.side_effect = credential_login

    def save_cache(self, content=None):
        self.tokens.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(TOKENS) if content is None else content)
        return self.token_file.read_bytes()

    def cli_login(self):
        out, err = io.StringIO(), io.StringIO()
        previous_umask = os.umask(0o077)
        try:
            with (
                patch.object(sys, "argv", ["health-garmin-sync", "login"]),
                patch.object(hg_runtime, "config", return_value={"enabled": False}),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = hg_cli.main()
        finally:
            os.umask(previous_umask)
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_interactive_save_failure_never_claims_success_for_new_or_cached_tokens(self):
        for cached, final_only in ((False, False), (True, False), (False, True)):
            with self.subTest(cached=cached, final_only=final_only):
                self.allow_interactive()
                original = self.save_cache() if cached else None
                if not cached and self.token_file.exists():
                    self.token_file.unlink()
                self.credential_login.reset_mock()
                original_dump = self.client_type.dump
                calls = []

                def fail_dump(client, path):
                    calls.append(path)
                    if final_only and len(calls) == 1:
                        return original_dump(client, path)
                    raise OSError(SECRET)

                with patch.object(
                    self.client_type, "dump", autospec=True, side_effect=fail_dump
                ) as dump:
                    code, out, err = self.cli_login()
                self.assertEqual(code, 2)
                self.assertNotIn("已登录 Garmin", out)
                self.assertIn("令牌保存失败", err)
                self.assertIn("OSError", err)
                # Native credential login swallows its first failed dump. A
                # restored cache has no SDK dump, so only our explicit one runs.
                self.assertEqual(dump.call_count, 1 if cached else 2)
                self.assertEqual(self.credential_login.call_count, 0 if cached else 1)
                if cached:
                    self.assertEqual(self.token_file.read_bytes(), original)
                elif final_only:
                    self.assertEqual(json.loads(self.token_file.read_text()), TOKENS)
                else:
                    self.assertFalse(self.token_file.exists())

    def test_successful_private_save_is_restorable_without_prompts_or_an_extra_dump(self):
        self.allow_interactive()
        original_dump = self.client_type.dump
        with patch.object(
            self.client_type, "dump", autospec=True, side_effect=original_dump
        ) as dump:
            code, out, err = self.cli_login()
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("令牌保存在本机", out)
            self.assertEqual(dump.call_count, 2)
            self.assertEqual(json.loads(self.token_file.read_text()), TOKENS)
            self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.tokens.stat().st_mode), 0o700)
            before = self.token_file.read_bytes(), self.token_file.stat().st_mtime_ns
            self.read_email.reset_mock()
            self.read_password.reset_mock()
            self.credential_login.reset_mock()
            self.read_email.side_effect = self.read_password.side_effect = forbidden
            self.credential_login.side_effect = forbidden
            restored = hg_garmin.api_client()
            self.assertEqual(restored.api.client.di_token, TOKENS["di_token"])
            self.assertEqual(dump.call_count, 2)
            self.assertEqual(
                (self.token_file.read_bytes(), self.token_file.stat().st_mtime_ns), before
            )
            self.read_email.assert_not_called()
            self.read_password.assert_not_called()
            self.credential_login.assert_not_called()
            self.refresh.assert_not_called()

    def test_noninteractive_invalid_caches_fail_safely_without_credentials_or_input(self):
        cases = ((None, False), ("{", False), ("{}", False), (json.dumps(TOKENS), True))
        for content, rejected in cases:
            with self.subTest(content_missing=content is None, rejected=rejected):
                if self.token_file.exists():
                    self.token_file.unlink()
                original = self.save_cache(content) if content is not None else None
                self.profile.reset_mock()
                self.profile.side_effect = (
                    self.sdk.GarminConnectAuthenticationError(SECRET) if rejected else None
                )
                with patch.object(self.client_type, "dump", side_effect=forbidden) as dump:
                    with self.assertRaises(hg_domain.SafeError) as caught:
                        hg_garmin.api_client()
                self.assertIn("登录/令牌恢复失败", str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(self.profile.call_count, 1 if rejected else 0)
                dump.assert_not_called()
                self.read_email.assert_not_called()
                self.read_password.assert_not_called()
                self.credential_login.assert_not_called()
                self.refresh.assert_not_called()
                if original is not None:
                    self.assertEqual(self.token_file.read_bytes(), original)
                else:
                    self.assertFalse(self.token_file.exists())

    def test_jwt_only_native_success_is_rejected_as_not_restorable(self):
        self.allow_interactive()

        def jwt_login(client, email, password, **kwargs):
            client.jwt_web = "synthetic-web-session"
            self.assertTrue(client.is_authenticated)
            return None, None

        self.credential_login.side_effect = jwt_login
        original_dump = self.client_type.dump
        with patch.object(
            self.client_type, "dump", autospec=True, side_effect=original_dump
        ) as dump:
            native = self.sdk.Garmin(
                email="synthetic@example.invalid", password="synthetic-password", is_cn=True
            )
            self.assertEqual(native.login(str(self.tokens)), (None, None))
            self.assertTrue(native.client.is_authenticated)
            self.assertIsNone(native.client.di_token)
            self.assertEqual(json.loads(self.token_file.read_text()), dict.fromkeys(TOKENS))
            self.assertEqual(dump.call_count, 1)
            restore = self.sdk.Garmin(is_cn=True)
            with self.assertRaises(self.sdk.GarminConnectAuthenticationError):
                restore.login(str(self.tokens))
            self.assertEqual(self.credential_login.call_count, 1)
            code, out, err = self.cli_login()
            self.assertEqual(code, 2)
            self.assertNotIn("已登录 Garmin", out)
            self.assertIn("可恢复的令牌", err)
            # Native login already dumps its DI-empty cache. The application
            # rejects this session before claiming or explicitly saving success.
            self.assertEqual(dump.call_count, 2)

    def expired_cache_and_refresh_response(self):
        def jwt(exp):
            payload = (
                base64.urlsafe_b64encode(
                    json.dumps({"exp": exp, "client_id": "synthetic-client"}).encode()
                )
                .rstrip(b"=")
                .decode()
            )
            return "e30." + payload + ".synthetic"

        old = dict(TOKENS, di_token=jwt(0))
        new = dict(
            TOKENS, di_token=jwt(time.time() + 86400), di_refresh_token="synthetic-renewed-refresh"
        )
        self.save_cache(json.dumps(old))
        response = SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "access_token": new["di_token"],
                "refresh_token": new["di_refresh_token"],
            },
        )
        return new, response

    def test_automatic_refresh_save_failure_stops_login_without_measurement_requests(self):
        _, response = self.expired_cache_and_refresh_response()
        before = self.token_file.read_bytes()
        with (
            patch.object(self.client_type, "_refresh_session", self.native_refresh),
            patch.object(self.client_type, "_http_post", return_value=response) as refresh,
            patch.object(self.client_type, "dump", side_effect=OSError(SECRET)) as dump,
        ):
            with self.assertRaises(hg_domain.TokenPersistenceError) as caught:
                hg_garmin.api_client()
        self.assertIn("令牌续期保存失败：OSError", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        refresh.assert_called_once()
        dump.assert_called_once_with(str(self.tokens))
        self.assertEqual(self.token_file.read_bytes(), before)
        self.read_email.assert_not_called()
        self.read_password.assert_not_called()
        self.credential_login.assert_not_called()
        # Even if a caller retained the failed instance, it cannot prepare a
        # later measurement request using the new credentials only in memory.
        with self.assertRaises(hg_domain.TokenPersistenceError):
            self.instances[-1].client.get_api_headers()

    def test_automatic_refresh_saves_once_and_next_instance_reuses_the_cache(self):
        renewed, response = self.expired_cache_and_refresh_response()
        original_dump = self.client_type.dump
        with (
            patch.object(self.client_type, "_refresh_session", self.native_refresh),
            patch.object(self.client_type, "_http_post", return_value=response) as refresh,
            patch.object(
                self.client_type, "dump", autospec=True, side_effect=original_dump
            ) as dump,
        ):
            first = hg_garmin.api_client()
            self.assertEqual(first.api.client.di_token, renewed["di_token"])
            self.assertEqual(json.loads(self.token_file.read_text()), renewed)
            self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.tokens.stat().st_mode), 0o700)
            second = hg_garmin.api_client()
            self.assertEqual(second.api.client.di_token, renewed["di_token"])
            self.assertIsNot(first.api, second.api)
        refresh.assert_called_once()
        self.assertEqual(dump.call_count, 1)
        self.credential_login.assert_not_called()
        self.read_email.assert_not_called()
        self.read_password.assert_not_called()

    def test_later_read_refresh_failure_keeps_the_specific_safe_diagnostic(self):
        before = self.save_cache()
        api = hg_garmin.api_client()
        client = api.api.client
        response = SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {
                "access_token": "synthetic-renewed-access",
                "refresh_token": "synthetic-renewed-refresh",
            },
        )
        original_replace = Path.replace

        def replace(path, target):
            if Path(target) == self.token_file:
                raise OSError(SECRET)
            return original_replace(path, target)

        with (
            patch.object(self.client_type, "_refresh_session", self.native_refresh),
            patch.object(client, "_http_post", return_value=response) as refresh,
            patch.object(client, "_token_expires_soon", return_value=True),
            patch.object(Path, "replace", replace),
        ):
            with self.assertRaises(hg_domain.TokenPersistenceError) as caught:
                api.get_body_composition("2020-02-20")
        self.assertIn("令牌续期保存失败：OSError", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        refresh.assert_called_once()
        self.assertEqual(self.token_file.read_bytes(), before)
