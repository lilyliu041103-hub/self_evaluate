"""Execute reviewed task-specific checks with one input/configuration contract.

Public output contains metric verdicts only. Legacy evaluator diagnostics stay
inside adapters and are not returned or requested from an LLM.
"""
from __future__ import annotations

import copy

from .binary_judge import build_request, normalize_binary, unknown, validate_response


def _from_legacy(value):
    if not isinstance(value, dict):
        return unknown()
    if value.get("known_failure") is True:
        return {"status": "scored", "verdict": False}
    status, score = value.get("status"), value.get("score")
    if status == "scored" and not isinstance(score, bool) and score in (0, 1):
        return {"status": "scored", "verdict": bool(score)}
    if status == "not_applicable" and score is None:
        return {"status": status, "verdict": None}
    return unknown()


def _registry(evaluated):
    """Adapters expose fn(payload); old per-handler arguments remain internal."""
    from .programmatic import HANDLERS as value_handlers
    from .temporal import HANDLERS as temporal_handlers

    def adapt(handler):
        def run(payload):
            case = {key: payload[key] for key in ("task", "observation", "reference")}
            # Compatibility for frozen acceptance-check references, not an input
            # field that callers must assemble differently for this rubric.
            case["evaluation"] = {"checks": {
                cid: {"status": r["status"],
                      "score": int(r["verdict"]) if r["verdict"] is not None else None}
                for cid, r in evaluated.items()}}
            return _from_legacy(handler(case, payload["check"]["bindings"]))
        return run

    return {name: adapt(handler) for name, handler in {**value_handlers, **temporal_handlers}.items()}


def _payload(input_data, check):
    return {"task": copy.deepcopy(input_data.get("task", {})),
            "observation": copy.deepcopy(input_data.get("observation", {})),
            "reference": copy.deepcopy(input_data.get("reference", {})),
            "check": copy.deepcopy(check)}


def _condition(input_data, condition):
    if condition is None:
        return {"status": "scored", "verdict": True}
    from .programmatic import assertions
    spec = {"actual_path": condition["path"], "op": condition["op"],
            "expected": condition["expected"]}
    return _from_legacy(assertions(input_data, {"items": [spec], "aggregation": "all"}))


def _all(results, ignore_na=True):
    active = [r for r in results if not ignore_na or r["status"] != "not_applicable"]
    if not active:
        return {"status": "not_applicable", "verdict": None}
    if any(r["status"] == "scored" and r["verdict"] is False for r in active):
        return {"status": "scored", "verdict": False}
    if all(r["status"] == "scored" and r["verdict"] is True for r in active):
        return {"status": "scored", "verdict": True}
    return unknown()




def evaluate_metrics(input_data, plan, judge_fn=None, judge_responses=None,
                     programs=None, request_sink=None):
    """Evaluate a reviewed plan; all P/J/H evaluators use one payload shape.

    programs maps trusted registry IDs to callables fn(payload)->{status,verdict}.
    No generated Python, arbitrary import paths or shell commands are executed.
    judge_fn(request) returns the same minimal binary response. request_sink can
    be a callback or a list and is only used to export explicit model requests.
    """
    from .contracts import assert_plan_usable

    evaluated = {}
    registry = _registry(evaluated)
    if programs is not None:
        if not isinstance(programs, dict) or any(not isinstance(k, str) or not callable(v)
                                                  for k, v in programs.items()):
            raise ValueError("programs must map trusted registry IDs to callables")
        registry.update(programs)
    normalized = assert_plan_usable(input_data, plan, approved_codes=set(registry))
    responses = judge_responses or {}
    if not isinstance(responses, dict):
        raise ValueError("judge_responses must be a mapping of check IDs to responses")
    grouped = {}

    def run_program(payload):
        try:
            fn = registry.get(payload["check"]["code"])
            # Applicability is decided by the frozen `when` predicate. A blank
            # program rule must not silently erase an applicable requirement.
            return normalize_binary(fn(copy.deepcopy(payload))) if fn else unknown()
        except Exception:
            return unknown()

    def run_judge(payload):
        try:
            request = build_request(copy.deepcopy(payload))
            if request_sink is not None:
                if callable(request_sink):
                    request_sink(copy.deepcopy(request))
                else:
                    request_sink.append(copy.deepcopy(request))
            cid = payload["check"]["id"]
            if cid in responses:
                response = responses[cid]
            elif judge_fn is not None:
                response = judge_fn(request)
            else:
                return unknown()
            return validate_response(response)
        except Exception:
            return unknown()

    for check in normalized["checks"]:
        payload = _payload(input_data, check)
        condition = _condition(input_data, check["when"])
        if condition["status"] != "scored":
            result = unknown()
        elif condition["verdict"] is False:
            result = {"status": "not_applicable", "verdict": None}
        elif check["method"] == "P":
            result = run_program(payload)
        elif check["method"] == "J":
            result = run_judge(payload)
        else:
            program_result = run_program(payload)
            if program_result["status"] == "scored" and program_result["verdict"] is False:
                result = program_result
            else:
                judge_result = run_judge(payload)
                result = _all([program_result, judge_result], ignore_na=False)
        evaluated[check["id"]] = result
        grouped.setdefault(check["metric_id"], []).append(result)

    for item in normalized["unresolved"]:
        # Missing an evaluator/oracle is not the same as task non-applicability.
        grouped.setdefault(item["metric_id"], []).append(unknown())
    return {"task_id": normalized["task_id"],
            "metrics": {mid: _all(results) for mid, results in grouped.items()}}
