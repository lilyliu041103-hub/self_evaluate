import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.scoring import build_groups, build_group_request, evaluate_prepared
from eval_bridge.evidence import EvidenceStore, canonical, make_index, write_json


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
    def test_judge_reasons_survive_scoring_and_failure_links(self):
        checks = [check("AC-1:pass"), check("AC-1:fail"), check("AC-1:unknown")]
        for item in checks:
            item["rubric_id"] = "AC-1"
        results = [
            {"check_id": "AC-1:pass", "status": "scored", "verdict": True,
             "reason": "shared 中的已交付内容满足要求。"},
            {"check_id": "AC-1:fail", "status": "scored", "verdict": False,
             "reason": "shared 中的事实陈述与参考记录冲突。"},
            {"check_id": "AC-1:unknown", "status": "unverifiable", "verdict": None,
             "reason": "shared 未包含确认该字段所需的时间记录。"},
        ]
        before = copy.deepcopy(results)
        requests = []
        report = evaluate_prepared(plan(checks[:1]), checks, EVIDENCE,
                                   lambda _: json.dumps({"results": results}), request_sink=requests)
        for result in results:
            self.assertEqual(report["checks"][result["check_id"]],
                             {k: v for k, v in result.items() if k != "check_id"})
        self.assertEqual(report["rubrics"]["AC-1"]["check_ids"], [c["id"] for c in checks])
        self.assertEqual(report["rubrics"]["AC-1"]["failed_check_ids"], ["AC-1:fail"])
        self.assertEqual(report["metrics"]["AC"]["failed_rubric_ids"], ["AC-1"])
        self.assertFalse(report["metrics"]["AC"]["verdict"])
        self.assertEqual(report["execution"]["diagnostics"], {})
        self.assertIn("Every result must include a nonempty reason", requests[0]["system"])
        self.assertEqual(results, before)

    def test_invalid_reason_does_not_discard_valid_sibling(self):
        checks = [check("AC-1"), check("AC-2")]
        for reason in (None, "", "  ", 3, [], {}):
            with self.subTest(reason=reason):
                response = {"results": [
                    {"check_id": "AC-1", "status": "scored", "verdict": False, "reason": reason},
                    {"check_id": "AC-2", "status": "scored", "verdict": True,
                     "reason": "shared supports this conclusion."},
                ]}
                report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: response)
                self.assertEqual(report["checks"]["AC-1"]["status"], "unverifiable")
                self.assertEqual(report["execution"]["diagnostics"]["AC-1"]["code"],
                                 "invalid_result_reason")
                self.assertTrue(report["checks"]["AC-2"]["verdict"])
                self.assertEqual(report["checks"]["AC-2"]["reason"], "shared supports this conclusion.")

    def test_legacy_verdict_is_preserved_and_missing_reason_is_diagnosed(self):
        checks = [check("AC-1")]
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda r: verdicts(r, False))
        self.assertEqual(report["checks"]["AC-1"], {"status": "scored", "verdict": False})
        self.assertEqual(report["execution"]["diagnostics"]["AC-1"]["code"], "missing_judge_reason")
        self.assertEqual(report["rubrics"]["AC-1"]["failed_check_ids"], ["AC-1"])

    def test_hybrid_retains_judge_reason_and_explains_program_uncertainty(self):
        checks = [check("AC-1", "H")]
        response = {"results": [{"check_id": "AC-1", "status": "scored", "verdict": True,
                                 "reason": "shared 满足语义要求。"}]}
        report = evaluate_prepared(plan(checks), checks, EVIDENCE, lambda _: response,
                                   {"assert": lambda data, bindings: {"status": "unverifiable", "score": None}})
        self.assertEqual(report["checks"]["AC-1"]["status"], "unverifiable")
        self.assertEqual(report["checks"]["AC-1"]["reason"],
                         "程序检查无法核验，模型检查通过。shared 满足语义要求。")
        self.assertEqual(report["metrics"]["AC"]["failed_rubric_ids"], [])

    def test_metric_mode_splits_metrics_and_preserves_legacy_evidence(self):
        checks = [check("AC-1"), check("TC-1", metric="TC"), check("AC-2")]
        baseline = build_groups(checks, EVIDENCE, scoring_mode="baseline")
        isolated = build_groups(checks, EVIDENCE, scoring_mode="metric")
        self.assertEqual(len(baseline), 1)
        self.assertEqual([group["check_ids"] for group in isolated], [["AC-1", "AC-2"], ["TC-1"]])
        for group in isolated:
            request = build_group_request(group)
            body = json.loads(request["user"])
            self.assertEqual(len({c["metric_id"] for c in body["checks"]}), 1)
            self.assertEqual(body["evidence"], json.loads(build_group_request(baseline[0])["user"])["evidence"])

    def test_common_prefix_is_stable_and_keeps_source_metadata(self):
        checks = [check("AC-1", ids=["shared", "ac"]), check("TC-1", metric="TC", ids=["shared", "tc"])]
        evidence = copy.deepcopy(EVIDENCE)
        evidence["shared"]["completeness"] = {"truncated": False}
        for eid in ("ac", "tc", "task:request"):
            evidence[eid] = {"id": eid, "scope": "task" if eid == "task:request" else "observation",
                             "kind": "task_request" if eid == "task:request" else "artifact",
                             "origin": {"pointer": "/" + eid}, "content_hash": eid, "data": eid + " content"}
        requests = []
        report = evaluate_prepared(plan(checks), checks, evidence, verdicts, request_sink=requests,
                                   scoring_mode="metric", common_evidence_mode="shared")
        self.assertEqual(len(requests), 2)
        prefixes = [r["user"][:r["common_prefix_chars"]] for r in requests]
        self.assertEqual(prefixes[0], prefixes[1])
        self.assertEqual(requests[0]["common_prefix_hash"], requests[1]["common_prefix_hash"])
        expected_hash = hashlib.sha256(json.dumps(
            {"system": requests[0]["system"], "user_prefix": prefixes[0]},
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(requests[0]["common_prefix_hash"], expected_hash)
        body = json.loads(requests[0]["user"])
        self.assertEqual(list(body), ["common", "metric_ids", "evidence", "checks"])
        self.assertEqual(body["common"]["evidence"]["shared"], evidence["shared"])
        self.assertEqual(body["common"]["evidence"]["task:request"], evidence["task:request"])
        self.assertEqual(list(body["evidence"]), ["ac"])
        self.assertEqual(body["evidence"]["ac"], evidence["ac"])
        self.assertTrue(report["execution"]["evidence_visibility_expanded"])
        self.assertEqual(report["execution"]["groups"][0]["additional_common_evidence_ids"], ["task:request"])
        reversed_evidence = {eid: dict(reversed(list(record.items())))
                             for eid, record in reversed(list(evidence.items()))}
        repeated = build_group_request(build_groups(checks, reversed_evidence,
            scoring_mode="metric", common_evidence_mode="shared")[0])
        self.assertEqual(repeated["common_prefix_hash"], expected_hash)

    def test_common_prefix_is_frozen_across_dependency_waves(self):
        checks = [check("AC-1"), check("TC-1", metric="TC")]
        checks[1]["bindings"] = {"path": "/evaluation/checks/AC-1/score"}
        evidence = copy.deepcopy(EVIDENCE)
        evidence["only-ac"] = {"id": "only-ac", "scope": "observation", "kind": "artifact", "data": "A"}
        checks[0]["evidence_ids"].append("only-ac")
        requests = []
        report = evaluate_prepared(plan(checks), checks, evidence, verdicts, request_sink=requests,
                                   scoring_mode="metric", common_evidence_mode="shared")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["common_prefix_hash"], requests[1]["common_prefix_hash"])
        self.assertNotIn("only-ac", json.loads(requests[0]["user"])["common"]["evidence"])
        self.assertTrue(report["metrics"]["TC"]["verdict"])

    def test_packs_share_trace_prefix_across_metrics_without_expanding_evidence(self):
        evidence = copy.deepcopy(EVIDENCE)
        for eid in ("trace-a", "trace-b", "trace-c"):
            evidence[eid] = {"id": eid, "kind": "trace_payload", "scope": "observation",
                             "data": {"input": eid, "output": eid + " result", "evidence_ref": "external"},
                             "origin": {"pointer": "/events/" + eid}, "completeness": {"truncated": False}}
        evidence["external"] = {"id": "external", "data": "Can be read through the evidence tool"}
        checks = [check("AC-1", ids=["shared", "trace-a", "trace-b"]),
                  check("TC-1", metric="TC", ids=["shared", "trace-a"]),
                  check("PE-1", metric="PE", ids=["shared", "trace-c"])]
        before = copy.deepcopy((checks, evidence))
        exports = {}
        for mode in ("shared", "packs"):
            exports[mode] = [build_group_request(g) for g in build_groups(
                checks, evidence, scoring_mode="metric", common_evidence_mode=mode)]
        for old, new in zip(exports["shared"], exports["packs"]):
            old_body, new_body = json.loads(old["user"]), json.loads(new["user"])
            self.assertEqual(old_body, new_body)  # Only ordering changes, including all provenance.
            self.assertEqual(new["request_chars"], old["request_chars"])
            self.assertEqual(list(new_body), ["common", "evidence", "metric_ids", "checks"])
            self.assertNotIn("external", new_body["evidence"])
            self.assertNotIn('"metric_ids"', new["user"][:new["evidence_prefix_chars"]])
        ac, tc, pe = exports["packs"]
        a_boundary = ac["pack_prefixes"][0]
        self.assertEqual(a_boundary, tc["pack_prefixes"][0])
        self.assertEqual(ac["user"][:a_boundary["prefix_chars"]], tc["user"][:a_boundary["prefix_chars"]])
        self.assertEqual(len(ac["evidence_pack_ids"]), 2)
        self.assertEqual(len(tc["evidence_pack_ids"]), 1)
        self.assertNotEqual(ac["evidence_prefix_hash"], tc["evidence_prefix_hash"])
        self.assertNotEqual(a_boundary["prefix_hash"], pe["pack_prefixes"][0]["prefix_hash"])
        self.assertEqual((checks, evidence), before)

    def test_pack_plan_and_prefix_survive_batching_and_dependency_waves(self):
        evidence = copy.deepcopy(EVIDENCE)
        for eid in ("trace-a", "trace-b"):
            evidence[eid] = {"id": eid, "kind": "trace_payload", "scope": "observation", "data": eid}
        checks = [check("AC-1", ids=["shared", "trace-a"]),
                  check("AC-2", ids=["shared", "trace-a"]),
                  check("TC-1", metric="TC", ids=["shared", "trace-a"]),
                  check("PE-1", metric="PE", ids=["shared", "trace-b"])]
        checks[2]["bindings"] = {"path": "/evaluation/checks/AC-1/score"}
        requests = []
        report = evaluate_prepared(plan(checks), checks, evidence, verdicts, request_sink=requests,
                                   scoring_mode="metric", common_evidence_mode="packs", max_checks=1)
        reused = [r for r in requests if r["metric_id"] in {"AC", "TC"}]
        self.assertEqual(len(reused), 3)
        self.assertEqual(len({r["evidence_prefix_hash"] for r in reused}), 1)
        self.assertEqual(len({r["user"][:r["evidence_prefix_chars"]] for r in reused}), 1)
        self.assertEqual(len(report["execution"]["evidence_packs"]["packs"]), 2)
        self.assertTrue(all(r["verdict"] for r in report["checks"].values()))
        # The same content and plan give the same prefixes despite dict/input order.
        reversed_evidence = {eid: dict(reversed(list(record.items())))
                             for eid, record in reversed(list(evidence.items()))}
        repeated = []
        evaluate_prepared(plan(checks), list(reversed(checks)), reversed_evidence, verdicts,
                          request_sink=repeated, scoring_mode="metric", common_evidence_mode="packs", max_checks=1)
        self.assertEqual({r["check_ids"][0]: r["evidence_prefix_hash"] for r in requests},
                         {r["check_ids"][0]: r["evidence_prefix_hash"] for r in repeated})

    def test_pack_hash_tracks_content_and_prefix_hash_tracks_preceding_context(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence["a"] = {"id": "a", "scope": "observation", "kind": "trace_payload", "data": "A"}
        evidence["b"] = {"id": "b", "scope": "observation", "kind": "trace_payload", "data": "B"}
        checks = [check("AC-1", ids=["shared", "a"]), check("TC-1", metric="TC", ids=["shared", "b"])]
        def request():
            return build_group_request(build_groups(checks, evidence, scoring_mode="metric",
                                                     common_evidence_mode="packs")[0])
        first = request()
        evidence["shared"]["data"] = "Different preceding context"
        changed_context = request()
        self.assertEqual(first["evidence_pack_ids"], changed_context["evidence_pack_ids"])
        self.assertNotEqual(first["pack_prefixes"][0]["prefix_hash"], changed_context["pack_prefixes"][0]["prefix_hash"])
        evidence["a"]["data"] = "Changed trace content"
        changed_trace = request()
        self.assertNotEqual(changed_context["evidence_pack_ids"], changed_trace["evidence_pack_ids"])
        self.assertNotEqual(changed_context["evidence_prefix_hash"], changed_trace["evidence_prefix_hash"])

    def test_pack_budget_does_not_truncate_or_drop_multi_record_evidence(self):
        evidence = {"a": {"id": "a", "data": "large original trace " * 300},
                    "b": {"id": "b", "data": "follow-up event " * 300},
                    "c": {"id": "c", "data": "separate observation"}}
        checks = [check("AC-1", ids=["a", "b"]), check("TC-1", metric="TC", ids=["c"])]
        options = dict(scoring_mode="metric", common_evidence_mode="packs",
                       request_overhead_chars=200, output_reserve_chars=100, tool_reserve_chars=100)
        groups = build_groups(checks, evidence, **options)
        size = groups[0]["request_chars"]
        exact = build_groups(checks, evidence, max_chars=size, **options)[0]
        self.assertEqual(exact["status"], "ready")
        blocked = build_groups(checks, evidence, max_chars=size - 1, **options)[0]
        self.assertEqual(blocked["reason"], "request_budget_exceeded")
        self.assertEqual(json.loads(build_group_request(blocked)["user"])["evidence"],
                         {eid: evidence[eid] for eid in ("a", "b")})
        broken = copy.deepcopy(exact)
        del broken["evidence"]["b"]
        with self.assertRaisesRegex(ValueError, "expand"):
            build_group_request(broken)
        broken = copy.deepcopy(exact)
        broken["evidence_packs"] = []
        with self.assertRaisesRegex(ValueError, "cover"):
            build_group_request(broken)

    def test_full_budget_includes_overhead_and_reserves(self):
        checks = [check("AC-1")]
        base = build_groups(checks, EVIDENCE)[0]["request_chars"]
        options = {"request_overhead_chars": 100, "output_reserve_chars": 200, "tool_reserve_chars": 300}
        exact = build_groups(checks, EVIDENCE, max_chars=base + 600, **options)[0]
        self.assertEqual(exact["status"], "ready")
        self.assertEqual(exact["input_request_chars"], base + 100)
        self.assertEqual(exact["request_chars"], base + 600)
        request = build_group_request(exact)
        self.assertEqual(request["tool_reserve_chars"], 300)
        no_tools = build_group_request(build_groups(checks, EVIDENCE, tool_reserve_chars=0)[0])
        self.assertEqual(no_tools["tool_reserve_chars"], 0)
        legacy = build_group_request(build_groups(checks, EVIDENCE)[0])
        self.assertNotIn("tool_reserve_chars", legacy)
        blocked = build_groups(checks, EVIDENCE, max_chars=base + 599, **options)[0]
        self.assertEqual(blocked["reason"], "request_budget_exceeded")
        self.assertEqual(blocked["evidence"], EVIDENCE)

    def test_bounded_common_index_keeps_full_supplement_and_reports_omissions(self):
        checks = [check("AC-1")]
        evidence = copy.deepcopy(EVIDENCE)
        evidence["shared"]["data"] = "long trace " * 200
        for n in range(30):
            evidence[str(n)] = {"id": str(n), "scope": "observation", "kind": "trace_event",
                                 "origin": {"pointer": "/events/" + str(n)}, "data": "trace"}
        request = build_group_request(build_groups(checks, evidence, scoring_mode="metric",
            common_evidence_mode="shared", common_prefix_max_chars=800)[0])
        self.assertLessEqual(request["common_prefix_chars"], 800)
        body = json.loads(request["user"])
        self.assertFalse(body["common"]["selection"]["index_complete"])
        self.assertEqual(body["common"]["selection"]["total_records"], len(evidence))
        self.assertEqual(body["evidence"]["shared"], evidence["shared"])
        self.assertEqual(body["checks"][0]["evidence_ids"], ["shared"])

    def test_compact_index_excludes_large_metadata_and_preserves_loaded_records(self):
        evidence = copy.deepcopy(EVIDENCE)
        for eid, length in (("shared", 1000), ("ac", 20000), ("index-only", 100000)):
            evidence[eid] = {"id": eid, "scope": "observation", "kind": "artifact",
                             "data": {"text": "original " + eid},
                             "origin": {"pointer": "/" + eid, "details": "o" * length},
                             "content_hash": "h" * length,
                             "arbitrary_metadata": {"payload": "m" * length},
                             "completeness": {"truncated": False}}
        before = copy.deepcopy(evidence)
        checks = [check("AC-1", ids=["shared", "ac"]), check("TC-1", metric="TC")]
        groups = build_groups(checks, evidence, max_chars=200000, scoring_mode="metric",
                              common_evidence_mode="shared")
        payload = json.loads(build_group_request(groups[0])["user"])
        common = payload["common"]
        self.assertEqual(common["schema_version"], "score-common/2")
        self.assertEqual({entry["id"] for entry in common["evidence_index"]}, set(evidence))
        for entry in common["evidence_index"]:
            self.assertEqual(set(entry), {"id", "kind", "scope"})
        self.assertEqual(common["evidence"]["shared"], evidence["shared"])
        self.assertEqual(payload["evidence"]["ac"], evidence["ac"])
        self.assertTrue(common["selection"]["index_complete"])
        self.assertEqual(evidence, before)

    def test_index_budget_stays_small_when_total_prefix_budget_increases(self):
        evidence = copy.deepcopy(EVIDENCE)
        for number in range(200):
            eid = "event:" + str(number).zfill(4)
            evidence[eid] = {"id": eid, "scope": "observation", "kind": "trace_event",
                             "data": {"event": number}, "origin": {"pointer": "/events/" + str(number)}}
        requests = []
        for limit in (12000, 24000):
            request = build_group_request(build_groups([check("AC-1")], evidence,
                scoring_mode="metric", common_evidence_mode="shared", common_prefix_max_chars=limit)[0])
            common = json.loads(request["user"])["common"]
            index = common["evidence_index"]
            self.assertLessEqual(len(canonical(index)), 2000)
            self.assertEqual(common["selection"]["index_max_chars"], 2000)
            self.assertEqual(common["selection"]["indexed_records"], len(index))
            self.assertEqual(common["selection"]["total_records"], len(evidence))
            self.assertFalse(common["selection"]["index_complete"])
            self.assertGreater(len(index), 0)
            self.assertLess(len(index), len(evidence))
            self.assertLess(request["common_prefix_chars"], 4000)
            requests.append(request)
        self.assertEqual(requests[0]["common_prefix_hash"], requests[1]["common_prefix_hash"])

    def test_compact_table_locator_reads_complete_record_and_schema(self):
        schema_id = "table:fixture:schema"
        row_id = "table:fixture:row:ROW-1:1"
        evidence = {
            schema_id: {"id": schema_id, "scope": "reference", "kind": "table_schema",
                        "data": {"table_id": "table:fixture", "columns": ["record_id", "value"]},
                        "origin": {"pointer": "/reference/table/header"}, "content_hash": "schema-hash"},
            row_id: {"id": row_id, "scope": "reference", "kind": "table_row",
                     "data": {"table_id": "table:fixture", "values": ["ROW-1", "original value"]},
                     "origin": {"pointer": "/reference/table/rows/0"}, "content_hash": "row-hash",
                     "table_id": "table:fixture", "business_id": "ROW-1", "schema_id": schema_id}}
        request = build_group_request(build_groups([check("AC-1", ids=[row_id])], evidence,
            scoring_mode="metric", common_evidence_mode="shared")[0])
        locator = next(entry for entry in json.loads(request["user"])["common"]["evidence_index"]
                       if entry["id"] == row_id)
        self.assertEqual(set(locator), {"id", "kind", "scope"})
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            write_json(bundle / "evidence.json", evidence)
            write_json(bundle / "index.json", make_index(evidence))
            result = EvidenceStore(bundle).read([locator["id"]])
        self.assertTrue(result["complete"])
        self.assertEqual({record["id"]: record for record in result["records"]}, evidence)

    def test_invalid_modes_and_budgets_rejected_for_program_only_plan(self):
        checks = [check("AC-3", "P")]
        for kwargs in ({"scoring_mode": "invalid"}, {"common_evidence_mode": "invalid"},
                       {"output_reserve_chars": -1}, {"tool_reserve_chars": True},
                       {"request_overhead_chars": 1.1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                evaluate_prepared(plan(checks), checks, EVIDENCE, **kwargs)

    def test_metric_isolation_preserves_hybrid_and_global_dependencies(self):
        checks = [check("AC-1"), check("TC-1", "H", metric="TC"),
                  check("BC-1", "P", metric="BC")]
        checks[1]["bindings"] = {"path": "/evaluation/checks/AC-1/score"}
        checks[2]["bindings"] = {"path": "/evaluation/checks/TC-1/score"}
        def program(data, bindings):
            prerequisite = bindings["path"].split("/")[3]
            self.assertEqual(data["evaluation"]["checks"][prerequisite]["score"], 1)
            return {"status": "scored", "score": 1}
        results = []
        for mode, layout in (("baseline", "shared"), ("metric", "shared"), ("metric", "packs")):
            report = evaluate_prepared(plan(checks), checks, EVIDENCE, verdicts,
                {"assert": program}, scoring_mode=mode, common_evidence_mode=layout)
            self.assertEqual(report["execution"]["program_call_count"], 2)
            self.assertEqual(report["execution"]["judge_call_count"], 2)
            self.assertEqual(len({g["common_prefix_hash"] for g in report["execution"]["groups"]}), 1)
            results.append({k: report[k] for k in ("checks", "rubrics", "metrics")})
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

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
        self.assertEqual(report["rubrics"]["AC-1"], {"status": "scored", "verdict": True,
                                                     "check_ids": ["AC-1"], "failed_check_ids": []})
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
        self.assertEqual(report["rubrics"]["AC-1"], {"status": "scored", "verdict": False,
                                                     "check_ids": ["AC-1"], "failed_check_ids": ["AC-1"]})
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
