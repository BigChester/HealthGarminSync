"""Package boundaries and module entries remain safe to inspect offline."""

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from project_fixtures import BOOTSTRAP_IMPORTS, COMMAND_GUARDS, REPO_ROOT, copy_project


class PackageArchitectureTests(unittest.TestCase):
    def test_package_dependencies_are_acyclic_and_do_not_import_old_entries(self):
        package = REPO_ROOT / "health_garmin_sync"
        modules = {
            path.stem: ast.parse(path.read_text(encoding="utf-8")) for path in package.glob("*.py")
        }
        graph = {name: set() for name in modules}
        old_entries = {
            "health_garmin",
            "agent_runner",
            "source_archive",
            "replace_history",
            "diagnostics",
        }
        for name, tree in modules.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(old_entries & {alias.name for alias in node.names}, name)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        dependencies = (
                            [node.module.split(".")[0]]
                            if node.module
                            else [alias.name for alias in node.names]
                        )
                        graph[name].update(dep for dep in dependencies if dep in modules)
                    else:
                        self.assertNotIn(node.module, old_entries, name)
        pending, done = set(), set()

        def visit(name):
            self.assertNotIn(name, pending, f"package dependency cycle through {name}")
            if name in done:
                return
            pending.add(name)
            for dependency in graph[name]:
                visit(dependency)
            pending.remove(name)
            done.add(name)

        for name in graph:
            visit(name)

    def test_export_codec_does_not_load_sources_or_archive(self):
        script = """
import sys
from health_garmin_sync import exports
assert 'health_garmin_sync.sources' not in sys.modules
assert 'health_garmin_sync.source_archive' not in sys.modules
assert 'garminconnect' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_modules_import_without_sdk_network_or_runtime_writes(self):
        script = """
import builtins
import importlib
import pkgutil
import socket

def forbidden(*args, **kwargs):
    raise AssertionError('network or SDK import during package inspection')
socket.create_connection = socket.getaddrinfo = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
original_import = builtins.__import__
def offline_import(name, *args, **kwargs):
    if name.split('.')[0] in ('garminconnect', 'requests', 'urllib3', 'curl_cffi', 'garth'):
        return forbidden()
    return original_import(name, *args, **kwargs)
builtins.__import__ = offline_import
import health_garmin_sync
for module in pkgutil.iter_modules(health_garmin_sync.__path__):
    if module.name != '__main__':
        importlib.import_module('health_garmin_sync.' + module.name)
"""
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            copy_project(project)
            before = set(project.rglob("*"))
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(project.rglob("*")), before)

    def test_canonical_help_entry_works_in_an_isolated_project(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            copy_project(project)
            before = set(project.rglob("*"))
            result = subprocess.run(
                [sys.executable, "-B", "-m", "health_garmin_sync", "--help"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("verify-operation", result.stdout)
            self.assertIn("doctor", result.stdout)
            self.assertEqual(set(project.rglob("*")), before)

    def test_bootstrap_command_guards_patch_the_application_call_sites(self):
        bootstrap = (
            BOOTSTRAP_IMPORTS
            + """
def forbidden(*args, **kwargs):
    raise AssertionError('application business boundary blocked')
"""
            + COMMAND_GUARDS
        )
        script = """
from health_garmin_sync import runtime, garmin, sync, launchagent
for function, arguments in ((runtime.config, ()), (garmin.api_client, ()),
                            (sync.run_with_attempt, ()), (launchagent.manage_agent, (False,))):
    try:
        function(*arguments)
    except AssertionError as exc:
        assert str(exc) == 'application business boundary blocked'
    else:
        raise AssertionError('fixture guard did not patch its application function')
"""
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            copy_project(project, bootstrap)
            before = set(project.rglob("*"))
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(project.rglob("*")), before)
