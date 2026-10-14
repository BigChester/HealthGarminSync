"""Lazily load the pinned SDK and guard credentials and complete weigh-in I/O."""

from __future__ import annotations

import contextlib
import getpass
import logging
import os
import time
from datetime import date
from importlib.metadata import version
from typing import Any

from . import domain as hg_domain
from . import file_io as hg_file_io
from . import remote as hg_remote
from . import runtime as hg_runtime


@contextlib.contextmanager
def utc_fit_clock():
    """0.3.16 FIT uses mktime(t.timetuple()), ignoring a datetime's offset.
    Use UTC only in this single-threaded process and pass UTC wall time.
    Does NOT modify macOS time-zone settings. Also correct for newer timestamp().
    """
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def guard_token_persistence(client):
    """Watch native refresh saves without rewriting unchanged token caches."""
    failure = None
    original_dump, original_headers = client.dump, client.get_api_headers

    def check():
        if failure is not None:
            raise hg_domain.TokenPersistenceError(
                f"Garmin 令牌续期保存失败：{failure}。"
                "检查 state/tokens 权限和磁盘后运行 ./health-garmin-sync login。"
            )

    def dump(path):
        nonlocal failure
        try:
            result = original_dump(path)
        except Exception as exc:
            failure = hg_domain.safe_exception(exc)
            raise
        else:
            failure = None
            return result

    def headers():
        # The pinned SDK builds headers before both the initial send and its
        # one allowed 401 replay, after any refresh that may swallow dump errors.
        check()
        return original_headers()

    client.dump, client.get_api_headers = dump, headers
    return check


def api_client(interactive: bool = False):
    try:
        installed = version("garminconnect")
        if installed != hg_runtime.REQUIRED_GARMIN_VERSION:
            raise hg_domain.SafeError(
                f"需要 garminconnect=={hg_runtime.REQUIRED_GARMIN_VERSION}，当前为 {installed}。"
            )
        from garminconnect import Garmin
    except ImportError as exc:
        raise hg_domain.SafeError("缺少依赖；运行 bash scripts/setup.sh。") from exc
    token_dir = hg_runtime.STATE / "tokens"
    hg_file_io.private_dir(token_dir)
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    if interactive:
        email = input("Garmin 中国区登录邮箱：").strip()
        password = getpass.getpass("密码（不会显示，也不写入文件）：")
        api = Garmin(
            email=email,
            password=password,
            is_cn=True,
            prompt_mfa=lambda: getpass.getpass("验证码：").strip(),
        )
        password = None
    else:
        api = Garmin(is_cn=True)
    check_saved = None if interactive else guard_token_persistence(api.client)
    try:
        api.login(str(token_dir))
    except Exception as exc:
        if check_saved is not None:
            check_saved()
        # Do not print raw exception text: upstream URLs can contain tickets.
        code = hg_domain.safe_exception(exc)
        raise hg_domain.SafeError(
            f"Garmin 登录/令牌恢复失败：{code}。网络正常后运行 ./health-garmin-sync login。"
        ) from exc
    if check_saved is not None:
        check_saved()
    if interactive:
        if not api.client.di_token:
            raise hg_domain.SafeError(
                "Garmin 登录未取得可恢复的令牌；请稍后重新运行 ./health-garmin-sync login。"
            )
        try:
            api.client.dump(str(token_dir))
        except Exception as exc:
            raise hg_domain.SafeError(
                f"Garmin 令牌保存失败：{hg_domain.safe_exception(exc)}。"
                "检查 state/tokens 权限和磁盘后运行 ./health-garmin-sync login。"
            ) from exc
    return GarminMeasurementAPI(api, check_saved)


class GarminMeasurementAPI:
    """Use full weigh-in details; dateRange only exposes the latest each day."""

    def __init__(self, api, check_saved=None):
        self.api = api
        self.check_saved = check_saved

    def __getattr__(self, name):
        return getattr(self.api, name)

    def get_body_composition(self, start: str, end: str | None = None):
        try:
            return self._get_body_composition(start, end)
        except Exception:
            if self.check_saved is not None:
                self.check_saved()
            raise

    def _get_body_composition(self, start: str, end: str | None = None):
        end = end or start
        if start == end:
            obj = self.api.get_daily_weigh_ins(start)
            self._validate_full_details(obj)
            return obj
        obj = self.api.get_weigh_ins(start, end)
        if not isinstance(obj, dict) or not isinstance(obj.get("dailyWeightSummaries"), list):
            raise hg_domain.SafeError("Garmin 完整称重明细结构未知，停止，不使用每日最后值替代。")
        rows = []
        for summary in obj["dailyWeightSummaries"]:
            if not isinstance(summary, dict) or not isinstance(
                summary.get("allWeightMetrics"), list
            ):
                raise hg_domain.SafeError("Garmin 每日称重明细不完整，停止。")
            items, count = summary["allWeightMetrics"], summary.get("numOfWeightEntries")
            if isinstance(count, bool) or not isinstance(count, int) or count != len(items):
                raise hg_domain.SafeError("Garmin 每日称重条数与明细不一致，停止。")
            day = summary.get("summaryDate")
            if not isinstance(day, str) or not start <= day <= end:
                raise hg_domain.SafeError("Garmin 明细日期超出请求范围，停止。")
            date.fromisoformat(day)
            if any(not isinstance(row, dict) or row.get("calendarDate") != day for row in items):
                raise hg_domain.SafeError("Garmin 每日明细的测量日期不一致，停止。")
            rows.extend(items)
        result = {"dateWeightList": rows}
        self._validate_full_details(result)
        return result

    @staticmethod
    def _validate_full_details(obj: Any) -> None:
        rows = hg_remote.RemoteIndex(obj).rows
        identities = set()
        for row in rows:
            raw = row.get("samplePk")
            if raw is None:
                continue
            pk = hg_remote.canonical_remote_id(raw)
            identity = (pk if pk is not None else str(raw), str(row.get("calendarDate")))
            if identity in identities:
                raise hg_domain.SafeError("Garmin 完整称重明细包含重复记录，停止。")
            identities.add(identity)
