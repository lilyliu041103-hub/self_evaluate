import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.scoring import build_groups, build_group_request, evaluate_prepared


def check(cid, method="J", ids=None, metric="AC"):
    return {"id": cid, "rubric_id": cid, "metric_id": metric, "method": method,
            "criterion": "Fixed criterion " + cid, "prompt": "Judge this criterion only.",
            "evidence_ids": ids or ["shared"], "group": "answer", "input_complete": True,
            "missing_evidence": [], "bindings": {"value": 1}}


def plan(checks):
    return {"task_id": "TEST", "plan_hash": "fixed", "profiles": [
        {"id": c["rubric_id"], "metric_id": c["metric_id"],
         "how": {"allowed_methods": ["P", "J", "H"], "programmatic_handler": "assert"}}
        for c in checks]}


EVIDENCE = {"shared": {"id": "shared", "scope": "observation", "kind": "final_answer",
                       "data": "A complete observation", "origin": "source", "content_hash": "hash"}}


def verdicts(request, value=True):
    return {"results": [{"check_id": cid, "status": "scored", "verdict": value}
                        for cid in request["check_ids"]]}


class GroupedScoringTests(unittest.TestCase):
    def test_shared_evidence_once_separate_verdicts(self):
        checks = [check("AC-1"), check("TC-1", metric="TC")]
        requests = []
        def judge(request):
            body = json.loads(request["user"])
            self.assertEqual(list(body["evidence"]), ["shared"])
            self.assertEqual(len(body["checks"]), 2)
            answer = verdicts(request)
            answer["results"][1]["verdict"] = False
            return answer
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, judge, request_sink=requests)
        self.assertEqual(len(requests), 1)
        self.assertTrue(report["rubrics"]["AC-1"]["verdict"])
        self.assertFalse(report["rubrics"]["TC-1"]["verdict"])

    def test_missing_evidence_never_calls_judge(self):
        checks = [check("AC-1", ids=["unavailable"])]
        report = evaluate_prepared(plan(checks), checks, EVIDENCE,
                                   lambda _: self.fail("No API expected"))
        self.assertEqual(report["rubrics"]["AC-1"]["status"], "unverifiable")
        self.assertEqual(report["execution"]["judge_call_count"], 0)

    def test_required_readiness_false(self):
        checks = [check("AC-1")]
        checks[0]["input_complete"] = False
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail())
        self.assertEqual(report["execution"]["diagnostics"]["AC-1"]["code"], "missing_evidence")

    def test_budget_does_not_truncate(self):
        checks = [check("AC-1")]
        groups = build_groups(checks, EVIDENCE, max_chars=20)
        self.assertEqual(groups[0]["reason"], "request_budget_exceeded")
        self.assertEqual(groups[0]["evidence"], EVIDENCE)
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail(), max_chars=20)
        self.assertEqual(report["rubrics"]["AC-1"]["status"], "unverifiable")
        self.assertEqual(report["execution"]["diagnostics"]["AC-1"]["code"], "request_budget_exceeded")

    def test_size_and_count_split(self):
        checks = [check("AC-1"), check("AC-2")]
        singleton = build_groups(checks[:1], EVIDENCE)[0]
        limit = singleton["request_chars"] + 10
        self.assertEqual(len(build_groups(checks, EVIDENCE, max_chars=limit)), 2)
        self.assertEqual(len(build_groups(checks, EVIDENCE, max_checks=1)), 2)

    def test_duplicate_id_does_not_cross_score(self):
        checks = [check("AC-1"), check("AC-2")]
        def judge(_):
            return {"results": [{"check_id": "AC-1", "status": "scored", "verdict": True},
                                {"check_id": "AC-1", "status": "scored", "verdict": False}]}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, judge)
        self.assertTrue(all(r["status"] == "unverifiable" for r in report["rubrics"].values()))
        self.assertEqual(report["execution"]["diagnostics"]["AC-1"]["code"], "duplicate_check_id")
        self.assertEqual(report["execution"]["diagnostics"]["AC-2"]["code"], "missing_check_result")

    def test_judge_cannot_choose_na_or_numeric_bool(self):
        checks = [check("AC-1")]
        for status, value in [("not_applicable", None), ("scored", 1)]:
            report = evaluate_prepared(plan(checks), checks, EVIDENCE,
                lambda _: {"results": [{"check_id": "AC-1", "status": status, "verdict": value}]})
            self.assertEqual(report["rubrics"]["AC-1"]["status"], "unverifiable")

    def test_h_program_failure_short_circuits(self):
        checks = [check("AC-1", "H")]
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail(),
                                   {"assert": lambda data, bindings: {"status": "scored", "score": 0}})
        self.assertFalse(report["metrics"]["AC"]["verdict"])
        self.assertEqual(report["execution"]["judge_call_count"], 0)

    def test_h_program_unknown_and_judge_pass_is_unknown(self):
        checks = [check("AC-1", "H")]
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, verdicts,
                                   {"assert": lambda data, bindings: {"status": "unverifiable", "score": None}})
        self.assertEqual(report["rubrics"]["AC-1"]["status"], "unverifiable")

    def test_false_wins_unknown_in_metric(self):
        checks = [check("AC-1"), check("AC-2", ids=["missing"])]
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda r: verdicts(r, False))
        self.assertFalse(report["metrics"]["AC"]["verdict"])

    def test_p_missing_bindings_never_falls_back(self):
        checks = [check("AC-3", "P")]
        checks[0]["bindings"] = {}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail())
        self.assertEqual(report["rubrics"]["AC-3"]["status"], "unverifiable")

    def test_full_profile_coverage(self):
        checks = [check("AC-1")]
        prepared = plan(checks + [check("TC-1", metric="TC")])
        report = evaluate_prepared(prepared, checks, EVIDENCE, verdicts)
        self.assertEqual(set(report["rubrics"]), {"AC-1", "TC-1"})
        self.assertEqual(report["rubrics"]["TC-1"]["status"], "unverifiable")

    def test_instantiation_notes_do_not_add_unknown_to_passing_checks(self):
        checks = [check("AC-1")]
        prepared = plan(checks)
        prepared["unresolved"] = [{"rubric_id": "AC-1", "missing": ["No independent numeric reference"]}]
        before = copy.deepcopy(prepared)
        report = evaluate_prepared(prepared, checks, EVIDENCE, verdicts)
        self.assertTrue(report["checks"]["AC-1"]["verdict"])
        self.assertEqual(report["rubrics"]["AC-1"], {"status": "scored", "verdict": True})
        self.assertTrue(report["metrics"]["AC"]["verdict"])
        self.assertEqual(report["unresolved"], prepared["unresolved"])
        self.assertEqual(report["execution"]["rubric_diagnostics"]["AC-1"]["missing"],
                         ["No independent numeric reference"])
        self.assertEqual(report["execution"]["rubric_diagnostics"]["AC-1"]["code"],
                         "instantiation_notes")
        self.assertEqual(prepared, before)

    def test_instantiation_notes_do_not_change_failed_checks(self):
        checks = [check("AC-1")]
        prepared = plan(checks)
        prepared["unresolved"] = [{"rubric_id": "AC-1", "missing": ["Missing reference"]}]
        report = evaluate_prepared(prepared, checks, EVIDENCE, lambda r: verdicts(r, False))
        self.assertEqual(report["rubrics"]["AC-1"], {"status": "scored", "verdict": False})
        self.assertFalse(report["metrics"]["AC"]["verdict"])
        self.assertTrue(report["unresolved"])

    def test_unresolved_with_zero_checks_remains_reported(self):
        prepared = plan([check("AC-1"), check("TC-1", metric="TC")])
        prepared["unresolved"] = [{"rubric_id": "AC-1", "missing": ["No supplied reference"]}]
        report = evaluate_prepared(prepared, [], EVIDENCE, lambda _: self.fail("No API expected"))
        self.assertEqual(set(report["rubrics"]), {"AC-1", "TC-1"})
        self.assertTrue(all(r["status"] == "unverifiable" for r in report["rubrics"].values()))
        self.assertEqual(report["execution"]["rubric_diagnostics"]["AC-1"]["code"], "instantiation_notes")
        self.assertEqual(report["execution"]["judge_call_count"], 0)

    def test_instantiation_notes_do_not_change_frozen_na(self):
        checks = [check("AC-1")]
        checks[0]["when"] = {"path": "/reference/enabled", "op": "eq", "expected": True}
        prepared = plan(checks)
        prepared["unresolved"] = [{"rubric_id": "AC-1", "missing": ["Another unconfigured requirement"]}]
        report = evaluate_prepared(prepared, checks, EVIDENCE, lambda _: self.fail("No API expected"),
                                   original_input={"reference": {"enabled": False}})
        self.assertEqual(report["checks"]["AC-1"]["status"], "not_applicable")
        self.assertEqual(report["rubrics"]["AC-1"]["status"], "not_applicable")
        self.assertEqual(report["execution"]["diagnostics"]["AC-1"]["code"], "frozen_condition_false")
        self.assertIn("AC-1", report["execution"]["rubric_diagnostics"])

    def test_actual_check_missing_evidence_still_makes_rubric_unknown(self):
        checks = [check("known"), check("missing", ids=["unavailable"])]
        for item in checks:
            item["rubric_id"] = "EE-4"
        prepared = plan(checks[:1])
        prepared["unresolved"] = [{"rubric_id": "EE-4", "missing": ["Other operations might exist"]}]
        report = evaluate_prepared(prepared, checks, EVIDENCE, verdicts)
        self.assertTrue(report["checks"]["known"]["verdict"])
        self.assertEqual(report["checks"]["missing"]["status"], "unverifiable")
        self.assertEqual(report["rubrics"]["EE-4"]["status"], "unverifiable")
        self.assertEqual(report["execution"]["diagnostics"]["missing"]["code"], "missing_evidence")

    def test_invalid_unresolved_never_calls_judge(self):
        checks = [check("AC-1")]
        valid = {"rubric_id": "AC-1", "missing": ["Missing reference"]}
        for invalid in [None, {}, [valid, valid], [{**valid, "rubric_id": "UNKNOWN"}],
                        [{**valid, "missing": []}], [{**valid, "missing": [""]}],
                        [{**valid, "missing": [1]}], [{**valid, "missing": ["a", "a"]}],
                        [{**valid, "extra": True}]]:
            with self.subTest(unresolved=invalid):
                prepared = plan(checks)
                prepared["unresolved"] = invalid
                with self.assertRaises(ValueError):
                    evaluate_prepared(prepared, checks, EVIDENCE, lambda _: self.fail("No API expected"))

    def test_multiple_checks_of_one_rubric_keep_independent_results(self):
        checks = [check("AC-1-facts"), check("AC-1-numbers")]
        for item in checks:
            item["rubric_id"] = "AC-1"
        prepared = plan(checks[:1])
        def judge(request):
            result = verdicts(request)
            result["results"][1]["verdict"] = False
            return result
        report = evaluate_prepared(prepared, checks, EVIDENCE, judge)
        self.assertTrue(report["checks"]["AC-1-facts"]["verdict"])
        self.assertFalse(report["checks"]["AC-1-numbers"]["verdict"])
        self.assertFalse(report["rubrics"]["AC-1"]["verdict"])

    def test_dependency_program_runs_after_judge(self):
        checks = [check("AC-2", "P"), check("AC-1")]
        checks[0]["bindings"] = {"path": "/evaluation/checks/AC-1/score"}
        def program(data, bindings):
            self.assertEqual(data["evaluation"]["checks"]["AC-1"]["score"], 1)
            return {"status": "scored", "score": 1}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, verdicts, {"assert": program})
        self.assertTrue(report["metrics"]["AC"]["verdict"])

    def test_invalid_input_does_not_mutate_original(self):
        checks = [check("AC-1")]
        before = copy.deepcopy(checks)
        evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: "not JSON")
        self.assertEqual(before, checks)
        bad = plan(checks)
        bad["profiles"][0]["how"]["allowed_methods"] = ["P"]
        with self.assertRaises(ValueError):
            evaluate_prepared(bad, checks, EVIDENCE)

    def test_wire_carries_scope_boundary_and_keeps_all_data(self):
        checks = [check("AC-1")]
        checks[0].update(applicability_policy="Absence needs complete evidence.",
                         selection={"bounded": True, "matched_count": 5, "selected_count": 1},
                         how={"boundary": "Facts only", "inputs": ["facts", "answer"]})
        evidence = copy.deepcopy(EVIDENCE)
        evidence["shared"]["data"] = {"text": "untouched", "nested": [1, None], "evidence_ref": "full-log"}
        request = build_group_request(build_groups(checks, evidence)[0])
        body = json.loads(request["user"])
        self.assertEqual(body["evidence"]["shared"]["data"], evidence["shared"]["data"])
        self.assertEqual(set(body["evidence"]["shared"]), {"kind", "scope", "data"})
        wire = body["checks"][0]
        self.assertEqual(wire["selection"], checks[0]["selection"])
        self.assertEqual(wire["boundary"], "Facts only")
        self.assertEqual(wire["inputs"], ["facts", "answer"])
        self.assertIn("query_evidence/read_evidence", request["system"])
        self.assertIn("never prove absence", request["system"])

    def test_frozen_false_when_marks_na_before_evidence_readiness(self):
        checks = [check("ER-1", ids=["missing"])]
        checks[0]["when"] = {"path": "/reference/has_error", "op": "eq", "expected": True}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail(),
                                   original_input={"reference": {"has_error": False}})
        self.assertEqual(report["rubrics"]["ER-1"]["status"], "not_applicable")
        self.assertEqual(report["metrics"]["AC"]["status"], "not_applicable")

    def test_true_when_runs_judge(self):
        checks = [check("AC-1")]
        checks[0]["when"] = {"path": "/reference/enabled", "op": "eq", "expected": True}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, verdicts,
                                   original_input={"reference": {"enabled": True}})
        self.assertTrue(report["rubrics"]["AC-1"]["verdict"])

    def test_when_missing_or_untrusted_is_unknown_not_na(self):
        checks = [check("ER-1")]
        for path, op, original in [
            ("/reference/has_error", "eq", {"reference": {}}),
            ("/reference/missing", "exists", {"reference": {}}),
            ("/observation/has_error", "eq", {"observation": {"has_error": False}}),
        ]:
            checks[0]["when"] = {"path": path, "op": op, "expected": True}
            report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail(), original_input=original)
            self.assertEqual(report["rubrics"]["ER-1"]["status"], "unverifiable")

    def test_trusted_observation_when_and_na_aggregation(self):
        checks = [check("ER-1"), check("AC-1")]
        checks[0]["when"] = {"path": "/observation/adapter/has_error", "op": "eq", "expected": True}
        original = {"reference": {"trusted_observation_paths": ["/observation/adapter"]},
                    "observation": {"adapter": {"has_error": False}}}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, verdicts, original_input=original)
        self.assertEqual(report["rubrics"]["ER-1"]["status"], "not_applicable")
        self.assertTrue(report["metrics"]["AC"]["verdict"])

    def test_nested_observation_trust_matches_instantiation_contract(self):
        checks = [check("ER-1")]
        checks[0]["when"] = {"path": "/observation/execution_status", "op": "eq", "expected": "error"}
        original = {"reference": {"observation_contract": {
            "trusted_observation_paths": ["/observation/execution_status"]}},
            "observation": {"execution_status": "ok"}}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail("No API expected"),
                                   original_input=original)
        self.assertEqual(report["rubrics"]["ER-1"]["status"], "not_applicable")
        original["observation"]["execution_status"] = "error"
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, verdicts, original_input=original)
        self.assertTrue(report["rubrics"]["ER-1"]["verdict"])

    def test_explicit_empty_nested_trust_overrides_legacy_declaration(self):
        checks = [check("ER-1")]
        checks[0]["when"] = {"path": "/observation/execution_status", "op": "eq", "expected": "error"}
        original = {"reference": {
            "trusted_observation_paths": ["/observation/execution_status"],
            "observation_contract": {"trusted_observation_paths": []}},
            "observation": {"execution_status": "ok"}}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail("No API expected"),
                                   original_input=original)
        self.assertEqual(report["rubrics"]["ER-1"]["status"], "unverifiable")
        self.assertEqual(report["execution"]["diagnostics"]["ER-1"]["code"], "untrusted_applicability_path")

    def test_malformed_nested_trust_is_unknown_without_legacy_fallback(self):
        checks = [check("ER-1")]
        checks[0]["when"] = {"path": "/observation/execution_status", "op": "eq", "expected": "error"}
        malformed = [None, [], {"trusted_observation_paths": None},
                     {"trusted_observation_paths": "/observation/execution_status"},
                     {"trusted_observation_paths": ["/observation/execution_status", 1]},
                     {"trusted_observation_paths": ["/observation/execution_status", "/task"]}]
        for contract in malformed:
            with self.subTest(contract=contract):
                original = {"reference": {
                    "trusted_observation_paths": ["/observation/execution_status"],
                    "observation_contract": contract}, "observation": {"execution_status": "ok"}}
                report = evaluate_prepared(plan(checks), checks, EVIDENCE,
                    lambda _: self.fail("No API expected"), original_input=original)
                self.assertEqual(report["rubrics"]["ER-1"]["status"], "unverifiable")

    def test_legacy_trust_remains_available_when_nested_not_declared(self):
        checks = [check("ER-1")]
        checks[0]["when"] = {"path": "/observation/execution_status", "op": "eq", "expected": "error"}
        original = {"reference": {
            "trusted_observation_paths": ["/observation/execution_status"],
            "observation_contract": {"execution_status": "Runtime status"}},
            "observation": {"execution_status": "ok"}}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail("No API expected"),
                                   original_input=original)
        self.assertEqual(report["rubrics"]["ER-1"]["status"], "not_applicable")

    def test_when_matches_v4_json_type_semantics(self):
        checks = [check("AC-1")]
        checks[0]["when"] = {"path": "/reference/number", "op": "eq", "expected": True}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: self.fail(),
                                   original_input={"reference": {"number": 1}})
        self.assertEqual(report["rubrics"]["AC-1"]["status"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
