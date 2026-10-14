"""Copy isolated application packages and patch only their test boundaries."""

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def copy_project(destination, bootstrap=""):
    """Prepare a private executable project without copying runtime data."""
    destination = Path(destination)
    package = destination / "health_garmin_sync"
    shutil.copytree(
        REPO_ROOT / "health_garmin_sync",
        package,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    if bootstrap:
        # The fixture explicitly imports its patches before either real entry.
        # Production has no environment switch or alternate dependency loader.
        (destination / "fixture_bootstrap.py").write_text(bootstrap, encoding="utf-8")
        init = package / "__init__.py"
        init.write_text(
            init.read_text(encoding="utf-8") + "\nimport fixture_bootstrap\n", encoding="utf-8"
        )


BOOTSTRAP_IMPORTS = """import contextlib
import sqlite3
import logging
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from health_garmin_sync import cli as hg_cli, domain as hg_domain, exports as hg_exports, file_io as hg_file_io, garmin as hg_garmin, launchagent as hg_launchagent, operations as hg_operations, processing as hg_processing, reconciliation as hg_reconciliation, remote as hg_remote, runtime as hg_runtime, sources as hg_sources, state as hg_state, sync as hg_sync
"""


COMMAND_GUARDS = """
hg_runtime.config = hg_garmin.api_client = hg_sync.run_with_attempt = hg_launchagent.manage_agent = forbidden
"""
