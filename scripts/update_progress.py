"""Build the offline progress snapshot. Facts live in docs/progress.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/progress.json"
TEMPLATE = ROOT / "docs/progress.template.html"
OUTPUT = ROOT / "progress.html"
STATUSES = {"passed": "通过", "failed": "未通过", "insufficient_evidence": "证据不足"}


def text(value: object) -> str:
    return escape(str(value), quote=True)


def link(href: str, label: str, css: str = "") -> str:
    parsed = urlsplit(href)
    if parsed.scheme:
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"Unsupported link: {href}")
    else:
        path = PurePosixPath(parsed.path)
        if parsed.netloc or path.is_absolute() or ".." in path.parts or "\\" in href:
            raise ValueError(f"Invalid local link: {href}")
        if not (ROOT / path).is_file():
            raise ValueError(f"Missing linked file: {href}")
    return f'<a class="{text(css)}" href="{text(href)}">{text(label)}</a>'


def badge(status: str) -> str:
    return f'<span class="badge {status}"><i aria-hidden="true"></i>{STATUSES[status]}</span>'


def validate(data: dict) -> None:
    if data["schema_version"] != "deep3d.progress/1":
        raise ValueError("Unsupported progress schema")
    updated = datetime.fromisoformat(data["updated_at"])
    if updated.tzinfo is None:
        raise ValueError("updated_at must include a time zone")
    datetime.strptime(data["target_date"], "%Y-%m-%d")
    if not re.fullmatch(r"[0-9a-f]{7,40}", data["source_commit"]):
        raise ValueError("source_commit must identify an existing capability baseline")
    if [g["id"] for g in data["gates"]] != [f"G{i}" for i in range(1, 8)]:
        raise ValueError("Exactly G1..G7 are required, in order")
    state = (ROOT / "docs/current-state.md").read_text(encoding="utf-8")
    for group in ("gates", "stages", "evidence"):
        for item in data[group]:
            if item["status"] not in STATUSES:
                raise ValueError(f"Unknown status: {item['status']}")
    for gate in data["gates"]:
        row = re.search(rf"^\| {gate['id']} .*?\| ([^|]+) \|", state, re.M)
        if row is None or row[1].strip() != STATUSES[gate["status"]]:
            raise ValueError(f"{gate['id']} differs from docs/current-state.md")
    if not data["history"] or not data["next_actions"]:
        raise ValueError("Task history and next actions must not be empty")
    if data["autonomy"]["status"] not in STATUSES:
        raise ValueError("Unknown autonomous run status")
    if data["focus_stage"] not in {s["id"] for s in data["stages"]}:
        raise ValueError("Focus must identify an existing stage")
    for item in data["history"]:
        datetime.strptime(item["date"], "%Y-%m-%d")


def render(data: dict) -> str:
    validate(data)
    passed = sum(g["status"] == "passed" for g in data["gates"])
    counts = {status: sum(g["status"] == status for g in data["gates"]) for status in STATUSES}
    updated = datetime.fromisoformat(data["updated_at"]).astimezone(timezone(timedelta(hours=8)))
    gate_rows = []
    for g in data["gates"]:
        gate_rows.append(
            f'<article class="gate" data-status="{g["status"]}">'
            f'<div class="gate-id">{g["id"]}</div><div class="gate-body">'
            f'<div class="gate-heading"><h3>{text(g["title"])}</h3>{badge(g["status"])}</div>'
            f'<p class="threshold">{text(g["threshold"])}</p>'
            f"<p>{text(g['current'])}</p><details><summary>查看缺口与依据</summary>"
            f"<p>{text(g['missing'])}</p>{link('docs/current-state.md', '查看状态依据 ↗')}"
            "</details></div></article>"
        )
    stages = []
    for stage in data["stages"]:
        current = stage["id"] == data["focus_stage"]
        stages.append(
            f'<article class="stage{" current" if current else ""}">'
            f'<div class="stage-top"><span class="stage-number">{text(stage["id"])}</span>'
            f"{badge(stage['status'])}</div><h3>{text(stage['title'])}</h3>"
            f'<p>{text(stage["detail"])}</p><div class="stage-foot">'
            f"<span>{text(stage['scope'])}</span>{link(stage['href'], '依据 ↗')}</div></article>"
        )
    evidence = [
        f'<article class="evidence-card"><div class="evidence-number">{text(e["value"])}'
        "<span>项通过</span></div>"
        f"<h3>{text(e['label'])}</h3><p>{text(e['detail'])}</p>"
        f"{link(e['href'], '回归记录 ↗')}</article>"
        for e in data["evidence"]
    ]
    blockers = [
        f'<article class="blocker"><span class="blocker-index">0{i}</span><div>'
        f"<h3>{text(b['title'])}</h3><p>{text(b['detail'])}</p>"
        f"{link(b['href'], '查看依据 ↗')}</div></article>"
        for i, b in enumerate(data["blockers"], 1)
    ]
    history = [
        '<article class="history-item"><span class="timeline-dot" aria-hidden="true"></span>'
        f'<time datetime="{text(h["date"])}">{text(h["date"])}</time><div>'
        f"<h3>{link(h['href'], h['title'])}</h3><p>{text(h['detail'])}</p></div></article>"
        for h in data["history"]
    ]
    v0_status = (
        "passed" if passed == 7 else "failed" if counts["failed"] else "insufficient_evidence"
    )
    fields = {
        "UPDATED": text(updated.strftime("%Y.%m.%d · %H:%M（UTC+8）")),
        "UPDATED_ISO": text(data["updated_at"]),
        "HEADLINE": text(data["headline"]),
        "RELEASE": text(data["release"]),
        "BASELINE": text(data["source_commit"]),
        "TARGET": text(data["target_date"]),
        "PASSED": str(passed),
        "V0_STATUS": STATUSES[v0_status],
        "V0_CLASS": v0_status,
        "CAD_STATUS": text("支持域回归" + STATUSES[data["stages"][0]["status"]]),
        "AUTONOMY_LABEL": text(data["autonomy"]["label"]),
        "AUTONOMY_DETAIL": text(data["autonomy"]["detail"]),
        "FAILED": str(counts["failed"]),
        "INSUFFICIENT": str(counts["insufficient_evidence"]),
        "FOCUS": text(data["focus"]),
        "FOCUS_DETAIL": text(data["focus_detail"]),
        "NEXT_ACTIONS": "".join(f"<li>{text(a)}</li>" for a in data["next_actions"]),
        "GATES": "\n".join(gate_rows),
        "STAGES": "\n".join(stages),
        "EVIDENCE": "\n".join(evidence),
        "BLOCKERS": "\n".join(blockers),
        "HISTORY": "\n".join(history),
        "LATEST_TASK": text(data["history"][0]["title"]),
    }
    template = TEMPLATE.read_text(encoding="utf-8")
    for key, value in fields.items():
        marker = "{{" + key + "}}"
        if marker not in template:
            raise ValueError(f"Unused template field: {key}")
        template = template.replace(marker, value)
    if re.search(r"\{\{[A-Z_]+\}\}", template):
        raise ValueError("Unresolved template field")
    return template


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if HTML is out of date")
    args = parser.parse_args()
    try:
        rendered = render(json.loads(DATA.read_text(encoding="utf-8")))
        if args.check:
            if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
                raise ValueError("progress.html is stale; run scripts/update_progress.py")
            print("Progress snapshot is current; G1-G7 agree with docs/current-state.md.")
        else:
            OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
            print("Updated progress.html from docs/progress.json.")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        print(f"Progress update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
