"""Submit and verify one measurement with durable duplicate-write protection."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, timedelta
from typing import Any

from . import domain as hg_domain
from . import garmin as hg_garmin
from . import reconciliation as hg_reconciliation
from . import remote as hg_remote
from . import state as hg_state


def remote_for(api: Any, r: hg_domain.Measurement) -> Any:
    d = r.at.astimezone(UTC).date()
    return api.get_body_composition(
        (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat()
    )


def process_record(
    api: Any,
    db: sqlite3.Connection,
    r: hg_domain.Measurement,
    sleep=time.sleep,
    reviewed: bool = False,
    *,
    local_record: hg_domain.Measurement | None = None,
) -> str:
    local = r if local_record is None else local_record
    if local.key != r.key:
        raise hg_domain.SafeError("本地测量与提交目标不一致，不能保存上传状态。")

    def persist(status, detail):
        if local_record is not None:
            return hg_state.save_snapshot_status(
                db, local, status, detail, request=r if status == "inflight" else None
            )
        hg_state.set_status(db, local, status, detail, request=r if status == "inflight" else None)
        return True

    row = hg_state.state_row(db, local)
    previous = row["status"] if row else "pending"
    if previous == "ignored":
        return previous
    # Reading errors happen before the write and are safe to try on the next run.
    classification = hg_remote.classify_remote(r, remote_for(api, r))
    if classification == "matched":
        persist("verified", "Read-back matched time, weight, fat and available BMI")
        return "verified"
    if not reviewed and r.key in hg_state.active_operations():
        persist(
            "repair_uncertain",
            "Unfinished reviewed operation; verify or explicitly resume its journal",
        )
        return "repair_uncertain"
    if not reviewed and (
        alias := hg_reconciliation.unknown_source_alias(r, hg_state.unknown_request_context(db))
    ):
        print(alias)
        return "uncertain"  # Do not create another unknown marker for a request never sent.
    if previous in ("inflight", "uncertain", "repair_uncertain"):
        # Never blindly re-POST an uncertain write, even when an immediate read is empty.
        persist("uncertain", "Awaiting remote visibility or explicit manual resolution")
        return "uncertain"
    if (
        classification == "absent"
        and not reviewed
        and not (row and previous == "pending")
        and (
            alias := hg_reconciliation.missing_source_alias(r, hg_state.verified_source_context(db))
        )
    ):
        persist("remote_missing", alias)
        return "remote_missing"
    if classification == "absent" and previous in (
        "verified",
        "verified_manual",
        "verified_conflict",
        "remote_missing",
    ):
        persist(
            "remote_missing",
            "Previously verified record is missing; explicit absence confirmation required",
        )
        return "remote_missing"
    if classification == "conflict":
        result = (
            "verified_conflict"
            if previous in ("verified", "verified_manual", "verified_conflict", "remote_missing")
            else "conflict"
        )
        persist(result, "Existing remote values differ or timestamp correspondence is ambiguous")
        return result
    if not persist("inflight", "Network write may occur; do not blindly retry"):
        raise hg_domain.SafeError("本地测量已修订或忽略，停止提交旧操作；请重新对账。")
    try:
        if r.fat is None and r.bmi is None:
            response = api.add_weigh_in(
                weight=r.weight, unitKey="kg", timestamp=r.at.isoformat(timespec="milliseconds")
            )
        else:
            with hg_garmin.utc_fit_clock():
                # UTC input fixes 0.3.16's mktime handling; the SDK encodes FIT seconds.
                response = api.add_body_composition(
                    timestamp=r.at.astimezone(UTC).isoformat(timespec="seconds"),
                    weight=r.weight,
                    percent_fat=r.fat,
                    bmi=r.bmi,
                )
        if isinstance(response, dict):
            result = response.get("detailedImportResult", {})
            if isinstance(result, dict) and result.get("failures"):
                persist("uncertain", "Garmin import response reported failures; inspect remotely")
                return "uncertain"
        for delay in (0, 3, 7):
            if delay:
                sleep(delay)
            if hg_remote.classify_remote(r, remote_for(api, r)) == "matched":
                persist("verified", "Read-back matched after upload")
                return "verified"
        persist("uncertain", "Submitted but not verified by remote read-back")
    except Exception as exc:
        persist("uncertain", hg_domain.safe_exception(exc))
    return "uncertain"
