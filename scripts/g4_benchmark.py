"""G4 invalidation benchmark: fixture/oracle generation and measured runs.

`generate` writes the registered hybrid graph, the fixed query order and the
expected invalidation sets. The oracle is derived from the textual registry
definition with a forward dynamic program over node order; it never calls the
Rust invalidation code it is used to judge.

Only the digest manifest is committed (the oracle with per-query reason edges
is ~24 MB); `run` regenerates the files into a scratch directory, refuses to
continue unless their digests equal the committed manifest, builds the release
runner, records the machine manifest and writes an observation fragment. It
does not freeze the registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "acceptance" / "v0.1" / "g4-fixture.json"
NODES = 1000
ROOTS = 100
QUERIES = 1000
WARMUP = 100
THRESHOLD_MS = 100


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def name(i: int) -> str:
    return f"f{i:04d}"


def registered_edges() -> list[tuple[int, int]]:
    # Registry text: f0000..f0099 have no parents. For i=100..999 add
    # f(i mod 100)->fi; for i>100 add f(i-1)->fi; if i mod 7 = 0 also add
    # f(max(0, i-101))->fi. Deduplicate edges.
    edges: set[tuple[int, int]] = set()
    for i in range(ROOTS, NODES):
        edges.add((i % 100, i))
        if i > 100:
            edges.add((i - 1, i))
        if i % 7 == 0:
            edges.add((max(0, i - 101), i))
    return sorted(edges)


def oracle(edges: list[tuple[int, int]], edit: int) -> dict:
    # Every edge goes from a lower to a higher index, so one pass in node
    # order decides each node from its already decided parents.
    parents: dict[int, list[int]] = {i: [] for i in range(NODES)}
    for parent, child in edges:
        assert parent < child, "registered graph is not index-ordered"
        parents[child].append(parent)
    dirty = [False] * NODES
    for i in range(NODES):
        dirty[i] = i == edit or any(dirty[p] for p in parents[i])
    reasons = {
        name(child): sorted(name(p) for p in parents[child] if dirty[p])
        for child in range(NODES)
        if any(dirty[p] for p in parents[child])
    }
    return {"dirty": [name(i) for i in range(NODES) if dirty[i]], "reasons": reasons}


def write(path: Path, value) -> str:
    data = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True).encode() + b"\n"
    path.write_bytes(data)
    return digest(canonical(value))


def generate(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    edges = registered_edges()
    graph = {
        "schema_version": "deep3d.g4-graph/1",
        "generator_contract": "hybrid-chain-fanout-joins@1",
        "seed": 20261027,
        "nodes": [
            {"id": name(i), "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"deep3d:g4:{name(i)}"))}
            for i in range(NODES)
        ],
        "edges": [[name(a), name(b)] for a, b in edges],
    }
    queries = {
        "schema_version": "deep3d.g4-queries/1",
        "warmup_requests": WARMUP,
        "queries": [{"index": j, "edit": name((37 * j) % 1000)} for j in range(QUERIES)],
    }
    expected = {
        "schema_version": "deep3d.g4-oracle/1",
        "method": "forward dynamic program over registered node order (scripts/g4_benchmark.py)",
        "expected": [
            {"query": q["index"], **oracle(edges, int(q["edit"][1:]))} for q in queries["queries"]
        ],
    }
    manifest = {
        "graph_asset_sha256": write(out / "graph.json", graph),
        "queries_sha256": write(out / "queries.json", queries),
        "independent_oracle_sha256": write(out / "oracle.json", expected),
        "identity": "SHA-256 of sorted-key compact JSON of each file's content",
    }
    return manifest


def command(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def machine(profile: str) -> dict:
    cpu = "unavailable"
    if Path("/proc/cpuinfo").exists():
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    memory = None
    if Path("/proc/meminfo").exists():
        kib = Path("/proc/meminfo").read_text().split()[1]
        memory = int(kib) * 1024
    governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    limits = {
        "nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
        "as": resource.getrlimit(resource.RLIMIT_AS),
        "cpu": resource.getrlimit(resource.RLIMIT_CPU),
    }
    return {
        "machine_id": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "affinity_cpus": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "memory_bytes": memory,
        "power_policy": governor.read_text().strip() if governor.exists() else "not exposed",
        "compiler": command("rustc", "--version"),
        "build_profile": profile,
        "process_limits": {k: list(v) for k, v in limits.items()},
        "clock": "std::time::Instant (CLOCK_MONOTONIC on Linux)",
        "environment": "shared cloud container; not a dedicated frozen test machine",
    }


def run(out: Path) -> int:
    if out.exists():
        raise SystemExit(f"{out} exists; use a new directory")
    fixture = Path(tempfile.mkdtemp(prefix="deep3d-g4-"))
    regenerated = generate(fixture)
    committed = json.loads(MANIFEST.read_text())
    if regenerated != committed:
        raise SystemExit("regenerated G4 fixture differs from the committed manifest")
    subprocess.run(
        ["cargo", "build", "--release", "--locked", "-p", "deep3d-cli", "--bin", "deep3d-g4"],
        cwd=ROOT,
        check=True,
    )
    out.mkdir(parents=True)
    manifest = machine("release")
    (out / "machine.json").write_text(json.dumps(manifest, indent=1) + "\n")
    binary = ROOT / "target" / "release" / ("deep3d-g4.exe" if os.name == "nt" else "deep3d-g4")
    started = datetime.now(UTC).isoformat(timespec="seconds")
    result = subprocess.run(
        [
            str(binary),
            "--graph",
            str(fixture / "graph.json"),
            "--queries",
            str(fixture / "queries.json"),
            "--oracle",
            str(fixture / "oracle.json"),
            "--out",
            str(out / "measurement.json"),
        ],
        cwd=ROOT,
    )
    shutil.rmtree(fixture)
    measurement = json.loads((out / "measurement.json").read_text())
    p95_ms = measurement["timed"]["rebuild_per_request"]["p95_ns"] / 1e6
    exact = measurement["exact_result_requests"]
    meets = exact == measurement["measured_requests"] == QUERIES and p95_ms < THRESHOLD_MS
    observation = {
        "observed_at": started,
        "source_commit": command("git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"),
        "fixture": committed,
        "invalidation_performance": {
            # The registry is still a draft and the oracle was not prepared by an
            # independent reviewer, so a met threshold is not a frozen G4 pass.
            "status": "insufficient_evidence" if meets else "failed",
            "evidence_ids": ["measurement.json", "machine.json"],
            "machine_manifest_sha256": digest(canonical(manifest)),
            "feature_nodes": measurement["feature_nodes"],
            "measured_requests": measurement["measured_requests"],
            "exact_result_requests": exact,
            "p95_ms": p95_ms,
            "quantile_method": "nearest_rank",
        },
        "threshold_met_on_this_machine": meets,
        "reused_index_p95_ms": measurement["timed"]["reused_index"]["p95_ns"] / 1e6,
        "cold_index_build_ms": measurement["cold_index_build_ns"] / 1e6,
    }
    (out / "observation.json").write_text(json.dumps(observation, indent=1) + "\n")
    print(json.dumps(observation, indent=1))
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    generator = sub.add_parser("generate")
    generator.add_argument("--out", type=Path, required=True)
    generator.add_argument("--write-manifest", action="store_true")
    runner = sub.add_parser("run")
    runner.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "generate":
        manifest = generate(args.out)
        if args.write_manifest:
            MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n")
        print(json.dumps(manifest, indent=1))
        return 0
    return run(args.out)


if __name__ == "__main__":
    sys.exit(main())
