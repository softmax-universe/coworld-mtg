import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gate  # noqa: E402


def record(case="g1", execution="completed", status=gate.MATCHED, fitted=8, total=8, full=0, nodes=50, fields=None, issues=None):
    return {
        "case_id": case, "game_index": 1, "execution": execution, "status": status, "nodes": nodes,
        "milestones_fitted": fitted, "milestones_total": total, "fully_covered_milestones": full,
        "field_contracts": fields if fields is not None else {
            "user_turn_1_eot_user_life": {"raw": "20.0", "disposition": "enforced", "projection": "life", "expected": 20}},
        "issues": issues or [], "rejections": [], "source_sha256": "s", "cards_mapping_sha256": "m",
    }


class CompareTests(unittest.TestCase):
    def test_newly_matched_game_is_an_improvement(self):
        row = gate.compare_pair(record(status="budget_exhausted", fitted=6), record())
        self.assertEqual(row["regressions"], [])
        self.assertEqual(row["improvements"], ["newly matched"])

    def test_longer_prefix_is_an_improvement_and_shorter_is_a_regression(self):
        self.assertTrue(gate.compare_pair(record(status="budget_exhausted", fitted=2), record(status="budget_exhausted", fitted=6))["improvements"])
        self.assertIn("matched fewer milestones", gate.compare_pair(record(status="budget_exhausted", fitted=6), record(status="budget_exhausted", fitted=2))["regressions"])

    def test_losing_a_match_blocks_even_with_other_gains(self):
        comparison = gate.compare(
            [record("a"), record("b", status="budget_exhausted", fitted=2)],
            [record("a", status="budget_exhausted", fitted=8), record("b")], 1)
        self.assertFalse(comparison["passed"])
        self.assertEqual(comparison["regressed"], ["a"])

    def test_unfinished_run_only_counts_against_a_finished_baseline(self):
        unfinished = record(execution="external_deadline", status="external_deadline", fitted=None, nodes=None, fields={})
        self.assertEqual(gate.compare_pair(unfinished, unfinished)["regressions"], [])
        self.assertIn("candidate run did not finish: external_deadline", gate.compare_pair(record(), unfinished)["regressions"])

    def test_dropping_or_rewriting_an_enforced_field_is_a_regression(self):
        dropped = record(fields={"user_turn_1_eot_user_life": {"raw": "20.0", "disposition": "informational", "projection": None, "expected": None}})
        self.assertTrue(any("enforced field" in r for r in gate.compare_pair(record(), dropped)["regressions"]))
        altered = record(fields={"user_turn_1_eot_user_life": {"raw": "19.0", "disposition": "enforced", "projection": "life", "expected": 19}})
        self.assertTrue(any("source field altered" in r for r in gate.compare_pair(record(), altered)["regressions"]))

    def test_checking_more_fields_counts_only_while_still_matching(self):
        more = dict(record()["field_contracts"])
        more["user_turn_1_user_mana_spent"] = {"raw": "1.0", "disposition": "enforced", "projection": "mana", "expected": 1}
        before = record(fields={**record()["field_contracts"], "user_turn_1_user_mana_spent": {"raw": "1.0", "disposition": "unsupported", "projection": None, "expected": None}})
        self.assertEqual(gate.compare_pair(before, record(fields=more))["improvements"], ["checks more recorded fields and still matches"])
        self.assertEqual(gate.compare_pair(before, record(fields=more, status="budget_exhausted"))["improvements"], [])

    def test_fewer_nodes_or_no_change_is_not_an_improvement(self):
        comparison = gate.compare([record()], [record(nodes=10)], 1)
        self.assertFalse(comparison["passed"])
        self.assertEqual(comparison["improved"], [])

    def test_changed_game_set_fails(self):
        self.assertFalse(gate.compare([record("a")], [record("b")], 0)["passed"])


class DiscoverTests(unittest.TestCase):
    def test_groups_by_cause_not_by_column(self):
        issues = [{"kind": "unsupported_projection", "detail": "x", "source_columns": [column],
                   "evidence": {"diagnostic_code": "mana_payment_observation_incomplete"}}
                  for column in ("user_turn_1_user_mana_spent", "oppo_turn_2_oppo_mana_spent")]
        found = gate.discover([record("a", issues=issues, full=8), record("b", issues=issues[:1], full=8)])
        self.assertEqual(len(found["issues"]), 1)
        self.assertEqual(found["issues"][0]["cases"], ["a", "b"])
        self.assertEqual(found["issues"][0]["columns"], ["user_mana_spent", "oppo_mana_spent"])

    def test_partial_match_is_reported(self):
        found = gate.discover([record()])
        self.assertEqual([i["kind"] for i in found["issues"]], ["partial_projection"])


if __name__ == "__main__":
    unittest.main()
