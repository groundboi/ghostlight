"""Loads the extensionless `ghostlight` script as an importable module."""
import importlib.machinery
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

# Keep __pycache__ out of the repo; recompiling one small script per test
# run costs nothing.
sys.dont_write_bytecode = True

_SCRIPT = Path(__file__).resolve().parent.parent / "ghostlight"
_loader = importlib.machinery.SourceFileLoader("ghostlight", str(_SCRIPT))
_spec = importlib.util.spec_from_loader("ghostlight", _loader)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["ghostlight"] = _mod
_loader.exec_module(_mod)


def pytest_sessionfinish(session, exitstatus):
    # This conftest is compiled by pytest's assertion rewriter before the
    # dont_write_bytecode flag above takes effect, so its cache is swept here.
    shutil.rmtree(Path(__file__).parent / "__pycache__", ignore_errors=True)


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    """Point the state dir at a per-test tmp dir."""
    d = tmp_path / "state"
    monkeypatch.setenv("GHOSTLIGHT_STATE_DIR", str(d))
    return d
