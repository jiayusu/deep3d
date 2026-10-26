"""The project bridge executes its actual feature graph and verifies reloaded geometry."""

import copy
import json
import math
import subprocess
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from deep3d_cad.contracts import CadError, FlangeSpec, PrecisionProfile
from deep3d_cad.jsonio import read_json
from deep3d_cad.kernel import read_brep
from deep3d_cad.project_worker import evaluate_plan, validate_plan
from deep3d_cad.runner import file_digest
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID


def identifier(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, "deep3d-project-fixture/" + name))


def plan_for(spec: FlangeSpec | None = None) -> dict:
    spec = spec or FlangeSpec()
    features = []

    def circle(name, radius, x=0, y=0):
        feature = {
            "id": identifier(name),
            "name": name,
            "op": "profile.circle@1",
            "inputs": {},
            "parameters": {"radius_mm": radius, "center_x_mm": x, "center_y_mm": y},
        }
        features.append(feature)
        return feature["id"]

    def extrude(name, profile):
        feature = {
            "id": identifier(name),
            "name": name,
            "op": "solid.extrude@1",
            "inputs": {"profile": profile},
            "parameters": {"distance_mm": spec.thickness_mm},
        }
        features.append(feature)
        return feature["id"]

    def cut(name, base, tool):
        feature = {
            "id": identifier(name),
            "name": name,
            "op": "solid.cut@1",
            "inputs": {"base": base, "tool": tool},
            "parameters": {},
        }
        features.append(feature)
        return feature["id"]

    current = extrude("stock", circle("stock_profile", spec.outer_diameter_mm / 2))
    center = extrude("center_tool", circle("center_profile", spec.center_hole_diameter_mm / 2))
    current = cut("center_cut", current, center)
    for index, (x, y) in enumerate(spec.bolt_centers_mm()):
        tool = extrude(
            f"bolt_{index}_tool",
            circle(f"bolt_{index}_profile", spec.bolt_hole_diameter_mm / 2, x, y),
        )
        current = cut(f"bolt_{index}_cut", current, tool)
    return {
        "schema_version": "deep3d.execution-plan/1",
        "project_id": identifier("project"),
        "revision_id": identifier("revision"),
        "units": "mm",
        "coordinate_system": "right_handed_z_up",
        "features": features,
        "outputs": {identifier("part"): current},
        "acceptance": spec.to_dict(),
        "precision": PrecisionProfile().to_dict(),
    }


def test_real_dag_materializes_each_feature_and_reloads_step_in_new_process(tmp_path):
    plan = plan_for()
    result = evaluate_plan(plan, tmp_path / "first")
    assert result["status"] == "pass", result
    assert len(result["feature_outputs"]) == 17
    assert (
        read_brep(tmp_path / "first/features" / f"{identifier('stock_profile')}.brep").ShapeType()
        == TopAbs_FACE
    )
    assert (
        read_brep(tmp_path / "first/features" / f"{identifier('stock')}.brep").ShapeType()
        == TopAbs_SOLID
    )
    assert result["stages"]["verify-step"]["measurements"]["volume_mm3"] == pytest.approx(
        26532 * math.pi
    )
    pids = {
        result["stages"][stage]["worker_pid"] for stage in ("build", "verify-brep", "verify-step")
    }
    assert len(pids) == 3
    assert result["evaluator_source_sha256"] == file_digest(
        Path(sys.modules[evaluate_plan.__module__].__file__)
    )
    artifacts = {item["path"]: item for item in result["artifacts"]}
    assert {
        "plan.json",
        "goal.json",
        "precision.json",
        "build.json",
        "flange.brep",
        "flange.step",
        "verify-brep.json",
        "verify-step.json",
    } <= set(artifacts)
    assert "result.json" not in artifacts
    for name, artifact in artifacts.items():
        assert file_digest(tmp_path / "first" / name) == artifact["sha256"]
        assert (tmp_path / "first" / name).stat().st_size == artifact["size_bytes"]
    final_feature = next(iter(plan["outputs"].values()))
    assert next(iter(result["node_outputs"].values())) == result["feature_outputs"][final_feature]


def test_editing_dimensions_changes_real_geometry_and_keeps_old_files(tmp_path):
    old = evaluate_plan(plan_for(), tmp_path / "old")
    old_step_hash = file_digest(tmp_path / "old/flange.step")
    edited = FlangeSpec(thickness_mm=16, bolt_circle_diameter_mm=80)
    new = evaluate_plan(plan_for(edited), tmp_path / "new")
    assert old["status"] == new["status"] == "pass"
    assert old["node_outputs"] != new["node_outputs"]
    measured = new["stages"]["verify-step"]
    assert measured["measurements"]["volume_mm3"] == pytest.approx(edited.expected_volume_mm3)
    assert file_digest(tmp_path / "old/flange.step") == old_step_hash
    assert {entry["id"]: entry for entry in measured["checks"]}["outer.z_max"][
        "observed"
    ] == pytest.approx(16)


def test_wrong_feature_radius_is_not_replaced_by_acceptance_recipe(tmp_path):
    plan = plan_for()
    for feature in plan["features"]:
        if feature["name"] == "bolt_0_profile":
            feature["parameters"]["radius_mm"] = 5
    result = evaluate_plan(plan, tmp_path / "wrong")
    assert result["status"] == "fail", result
    assert result["stages"]["build"]["status"] == "pass"
    assert result["stages"]["verify-brep"]["status"] == "fail"
    assert result["stages"]["verify-step"]["status"] == "fail"
    assert read_json(tmp_path / "wrong/result.json")["status"] == "fail"


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda p: p.update(schema_version="deep3d.execution-plan/99"), "SCHEMA_UNSUPPORTED"),
        (lambda p: p.update(units="m"), "SCHEMA_INVALID"),
        (lambda p: p.update(coordinate_system="left_handed_y_up"), "SCHEMA_INVALID"),
        (lambda p: p.update(arbitrary_code="print('not allowed')"), "SCHEMA_INVALID"),
        (lambda p: p["features"][-1].update(op="python.exec@1"), "CAPABILITY_UNAVAILABLE"),
        (lambda p: p["features"][-1].update(extra="ignored?"), "SCHEMA_INVALID"),
        (lambda p: p["features"][0].update(id="../escaped.brep"), "SCHEMA_INVALID"),
        (lambda p: p["features"][1].update(id=p["features"][0]["id"]), "DUPLICATE_ID"),
        (lambda p: p["features"][0]["parameters"].update(radius_mm=True), "PARAMETER_INVALID"),
        (lambda p: p["features"][0]["parameters"].update(radius_mm=float("nan")), "SCHEMA_INVALID"),
        (lambda p: p["features"][0]["parameters"].update(ignored=1), "SCHEMA_INVALID"),
        (
            lambda p: p["features"][1]["inputs"].update(profile=identifier("absent")),
            "DEPENDENCY_NOT_READY",
        ),
        (
            lambda p: p["features"][1]["inputs"].update(profile=p["features"][-1]["id"]),
            "DEPENDENCY_NOT_READY",
        ),
        (
            lambda p: p["features"][2]["inputs"].update(unexpected=p["features"][0]["id"]),
            "SCHEMA_INVALID",
        ),
        (
            lambda p: p["features"][-1]["inputs"].update(tool=p["features"][0]["id"]),
            "TYPE_MISMATCH",
        ),
        (
            lambda p: p["outputs"].update({identifier("part2"): p["features"][-1]["id"]}),
            "CAPABILITY_UNAVAILABLE",
        ),
        (lambda p: p.update(outputs={identifier("part"): p["features"][0]["id"]}), "TYPE_MISMATCH"),
        (lambda p: p.update(features=[]), "RESOURCE_LIMIT"),
    ],
)
def test_entire_plan_is_rejected_before_any_output_or_geometry(tmp_path, mutation, code):
    plan = plan_for()
    mutation(plan)
    with pytest.raises(CadError) as error:
        evaluate_plan(plan, tmp_path / "invalid")
    assert error.value.code == code
    assert not (tmp_path / "invalid").exists()


def test_preflight_normalization_does_not_mutate_caller_snapshot():
    plan = plan_for()
    before = copy.deepcopy(plan)
    validated, _, _ = validate_plan(plan)
    validated["features"][0]["parameters"]["radius_mm"] = 99
    assert plan == before


def test_existing_output_is_not_overwritten(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(CadError) as error:
        evaluate_plan(plan_for(), output)
    assert error.value.code == "OUTPUT_EXISTS"
    assert sentinel.read_text() == "keep"
    assert list(output.iterdir()) == [sentinel]


def test_module_cli_runs_plan_and_returns_result(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_for()), encoding="utf-8")
    out = tmp_path / "cli"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "deep3d_cad.project_worker",
            "--plan",
            str(plan_path),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert read_json(out / "result.json", max_bytes=8_388_608)["status"] == "pass"
