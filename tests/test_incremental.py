"""Incremental execution: only invalidated Features run; results match a cold full rebuild.

These are engineering regressions on the development fixtures, not the frozen
G3 change cases (M01..M20).
"""

import copy
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from deep3d_cad.kernel import read_brep
from OCP.Bnd import Bnd_Box
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/debug" / ("deep3d.exe" if sys.platform == "win32" else "deep3d")


def invoke(*args):
    if not BINARY.is_file():
        pytest.skip("Build Rust CLI for real native incremental integration")
    process = subprocess.run(
        [str(BINARY), *(str(arg) for arg in args)],
        capture_output=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    payload = json.loads(process.stdout)
    assert process.returncode == 0 and payload["status"] == "pass", payload
    return payload["result"]


def evaluate(project, out, cache, *extra):
    invoke(
        "evaluate", "--project", project, "--python", sys.executable, "--out", out,
        "--cache-dir", cache, *extra,
    )  # fmt: skip
    return json.loads((out / "execution.json").read_text())


def volume(shape):
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def summary(shape):
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    counts = {}
    for label, kind in [
        ("solids", TopAbs_SOLID),
        ("faces", TopAbs_FACE),
        ("edges", TopAbs_EDGE),
        ("vertices", TopAbs_VERTEX),
    ]:
        mapped = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
        TopExp.MapShapes_s(shape, kind, mapped)
        counts[label] = mapped.Extent()
    return volume(shape), [*box.CornerMin().Coord(), *box.CornerMax().Coord()], counts


def assert_equivalent(first, second, tolerance=1e-6):
    a, b = read_brep(first), read_brep(second)
    (va, box_a, topo_a), (vb, box_b, topo_b) = summary(a), summary(b)
    assert topo_a == topo_b
    assert va == pytest.approx(vb, rel=1e-9)
    assert box_a == pytest.approx(box_b, abs=tolerance)
    # Bytes may differ; geometry must not. Both one-sided differences are empty.
    for left, right in [(a, b), (b, a)]:
        assert volume(BRepAlgoAPI_Cut(left, right).Shape()) <= tolerance * max(va, 1)


def plate_width_24(target):
    for expected in [*target["nodes"].values(), target["assembly"]]:
        expected.update(volume_mm3=1420 - 433 * math.pi / 12, bounds_mm=[0, 0, 0, 24, 12, 5])


def spacer_height_8(target):
    spacer = 9 * math.pi * 8
    for expected in target["nodes"].values():
        if expected["bounds_mm"][2] == 5:
            expected["volume_mm3"] = spacer
            expected["bounds_mm"][5] = 13
    target["assembly"]["volume_mm3"] = 12000 + 4 * spacer


def edit(project, tmp_path, parameter, value, target=None):
    current = invoke("show", "--project", project)["snapshot"]
    identifier = next(
        p["id"] for p in current["project"]["parameters"].values() if p["name"] == parameter
    )
    commands = [
        {"op": "parameter.set", "id": identifier, "value": {"kind": "length_mm", "value": value}}
    ]
    acceptance = copy.deepcopy(current["project"]["acceptance"])
    if target is not None:
        target(acceptance)
        commands.append({"op": "acceptance.set", "acceptance": acceptance})
    transaction = {
        "schema_version": "deep3d.transaction/1",
        "id": str(uuid4()),
        "base_revision": current["head"]["revision_id"],
        "expected_generation": current["head"]["generation"],
        "message": f"Set {parameter}",
        "commands": commands,
    }
    path = tmp_path / f"transaction-{uuid4().hex}.json"
    path.write_text(json.dumps(transaction), encoding="utf-8")
    invoke("apply", "--project", project, "--transaction", path)
    return current["revision"]["id"]


@pytest.mark.parametrize(
    ("name", "parameter", "value", "target"),
    [
        ("rigid-stack", "spacer_height_mm", 8, spacer_height_8),
        # The sketch width feeds every Feature, so nothing may be reused here.
        ("constrained-plate", "width_mm", 24, plate_width_24),
    ],
)
def test_edit_executes_exactly_the_invalidated_features_and_matches_full_rebuild(
    tmp_path, name, parameter, value, target
):
    project = tmp_path / f"{name}.deep3d"
    invoke("import", "--project", project, "--spec", ROOT / "fixtures/mechanical" / f"{name}.json")
    cache = tmp_path / "cache"
    cold = evaluate(project, tmp_path / "cold", cache)
    assert cold["reused"] == [] and cold["executed"]

    base = edit(project, tmp_path, parameter, value, target)
    explanation = invoke("explain", "--project", project, "--from", base)
    dirty = {f["id"] for f in explanation["dirty"]}
    reused = {f["id"] for f in explanation["reused"]}
    assert dirty and (reused or name == "constrained-plate"), explanation
    assert {f["id"] for f in explanation["changed"]} <= dirty
    for reason in explanation["reasons"]:
        assert reason["feature"]["id"] in dirty
        assert {i["id"] for i in reason["invalidated_inputs"]} <= dirty

    warm = evaluate(project, tmp_path / "warm", cache)
    assert set(warm["executed"]) == dirty
    assert set(warm["reused"]) == reused
    for feature in reused:
        action = warm["actions"][feature]
        assert action["reason"] == "verified_content_addressed_hit"
        # A reused Feature keeps the exact bytes of the earlier materialization.
        assert (
            action["output_artifact_sha256"] == cold["actions"][feature]["output_artifact_sha256"]
        )

    # Independent full rebuild: new cache namespace and no cache reads.
    full = evaluate(project, tmp_path / "full", tmp_path / "full-cache", "--force-rebuild")
    assert full["reused"] == [] and set(full["executed"]) == dirty | reused
    for feature in dirty | reused:
        assert_equivalent(
            tmp_path / "warm/features" / f"{feature}.brep",
            tmp_path / "full/features" / f"{feature}.brep",
        )
    for node in (tmp_path / "warm/nodes").iterdir():
        assert_equivalent(node, tmp_path / "full/nodes" / node.name)
    assert_equivalent(tmp_path / "warm/model.brep", tmp_path / "full/model.brep")


def test_reverting_an_edit_reuses_every_feature_from_the_cache(tmp_path):
    project = tmp_path / "plate.deep3d"
    spec = ROOT / "fixtures/mechanical/constrained-plate.json"
    invoke("import", "--project", project, "--spec", spec)
    cache = tmp_path / "cache"
    first = evaluate(project, tmp_path / "first", cache)
    edit(project, tmp_path, "width_mm", 24, plate_width_24)
    evaluate(project, tmp_path / "edited", cache)
    original = json.loads(spec.read_text())["acceptance"]
    edit(project, tmp_path, "width_mm", 20, lambda target: target.update(original))
    reverted = evaluate(project, tmp_path / "reverted", cache)
    assert reverted["executed"] == []
    assert set(reverted["reused"]) == set(first["executed"])


def test_corrupted_cache_object_is_rejected_not_silently_reused(tmp_path):
    project = tmp_path / "bushing.deep3d"
    invoke("import", "--project", project, "--spec", ROOT / "fixtures/mechanical/bushing.json")
    cache = tmp_path / "cache"
    evaluate(project, tmp_path / "first", cache)
    for item in (cache / "objects").iterdir():
        item.write_bytes(item.read_bytes()[:-16] + b"0" * 16)
    process = subprocess.run(
        [
            str(BINARY), "evaluate", "--project", str(project), "--python", sys.executable,
            "--out", str(tmp_path / "second"), "--cache-dir", str(cache),
        ],
        capture_output=True, encoding="utf-8", timeout=300, check=False,
    )  # fmt: skip
    payload = json.loads(process.stdout)
    assert process.returncode != 0 and payload["status"] == "error"
    assert "CACHE_CORRUPT" in json.dumps(payload)
    shutil.rmtree(cache)
    assert evaluate(project, tmp_path / "third", cache)["reused"] == []
