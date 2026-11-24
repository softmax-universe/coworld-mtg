"""Frozen measurement for the local improvement loop.

Everything here is pure: it turns native fitter output into comparable
summaries, groups what went wrong, and decides whether a candidate improved the
cohort without regressing it. Contributors may read this file; a candidate may
not change it.
"""
from __future__ import annotations

import hashlib
import json

MATCHED = "matched_supported_projection"


def digest(value) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def summarize(case: dict, execution: str, result: dict | None, constraints: dict | None) -> dict:
    """One comparable record per game. `result` is None when the fitter did not finish."""
    result = result or {"status": execution, "issues": [
        {"kind": execution, "detail": "The fitter did not return a result.", "source_columns": []}]}
    fields = (constraints or {}).get("fields", [])
    return {
        "case_id": case["case_id"],
        "game_index": case["game_index"],
        "execution": execution,
        "status": result["status"],
        "nodes": result.get("nodes"),
        "duplicate_states": result.get("duplicate_states"),
        "milestones_fitted": result.get("milestones_fitted"),
        "milestones_total": result.get("milestones_total", len((constraints or {}).get("milestones", []))),
        "fully_covered_milestones": result.get("fully_covered_milestones"),
        "field_contracts": {
            f["column"]: {k: f.get(k) for k in ("raw", "disposition", "projection", "expected")}
            for f in fields if not f["column"].startswith("deck_")
        },
        "issues": result.get("issues", []),
        "rejections": result.get("rejections", []),
        "source_sha256": result.get("source_sha256"),
        "cards_mapping_sha256": result.get("cards_mapping_sha256"),
    }


def issue_cause(issue: dict) -> tuple[str, str]:
    """Group by cause, not by column: one unsupported field family is one issue."""
    code = (issue.get("evidence") or {}).get("diagnostic_code")
    if code:
        return issue["kind"], code
    return issue["kind"], issue.get("detail", "")


def discover(summaries: list[dict]) -> dict:
    counts: dict[str, int] = {}
    groups: dict[str, dict] = {}
    for summary in summaries:
        counts[summary["status"]] = counts.get(summary["status"], 0) + 1
        issues = list(summary["issues"])
        if summary["status"] == MATCHED and (summary["fully_covered_milestones"] or 0) < (summary["milestones_total"] or 0):
            issues.append({"kind": "partial_projection", "detail": "The witness leaves some recorded fields unchecked."})
        seen = set()
        for issue in issues:
            kind, cause = issue_cause(issue)
            key = digest([kind, cause])[:16]
            group = groups.setdefault(key, {"id": key, "kind": kind, "cause": cause, "cases": [], "columns": []})
            if key not in seen:
                seen.add(key)
                group["cases"].append(summary["case_id"])
            for column in issue.get("source_columns", []):
                family = column.split("_", 3)[-1] if column[:5] in ("user_", "oppo_") else column
                if family not in group["columns"]:
                    group["columns"].append(family)
    issues = sorted(groups.values(), key=lambda g: (-len(g["cases"]), g["id"]))
    return {"counts": counts, "issues": issues}


def compare_pair(before: dict, after: dict) -> dict:
    """Regressions always block. An unresolved run is a regression only if the
    baseline run of the same game finished."""
    regressions = []

    def integer(record, key):
        return record.get(key) if type(record.get(key)) is int else None

    def lost(key):
        return integer(before, key) is not None and (integer(after, key) is None or after[key] < before[key])

    def gained(key):
        return integer(before, key) is not None and integer(after, key) is not None and after[key] > before[key]

    for key in ("source_sha256", "cards_mapping_sha256"):
        if before.get(key) and after.get(key) and before[key] != after[key]:
            regressions.append(key + " changed")
    after_fields = after.get("field_contracts", {})
    for name, field in before.get("field_contracts", {}).items():
        other = after_fields.get(name)
        if other is None:
            if after["execution"] == "completed":
                regressions.append("source field omitted: " + name)
            continue
        if field.get("raw") != other.get("raw"):
            regressions.append("source field altered: " + name)
        if field["disposition"] == "enforced" and any(field.get(k) != other.get(k) for k in ("disposition", "projection", "expected")):
            regressions.append("enforced field changed or dropped: " + name)
    if before["execution"] == "completed" and after["execution"] != "completed":
        regressions.append("candidate run did not finish: " + after["execution"])
    if before["status"] == MATCHED and after["status"] != MATCHED:
        regressions.append("lost a matched game")
    if lost("milestones_fitted"):
        regressions.append("matched fewer milestones")
    if lost("fully_covered_milestones"):
        regressions.append("fully covered fewer milestones")
    newly_enforced = sorted(
        name for name, field in after_fields.items()
        if field["disposition"] == "enforced"
        and before.get("field_contracts", {}).get(name, {}).get("disposition") != "enforced"
    )
    improvements = []
    if not regressions:
        if after["status"] == MATCHED and before["status"] != MATCHED:
            improvements.append("newly matched")
        elif gained("milestones_fitted"):
            improvements.append("matched more milestones")
        if gained("fully_covered_milestones"):
            improvements.append("fully covered more milestones")
        if newly_enforced and after["status"] == MATCHED and before["status"] == MATCHED:
            improvements.append("checks more recorded fields and still matches")
    return {
        "case_id": before["case_id"],
        "before": {k: before.get(k) for k in ("execution", "status", "milestones_fitted", "milestones_total", "fully_covered_milestones", "nodes")},
        "after": {k: after.get(k) for k in ("execution", "status", "milestones_fitted", "milestones_total", "fully_covered_milestones", "nodes")},
        "newly_enforced_fields": newly_enforced,
        "regressions": regressions,
        "improvements": improvements,
    }


def compare(before: list[dict], after: list[dict], minimum_improved: int) -> dict:
    left = {s["case_id"]: s for s in before}
    right = {s["case_id"]: s for s in after}
    reasons = [] if set(left) == set(right) else ["the set of games changed"]
    rows = [compare_pair(left[k], right[k]) for k in sorted(set(left) & set(right))]
    improved = [r["case_id"] for r in rows if r["improvements"]]
    regressed = [r["case_id"] for r in rows if r["regressions"]]
    if regressed:
        reasons.append(f"{len(regressed)} game(s) regressed")
    if len(improved) < minimum_improved:
        reasons.append(f"{len(improved)} game(s) improved; the policy requires {minimum_improved}")
    return {"passed": not reasons, "improved": improved, "regressed": regressed, "reasons": reasons, "cases": rows}
