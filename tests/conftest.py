"""Load the integration's modules without running its package __init__.

The package's __init__ sets up the whole integration, so a stub package named "bsp" is
registered that points at the same folder. Relative imports between the modules then resolve
normally. The pure logic modules need nothing else. The controller tests (test_controller*.py,
test_removal.py, test_unload.py) also need Home Assistant installed, and skip without it.

With BSP_NO_SKIPS=1 (set in CI) any skipped test fails the run, so a Home Assistant import
that breaks cannot leave the controller tests skipped with the job still passing.
"""

import os
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "byd_smart_poll"

if "bsp" not in sys.modules:
    stub = types.ModuleType("bsp")
    stub.__path__ = [str(PKG_DIR)]
    sys.modules["bsp"] = stub


_skipped: list[str] = []


def pytest_collectreport(report) -> None:
    if report.skipped:
        _skipped.append(report.nodeid)


def pytest_runtest_logreport(report) -> None:
    if report.skipped:
        _skipped.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    if os.environ.get("BSP_NO_SKIPS") == "1" and _skipped and exitstatus == 0:
        print(f"BSP_NO_SKIPS: {len(_skipped)} skipped, failing the run: {_skipped}")
        session.exitstatus = 1
