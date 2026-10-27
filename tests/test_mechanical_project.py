"""Native mechanical engineering regression; these are not NL V0.1 benchmark results."""

import copy
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/debug" / ("deep3d.exe" if sys.platform == "win32" else "deep3d")


def invoke(*args, succeeds=True):
    if not BINARY.is_file():
        pytest.skip("Build Rust CLI for real native mechanical integration")
    process = subprocess.run(
        [str(BINARY), *(str(arg) for arg in args)],
        capture_output=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    payload = json.loads(process.stdout)
    assert (process.returncode == 0) == succeeds, payload
    if succeeds:
        assert payload["status"] == "pass", payload
        return payload["result"]
    return payload


def imported(tmp_path, name):
    project = tmp_path / (name + ".deep3d")
    invoke(
        "import", "--project", project, "--spec", ROOT / "fixtures/mechanical" / (name + ".json")
    )
    return project


def evaluated(project, directory, succeeds=True):
    return invoke(
        "evaluate",
        "--project",
        project,
        "--python",
        sys.executable,
        "--out",
        directory,
        succeeds=succeeds,
    )


@pytest.mark.parametrize("name", ["constrained-plate", "bushing", "rigid-stack"])
def test_editable_native_projects_have_independent_named_step_and_brep_evidence(tmp_path, name):
    project = imported(tmp_path, name)
    original = invoke("show", "--project", project)["snapshot"]
    result = evaluated(project, tmp_path / "built")
    reopened = invoke("show", "--project", project)
    assert reopened["snapshot"]["project"] == original["project"]
    assert reopened["materializations"][0][0] == result["materialization"]
    materialization = reopened["materializations"][0][1]
    assert set(materialization["node_outputs"]) == set(original["project"]["acceptance"]["nodes"])
    native_report = json.loads((tmp_path / "built/verify-brep.json").read_text())
    assert (
        native_report["input_assembly_artifact_sha256"]
        == materialization["files"]["assembly.json"]["sha256"]
    )
    for filename, stage in [("verify-brep.json", "native"), ("verify-step.json", "step")]:
        report = json.loads((tmp_path / "built" / filename).read_text())
        assert report["status"] == "pass" and report["stage"] == stage
        assert report["checks"] and all(c["status"] == "pass" for c in report["checks"])
        assert report["expected_spec"] == original["project"]["acceptance"]
        assert (
            report["input_artifact_sha256"]
            == materialization["files"]["model.brep" if stage == "native" else "model.step"][
                "sha256"
            ]
        )
    exported = tmp_path / "delivered.step"
    invoke(
        "export",
        "--project",
        project,
        "--materialization",
        result["materialization"],
        "--out",
        exported,
    )
    assert exported.read_bytes() == (tmp_path / "built/model.step").read_bytes()
    if name == "rigid-stack":
        assembly = json.loads((tmp_path / "built/assembly.json").read_text())
        assert len(assembly["instances"]) == 6
        assert sorted(part["quantity"] for part in assembly["bom"]) == [2, 4]
        assert len(assembly["bom"]) == 2


@pytest.mark.parametrize(
    "name,parameter,new_value,volume,bounds",
    [
        ("constrained-plate", "width_mm", 24, 1420 - 433 * math.pi / 12, [0, 0, 0, 24, 12, 5]),
        ("bushing", "height_mm", 24, 3000 * math.pi, [-15, 0, -15, 15, 24, 15]),
    ],
)
def test_fresh_process_parameter_and_target_edits_rebuild_without_agent_and_keep_old_versions(
    tmp_path, name, parameter, new_value, volume, bounds
):
    project = imported(tmp_path, name)
    original = invoke("show", "--project", project)["snapshot"]
    old_result = evaluated(project, tmp_path / "original")
    old_step = (tmp_path / "original/model.step").read_bytes()
    definition = original["project"]
    identifier = next(p["id"] for p in definition["parameters"].values() if p["name"] == parameter)
    target = copy.deepcopy(definition["acceptance"])
    for expected in [*target["nodes"].values(), target["assembly"]]:
        expected.update(volume_mm3=volume, bounds_mm=bounds)
    transaction = {
        "schema_version": "deep3d.transaction/1",
        "id": str(uuid4()),
        "base_revision": original["head"]["revision_id"],
        "expected_generation": original["head"]["generation"],
        "message": "Independent parameter and target change",
        "commands": [
            {
                "op": "parameter.set",
                "id": identifier,
                "value": {"kind": "length_mm", "value": new_value},
            },
            {"op": "acceptance.set", "acceptance": target},
        ],
    }
    path = tmp_path / "transaction.json"
    path.write_text(json.dumps(transaction), encoding="utf-8")
    changed = invoke("apply", "--project", project, "--transaction", path)
    new_result = evaluated(project, tmp_path / "edited")
    assert new_result["revision_id"] != old_result["revision_id"]
    assert changed["project"]["features"] == definition["features"]
    assert changed["project"]["nodes"] == definition["nodes"]
    assert set(changed["project"]["parameters"]) == set(definition["parameters"])
    old = invoke("show", "--project", project, "--revision", original["revision"]["id"])
    assert old["snapshot"]["project"] == definition
    assert len(invoke("history", "--project", project)) == 2
    exported = tmp_path / "historical.step"
    invoke(
        "export",
        "--project",
        project,
        "--materialization",
        old_result["materialization"],
        "--out",
        exported,
    )
    assert exported.read_bytes() == old_step
    assert (
        hashlib.sha256((tmp_path / "edited/model.step").read_bytes()).digest()
        != hashlib.sha256(old_step).digest()
    )


def test_wrong_independent_goal_cannot_attach_a_success_materialization(tmp_path):
    source = json.loads((ROOT / "fixtures/mechanical/bushing.json").read_text())
    for target in source["acceptance"]["nodes"].values():
        target["volume_mm3"] *= 1.1
    file = tmp_path / "wrong-goal.json"
    file.write_text(json.dumps(source), encoding="utf-8")
    project = tmp_path / "wrong.deep3d"
    invoke("import", "--project", project, "--spec", file)
    before = project.read_bytes()
    evaluated(project, tmp_path / "rejected", succeeds=False)
    assert project.read_bytes() == before
    assert invoke("show", "--project", project)["materializations"] == []
    report = json.loads((tmp_path / "rejected/verify-brep.json").read_text())
    assert any(c["id"].endswith(".volume") and c["status"] == "fail" for c in report["checks"])


def test_unicode_component_names_survive_native_and_step_reopen(tmp_path):
    source = json.loads((ROOT / "fixtures/mechanical/bushing.json").read_text())
    source["name"] = "可编辑的回转组件"
    for node in source["nodes"].values():
        node["name"] = "世界" if node["parent"] is None else "轴套组件"
    for feature in source["features"].values():
        feature["name"] = "建模特征_" + feature["name"]
    file = tmp_path / "unicode.json"
    file.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "unicode.deep3d"
    invoke("import", "--project", project, "--spec", file)
    evaluated(project, tmp_path / "built")
    from deep3d_cad.assembly_io import read_assembly

    _, metadata = read_assembly(tmp_path / "built/model.step")
    assert {node: item["name"] for node, item in metadata.items()} == {
        node: item["name"] for node, item in source["nodes"].items()
    }


def test_actual_assembly_overlap_is_rejected_despite_correct_part_volume_and_bounds(tmp_path):
    source = json.loads((ROOT / "fixtures/mechanical/rigid-stack.json").read_text())
    node, instance = next(
        (key, value)
        for key, value in source["nodes"].items()
        if "spacer" in value["name"].lower() and value["feature"]
    )
    operation = source["features"][instance["feature"]]["operation"]
    operation["translation"][2] = {"kind": "literal", "value": {"kind": "length_mm", "value": 3}}
    source["acceptance"]["nodes"][node]["bounds_mm"][2] = 3
    source["acceptance"]["nodes"][node]["bounds_mm"][5] = 13
    file = tmp_path / "overlap.json"
    file.write_text(json.dumps(source), encoding="utf-8")
    project = tmp_path / "overlap.deep3d"
    invoke("import", "--project", project, "--spec", file)
    before = project.read_bytes()
    evaluated(project, tmp_path / "rejected", succeeds=False)
    assert project.read_bytes() == before
    report = json.loads((tmp_path / "rejected/verify-brep.json").read_text())
    assert any(
        c["id"].startswith("interference.") and c["status"] == "fail" for c in report["checks"]
    )
