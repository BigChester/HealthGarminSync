"""Exercise Python publishing and the real Swift import protocol with a mock Health store."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from health_garmin_sync.health_outbox import export_blood_pressure  # noqa: E402


class FakeGarmin:
    profile_id = 42

    def get_blood_pressure(self, day):
        assert day == "2026-10-15"
        rows = [
            {
                "version": version,
                "measurementTimestampGMT": f"2026-10-14T16:30:{second:02d}.123456",
                "measurementTimestampLocal": f"2026-10-15T00:30:{second:02d}.123456",
                "sourceType": "MANUAL",
                "systolic": 121,
                "diastolic": 79,
                "pulse": pulse,
                "multiMeasurement": False,
            }
            for version, second, pulse in ((1001, 1, 67), (1002, 2, None))
        ]
        return {"measurementSummaries": [{"numOfMeasurements": 2, "measurements": rows}]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", type=Path, required=True)
    args = parser.parse_args()
    driver = args.driver.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="health-bridge-e2e-") as directory:
        root = Path(directory)
        cloud = root / "cloud"
        state = root / "private" / "garmin_health.sqlite3"
        day = date(2026, 10, 15)

        def export():
            return export_blood_pressure(
                FakeGarmin(), outbox=cloud, since=day, until=day, batch_size=1, state_path=state
            )

        result = export()
        assert result["new_records"] == 2 and result["queued_records"] == 2
        published_ids = []
        for remaining in (1, 0):
            original = (cloud / "pending.json").read_bytes()
            packet = json.loads(original)
            published_ids.append(packet["records"][0]["id"])
            subprocess.run([str(driver), str(cloud)], check=True, timeout=30)
            receipt_bytes = (cloud / "receipt.json").read_bytes()
            receipt = json.loads(receipt_bytes)
            assert receipt["payload_sha256"] == hashlib.sha256(original).hexdigest()
            assert receipt["batch_id"] == packet["batch_id"]
            assert receipt["records"] == [
                {"id": row["id"], "revision": row["revision"]} for row in packet["records"]
            ]
            # Restart the Swift process: an exact existing receipt is reused unchanged.
            subprocess.run([str(driver), str(cloud)], check=True, timeout=30)
            assert (cloud / "receipt.json").read_bytes() == receipt_bytes
            result = export()
            assert result["acknowledged_records"] == 1 and result["queued_records"] == remaining
            assert len(list(cloud.glob("*.json"))) <= 2
        assert len(set(published_ids)) == 2
        assert not list(cloud.iterdir())
        result = export()
        assert result["queued_records"] == result["published_batches"] == 0
        assert not list(cloud.iterdir())
        with sqlite3.connect(state) as database:
            assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert database.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 2
            assert (
                database.execute(
                    "SELECT COUNT(*) FROM measurements WHERE delivered_revision=revision"
                ).fetchone()[0]
                == 2
            )
            assert database.execute("SELECT COUNT(*) FROM source_responses").fetchone()[0] >= 3
            assert database.execute("SELECT COUNT(*) FROM batch").fetchone()[0] == 0
    print("Python → Swift mock Health → receipt → Python: passed; cloud queue empty.")


if __name__ == "__main__":
    main()
