"""Execute the 20 registered V0.1 change cases (M01..M20) on native seeds.

For every case the runner starts from a fresh, independently verified seed,
prepares the change as an unpublished candidate, and records:

* the invalidation the system explains, compared with the role oracle bound in
  `g3_seeds.py` before execution (both difference directions);
* the Features actually executed vs reused from the ActionCache;
* adoption (or the required rejection) and the authoritative head afterwards;
* a cache-free full rebuild in a separate namespace, compared Feature by
  Feature and node by node for geometric equivalence;
* harness-side point-classification probes for relations that volume and
  bounds cannot capture (hole positions, fins, slot ends, ...).

The registry is still a draft; natural-language cases are executed through
their typed-IR equivalent, and this is recorded per case. Nothing here freezes
the registry or converts a result into a formal G3 pass.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import g3_seeds as seeds  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "acceptance" / "v0.1" / "registry.draft.json"
BINARY = ROOT / "target" / "debug" / ("deep3d.exe" if sys.platform == "win32" else "deep3d")
PI = math.pi


class CommandFailed(Exception):
    def __init__(self, payload):
        super().__init__(json.dumps(payload)[:2000])
        self.payload = payload


def deep3d(*args):
    process = subprocess.run(
        [str(BINARY), *(str(a) for a in args)],
        capture_output=True,
        encoding="utf-8",
        timeout=600,
        check=False,
    )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        raise CommandFailed({"stdout": process.stdout, "stderr": process.stderr}) from None
    if process.returncode != 0 or payload.get("status") != "pass":
        raise CommandFailed(payload)
    return payload["result"]


# Change definitions ----------------------------------------------------------


def set_param(b, name, value):
    return {
        "op": "parameter.set",
        "id": seeds.ident(b.case, "parameter", name),
        "value": {"kind": "length_mm", "value": value},
    }


def replace(b, name, **changes):
    feature = copy.deepcopy(b.features[b.fid(name)])
    feature["operation"].update(changes)
    return {"op": "feature.replace", "feature": feature}


def accept(expect, **kwargs):
    """Acceptance recomputed from closed-form formulas for the changed request."""

    def build(b):
        fresh = copy.deepcopy(b)
        fresh.acceptance = {}
        expect(fresh, **kwargs)
        return {"op": "acceptance.set", "acceptance": fresh.project()["acceptance"]}

    return build


def inside(*points):
    return [{"point": list(p), "inside": True} for p in points]


def outside(*points):
    return [{"point": list(p), "inside": False} for p in points]


def polar(radius, degrees, z):
    a = math.radians(degrees)
    return (radius * math.cos(a), radius * math.sin(a), z)


def m15(b):
    return [{"op": "node.rename", "id": b.nid("spacer_front_left"), "name": "spacer_service"}]


def m17(b):
    spacer = b.fid("SpacerBody")
    fid = seeds.ident("C09", "feature", "SpacerCenterInstance")
    feature = {
        "id": fid,
        "name": "SpacerCenterInstance",
        "operation": {
            "op": "shape.transform@1",
            "shape": spacer,
            "translation": [seeds.L(0), seeds.L(0), b.P("plate_thickness")],
            "axis": [0, 0, 1],
            "angle": seeds.A(0),
        },
    }
    node = {
        "id": seeds.ident("C09", "node", "spacer_center"),
        "name": "spacer_center",
        "parent": b.nid("plate_spacer_stack"),
        "feature": fid,
    }
    return [{"op": "feature.create", "feature": feature}, {"op": "node.create", "node": node}]


def m20(b):
    return [
        {"op": "node.delete", "id": b.nid("spacer_front_left")},
        {"op": "feature.delete", "id": b.fid("SpacerFrontLeftInstance")},
    ]


def c09_spacers(extra=(), drop=()):
    return [s for s in seeds.C09_SPACERS() if s[0] not in drop] + list(extra)


CHANGES = {
    "M01": {
        "commands": lambda b: [set_param(b, "plate_thickness", 10)],
        "acceptance": accept(seeds.c01_expect, t=10),
        "probes": outside((25, 15, 9.5), (-25, -15, 0.5)) + inside((0, 0, 9.5)),
    },
    "M02": {
        "commands": lambda b: [set_param(b, "plate_width", 96)],
        "acceptance": accept(seeds.c01_expect, w=96),
        "probes": outside((25, 15, 4), (-25, -15, 4), (-25, 15, 4)) + inside((45, 0, 4)),
    },
    "M03": {
        "commands": lambda b: [set_param(b, "large_section_diameter", 28)],
        "acceptance": accept(seeds.c02_expect, d1=28),
        "probes": inside((13, 0, 10)) + outside((13, 0, 30), (0, 0, 20)),
    },
    "M04": {
        "commands": lambda b: [set_param(b, "free_end_fillet_radius", 2.5)],
        "acceptance": accept(seeds.c03_expect, r=2.5),
        "probes": outside((39.9, 5.9, 10), (5.9, 29.9, 10))
        + inside((0.05, 0.05, 10), (5.95, 5.95, 10)),
    },
    "M05": {
        "commands": lambda b: [set_param(b, "hole_mouth_chamfer", 0.5)],
        "acceptance": accept(seeds.c04_expect, c=0.5),
        "probes": outside((5.3, 0, 9.9)) + inside((5.3, 0, 9.0), (5.6, 0, 9.4)),
    },
    "M06": {
        "commands": lambda b: [set_param(b, "bolt_pitch_diameter", 76)],
        "acceptance": accept(seeds.c05_expect, pitch=76),
        "probes": outside(*(polar(38, 45 * k, 5) for k in range(8)))
        + inside(*(polar(33.5, 45 * k, 5) for k in range(8))),
    },
    "M07": {
        "commands": lambda b: [replace(b, "BoltPattern", count=6, step_angle=seeds.A(PI / 3))],
        "acceptance": accept(seeds.c05_expect, count=6),
        "probes": outside(*(polar(35, 60 * k, 5) for k in range(6)))
        + inside(polar(35, 45, 5), polar(35, 135, 5)),
    },
    "M08": {
        "commands": lambda b: [set_param(b, "fin_height", 18)],
        "acceptance": accept(seeds.c06_expect, height=18),
        "probes": inside((0, 0, 22.5), (20, 0, 22.5)) + outside((5, 0, 22.5)),
    },
    "M09": {
        "commands": lambda b: [
            set_param(b, "fin_pitch", 8),
            replace(b, "FinPattern", count=7),
            replace(
                b,
                "FinPlacement",
                translation=[b.P("fin_pitch", -3), seeds.L(0), b.P("base_thickness")],
            ),
        ],
        "acceptance": accept(seeds.c06_expect, count=7, pitch=8),
        "probes": inside(*((x, 0, 12) for x in (-24, -16, -8, 0, 8, 16, 24)))
        + outside((-20, 0, 12), (20, 0, 12)),
    },
    "M10": {
        "commands": lambda b: [set_param(b, "slot_width", 8)],
        "acceptance": accept(seeds.c07_expect, width=8),
        "probes": outside((0, 3.5, 3), (9.5, 0, 3), (-9.5, 0, 3))
        + inside((0, 4.5, 3), (10.5, 0, 3), (9.5, 3.5, 3)),
    },
    "M11": {
        "commands": lambda b: [set_param(b, "block_width", 96)],
        "acceptance": accept(seeds.c08_expect, w=96),
        "probes": outside((24, 0, 10), (-24, 0, 10), (24, 9, 18))
        + inside((17, 0, 10), (-17, 0, 10), (24, 9, 10)),
    },
    "M12": {
        "commands": lambda b: [set_param(b, "plate_gap", 14)],
        "acceptance": accept(seeds.c09_expect, gap=14),
        "probes": inside((12, 8, 18), (0, 0, 21)) + outside((0, 0, 17)),
    },
    "M13": {
        "commands": lambda b: [set_param(b, "upright_height", 52)],
        "acceptance": accept(seeds.c10_expect, height=52),
        "probes": inside((35, 0, 57), (0, 0, 59)) + outside((0, 0, 57)),
    },
    "M14": {
        "commands": lambda b: [set_param(b, "beam_length", 94)],
        "acceptance": accept(seeds.c10_expect, beam=94),
        "probes": inside((46.5, 0, 50)) + outside((47.5, 0, 50)),
    },
    "M15": {
        "commands": m15,
        "acceptance": None,
        "probes": inside((-12, -8, 10)),
        "names": {"spacer_service": 1},
    },
    "M16": {
        "commands": lambda b: [set_param(b, "spacer_x_offset", 10)],
        "acceptance": lambda b: accept(seeds.c09_expect, spacers=c09_spacers_x(10))(b),
        "probes": inside((10, 8, 10), (-10, -8, 10)) + outside((13.5, 8, 10), (-13.5, -8, 10)),
    },
    "M17": {
        "commands": m17,
        "acceptance": accept(
            seeds.c09_expect, spacers=c09_spacers(extra=[("spacer_center", 0, 0)])
        ),
        "probes": inside((0, 0, 10)),
        "new_roles": {"spacer.center_instance": ["SpacerCenterInstance"]},
        "bom": {"SpacerBody": 5, "PlateBody": 2},
    },
    "M18": {
        "commands": lambda b: [set_param(b, "hole_diameter", 60)],
        # The request as stated: 60 mm holes, original centers, inside the
        # plate, separate, with at least 2 mm of wall.
        "acceptance": accept(seeds.c01_expect, hd=60, min_wall=2),
    },
    "M19": {
        "commands": lambda b: [{"op": "feature.delete", "id": b.fid("ShaftSection")}],
        "acceptance": None,
    },
    "M20": {
        "commands": m20,
        "acceptance": accept(seeds.c09_expect, spacers=c09_spacers(drop={"spacer_front_left"})),
        "probes": outside((-12, -8, 10)) + inside((12, -8, 10)),
        "removed_roles": {"spacer.front_left_instance": ["SpacerFrontLeftInstance"]},
        "bom": {"SpacerBody": 3, "PlateBody": 2},
    },
}


def c09_spacers_x(sx):
    return seeds.C09_SPACERS(sx=sx)


# Oracle ------------------------------------------------------------------------


def expand(roles, seed, extra, case):
    names, unbound = set(), []
    all_features = {f["name"] for f in seed.features.values()}
    for role in roles:
        if role in ("all_existing_geometry_features", "all_committed_features"):
            names |= all_features
        elif role in ("last_verified_publication",):
            continue
        elif role in (
            "remaining_plate_features",
            "remaining_spacer_features",
            "shared_part_definitions",
        ):
            removed = {n for v in case.get("removed_roles", {}).values() for n in v}
            names |= all_features - removed
        elif role in seed.roles or role in extra:
            names |= set(seed.roles.get(role, extra.get(role, [])))
        else:
            unbound.append(role)
    return names, unbound


# Geometry checks ---------------------------------------------------------------


def classify(path: Path, probes: list[dict]) -> list[dict]:
    from deep3d_cad.kernel import read_brep
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN

    shape = read_brep(path)
    results = []
    for probe in probes:
        classifier = BRepClass3d_SolidClassifier(shape, gp_Pnt(*probe["point"]), 1e-6)
        observed = classifier.State() == TopAbs_IN
        results.append({**probe, "observed_inside": observed, "pass": observed == probe["inside"]})
    return results


def equivalent(first: Path, second: Path, tolerance=1e-6) -> dict:
    from deep3d_cad.features import _map
    from deep3d_cad.kernel import read_brep
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX

    def volume(shape):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        return props.Mass()

    a, b = read_brep(first), read_brep(second)
    topo = [
        [_map(s, k).Extent() for k in (TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX)]
        for s in (a, b)
    ]
    if topo[0][0] == 0:  # planar profile face: topology and area

        def area(shape):
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(shape, props)
            return props.Mass()

        aa, ab = area(a), area(b)
        same = topo[0] == topo[1] and abs(aa - ab) <= 1e-9 * max(aa, 1)
        return {"pass": same, "kind": "profile", "areas": [aa, ab], "topology": topo}
    va, vb = volume(a), volume(b)
    diff = [volume(BRepAlgoAPI_Cut(x, y).Shape()) for x, y in ((a, b), (b, a))]
    same = (
        topo[0] == topo[1]
        and abs(va - vb) <= 1e-9 * max(abs(va), 1)
        and all(d <= tolerance * max(va, 1) for d in diff)
    )
    return {
        "pass": same,
        "kind": "solid",
        "volumes": [va, vb],
        "one_sided_differences": diff,
        "topology": topo,
    }


# Runner ------------------------------------------------------------------------


def run_case(change: dict, out: Path, python: str) -> dict:
    cid = change["id"]
    case = CHANGES[cid]
    seed = seeds.SEEDS[change["seed_case"]]()
    work = out / cid
    work.mkdir(parents=True)
    record = {
        "id": cid,
        "seed_case": change["seed_case"],
        "category": change["category"],
        "registered_entry_mode": change["entry_mode"],
        "executed_entry_mode": "typed_ir",
        "expected_outcome": change["expected_outcome"],
    }
    if change["entry_mode"] != "typed_ir":
        record["entry_mode_note"] = (
            "natural-language entry not exercised; typed-IR equivalent executed"
        )
    started = time.monotonic()
    spec = work / "seed.json"
    spec.write_text(json.dumps(seed.project(), ensure_ascii=False), encoding="utf-8")
    project = work / "project.deep3d"
    deep3d("import", "--project", project, "--spec", spec)
    cache = work / "cache"
    deep3d(
        "evaluate",
        "--project",
        project,
        "--python",
        python,
        "--out",
        work / "seed-eval",
        "--cache-dir",
        cache,
    )
    base = deep3d("show", "--project", project)
    head = base["snapshot"]["head"]
    seed_materialization = base["materializations"][0][0]
    commands = case["commands"](seed)
    if case.get("acceptance"):
        commands.append(case["acceptance"](seed))
    transaction = {
        "schema_version": "deep3d.transaction/1",
        "id": str(uuid4()),
        "base_revision": head["revision_id"],
        "expected_generation": head["generation"],
        "message": f"{cid}: {change['change_request']}",
        "commands": commands,
    }
    (work / "transaction.json").write_text(
        json.dumps(transaction, ensure_ascii=False), encoding="utf-8"
    )
    candidate = work / "candidate.deep3d"
    rejected = None
    try:
        deep3d(
            "candidate",
            "--project",
            project,
            "--transaction",
            work / "transaction.json",
            "--out",
            candidate,
        )
    except CommandFailed as exc:
        rejected = {"stage": "candidate", "error": exc.payload.get("error", exc.payload)}
    if rejected is None:
        try:
            deep3d(
                "evaluate",
                "--project",
                candidate,
                "--python",
                python,
                "--out",
                work / "candidate-eval",
                "--cache-dir",
                cache,
            )
        except CommandFailed as exc:
            rejected = {"stage": "evaluation", "error": exc.payload.get("error", exc.payload)}
            for stage in ("verify-brep", "verify-step"):
                report = work / "candidate-eval" / f"{stage}.json"
                if report.exists():
                    checks = json.loads(report.read_text())["checks"]
                    rejected.setdefault("failed_checks", []).extend(
                        c for c in checks if c["status"] != "pass"
                    )
    after = deep3d("show", "--project", project)
    if change["expected_outcome"] == "reject_preserve_last_verified":
        unchanged = (
            after["snapshot"]["head"] == head
            and after["materializations"][0][0] == seed_materialization
        )
        record.update(
            outcome="rejected" if rejected else "accepted",
            rejection=rejected,
            authoritative_head_unchanged=unchanged,
            history_length=len(deep3d("history", "--project", project)),
        )
        witnesses = change["final_state_oracle"].get("conflict_witnesses", [])
        text = json.dumps(rejected or {}, ensure_ascii=False)
        record["registered_witnesses"] = {w: (w in text) for w in witnesses}
        record["witnesses_reported"] = all(record["registered_witnesses"].values())
        # A registered conflict needs its geometric witnesses, not only a refusal.
        record["pass"] = (
            bool(rejected)
            and unchanged
            and record["history_length"] == 1
            and record["witnesses_reported"]
        )
        record["seconds"] = round(time.monotonic() - started, 3)
        return record
    if rejected:
        record.update(outcome="rejected", rejection=rejected)
        record["pass"] = False
        return record

    explanation = deep3d("explain", "--project", candidate, "--from", head["revision_id"])
    seed_names = {f["name"] for f in seed.features.values()}
    by_id = {
        f["id"]: f["name"]
        for group in ("dirty", "reused", "removed", "changed")
        for f in explanation[group]
    }
    dirty = {f["name"] for f in explanation["dirty"]}
    removed = {f["name"] for f in explanation["removed"]}
    oracle = change["dependency_oracle"]
    new_roles = case.get("new_roles", {})
    removed_roles = case.get("removed_roles", {})
    expected_invalid, unbound = expand(oracle["invalidated_existing_roles"], seed, {}, case)
    preserved, unbound_preserved = expand(oracle["preserved_roles"], seed, {}, case)
    expected_new = {n for r in oracle["new_roles"] for n in new_roles.get(r, [])}
    expected_removed = {n for r in oracle["removed_roles"] for n in removed_roles.get(r, [])}
    unbound += [r for r in oracle["new_roles"] if r not in new_roles]
    unbound += [r for r in oracle["removed_roles"] if r not in removed_roles]
    actual_existing = (dirty & seed_names) - removed
    actual_new = dirty - seed_names
    reused_names = {f["name"] for f in explanation["reused"]}
    dependency = {
        "expected_invalidated": sorted(expected_invalid),
        "actual_invalidated": sorted(actual_existing),
        "false_positive": sorted(actual_existing - expected_invalid),
        "false_negative": sorted(expected_invalid - actual_existing),
        "preserved_but_invalidated": sorted(preserved & (dirty - removed)),
        "expected_new": sorted(expected_new),
        "actual_new": sorted(actual_new),
        "expected_removed": sorted(expected_removed),
        "actual_removed": sorted(removed),
        "unbound_roles": sorted(set(unbound + unbound_preserved)),
        "unclassified_seed_features": sorted(
            seed_names - expected_invalid - preserved - expected_removed
        ),
        "reasons": [
            {
                "feature": r["feature"]["name"],
                "because": sorted(i["name"] for i in r["invalidated_inputs"]),
            }
            for r in explanation["reasons"]
        ],
    }
    classes = {k: sorted(x["name"] for x in v) for k, v in explanation["classes"].items()}
    dependency["classes"] = classes
    # Every invalidated Feature is either recomputed locally or only re-placed.
    dependency["classes_cover_invalidation"] = set(classes["geometry"]) | set(
        classes["placement"]
    ) == (dirty - removed)
    if change["category"] == "metadata_only":
        dependency["metadata_only_respected"] = (
            not classes["geometry"] and not classes["placement"] and bool(classes["delivery"])
        )
    dependency["exact"] = (
        not dependency["false_positive"]
        and not dependency["false_negative"]
        and not dependency["preserved_but_invalidated"]
        and actual_new == expected_new
        and removed == expected_removed
        and not dependency["unbound_roles"]
        and dependency["classes_cover_invalidation"]
        and dependency.get("metadata_only_respected", True)
    )
    execution = json.loads((work / "candidate-eval" / "execution.json").read_text())
    executed = {by_id.get(i, i) for i in execution["executed"]}
    reused = {by_id.get(i, i) for i in execution["reused"]}
    seed_actions = json.loads((work / "seed-eval" / "execution.json").read_text())["actions"]
    byte_reuse = all(
        execution["actions"][i]["output_artifact_sha256"]
        == seed_actions[i]["output_artifact_sha256"]
        for i in execution["reused"]
    )
    incremental = {
        "executed": sorted(executed),
        "reused": sorted(reused),
        "executed_equals_invalidated": executed == dirty - removed,
        "reused_equals_explained": reused == reused_names,
        "reused_bytes_identical": byte_reuse,
    }
    candidate_head = deep3d("show", "--project", candidate)
    materialization = candidate_head["materializations"][0][0]
    deep3d(
        "adopt",
        "--project",
        project,
        "--candidate",
        candidate,
        "--expected-revision",
        head["revision_id"],
        "--expected-generation",
        head["generation"],
        "--materialization",
        materialization,
    )
    adopted = deep3d("show", "--project", project)
    publication = {
        "adopted_revision": adopted["snapshot"]["head"]["revision_id"],
        "matches_candidate": adopted["snapshot"]["head"] == candidate_head["snapshot"]["head"],
        "history_length": len(deep3d("history", "--project", project)),
    }
    # Independent full rebuild: separate cache namespace and no cache reads.
    deep3d(
        "evaluate",
        "--project",
        project,
        "--python",
        python,
        "--out",
        work / "full-eval",
        "--cache-dir",
        work / "full-cache",
        "--force-rebuild",
    )
    full = json.loads((work / "full-eval" / "execution.json").read_text())
    comparisons = {}
    for fid in execution["actions"]:
        comparisons[by_id.get(fid, fid)] = equivalent(
            work / "candidate-eval" / "features" / f"{fid}.brep",
            work / "full-eval" / "features" / f"{fid}.brep",
        )
    for node in sorted((work / "candidate-eval" / "nodes").iterdir()):
        comparisons["node:" + node.stem] = equivalent(
            node, work / "full-eval" / "nodes" / node.name
        )
    comparisons["model"] = equivalent(
        work / "candidate-eval" / "model.brep", work / "full-eval" / "model.brep"
    )
    full_rebuild = {
        "full_reused_nothing": full["reused"] == [],
        "all_equivalent": all(c["pass"] for c in comparisons.values()),
        "compared": len(comparisons),
        "failures": {k: v for k, v in comparisons.items() if not v["pass"]},
    }
    probes = classify(work / "candidate-eval" / "model.brep", case.get("probes", []))
    assembly = json.loads((work / "candidate-eval" / "assembly.json").read_text())
    final = {"probes": probes, "probes_pass": all(p["pass"] for p in probes)}
    if "bom" in case:
        quantities = {part["name"]: part["quantity"] for part in assembly["bom"]}
        final["bom"] = quantities
        final["bom_pass"] = quantities == case["bom"]
    if "names" in case:
        names = (
            [i["name"] for i in assembly["instances"].values()]
            if isinstance(assembly["instances"], dict)
            else [i["name"] for i in assembly["instances"]]
        )
        final["instance_names_pass"] = all(names.count(n) == k for n, k in case["names"].items())
        step = (work / "candidate-eval" / "model.step").read_text(
            encoding="utf-8", errors="replace"
        )
        final["step_names_pass"] = all(f":{n}'" in step for n in case["names"])
    verification = {
        stage: json.loads((work / "candidate-eval" / f"{stage}.json").read_text())["status"]
        for stage in ("verify-brep", "verify-step")
    }
    record.update(
        outcome="verified_new_commit",
        dependency=dependency,
        incremental=incremental,
        publication=publication,
        full_rebuild=full_rebuild,
        verification=verification,
        final_state=final,
    )
    record["pass"] = (
        dependency["exact"]
        and all(v for k, v in incremental.items() if k not in ("executed", "reused"))
        and publication["matches_candidate"]
        and publication["history_length"] == 2
        and full_rebuild["full_reused_nothing"]
        and full_rebuild["all_equivalent"]
        and all(v == "pass" for v in verification.values())
        and all(v for k, v in final.items() if k.endswith("_pass"))
    )
    record["seconds"] = round(time.monotonic() - started, 3)
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--python", default=str(ROOT / ".venv" / "bin" / "python"))
    args = parser.parse_args(argv)
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; use a new directory")
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True)
    results = []
    for change in registry["changes"]:
        if args.only and change["id"] not in args.only:
            continue
        try:
            result = run_case(change, args.out / "work", args.python)
        except CommandFailed as exc:
            result = {"id": change["id"], "pass": False, "harness_error": exc.payload}
        results.append(result)
        print(change["id"], "PASS" if result["pass"] else "FAIL", flush=True)
    valid = [r for r in results if r.get("expected_outcome") == "verified_new_commit"]
    summary = {
        "schema_version": "deep3d.g3-run/1",
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_commit": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "registry_state": registry["registration"]["state"],
        "machine": {"os": platform.platform(), "python": platform.python_version()},
        "cases": len(results),
        "passed": sum(r["pass"] for r in results),
        "valid_change_successes": sum(r["pass"] for r in valid),
        "correct_rejections": sum(
            r["pass"]
            for r in results
            if r.get("expected_outcome") == "reject_preserve_last_verified"
        ),
        "errors_or_unknown": [r["id"] for r in results if "harness_error" in r],
        "dependency_exact": sum(r.get("dependency", {}).get("exact", False) for r in valid),
        "full_rebuild_equivalent": sum(
            r.get("full_rebuild", {}).get("all_equivalent", False) for r in valid
        ),
        "natural_language_cases_run_as_typed_ir": [
            r["id"] for r in results if r.get("registered_entry_mode") == "natural_language"
        ],
        "results": results,
    }
    (args.out / "g3-run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=1
        )
    )
    return 0 if summary["passed"] == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
