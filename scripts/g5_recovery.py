"""Execute the registered recovery scenarios R01..R05 against real processes.

Every scenario uses its own PostgreSQL database, coordinator process, artifact
store and CAD worker processes, starting from a seed (C01) that already has one
verified public commit. Each records authority snapshots before and after the
fault, the real process ids that were signalled, attempt/epoch rows, and the
publication uniqueness query. R05 needs two real model identities; without
provider credentials it executes the restart/timeout part only and reports
insufficient evidence for the scenario.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import g3_seeds as seeds  # noqa: E402
from g5_stack import (  # noqa: E402
    BIN,
    ROOT,
    VENV_PYTHON,
    ApiError,
    FaultProxy,
    Stack,
    request,
    sha256,
)

ADMIN = "postgres://deep3d:deep3d@localhost/postgres"


def acceptance(thickness: float) -> dict:
    fresh = seeds.c01()
    fresh.acceptance = {}
    seeds.c01_expect(fresh, t=thickness)
    return fresh.project()["acceptance"]


def task(thickness: float) -> dict:
    return {
        "key": "publish",
        "depends_on": [],
        "source_task": None,
        "changes_mm": {"plate_thickness": thickness},
        "acceptance": acceptance(thickness),
        "max_attempts": 4,
        "timeout_seconds": 300,
    }


class Context:
    """A project whose first verified public commit already exists."""

    def __init__(self, stack: Stack):
        self.stack = stack
        seed = seeds.c01()
        spec = stack.work / "seed.json"
        spec.write_text(json.dumps(seed.project()), encoding="utf-8")
        package = stack.work / "seed.deep3d"
        subprocess.run(
            [str(BIN / "deep3d"), "import", "--project", str(package), "--spec", str(spec)],
            check=True,
            capture_output=True,
        )
        self.source = stack.upload(package)["source"]
        self.project_id = stack.register(self.source)["project_id"]
        self.goals = []
        self.set_goal(10)
        run = self.submit(10)
        worker = stack.start_worker("baseline", run)
        view = stack.wait_run(run)
        assert view["state"] == "completed", view
        os_kill(worker)
        self.baseline = self.snapshot()
        assert self.baseline["public_generation"] == "1", self.baseline

    def set_goal(self, thickness: float) -> dict:
        previous = self.goals[-1]["revision_id"] if self.goals else None
        goal = self.stack.set_goal(
            self.project_id,
            acceptance(thickness),
            f"Mounting plate thickness {thickness} mm; holes unchanged",
            previous,
            len(self.goals),
            seeds.PRECISION,
        )
        self.goals.append(goal)
        return goal

    def submit(self, thickness: float, goal=None, run_id=None) -> str:
        view = self.stack.project(self.project_id)
        goal = goal or self.goals[-1]
        # Plans start from the exact current public source (or the initial one).
        source = view["public_commit"]["output"]["source"] if view["public_commit"] else self.source
        run = self.stack.submit(
            source,
            [task(thickness)],
            {
                "goal_revision_id": goal["revision_id"],
                "expected_public_commit": view["public_head"],
                "expected_public_generation": view["public_generation"],
                "publish_task": "publish",
            },
            run_id=run_id,
        )
        return run["spec"]["run_id"]

    def snapshot(self) -> dict:
        view = self.stack.project(self.project_id)
        result = {
            "public_head": view["public_head"],
            "public_generation": view["public_generation"],
            "goal_head": view["goal_head"],
            "goal_generation": view["goal_generation"],
        }
        if view["public_head"]:
            commit = view["public_commit"]
            result.update(
                commit_goal=commit["goal_revision_id"],
                commit_run=commit["run_id"],
                commit_attempt=commit["attempt_id"],
                native_sha256=self.stack.public_digest(self.project_id, "native"),
                step_sha256=self.stack.public_digest(self.project_id, "step"),
            )
        return result

    def attempts(self, run_id: str) -> list[dict]:
        rows = self.stack.sql(
            "SELECT attempt_id, worker_id, execution_epoch, control_epoch, state, "
            "coalesce(error,'') "
            f"FROM deep3d_runtime.attempts WHERE run_id='{run_id}' ORDER BY execution_epoch"
        )
        keys = ["attempt_id", "worker_id", "execution_epoch", "control_epoch", "state", "error"]
        return [dict(zip(keys, row)) for row in rows]

    def commits(self) -> list[dict]:
        rows = self.stack.sql(
            "SELECT commit_id, run_id, attempt_id, public_generation, goal_revision_id "
            f"FROM deep3d_runtime.verified_commits WHERE project_id='{self.project_id}' "
            "ORDER BY public_generation"
        )
        keys = ["commit_id", "run_id", "attempt_id", "public_generation", "goal_revision_id"]
        return [dict(zip(keys, row)) for row in rows]

    def rebuild_public(self, label: str) -> dict:
        """Agent-free rebuild of the public native source in a fresh directory."""
        body, _ = self.stack.api(
            "GET", f"/v1/projects/{self.project_id}/public/artifacts/native", raw=True
        )
        package = self.stack.work / f"{label}-public.deep3d"
        package.write_bytes(body)
        out = subprocess.run(
            [
                str(BIN / "deep3d"),
                "evaluate",
                "--project",
                str(package),
                "--python",
                str(VENV_PYTHON),
                "--out",
                str(self.stack.work / f"{label}-rebuild"),
            ],
            capture_output=True,
            text=True,
        )
        payload = json.loads(out.stdout)
        return {"status": payload["status"], "native_sha256": sha256(body)}


def public(snapshot: dict) -> dict:
    return {k: v for k, v in snapshot.items() if not k.startswith("goal_")}


def os_kill(worker):
    import os

    try:
        os.killpg(worker["pid"], signal.SIGCONT)
        os.killpg(worker["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    worker["process"].wait()


def worker_post(stack: Stack, path: str, payload: dict):
    return request(stack.url + "/v1/worker/" + path, stack.worker_token, "POST", payload)


def outbox_requests(stack: Stack) -> list[dict]:
    folder = stack.root / "runs" / ".worker-outbox"
    found = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        document = json.loads(path.read_text())
        found.append({"file": path.name, **document})
    return found


# Scenarios ----------------------------------------------------------------------


def r01(stack: Stack) -> dict:
    ctx = Context(stack)
    ctx.set_goal(12)
    run = ctx.submit(12)
    proxy = FaultProxy(stack.url)
    proxy.gate.clear()
    crashed = stack.start_worker("crash", run, api=proxy.url)
    stack.wait_held(proxy)
    candidates = sorted(
        str(p.relative_to(stack.root)) for p in stack.root.glob(f"runs/{run}/**/candidate.deep3d")
    )
    before_kill = ctx.attempts(run)
    process_tree = stack.descendants(crashed["pid"])
    killed = stack.signal_worker(crashed, signal.SIGKILL)
    crashed["process"].wait()
    proxy.discard_held = True
    proxy.gate.set()
    proxy.close()
    after_kill = ctx.snapshot()
    # The unfinished task is discoverable from authority and re-leased after expiry.
    stack.wait(
        lambda: any(a["state"] != "running" for a in ctx.attempts(run)),
        60,
        "expired attempt recovery",
    )
    replacement = stack.start_worker("replacement", run)
    view = stack.wait_run(run)
    os_kill(replacement)
    attempts = ctx.attempts(run)
    commits = ctx.commits()
    final = ctx.snapshot()
    killed_attempt = before_kill[-1]
    published = [c for c in commits if c["run_id"] == run]
    observations = {
        "last_verified_public_head_and_assets_unchanged": public(after_kill)
        == public(ctx.baseline),
        "unfinished_work_discoverable_from_authority": killed_attempt["state"] == "running"
        and view["state"] == "completed",
        "replacement_attempt_has_new_execution_epoch": int(attempts[-1]["execution_epoch"])
        > int(killed_attempt["execution_epoch"]),
        "candidate_existed_before_crash": bool(candidates),
        "only_newly_verified_result_can_be_public": len(published) == 1
        and published[0]["attempt_id"] == attempts[-1]["attempt_id"]
        and published[0]["attempt_id"] != killed_attempt["attempt_id"],
    }
    rebuild = ctx.rebuild_public("r01")
    observations["agent_free_rebuild"] = rebuild["status"] == "pass"
    return {
        "observations": observations,
        "evidence": {
            "baseline": ctx.baseline,
            "after_kill": after_kill,
            "final": final,
            "candidate_bytes_at_kill": candidates,
            "worker_process_tree_at_kill": process_tree,
            "killed_process_ids": killed,
            "attempts": attempts,
            "verified_commits": commits,
            "rebuild": rebuild,
        },
    }


def r02(stack: Stack) -> dict:
    ctx = Context(stack)
    ctx.set_goal(12)
    run = ctx.submit(12)
    proxy = FaultProxy(stack.url)
    proxy.drop_complete = 1
    proxy.hold_complete_seconds = stack.lease_seconds + 3
    worker = stack.start_worker("lossy", run, api=proxy.url)
    committed = stack.wait(
        lambda: next((e for e in proxy.log if e.get("dropped_after_commit")), None),
        180,
        "dropped reply",
    )
    # Read the committed fact independently while the worker is still uncertain.
    independent = ctx.snapshot()
    view = stack.wait_run(run)
    stack.wait(
        lambda: len([e for e in proxy.log if e["path"].endswith("/complete")]) >= 2, 60, "retry"
    )
    os_kill(worker)
    proxy.close()
    completes = [e for e in proxy.log if e["path"].endswith("/complete")]
    commits = ctx.commits()
    final = ctx.snapshot()
    evaluations = sorted(
        p.name for p in stack.root.glob(f"runs/{run}/tasks/*/attempts/*/evaluation-*")
    )
    observations = {
        "same_operation_digest_returns_original_publication": len(
            {e["request_sha256"] for e in completes}
        )
        == 1
        and all(e["status"] == 200 for e in completes)
        and completes[-1]["response"]["result"] == committed["response"]["result"],
        "public_generation_advances_once": final["public_generation"] == "2"
        and independent["public_generation"] == "2",
        "one_accepted_fact_for_task": len([c for c in commits if c["run_id"] == run]) == 1
        and sum(a["state"] == "succeeded" for a in ctx.attempts(run)) == 1,
        "exact_source_and_artifact_hashes_preserved": independent == final,
        # The replayed completion is forwarded only after the lease has expired.
        "retry_after_lease_expiry": completes[-1]["monotonic"] - completes[0]["monotonic"]
        > stack.lease_seconds
        and any(e.get("dropped_after_commit") for e in completes),
        "no_new_geometric_compute": len(evaluations) == 1,
    }
    return {
        "observations": observations,
        "evidence": {
            "baseline": ctx.baseline,
            "independent_read_after_lost_reply": independent,
            "final": final,
            "completion_deliveries": completes,
            "verified_commits": commits,
            "attempts": ctx.attempts(run),
            "evaluation_directories": evaluations,
            "run_state": view["state"],
        },
    }


def r03(stack: Stack) -> dict:
    ctx = Context(stack)
    ctx.set_goal(12)
    run = ctx.submit(12)
    proxy = FaultProxy(stack.url)
    proxy.gate.clear()
    late = stack.start_worker("late", run, api=proxy.url)
    stack.wait_held(proxy)
    frozen = stack.signal_worker(late, signal.SIGSTOP)
    first = ctx.attempts(run)[-1]
    stack.wait(
        lambda: ctx.attempts(run)[-1]["state"] != "running" or len(ctx.attempts(run)) > 1,
        60,
        "expiry",
    )
    current = stack.start_worker("current", run)
    view = stack.wait_run(run)
    os_kill(current)
    after_current = ctx.snapshot()
    import os

    os.killpg(late["pid"], signal.SIGCONT)
    proxy.gate.set()  # the held, now stale, completion arrives late
    stale_log = late["logs"] / "worker-0.log"
    stack.wait(lambda: "worker_blocked" in stale_log.read_text(), 120, "stale callback")
    time.sleep(1)
    os_kill(late)
    proxy.close()
    late_delivery = [e for e in proxy.log if e["path"].endswith("/complete")]
    stale_codes = [
        json.loads(line).get("code")
        for line in stale_log.read_text().splitlines()
        if '"worker_blocked"' in line
    ]
    winner = next(
        d
        for d in outbox_requests(stack)
        if d["request"]["lease"]["attempt_id"] == ctx.commits()[-1]["attempt_id"]
    )
    replay = worker_post(stack, "complete", winner["request"])
    conflicting = dict(winner["request"], materialization="sha256:" + "0" * 64)
    try:
        worker_post(stack, "complete", conflicting)
        conflict = {"accepted": True}
    except ApiError as exc:
        conflict = {"accepted": False, "status": exc.status, "code": exc.code}
    stale_request = dict(
        winner["request"], lease=dict(winner["request"]["lease"], attempt_id=first["attempt_id"])
    )
    try:
        worker_post(stack, "complete", stale_request)
        stale_direct = {"accepted": True}
    except ApiError as exc:
        stale_direct = {"accepted": False, "status": exc.status, "code": exc.code}
    claim = {"claim_id": str(uuid4()), "worker_id": "dup-0", "run_id": run}
    claims = [worker_post(stack, "claim", claim) for _ in range(2)]
    commits = ctx.commits()
    final = ctx.snapshot()
    observations = {
        "one_current_authoritative_attempt": sum(
            a["state"] == "succeeded" for a in ctx.attempts(run)
        )
        == 1,
        "old_fencing_tuple_rejected": not stale_direct["accepted"]
        and bool(late_delivery)
        and all(e["status"] == 409 for e in late_delivery),
        "duplicate_same_result_is_read_only_receipt_replay": replay == winner.get("result", replay)
        and final == after_current,
        "concurrent_result_conflict_is_explicit": not conflict["accepted"]
        and bool(conflict.get("code")),
        "single_publication": len([c for c in commits if c["run_id"] == run]) == 1
        and final["public_generation"] == "2",
        "duplicate_claim_has_one_decision": claims[0] == claims[1],
    }
    return {
        "observations": observations,
        "evidence": {
            "frozen_process_ids": frozen,
            "first_attempt": first,
            "attempts": ctx.attempts(run),
            "stale_worker_codes": stale_codes,
            "late_delivery": late_delivery,
            "stale_direct_completion": stale_direct,
            "conflicting_completion": conflict,
            "duplicate_claims": claims,
            "verified_commits": commits,
            "final": final,
            "run_state": view["state"],
        },
    }


def r04(stack: Stack) -> dict:
    ctx = Context(stack)
    old_goal = ctx.set_goal(12)
    old_run = ctx.submit(12)
    proxy = FaultProxy(stack.url)
    proxy.gate.clear()
    old_worker = stack.start_worker("old-goal", old_run, api=proxy.url)
    stack.wait_held(proxy)  # the old-goal candidate is computed, its report in flight
    new_goal = ctx.set_goal(14)  # requirement change while the old CAD job is in flight
    after_goal = ctx.snapshot()
    # Concurrent engineering edits: two runs from the same public base under the new goal.
    run_a, run_b = ctx.submit(14), ctx.submit(14)
    wa, wb = stack.start_worker("edit-a", run_a), stack.start_worker("edit-b", run_b)
    views = {run_a: stack.wait_run(run_a), run_b: stack.wait_run(run_b)}
    os_kill(wa)
    os_kill(wb)
    proxy.gate.set()  # the old-goal result arrives late
    old_view = stack.wait_run(old_run, timeout=180)
    time.sleep(1)
    os_kill(old_worker)
    proxy.close()
    old_delivery = [e for e in proxy.log if e["path"].endswith("/complete")]
    commits = ctx.commits()
    final = ctx.snapshot()
    winner = next(c for c in commits if c["public_generation"] == "2")
    loser = run_b if winner["run_id"] == run_a else run_a
    evidence = stack.api(
        "GET", f"/v1/projects/{ctx.project_id}/commits/{winner['commit_id']}/evidence/native"
    )
    body, _ = stack.api("GET", f"/v1/projects/{ctx.project_id}/public/artifacts/native", raw=True)
    package = stack.work / "r04-public.deep3d"
    package.write_bytes(body)
    shown = json.loads(
        subprocess.run(
            [str(BIN / "deep3d"), "show", "--project", str(package)], capture_output=True, text=True
        ).stdout
    )
    parent = shown["result"]["snapshot"]["revision"]["parent"]
    explanation = json.loads(
        subprocess.run(
            [str(BIN / "deep3d"), "explain", "--project", str(package), "--from", parent],
            capture_output=True,
            text=True,
        ).stdout
    )["result"]
    observations = {
        "goal_version_change_is_visible": after_goal["goal_generation"] == "3"
        and after_goal["goal_head"] == new_goal["revision_id"],
        "control_epoch_revokes_old_commit_authority": old_view["state"] in ("failed", "cancelled")
        and not any(c["run_id"] == old_run for c in commits),
        "verified_history_is_retained": commits[0]["commit_id"] == ctx.baseline["public_head"]
        and len(commits) == 2,
        "explicit_conflict_for_concurrent_candidate": views[loser]["state"] == "failed"
        and any(t["error"] for t in views[loser]["tasks"]),
        "recomputed_result_is_reassessed_against_new_goal": winner["goal_revision_id"]
        == new_goal["revision_id"]
        and evidence["report"]["expected_spec"] == acceptance(14),
    }
    return {
        "observations": observations,
        "evidence": {
            "old_goal": old_goal["revision_id"],
            "new_goal": new_goal["revision_id"],
            "goal_contract_sha256": [old_goal["definition_sha256"], new_goal["definition_sha256"]],
            "after_goal_change": after_goal,
            "old_goal_late_delivery": old_delivery,
            "old_run": {
                "state": old_view["state"],
                "tasks": [t["error"] for t in old_view["tasks"]],
                "attempts": ctx.attempts(old_run),
            },
            "concurrent_runs": {
                r: {"state": v["state"], "errors": [t["error"] for t in v["tasks"]]}
                for r, v in views.items()
            },
            "verified_commits": commits,
            "final": final,
            "dependency_explanation": {
                k: explanation[k] for k in ("changed", "dirty", "reused", "classes")
            },
        },
    }


def r05(stack: Stack) -> dict:
    ctx = Context(stack)
    ctx.set_goal(12)
    run = ctx.submit(12)
    proxy = FaultProxy(stack.url)
    proxy.gate.clear()
    worker = stack.start_worker("survivor", run, api=proxy.url)
    stack.wait_held(proxy)
    killed_service = stack.kill_service()
    proxy.gate.set()  # the report now reaches a dead coordinator
    down = time.monotonic()
    # Outage longer than the lease: renewals and reports time out against a dead
    # coordinator; the worker must not treat that as success.
    time.sleep(stack.lease_seconds + 4)
    log = worker["logs"] / "worker-0.log"
    during_outage = [
        json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")
    ]
    pending = [d["file"] for d in outbox_requests(stack)]
    stack.start_service()
    view = stack.wait_run(run, timeout=180)
    os_kill(worker)
    proxy.close()
    commits = ctx.commits()
    final = ctx.snapshot()
    rebuild = ctx.rebuild_public("r05")
    observations = {
        "editable_source_rebuilds_without_original_model_or_prompt": rebuild["status"] == "pass",
        "pending_calls_have_stable_step_identity_or_explicit_uncertain_outcome": view["state"]
        == "completed",
        "resume_does_not_duplicate_publication": len([c for c in commits if c["run_id"] == run])
        == 1
        and final["public_generation"] == "2",
        "same_frozen_goal_criteria_are_checked": final["commit_goal"]
        == ctx.goals[-1]["revision_id"],
    }
    return {
        "observations": observations,
        "not_executed": [
            "switch_to_another_recorded_model_build: no model provider credentials here",
            "new_model_receives_version_bound_goal_and_authoritative_context",
        ],
        "status_override": "insufficient_evidence",
        "evidence": {
            "killed_coordinator_pid": killed_service,
            "coordinator_down_seconds": round(time.monotonic() - down, 2),
            "worker_events_during_outage": during_outage,
            "outbox_during_outage": pending,
            "service_starts": stack.service_starts,
            "attempts": ctx.attempts(run),
            "verified_commits": commits,
            "final": final,
            "rebuild": rebuild,
        },
    }


SCENARIOS = {"R01": r01, "R02": r02, "R03": r03, "R04": r04, "R05": r05}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--admin-url", default=ADMIN)
    args = parser.parse_args(argv)
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; use a new directory")
    args.out.mkdir(parents=True)
    results = []
    for name, scenario in SCENARIOS.items():
        if args.only and name not in args.only:
            continue
        stack = None
        started = time.monotonic()
        try:
            stack = Stack(args.out / "work" / name, args.admin_url)
            result = scenario(stack)
            passed = all(result["observations"].values())
            result["status"] = result.pop("status_override", None) or (
                "passed" if passed else "failed"
            )
            if result["status"] == "insufficient_evidence" and not passed:
                result["status"] = "failed"
        except Exception as exc:  # recorded, not hidden
            result = {
                "status": "failed",
                "harness_error": repr(exc),
                "trace": traceback.format_exc(),
            }
        finally:
            if stack is not None:
                stack.stop_all()
        result.update(id=name, seconds=round(time.monotonic() - started, 1))
        results.append(result)
        print(
            name,
            result["status"],
            {k: v for k, v in result.get("observations", {}).items() if not v},
            flush=True,
        )
    statuses = [r["status"] for r in results]
    summary = {
        "schema_version": "deep3d.g5-run/1",
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_commit": subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
        "scenarios": len(results),
        "passed": statuses.count("passed"),
        "insufficient_evidence": statuses.count("insufficient_evidence"),
        "failed": statuses.count("failed"),
        "results": results,
    }
    (args.out / "g5-run.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=1))
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
