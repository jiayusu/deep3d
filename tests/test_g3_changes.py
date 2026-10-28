"""The G3 change runner on representative cases; these are regressions, not the recorded run."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/debug" / ("deep3d.exe" if sys.platform == "win32" else "deep3d")


def load(name):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    if not BINARY.is_file():
        pytest.skip("Build Rust CLI for native change execution")
    return load("g3_changes")


def change(case_id):
    registry = json.loads((ROOT / "acceptance/v0.1/registry.draft.json").read_text("utf-8"))
    return next(c for c in registry["changes"] if c["id"] == case_id)


def test_every_registered_role_is_bound_before_execution():
    seeds = load("g3_seeds")
    changes = load("g3_changes")
    registry = json.loads((ROOT / "acceptance/v0.1/registry.draft.json").read_text("utf-8"))
    for item in registry["changes"]:
        seed = seeds.SEEDS[item["seed_case"]]()
        case = changes.CHANGES[item["id"]]
        oracle = item["dependency_oracle"]
        for group in ("invalidated_existing_roles", "preserved_roles"):
            _, unbound = changes.expand(oracle[group], seed, {}, case)
            assert not unbound, (item["id"], unbound)
        names = {f["name"] for f in seed.features.values()}
        for role, members in seed.roles.items():
            assert set(members) <= names, (item["seed_case"], role)


@pytest.mark.parametrize("case_id", ["M04", "M16"])
def test_local_edit_runs_only_the_explained_features_and_matches_full_rebuild(
    runner, tmp_path, case_id
):
    result = runner.run_case(change(case_id), tmp_path, sys.executable)
    assert result["pass"], json.dumps(result)[:3000]
    assert result["dependency"]["exact"] and result["incremental"]["reused"]


def test_metadata_rename_recomputes_no_geometry(runner, tmp_path):
    result = runner.run_case(change("M15"), tmp_path, sys.executable)
    assert result["pass"]
    assert result["incremental"]["executed"] == []
    assert result["dependency"]["classes"]["delivery"] == ["spacer_service"]


def test_dangling_reference_is_rejected_and_head_is_kept(runner, tmp_path):
    result = runner.run_case(change("M19"), tmp_path, sys.executable)
    assert result["pass"] and result["outcome"] == "rejected"
    assert result["authoritative_head_unchanged"]
