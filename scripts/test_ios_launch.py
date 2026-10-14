"""Install and launch the app in a fresh CI simulator without importing health data."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


def simctl(*arguments: str, timeout: int = 60) -> str:
    return subprocess.run(
        ["xcrun", "simctl", *arguments], check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    args = parser.parse_args()
    app = args.app.resolve(strict=True)
    devices = json.loads(simctl("list", "devices", "available", "--json"))["devices"]
    candidates = [
        (runtime, device)
        for runtime, entries in devices.items()
        if ".iOS-" in runtime
        for device in entries
        if device.get("isAvailable", True)
        and device["name"].startswith("iPhone")
        and device.get("deviceTypeIdentifier")
    ]
    if not candidates:
        raise RuntimeError("Install an iOS Simulator runtime before the launch check")
    runtime, device = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    identifier = simctl(
        "create", "HealthGarminSync-CI", device["deviceTypeIdentifier"], runtime
    ).strip()
    try:
        simctl("boot", identifier)
        simctl("bootstatus", identifier, "-b", timeout=180)
        simctl("install", identifier, str(app))
        bundle = "org.healthgarminsync.bridge"
        result = simctl("launch", "--terminate-running-process", identifier, bundle)
        match = re.search(r":\s*(\d+)\s*$", result)
        if not match:
            raise RuntimeError("Simulator did not report an app process")
        process = match.group(1)
        for _ in range(4):
            time.sleep(2)
            jobs = simctl("spawn", identifier, "launchctl", "list")
            if not any(bundle in line and line.split()[0] == process for line in jobs.splitlines()):
                raise RuntimeError("App exited during startup; inspect Simulator crash logs")
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        simctl("io", identifier, "screenshot", str(args.screenshot))
        assert args.screenshot.stat().st_size > 1000
        print("iOS Simulator app launch remained active; first-screen screenshot saved.")
    finally:
        subprocess.run(["xcrun", "simctl", "shutdown", identifier], capture_output=True, timeout=30)
        subprocess.run(["xcrun", "simctl", "delete", identifier], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
