#!/usr/bin/env python3
"""Run one improvement epoch, or several, on this machine.

An epoch freezes the current source as the baseline, fits the recorded-game
cohort, hands a contributor an isolated copy of the source together with what
went wrong, then rebuilds and refits the contributor's copy and lets the frozen
gate and an independent reviewer decide. Nothing here needs CAOS.

Set COWORLD_LOOP_EXEC to a command prefix when the Rust toolchain lives
somewhere else, for example a container wrapper that mounts this repository at
the same absolute path. Builds, tests and fits all run behind that prefix.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import json
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402

EXEC = shlex.split(os.environ.get("COWORLD_LOOP_EXEC", ""))
SOURCE_ROOTS = ("crates/", "fixtures/", ".cargo/")
SOURCE_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "phase-source.json")
SCAFFOLD = ("try", "BRIEF.md", "PROPOSAL.json", "evidence", "target")


def log(message: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {message}", flush=True)


def read_json(path: Path):
    return json.loads(path.read_text())


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def run(argv, cwd: Path, log_path: Path, timeout: int, env: dict | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as output:
        try:
            return subprocess.run([*EXEC, *map(str, argv)], cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                  timeout=timeout, env={**os.environ, **(env or {})}).returncode
        except subprocess.TimeoutExpired:
            output.write(f"\n[loop] timed out after {timeout}s\n")
            return 124


def mark_changed(tree: Path) -> None:
    """Give a copied source tree fresh modification times.

    Copies of the workspace can share one cargo target directory, and cargo
    decides whether to rebuild from modification times. Without this, a copy
    older than the last build of a different copy would silently reuse that
    build.
    """
    for path in (tree / "crates").rglob("*"):
        if path.is_file():
            os.utime(path)


class Epoch:
    def __init__(self, run_dir: Path, policy_path: Path, card_data: Path | None):
        self.dir = run_dir
        policy_copy = run_dir / "policy.json"
        if not policy_copy.exists():
            run_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(policy_path, policy_copy)
        self.policy = read_json(policy_copy)
        self.bounds = self.policy["bounds"]
        self.cohort = ROOT / self.policy["cohort"]
        self.card_data = (card_data or ROOT / self.policy["card_data"]).resolve()
        self.cases = read_json(self.cohort / "cohort.json")["cases"]

    # ---- source snapshots -------------------------------------------------
    def snapshot(self, name: str) -> Path:
        dest = self.dir / name
        if dest.exists():
            return dest
        tracked = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"], text=True).split("\0")
        for relative in tracked:
            if relative and (relative.startswith(SOURCE_ROOTS) or relative in SOURCE_FILES) and (ROOT / relative).is_file():
                target = dest / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
        mark_changed(dest)
        return dest

    # ---- build and test ---------------------------------------------------
    def commands(self) -> list[tuple[str, list[str]]]:
        """The build, then every check a change must pass before it is measured."""
        target = self.policy["target"]
        return [("build", target["build_command"]), *[(c["name"], c["argv"]) for c in target["checks"]]]

    def build(self, source: Path, label: str) -> dict:
        record_path = self.dir / f"{label}-build.json"
        if record_path.exists():
            return read_json(record_path)
        target = self.dir / "target" if not os.environ.get("CARGO_TARGET_DIR") and not EXEC else None
        env = {"CARGO_TARGET_DIR": str(target)} if target else {}
        record = {"label": label, "commands": [], "passed": True}
        for name, argv in self.commands():
            log(f"{label}: {' '.join(argv)}")
            started = time.monotonic()
            code = run(argv, source, self.dir / f"{label}-{name}.log", 3600, env)
            record["commands"].append({"name": name, "argv": argv, "exit_code": code, "seconds": round(time.monotonic() - started, 1)})
            if code:
                record["passed"] = False
                break
            if name == "build":
                binary = self.dir / f"{label}-fitter"
                locate = "import os,shutil,sys;shutil.copy2(os.path.join(os.environ.get('CARGO_TARGET_DIR','target'),sys.argv[1]),sys.argv[2])"
                if run(["python3", "-c", locate, self.policy["target"]["binary"], binary], source, self.dir / f"{label}-copy.log", 120, env):
                    record["passed"] = False
                    break
        write_json(record_path, record)
        return record

    # ---- corpus -----------------------------------------------------------
    def manifest(self, binary: Path) -> Path:
        corpus = self.dir / "corpus"
        manifest = corpus / "manifest.json"
        if not manifest.exists():
            if not self.card_data.exists():
                raise SystemExit(f"card data not found: {self.card_data} (pass --card-data)")
            log("preparing the card-data manifest")
            code = run([binary, "materialize", "--set", self.policy["card_set"], "--phase-card-data", self.card_data,
                        "--output-dir", corpus], ROOT, self.dir / "corpus.log", 600)
            if code:
                raise SystemExit(f"could not prepare card data; see {self.dir / 'corpus.log'}")
        return manifest

    # ---- fitting ----------------------------------------------------------
    def fit(self, binary: Path, out: Path, games: list[int] | None = None) -> list[dict]:
        manifest = self.manifest(binary)
        cases = [c for c in self.cases if games is None or c["game_index"] in games]

        def one(case):
            case_dir = out / case["case_id"]
            summary = case_dir / "summary.json"
            if not summary.exists():
                run(["python3", Path(__file__).resolve(), "_fit-one", "--binary", binary, "--manifest", manifest,
                     "--cohort", self.cohort, "--game", case["game_index"], "--out", case_dir,
                     "--bounds", json.dumps(self.bounds), "--filler", self.policy["opponent_filler"]],
                    ROOT, case_dir / "supervisor.log", self.bounds["deadline_seconds"] + 120)
            if not summary.exists():
                write_json(summary, gate.summarize(case, "supervisor_failure", None, None))
            return read_json(summary)

        with ThreadPoolExecutor(self.bounds["concurrency"]) as pool:
            return list(pool.map(one, cases))

    # ---- evidence for the contributor ------------------------------------
    def oracle(self) -> dict:
        if not hasattr(self, "_oracle"):
            self._oracle = {card["name"]: card.get("oracle_text") or "" for card in read_json(self.card_data).values()
                            if isinstance(card, dict) and "name" in card}
        return self._oracle

    def brief(self, summaries: list[dict], discovery: dict, fit_dir: Path) -> str:
        lines = ["# What the baseline fit found", ""]
        lines.append("Each recorded 17Lands game is fitted by searching the engine's legal actions for a line "
                     "that agrees with the recording at every end-of-turn milestone.")
        lines += ["", "## Outcomes", ""]
        for status, count in sorted(discovery["counts"].items()):
            lines.append(f"- {status}: {count}")
        lines += ["", "## Issues, grouped by cause", ""]
        for issue in discovery["issues"]:
            lines.append(f"- `{issue['id']}` {issue['kind']}: {issue['cause']} "
                         f"({len(issue['cases'])} games; fields: {', '.join(issue['columns'][:8]) or 'n/a'})")
        lines += ["", "## Games that did not match", ""]
        names = set()
        for summary in summaries:
            if summary["status"] == gate.MATCHED:
                continue
            lines.append(f"### {summary['case_id']} (game {summary['game_index']}): {summary['status']}")
            lines.append(f"- matched {summary['milestones_fitted']} of {summary['milestones_total']} milestones, "
                         f"{summary['nodes']} search nodes, {summary['duplicate_states']} repeated states skipped")
            native = fit_dir / summary["case_id"] / "native"
            if (native / "constraints.json").exists():
                constraints = read_json(native / "constraints.json")
                lines.append(f"- assumed opponent library, top first: {constraints['libraries'][1][:12]}")
                lines.append(f"- user library, top first: {constraints['libraries'][0][:12]}")
                names.update(constraints["libraries"][0][:14] + constraints["libraries"][1][:10])
            for issue in summary["issues"]:
                if issue["kind"] not in ("unsupported_projection",):
                    lines.append(f"- issue {issue['kind']}: {issue['detail'][:300]}")
            if summary["rejections"]:
                lines.append("- why branches were rejected, most frequent first:")
            for rejection in summary["rejections"][:10]:
                key = rejection.get("milestone")
                where = f"turn owner {key['turn_owner']} turn {key['turn_index']} observing seat {key['observed_player']}" if key else "n/a"
                detail = f"expected {json.dumps(rejection.get('expected'))}, one branch had {json.dumps(rejection.get('example_actual'))}" \
                    if rejection["kind"] == "mismatched" else ""
                lines.append(f"  - {rejection['count']}x {rejection['kind']} `{rejection['subject'][:160]}` at {where}. {detail}"[:700])
                for value in (rejection.get("expected"), rejection.get("example_actual")):
                    if isinstance(value, list):
                        names.update(v for v in value if isinstance(v, str))
            lines.append(f"- full native output: `evidence/{summary['case_id']}/native/` (constraints.json, result.json)")
            lines.append("")
        lines += ["## Oracle text of cards named above", ""]
        oracle = self.oracle()
        for name in sorted(names):
            if name in oracle:
                lines.append(f"- **{name}**: {oracle[name].replace(chr(10), ' / ')}")
        return "\n".join(lines) + "\n"

    # ---- candidate --------------------------------------------------------
    def candidate_diff(self) -> tuple[str, list[str]]:
        baseline, candidate = self.dir / "baseline", self.dir / "candidate"
        changed, chunks = [], []
        files = {p.relative_to(candidate) for p in candidate.rglob("*") if p.is_file()} | \
                {p.relative_to(baseline) for p in baseline.rglob("*") if p.is_file()}
        for relative in sorted(files):
            if relative.parts[0] in SCAFFOLD:
                continue
            before = (baseline / relative).read_bytes() if (baseline / relative).exists() else b""
            after = (candidate / relative).read_bytes() if (candidate / relative).exists() else b""
            if before != after:
                changed.append(str(relative))
                chunks.extend(difflib.unified_diff(before.decode(errors="replace").splitlines(True),
                                                   after.decode(errors="replace").splitlines(True),
                                                   "a/" + str(relative), "b/" + str(relative)))
        return "".join(chunks), changed


CONTRIBUTOR_PROMPT = """You are the contributor in a software improvement loop for a Magic: The Gathering trajectory fitter.

Your working directory is an isolated copy of the Rust source. Read BRIEF.md first: it lists what the
baseline fit found on a frozen cohort of real recorded games. evidence/ holds the baseline's native output per game.

Your job: find the real cause of one issue and fix it, so that at least one game improves and none regresses.

Rules
- You may edit only these files: {allowed}
- Run `./try` to build your copy, run the checks (format, lint, tests) and refit games. `./try 6 8` refits only games 6 and 8;
  `./try` refits the whole cohort and prints the same comparison the gate will use. Use it to test hypotheses.
  Each run takes a few minutes. Investigate before you edit; a guess that does not move a game is worth nothing.
- The gate's exact rules are in {gate}. Improvement means a game newly matches, matches more milestones,
  or checks more recorded fields while still matching. Any regression in any game rejects the change.
- Never weaken a check, drop a recorded field, invent hidden information beyond the stated assumptions,
  or special-case a game or card by name. Do not raise budgets.
- A search that ran out of budget is not proof of an engine bug. Keep what the recording says separate from
  how the search, reconstruction or engine behaves.
- Add or extend a unit test for what you change.

When you are done, write PROPOSAL.json in the working directory with exactly these fields:
{{"status": "propose" or "no_safe_change", "component": "engine"|"observation_adapter"|"reconstruction"|"search"|"invariant",
  "issue_ids": [ids from BRIEF.md], "hypothesis": "what was wrong and how you know",
  "summary": "what you changed", "validation": "what ./try showed", "risks": ["..."]}}
If nothing safe can be done, leave the source unchanged and say why with status no_safe_change.
"""

REVIEWER_PROMPT = """You are the independent reviewer in a software improvement loop for a Magic: The Gathering trajectory fitter.
You did not write this change. Decide whether it should be kept.

Approve only if the diff addresses the cause it claims, the measured improvement plausibly follows from it,
and it does not weaken a check, drop or reinterpret a recorded observation, fabricate hidden information,
prune valid lines unsoundly, or special-case a particular game or card. An improved score alone is not enough.

Be brief. Reply in exactly this plain-text form and nothing else:
VERDICT: approve   (or: VERDICT: reject)
- at most six findings, one sentence each, one per line
SUMMARY: two or three sentences

## Contributor's proposal
PROPOSAL

## Diff
```diff
DIFF
```

## Gate comparison (baseline -> candidate)
COMPARISON

## Build and test
BUILD
"""


def claude(prompt: str, cwd: Path, settings: dict, log_path: Path, tools: list[str], allowed: list[str]) -> dict:
    argv = ["claude", "-p", "--output-format", "json", "--strict-mcp-config", "--no-session-persistence",
            "--max-budget-usd", str(settings["max_budget_usd"]), "--tools", ",".join(tools)]
    if allowed:
        argv += ["--permission-mode", "acceptEdits", "--allowedTools", *allowed]
    if settings.get("model"):
        argv += ["--model", settings["model"]]
    started = time.monotonic()
    try:
        done = subprocess.run(argv, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=settings["timeout_seconds"])
        raw, code = done.stdout, done.returncode
        error = done.stderr[-2000:]
    except subprocess.TimeoutExpired as expired:
        raw, code, error = (expired.stdout or b"").decode(errors="replace") if isinstance(expired.stdout, bytes) else (expired.stdout or ""), 124, "timed out"
    parsed = {"result": raw}
    for line in reversed(raw.splitlines()):  # the CLI may print notices before its JSON line
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "result" in value:
            parsed = value
            break
    record = {"exit_code": code, "seconds": round(time.monotonic() - started, 1), "stderr": error,
              "result": parsed.get("result"), "model_usage": parsed.get("modelUsage"), "cost_usd": parsed.get("total_cost_usd"),
              "turns": parsed.get("num_turns"), "is_error": parsed.get("is_error")}
    write_json(log_path, record)
    return record


def extract_json(text: str | None):
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def parse_review(text: str | None):
    """Read the reviewer's plain-text reply. None when it holds no clear verdict."""
    verdicts = re.findall(r"(?im)^\s*VERDICT:\s*(approve|reject)\b", text or "")
    if len(set(v.lower() for v in verdicts)) != 1:
        return None
    summary = re.search(r"(?ims)^\s*SUMMARY:\s*(.+)", text)
    return {"approve": verdicts[0].lower() == "approve",
            "findings": [line.strip()[1:].strip() for line in text.splitlines() if line.strip().startswith("-")],
            "summary": summary.group(1).strip() if summary else ""}


def table(comparison: dict) -> str:
    rows = ["| game | before | after | result |", "| --- | --- | --- | --- |"]
    for row in comparison["cases"]:
        b, a = row["before"], row["after"]
        note = "; ".join(row["regressions"]) and "REGRESSED: " + "; ".join(row["regressions"]) or "; ".join(row["improvements"]) or ""
        rows.append(f"| {row['case_id'][-2:]} | {b['status']} {b['milestones_fitted']}/{b['milestones_total']} ({b['nodes']} nodes) "
                    f"| {a['status']} {a['milestones_fitted']}/{a['milestones_total']} ({a['nodes']} nodes) | {note} |")
    rows.append("")
    rows.append(f"passed: {comparison['passed']}; improved: {len(comparison['improved'])}; regressed: {len(comparison['regressed'])}"
                + ("; " + "; ".join(comparison["reasons"]) if comparison["reasons"] else ""))
    return "\n".join(rows)


def finish(epoch: Epoch, verdict: str, reason: str, extra: dict | None = None) -> dict:
    decision = {"verdict": verdict, "reason": reason, "finished": datetime.datetime.now().isoformat(timespec="seconds"),
                "claim": "Bounded compatibility on a previously inspected cohort; not a proof of rules correctness.", **(extra or {})}
    write_json(epoch.dir / "decision.json", decision)
    log(f"decision: {verdict} ({reason})")
    return decision


def cmd_epoch(args) -> dict:
    epoch = Epoch(args.run_dir.resolve(), args.policy, args.card_data)
    if (epoch.dir / "decision.json").exists():
        return read_json(epoch.dir / "decision.json")
    policy = epoch.policy
    baseline = epoch.snapshot("baseline")
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "crates"], capture_output=True, text=True).stdout
    write_json(epoch.dir / "run.json", {"base_commit": head, "uncommitted_source_changes": bool(dirty.strip()),
                                         "card_data": str(epoch.card_data), "exec_prefix": EXEC})
    build = epoch.build(baseline, "baseline")
    if not build["passed"]:
        return finish(epoch, "inconclusive", "The baseline did not build or its tests failed.")
    log("fitting the cohort with the baseline")
    before = epoch.fit(epoch.dir / "baseline-fitter", epoch.dir / "baseline-fit")
    discovery = gate.discover(before)
    write_json(epoch.dir / "discovery.json", discovery)
    log(f"baseline outcomes: {discovery['counts']}")

    candidate = epoch.snapshot("candidate")
    (candidate / "BRIEF.md").write_text(epoch.brief(before, discovery, epoch.dir / "baseline-fit"))
    if not (candidate / "evidence").exists():
        (candidate / "evidence").symlink_to(epoch.dir / "baseline-fit")
    helper = candidate / "try"
    helper.write_text(f"#!/bin/sh\nexec python3 {shlex.quote(str(Path(__file__).resolve()))} try --run-dir {shlex.quote(str(epoch.dir))} \"$@\"\n")
    helper.chmod(0o755)

    kind = args.contributor or policy["contributor"]["kind"]
    if kind == "none":
        log(f"no contributor configured. Edit {candidate}, then rerun with --contributor manual")
        return {"verdict": "awaiting_contributor"}
    if kind == "claude-cli" and not (epoch.dir / "contributor.json").exists():
        log("contributor is working in the candidate copy (this can take a while)")
        prompt = CONTRIBUTOR_PROMPT.format(allowed=", ".join(policy["target"]["allowed_paths"]), gate=Path(gate.__file__).resolve())
        claude(prompt, candidate, policy["contributor"], epoch.dir / "contributor.json",
               ["Read", "Grep", "Glob", "Edit", "Write", "Bash"], ["Read", "Grep", "Glob", "Edit", "Write", "Bash(./try:*)", "Bash(./try)"])
    proposal = read_json(candidate / "PROPOSAL.json") if (candidate / "PROPOSAL.json").exists() else None
    diff, changed = epoch.candidate_diff()
    (epoch.dir / "candidate.patch").write_text(diff)
    write_json(epoch.dir / "proposal.json", proposal)
    if not changed:
        return finish(epoch, "no_change", "The contributor left the source unchanged.", {"proposal": proposal})
    outside = [path for path in changed if path not in policy["target"]["allowed_paths"]]
    if outside:
        return finish(epoch, "rejected", "The candidate edits files outside the allowed paths: " + ", ".join(outside), {"proposal": proposal})

    # Never trust the contributor's own trial builds: rebuild and refit from the frozen copy.
    final = epoch.dir / "final"
    if final.exists():
        shutil.rmtree(final)
    shutil.copytree(candidate, final, ignore=shutil.ignore_patterns(*SCAFFOLD))
    mark_changed(final)
    for stale in ("candidate-build.json",):
        (epoch.dir / stale).unlink(missing_ok=True)
    build = epoch.build(final, "candidate")
    if not build["passed"]:
        return finish(epoch, "rejected", "The candidate did not build or its tests failed.", {"proposal": proposal, "changed": changed})
    log("fitting the cohort with the candidate")
    after = epoch.fit(epoch.dir / "candidate-fitter", epoch.dir / "candidate-fit")
    comparison = gate.compare(before, after, policy["evaluation"]["minimum_improved_cases"])
    write_json(epoch.dir / "comparison.json", comparison)
    (epoch.dir / "comparison.md").write_text(table(comparison) + "\n")
    log(f"gate: passed={comparison['passed']} improved={len(comparison['improved'])} regressed={len(comparison['regressed'])}")
    extra = {"proposal": proposal, "changed": changed, "improved": comparison["improved"], "regressed": comparison["regressed"]}
    if not comparison["passed"]:
        return finish(epoch, "rejected", "The gate did not pass: " + "; ".join(comparison["reasons"]), extra)

    return decide(epoch, args, proposal, diff, changed, comparison, build)


def decide(epoch: Epoch, args, proposal, diff: str, changed: list[str], comparison: dict, build: dict) -> dict:
    """Independent review of a change the gate already passed, then the decision."""
    policy = epoch.policy
    extra = {"proposal": proposal, "changed": changed, "improved": comparison["improved"], "regressed": comparison["regressed"]}
    review = None
    if (args.reviewer or policy["reviewer"]["kind"]) == "claude-cli":
        prompt = (REVIEWER_PROMPT.replace("PROPOSAL", json.dumps(proposal, indent=1)).replace("DIFF", diff[:150000])
                  .replace("COMPARISON", table(comparison)).replace("BUILD", json.dumps(build["commands"], indent=1)))
        # A model call can fail for reasons unrelated to the change; try the next model before giving up.
        models = [policy["reviewer"].get("model"), *policy["reviewer"].get("fallback_models", [])]
        for attempt, model in enumerate(models, 1):
            log(f"independent review (attempt {attempt}, model {model or 'default'})")
            record = claude(prompt, epoch.dir, {**policy["reviewer"], "model": model},
                            epoch.dir / f"reviewer-{attempt}.json", [], [])
            review = parse_review(record.get("result")) if not record.get("is_error") else None
            if review:
                review["model"] = model or "default"
                break
        write_json(epoch.dir / "review.json", review)
        if not review:
            return finish(epoch, "inconclusive", "The reviewer did not return a usable verdict. "
                          "Rerun the review with: run.py review --run-dir <this run>", extra)
        if not review["approve"]:
            return finish(epoch, "rejected", "The independent reviewer rejected the change.", {**extra, "review": review})
    decision = finish(epoch, "accepted", "Tests pass, at least one game improved, none regressed, and the reviewer approved."
                      if review else "Tests pass, at least one game improved and none regressed. No reviewer was configured.",
                      {**extra, "review": review})
    if args.apply:
        for relative in changed:
            shutil.copy2(epoch.dir / "final" / relative, ROOT / relative)
        log(f"applied {len(changed)} file(s) to the working tree")
    return decision


def cmd_review(args) -> dict:
    """Redo only the review and decision of an epoch whose gate already passed."""
    epoch = Epoch(args.run_dir.resolve(), args.run_dir / "policy.json", None)
    comparison = read_json(epoch.dir / "comparison.json")
    if not comparison["passed"]:
        raise SystemExit("this epoch's gate did not pass; there is nothing to review")
    diff, changed = epoch.candidate_diff()
    if diff != (epoch.dir / "candidate.patch").read_text():
        raise SystemExit("the candidate copy changed after it was measured; run a new epoch")
    return decide(epoch, args, read_json(epoch.dir / "proposal.json"), diff, changed, comparison,
                  read_json(epoch.dir / "candidate-build.json"))


def cmd_try(args) -> None:
    """Contributor's experiment: build the candidate copy, test it, refit, compare."""
    epoch = Epoch(args.run_dir.resolve(), args.run_dir / "policy.json", None)
    epoch.card_data = Path(read_json(epoch.dir / "run.json")["card_data"])
    candidate = epoch.dir / "candidate"
    number = len(list((epoch.dir / "tries").glob("*"))) + 1 if (epoch.dir / "tries").exists() else 1
    out = epoch.dir / "tries" / f"{number:03d}"
    target = {} if os.environ.get("CARGO_TARGET_DIR") or EXEC else {"CARGO_TARGET_DIR": str(epoch.dir / "target")}
    for name, argv in epoch.commands():
        code = run(argv, candidate, out / f"{name}.log", 3600, target)
        if code:
            print(f"{name} FAILED (exit {code}). Last lines of the log:")
            print("".join((out / f"{name}.log").read_text().splitlines(True)[-60:]))
            return
        if name == "build":
            locate = "import os,shutil,sys;shutil.copy2(os.path.join(os.environ.get('CARGO_TARGET_DIR','target'),sys.argv[1]),sys.argv[2])"
            run(["python3", "-c", locate, epoch.policy["target"]["binary"], out / "fitter"], candidate, out / "copy.log", 120, target)
    print("build and checks passed")
    games = [int(g) for g in args.games] or None
    after = epoch.fit(out / "fitter", out / "fit", games)
    before = [read_json(epoch.dir / "baseline-fit" / s["case_id"] / "summary.json") for s in after]
    comparison = gate.compare(before, after, epoch.policy["evaluation"]["minimum_improved_cases"])
    print(table(comparison))
    for summary in after:
        if summary["status"] != gate.MATCHED and summary["rejections"]:
            print(f"\n{summary['case_id']} top rejections:")
            for rejection in summary["rejections"][:6]:
                print(f"  {rejection['count']}x {rejection['kind']} {rejection['subject'][:120]} "
                      f"expected={json.dumps(rejection.get('expected'))[:160]} had={json.dumps(rejection.get('example_actual'))[:160]}")
    print(f"\nnative output of this try: {out / 'fit'}")
    if games:
        print("This compared only the games you named. Run ./try with no arguments before finishing.")


def cmd_fit_one(args) -> None:
    """Supervise one native fit inside the execution environment."""
    bounds = json.loads(args.bounds)
    cohort = Path(args.cohort)
    case = next(c for c in read_json(cohort / "cohort.json")["cases"] if c["game_index"] == args.game)
    out = Path(args.out)
    native = out / "native"
    out.mkdir(parents=True, exist_ok=True)
    games, cards = cohort / "games.csv", cohort / "cards.csv"

    def sha(path):
        return subprocess.check_output(["sha256sum", str(path)], text=True).split()[0]

    argv = [args.binary, "fit17lands", "--manifest-uri", args.manifest, "--replay-data", str(games), "--replay-sha256", sha(games),
            "--cards-csv", str(cards), "--cards-csv-sha256", sha(cards), "--game-index", str(args.game),
            "--turn-pairs", str(bounds["turn_pairs"]), "--nodes", str(bounds["nodes"]), "--max-actions", str(bounds["max_actions"]),
            "--opponent-filler", args.filler, "--output-dir", str(native)]

    def limits():
        resource.setrlimit(resource.RLIMIT_AS, (bounds["memory_bytes"], bounds["memory_bytes"]))
        os.setsid()

    started = time.monotonic()
    with (out / "stdout.txt").open("w") as stdout, (out / "stderr.txt").open("w") as stderr:
        process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, preexec_fn=limits)
        try:
            code = process.wait(timeout=bounds["deadline_seconds"])
            execution = "completed" if code == 0 else "process_failure"
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            code, execution = None, "external_deadline"
    result = read_json(native / "result.json") if execution == "completed" and (native / "result.json").exists() else None
    constraints = read_json(native / "constraints.json") if (native / "constraints.json").exists() else None
    summary = gate.summarize(case, execution, result, constraints)
    summary["seconds"] = round(time.monotonic() - started, 2)
    write_json(out / "summary.json", summary)


def cmd_loop(args) -> None:
    root = args.run_root.resolve()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(1, args.epochs + 1):
        args.run_dir = root / f"{stamp}-epoch{index}"
        args.apply = True
        log(f"epoch {index} of {args.epochs}: {args.run_dir}")
        decision = cmd_epoch(args)
        if decision["verdict"] != "accepted":
            log("stopping: the last epoch kept nothing")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--policy", type=Path, default=ROOT / "loop/policy.json")
        p.add_argument("--card-data", type=Path, help="Phase card export; defaults to the policy's card_data")
        p.add_argument("--contributor", choices=["claude-cli", "manual", "none"])
        p.add_argument("--reviewer", choices=["claude-cli", "none"])

    epoch = sub.add_parser("epoch", help="run one epoch")
    common(epoch)
    epoch.add_argument("--run-dir", type=Path, required=True)
    epoch.add_argument("--apply", action="store_true", help="copy an accepted change into the working tree")
    epoch.set_defaults(fn=cmd_epoch)
    loop = sub.add_parser("loop", help="run epochs back to back, applying each accepted change")
    common(loop)
    loop.add_argument("--epochs", type=int, default=3)
    loop.add_argument("--run-root", type=Path, default=ROOT / "tmp/loop-runs")
    loop.set_defaults(fn=cmd_loop)
    review = sub.add_parser("review", help="redo the review and decision of an epoch whose gate passed")
    review.add_argument("--run-dir", type=Path, required=True)
    review.add_argument("--reviewer", choices=["claude-cli", "none"])
    review.add_argument("--apply", action="store_true")
    review.set_defaults(fn=cmd_review)
    attempt = sub.add_parser("try", help="contributor experiment inside an epoch")
    attempt.add_argument("--run-dir", type=Path, required=True)
    attempt.add_argument("games", nargs="*")
    attempt.set_defaults(fn=cmd_try)
    one = sub.add_parser("_fit-one")
    for name in ("binary", "manifest", "cohort", "out", "bounds", "filler"):
        one.add_argument("--" + name, required=True)
    one.add_argument("--game", type=int, required=True)
    one.set_defaults(fn=cmd_fit_one)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
