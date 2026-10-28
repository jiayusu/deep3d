"""Recovery scenarios against real coordinator, PostgreSQL and CAD worker processes.

Opt-in: set DEEP3D_TEST_ADMIN_DATABASE_URL to a PostgreSQL role that may create
databases; each scenario creates its own database.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = os.environ.get("DEEP3D_TEST_ADMIN_DATABASE_URL")


@pytest.fixture(scope="module")
def recovery():
    if not ADMIN:
        pytest.skip("NOT RUN: set DEEP3D_TEST_ADMIN_DATABASE_URL for real recovery scenarios")
    for name in ("deep3d", "deep3d-runtime"):
        if not (ROOT / "target/debug" / name).is_file():
            pytest.skip("Build the Rust workspace first")
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("g5_recovery", ROOT / "scripts/g5_recovery.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["g5_recovery"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("scenario", ["R02", "R03"])
def test_lost_replies_and_stale_attempts_never_publish_twice(recovery, tmp_path, scenario):
    stack = recovery.Stack(tmp_path / scenario, ADMIN)
    try:
        result = recovery.SCENARIOS[scenario](stack)
    finally:
        stack.stop_all()
    assert all(result["observations"].values()), result["observations"]
