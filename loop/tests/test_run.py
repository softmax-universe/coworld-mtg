import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run  # noqa: E402


class RunnerTests(unittest.TestCase):
    def epoch(self, root: Path) -> run.Epoch:
        (root / "policy.json").write_text((run.ROOT / "loop/policy.json").read_text())
        return run.Epoch(root, root / "policy.json", None)

    def test_reply_json_is_found_inside_surrounding_text(self):
        self.assertEqual(run.extract_json('Sure.\n```json\n{"approve": true}\n```'), {"approve": True})
        self.assertIsNone(run.extract_json("no verdict here"))
        self.assertIsNone(run.extract_json(None))

    def test_review_needs_one_unambiguous_verdict(self):
        review = run.parse_review("VERDICT: approve\n- narrow change\n- has a test\nSUMMARY: Keep it.\nIt is sound.")
        self.assertEqual(review, {"approve": True, "findings": ["narrow change", "has a test"], "summary": "Keep it.\nIt is sound."})
        self.assertFalse(run.parse_review("verdict: reject\nSUMMARY: weakens a check")["approve"])
        self.assertIsNone(run.parse_review("I would approve this."))
        self.assertIsNone(run.parse_review("VERDICT: approve\nVERDICT: reject"))
        self.assertIsNone(run.parse_review(None))

    def test_diff_reports_source_edits_and_ignores_the_contributor_scaffold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("baseline", "candidate"):
                (root / name / "crates/x/src").mkdir(parents=True)
                (root / name / "crates/x/src/lib.rs").write_text("fn a() {}\n")
            (root / "candidate/crates/x/src/lib.rs").write_text("fn b() {}\n")
            (root / "candidate/BRIEF.md").write_text("notes")
            (root / "candidate/PROPOSAL.json").write_text("{}")
            diff, changed = self.epoch(root).candidate_diff()
            self.assertEqual(changed, ["crates/x/src/lib.rs"])
            self.assertIn("+fn b() {}", diff)

    def test_checks_run_after_the_build(self):
        with tempfile.TemporaryDirectory() as directory:
            names = [name for name, _ in self.epoch(Path(directory)).commands()]
            self.assertEqual(names[0], "build")
            self.assertEqual(set(names[1:]), {"format", "lint", "test"})

    def test_policy_only_allows_edits_to_existing_fitter_files(self):
        policy = json.loads((run.ROOT / "loop/policy.json").read_text())
        for path in policy["target"]["allowed_paths"]:
            self.assertTrue((run.ROOT / path).is_file(), path)
            self.assertFalse(path.startswith("loop/"))


if __name__ == "__main__":
    unittest.main()
