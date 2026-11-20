"""Tests for the offline metering audit (``power_billing.audit``).

Run with::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest
from fractions import Fraction as F
from pathlib import Path

from power_billing import (
    ArchiveFile,
    AuditError,
    MeterAudit,
    MeterReadingStore,
    optimal_strategy,
    parse_archives,
    parse_case,
    parse_check,
    parse_topology,
    run_scenario,
)

MATERIALS = Path(__file__).resolve().parent.parent / "materials" / "900_audit"


def load(name: str) -> dict:
    return json.loads((MATERIALS / name).read_text())


# --------------------------------------------------------------------------- #
# A compact helper to build an audit without a scenario document.
# --------------------------------------------------------------------------- #
def _topology(epochs):
    return {
        "topology_id": "T", "revision": "t1",
        "epochs": epochs,
    }


def _epoch(eid, start, end, edges):
    """edges: list of (id, parent, kind, total_meter_or_None)."""
    return {
        "epoch_id": eid, "start": start, "end": end,
        "nodes": [
            {"id": i, "parent": p, "kind": k,
             **({"total_meter": m} if m else {})}
            for i, p, k, m in edges
        ],
    }


def _readings(revision, meters):
    return {"revision": revision, "meters": meters}


def _segs(*triples):
    return [{"start": s, "end": e, "power": p} for s, e, p in triples]


class MainScenarioTests(unittest.TestCase):
    """The worked one-hour example from the brief."""

    def setUp(self):
        self.report = run_scenario(load("scenario.json"))

    def test_root_check_keeps_all_four(self):
        root = self.report["initial"]["checks"][0]
        self.assertEqual(root["status"], "constraint")
        self.assertEqual(root["measured_difference"], {"n": 60, "d": 1})
        self.assertEqual(root["matches"], ["a", "b", "c", "d"])

    def test_branch_check_narrows_to_a_b(self):
        # Branch M1 covers a and b; c,d predict 0.
        ab = self.report["initial"]["checks"][1]
        self.assertEqual(ab["matches"], ["a", "b"])
        self.assertEqual(ab["predicted_difference"]["c"], {"n": 0, "d": 1})
        self.assertEqual(self.report["final_candidates"], ["a", "b"])
        self.assertEqual(self.report["final_status"], "multiple")

    def test_strategy_all_open_starts_with_G_worst_two(self):
        strat = next(s for s in self.report["strategies"]
                     if s["name"] == "all4_all_open")
        self.assertEqual(strat["status"], "identifiable")
        self.assertEqual(strat["worst_cost"], {"n": 2, "d": 1})
        self.assertEqual(strat["first_file"], 1)     # G is file 1
        self.assertTrue(strat["identifiable"])

    def test_closing_G_makes_b_and_d_indistinguishable(self):
        strat = next(s for s in self.report["strategies"]
                     if s["name"] == "all4_without_G")
        self.assertEqual(strat["status"], "indistinguishable")
        self.assertFalse(strat["identifiable"])
        self.assertEqual(strat["indistinguishable"], [["b", "d"]])

    def test_after_branch_check_only_A_needed(self):
        strat = next(s for s in self.report["strategies"]
                     if s["name"] == "ab_after_branch_check")
        self.assertEqual(strat["status"], "identifiable")
        self.assertEqual(strat["first_file"], 2)     # A is file 2
        self.assertEqual(strat["worst_cost"], {"n": 1, "d": 1})
        self.assertEqual(strat["candidates"], ["a", "b"])

    def test_adaptive_tree_yes_no_subsets(self):
        strat = next(s for s in self.report["strategies"]
                     if s["name"] == "all4_all_open")
        tree = strat["tree"]
        self.assertEqual(tree["action"], "read")
        self.assertEqual(tree["file_no"], 1)
        # G = {a,b}: yes -> {a,b} (then read A=file 2), no -> {c,d} (then C=3).
        self.assertEqual(tree["yes"]["action"], "read")
        self.assertEqual(tree["yes"]["file_no"], 2)
        self.assertEqual(tree["no"]["action"], "read")
        self.assertEqual(tree["no"]["file_no"], 3)
        self.assertEqual(tree["yes"]["worst_cost"], {"n": 1, "d": 1})
        self.assertEqual(tree["no"]["worst_cost"], {"n": 1, "d": 1})


class FractionalAndRevisionTests(unittest.TestCase):
    """A moves out of branch N1 at t=25/2; a revision supplies the gap."""

    def setUp(self):
        self.report = run_scenario(load("fractional.json"))

    def test_initial_branch_check_is_missing_evidence(self):
        report = self.report["initial"]
        self.assertEqual(report["readings_revision"], "r1")
        xp1 = next(c for c in report["checks"] if c["check_id"] == "Xp1")
        self.assertEqual(xp1["status"], "missing_evidence")
        self.assertEqual(len(xp1["missing"]), 1)
        self.assertEqual(xp1["missing"][0]["meter"], "N1")
        self.assertEqual(xp1["missing"][0]["reason"], "gap")
        self.assertEqual(xp1["missing"][0]["start"], {"n": 0, "d": 1})
        self.assertEqual(xp1["missing"][0]["end"], {"n": 25, "d": 2})
        # Only the root check constrains, so all four remain.
        self.assertEqual(report["candidates"], ["a", "b", "c", "d"])

    def test_fractional_predicted_difference(self):
        # a belongs to N1 only over [0, 25/2), so predicted = d * 25/2.
        xp1 = next(c for c in self.report["initial"]["checks"]
                   if c["check_id"] == "Xp1")
        self.assertEqual(xp1["predicted_difference"]["a"], {"n": 25, "d": 2})
        self.assertEqual(xp1["predicted_difference"]["b"], {"n": 60, "d": 1})

    def test_revision_is_adopted_and_narrows_to_a(self):
        revised = self.report["revised"]
        self.assertIsNotNone(revised)
        self.assertEqual(revised["readings_revision"], "r2")
        xp1 = next(c for c in revised["checks"] if c["check_id"] == "Xp1")
        self.assertEqual(xp1["status"], "constraint")
        self.assertEqual(xp1["measured_difference"], {"n": 25, "d": 2})
        self.assertEqual(xp1["matches"], ["a"])
        self.assertEqual(self.report["final_candidates"], ["a"])
        self.assertEqual(self.report["final_status"], "unique")

    def test_same_leaf_identity_across_epochs(self):
        topo = parse_topology(load("fractional.json")["topology"])
        # a appears exactly once in the leaf universe yet sits under P1 in E0
        # and P2 in E1 (identity preserved across the re-switch).
        self.assertEqual(topo.leaf_ids.count("a"), 1)
        self.assertIn("a", topo.leaves_under("E0", "P1"))
        self.assertIn("a", topo.leaves_under("E1", "P2"))
        self.assertNotIn("a", topo.leaves_under("E1", "P1"))


class ContradictionTests(unittest.TestCase):
    def setUp(self):
        self.report = run_scenario(load("contradiction.json"))

    def test_contradiction_when_no_leaf_matches(self):
        self.assertEqual(self.report["final_status"], "contradiction")
        self.assertEqual(self.report["final_candidates"], [])
        xbad = next(c for c in self.report["initial"]["checks"]
                    if c["check_id"] == "Xbad")
        self.assertEqual(xbad["status"], "constraint")
        self.assertEqual(xbad["measured_difference"], {"n": 30, "d": 1})
        self.assertEqual(xbad["matches"], [])

    def test_no_match_is_explained(self):
        xbad = next(c for c in self.report["initial"]["checks"]
                    if c["check_id"] == "Xbad")
        self.assertIn("matches no leaf", xbad["explanation"])

    def test_distinct_from_missing_evidence(self):
        # A constraint with no matches (contradiction) is not the same status
        # as a check that could not be evaluated (missing evidence).
        statuses = {c["check_id"]: c["status"]
                    for c in self.report["initial"]["checks"]}
        self.assertEqual(statuses["Xbad"], "constraint")
        self.assertEqual(self.report["initial"]["status"], "contradiction")


class CheckSemanticsTests(unittest.TestCase):
    def test_temporal_split_and_predicted_duration(self):
        # a is under branch P only for [0,10); b stays under P the whole time.
        topo = parse_topology(_topology([
            _epoch("E0", 0, 10, [
                ("R", None, "branch", "M"),
                ("P", "R", "branch", "MX"),
                ("a", "P", "leaf", None), ("b", "P", "leaf", None)]),
            _epoch("E1", 10, 20, [
                ("R", None, "branch", "M"),
                ("P", "R", "branch", "MX"),
                ("a", "R", "leaf", None), ("b", "P", "leaf", None)]),
        ]))
        case = parse_case({"case_id": "c", "period": [0, 20],
                           "deviation_power": 2})
        _, meters = _meter_store(_readings("r", {
            "M": _segs((0, 20, 0)),
            "MX": _segs((0, 10, 0), (10, 20, 0)),
            "a": _segs((0, 20, 0)),
            "b": _segs((0, 20, 0)),
        }))
        audit = MeterAudit(topo, meters, case)
        chk = parse_check({"check_id": "cX", "branch": "MX", "period": [0, 20]})
        res = audit.run_check(chk)
        # a belongs to MX for [0,10) -> predicted 2*10 = 20; b for 20 -> 40.
        self.assertEqual(res.predicted_difference["a"], F(20))
        self.assertEqual(res.predicted_difference["b"], F(40))

    def test_missing_total_meter_is_missing_evidence(self):
        topo = parse_topology(load("scenario.json")["topology"])
        case = parse_case(load("scenario.json")["case"])
        _, meters = _meter_store(_readings("r", {
            "M1": _segs((0, 60, 1)), "Mroot": _segs((0, 60, 1)),
            "M2": _segs((0, 60, 0)),
            "a": _segs((0, 60, 0)), "b": _segs((0, 60, 0)),
            "c": _segs((0, 60, 0)), "d": _segs((0, 60, 0)),
        }))
        audit = MeterAudit(topo, meters, case)
        chk = parse_check({"check_id": "x", "branch": "NOPE", "period": [0, 60]})
        res = audit.run_check(chk)
        self.assertEqual(res.status, "missing_evidence")
        self.assertTrue(any(m.reason == "branch_absent" for m in res.missing))

    def test_check_outside_topology_lists_no_topology_range(self):
        topo = parse_topology(load("scenario.json")["topology"])
        case = parse_case(load("scenario.json")["case"])
        _, meters = _meter_store(_readings("r", {
            "M1": _segs((0, 60, 1)), "Mroot": _segs((0, 60, 1)),
            "M2": _segs((0, 60, 0)), "a": _segs((0, 60, 0)),
            "b": _segs((0, 60, 0)), "c": _segs((0, 60, 0)),
            "d": _segs((0, 60, 0)),
        }))
        audit = MeterAudit(topo, meters, case)
        chk = parse_check({"check_id": "x", "branch": "M1", "period": [30, 90]})
        res = audit.run_check(chk)
        self.assertEqual(res.status, "missing_evidence")
        self.assertTrue(any(m.reason == "no_topology" for m in res.missing))


def _meter_store(readings_doc):
    store = MeterReadingStore()
    rev = store.register_json(readings_doc)
    return store.current()


class StrategyTests(unittest.TestCase):
    def _files(self, *specs):
        return [ArchiveFile(no, F(cost), frozenset(leaves))
                for no, cost, leaves in specs]

    def test_example_worst_cost_two_first_G(self):
        files = self._files((1, 1, ["a", "b"]), (2, 1, ["a"]), (3, 1, ["c"]))
        strat = optimal_strategy(["a", "b", "c", "d"], files)
        self.assertEqual(strat["worst_cost"], {"n": 2, "d": 1})
        self.assertEqual(strat["first_file"], 1)

    def test_tie_breaks_to_smallest_file_number(self):
        # Two singleton files both cost 1 and both split {x,y} perfectly.
        files = self._files((5, 1, ["x"]), (2, 1, ["x"]))
        strat = optimal_strategy(["x", "y"], files)
        self.assertEqual(strat["worst_cost"], {"n": 1, "d": 1})
        self.assertEqual(strat["first_file"], 2)  # smaller file number wins

    def test_all_identical_answers_is_indistinguishable(self):
        # Every file includes both or neither -> b,d inseparable from the rest.
        files = self._files((1, 1, ["a", "b"]), (2, 1, ["a", "b"]))
        strat = optimal_strategy(["a", "b"], files)
        self.assertEqual(strat["status"], "indistinguishable")
        self.assertEqual(strat["indistinguishable"], [["a", "b"]])
        self.assertFalse(strat["identifiable"])

    def test_cost_overrides_smaller_file_number(self):
        # File 1 and file 2 have the same leaf set {a}; file 2 is cheaper, so
        # the cheapest first read wins even though file 1 has the smaller
        # number (tie-break by number applies only *among equal costs*).
        files = self._files((1, 100, ["a"]), (2, 1, ["a"]), (3, 1, ["c"]))
        strat = optimal_strategy(["a", "b", "c"], files)
        self.assertEqual(strat["first_file"], 2)
        self.assertEqual(strat["worst_cost"], {"n": 2, "d": 1})

    def test_non_splitting_file_is_never_a_first_read(self):
        # A file containing every candidate (or none) does not split the set
        # and must not be chosen while a real split exists.
        files = self._files((1, 1, ["a", "b", "c"]), (2, 1, ["a"]))
        strat = optimal_strategy(["a", "b", "c"], files)
        self.assertEqual(strat["first_file"], 2)

    def test_open_file_restriction_changes_result(self):
        files = self._files((1, 1, ["a", "b"]), (2, 1, ["a"]), (3, 1, ["c"]))
        open_only = optimal_strategy(["a", "b", "c", "d"], files,
                                     open_files=[2, 3])
        self.assertEqual(open_only["open_files"], [2, 3])
        self.assertEqual(open_only["first_file"], 2)

    def test_single_candidate_needs_no_reads(self):
        files = self._files((1, 1, ["a"]))
        strat = optimal_strategy(["a"], files)
        self.assertEqual(strat["status"], "unique")
        self.assertEqual(strat["worst_cost"], {"n": 0, "d": 1})
        self.assertIsNone(strat["first_file"])

    def test_limits_enforced(self):
        files = [ArchiveFile(i, F(1), frozenset(["a"])) for i in range(1, 14)]
        with self.assertRaises(AuditError):
            optimal_strategy(list("abcdefghij"), files)   # 10 ok
        with self.assertRaises(AuditError):
            optimal_strategy(list("abcdefghijk"), files)  # 11 not ok

    def test_costs_parsed_and_integer_only(self):
        with self.assertRaises(AuditError):
            parse_archives({"files": [{"file_no": 1, "cost": "3/2",
                                       "leaves": ["a"]}]})
        with self.assertRaises(AuditError):
            parse_archives({"files": [{"file_no": 1, "cost": 0,
                                       "leaves": ["a"]}]})
        files = parse_archives({"files": [{"file_no": 1, "cost": 2,
                                           "leaves": ["a"]}]})
        self.assertEqual(files[0].cost, F(2))


class ValidationTests(unittest.TestCase):
    def test_float_rejected(self):
        with self.assertRaises(AuditError):
            parse_case({"case_id": "c", "period": [0, 1.5],
                        "deviation_power": 1})

    def test_leaf_set_must_be_stable(self):
        with self.assertRaises(AuditError):
            parse_topology(_topology([
                _epoch("E0", 0, 10, [("R", None, "branch", "M"),
                                     ("a", "R", "leaf", None)]),
                _epoch("E1", 10, 20, [("R", None, "branch", "M"),
                                      ("b", "R", "leaf", None)]),
            ]))

    def test_single_root_required(self):
        with self.assertRaises(AuditError):
            parse_topology(_topology([
                _epoch("E0", 0, 10, [("R", None, "branch", "M"),
                                     ("S", None, "branch", "N"),
                                     ("a", "R", "leaf", None),
                                     ("b", "S", "leaf", None)])]))

    def test_leaf_cannot_have_children(self):
        with self.assertRaises(AuditError):
            parse_topology(_topology([
                _epoch("E0", 0, 10, [("R", None, "branch", "M"),
                                     ("a", "R", "leaf", None),
                                     ("z", "a", "leaf", None)])]))

    def test_deviation_power_must_be_positive(self):
        with self.assertRaises(AuditError):
            parse_case({"case_id": "c", "period": [0, 10],
                        "deviation_power": 0})


class JsonShapeTests(unittest.TestCase):
    def test_report_is_json_serializable(self):
        report = run_scenario(load("scenario.json"))
        json.dumps(report)  # raises if a Fraction leaked

    def test_fraction_objects_have_positive_denominator(self):
        report = run_scenario(load("fractional.json"))
        pred = report["revised"]["checks"][1]["predicted_difference"]["a"]
        self.assertEqual(pred, {"n": 25, "d": 2})


if __name__ == "__main__":
    unittest.main()
