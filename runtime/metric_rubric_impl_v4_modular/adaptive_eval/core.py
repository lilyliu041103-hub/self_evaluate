"""Config-driven runner. No model API is called unless a caller supplies judge_fn."""
from __future__ import annotations
from .resources import resource as _resource, resource_files as _resource_files, PROJECT as _PROJECT

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def catalog():
    """Legacy runner view, derived from the same per-rubric source of truth."""
    from .library import load_library
    data = load_library()
    data["rubrics"] = [{**r, "binary_question": r["definition"], "aggregation": "all",
                        "decision_values": [True, False],
                        # Legacy output includes why/evidence and needs its own
                        # protocol template. New v4 Judge capabilities must not
                        # advertise nonexistent legacy files.
                        "prompt_file": f"legacy/prompts/{r['id']}.txt"
                        if r["judge_rules"] and (_resource('legacy/prompts') / (r["id"] + ".txt")).is_file()
                        else None}
                       for r in data["rubrics"]]
    return data


def implementation_fingerprint():
    paths = sorted((ROOT / "adaptive_eval").glob("*.py"))
    paths += _resource_files()
    paths += sorted((ROOT / "configs").glob("*.schema.json"))
    paths += sorted((_resource('prompts')).rglob("*.txt"))
    paths += sorted((_resource('legacy/prompts')).glob("*.txt"))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.resolve().relative_to(_PROJECT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def unknown(why, **extra):
    return {"status": "unverifiable", "score": None, "verdict": None, "verdict_label": None, "evidence": [],
            "why": why, "raw_value": None, **extra}


def not_applicable(why):
    return {"status": "not_applicable", "score": None, "verdict": None, "verdict_label": None, "evidence": [],
            "why": why, "raw_value": None}


def normalize_result(result):
    """A broken evaluator is never silently converted into an agent failure."""
    if not isinstance(result, dict):
        return unknown("Evaluator returned a non-object result.")
    result = copy.deepcopy(result)
    status = result.get("status")
    score = result.get("score")
    if status not in {"scored", "unverifiable", "not_applicable"}:
        return unknown("Evaluator returned an invalid status.")
    if status == "scored":
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or score not in (0, 1)):
            return unknown("Binary evaluator requires score=0 or 1; fractional scores are invalid.")
    elif score is not None:
        return unknown("An unscored result must have score=null.")
    if not isinstance(result.get("why"), str) or not result["why"].strip():
        return unknown("Evaluator omitted the explanation.")
    refs = result.get("evidence", [])
    if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
        return unknown("Evaluator evidence must be JSONPointer strings.")
    result.setdefault("evidence", [])
    result.setdefault("raw_value", None)
    result["verdict"] = bool(score) if status == "scored" else None
    result["verdict_label"] = ("是" if score == 1 else "否") if status == "scored" else None
    return result


def _catalog_maps():
    data = catalog()
    return ({m["id"]: m for m in data["metrics"]},
            {r["id"]: r for r in data["rubrics"]})


def validate_config(config):
    metrics, rubrics = _catalog_maps()
    if not isinstance(config.get("checks"), list):
        raise ValueError("config.checks must be a list")
    seen = set()
    for spec in config["checks"]:
        cid = spec.get("id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError(f"Missing or duplicate check id: {cid!r}")
        seen.add(cid)
        if spec.get("rubric_id") not in rubrics:
            raise ValueError(f"Unknown rubric for {cid}")
        method = spec.get("method")
        if method not in {"P", "J", "H"}:
            raise ValueError(f"{cid}: method must be P, J or H")
        for field in ("applicable", "hard", "required_for_success"):
            if field in spec and not isinstance(spec[field], bool):
                raise ValueError(f"{cid}: {field} must be boolean")
        if spec.get("applicable", True) is False and not spec.get("applicability_reason"):
            raise ValueError(f"{cid}: an N/A configuration requires a reason")
        if method == "P" and not isinstance(spec.get("handler"), str):
            raise ValueError(f"{cid}: a P check requires a handler")
        if method == "H" and not all(isinstance(spec.get(k), dict)
                                     for k in ("programmatic", "judge")):
            raise ValueError(f"{cid}: H requires programmatic and judge components")
        program = spec if method == "P" else spec.get("programmatic", {}) if method == "H" else {}
        if program.get("handler") == "action_grounding":
            from .temporal import validate_grounding_params
            validate_grounding_params(program.get("params", {}), method)
        if "weight" in spec:
            raise ValueError(f"{cid}: binary all-conditions evaluation does not use weights")
    return metrics, rubrics


def combine_hybrid(programmatic, judged):
    """Conjunction: a known failure dominates; unknown is not discarded."""
    components = {"programmatic": programmatic, "judge": judged}
    vals = list(components.values())
    evidence = list(dict.fromkeys(p for r in vals for p in r.get("evidence", [])))
    failures = [r for r in vals if r["status"] == "scored" and r["score"] == 0]
    if failures:
        return {"status": "scored", "score": 0.0, "evidence": evidence,
                "why": "Hybrid conjunction failed: " + " | ".join(r["why"] for r in failures),
                "raw_value": None, "components": components}
    if any(r["status"] != "scored" for r in vals):
        return unknown("A required hybrid component could not be scored.",
                       evidence=evidence, components=components,
                       known_failure=any(known_failure(r) for r in vals))
    return {"status": "scored", "score": min(r["score"] for r in vals),
            "evidence": evidence, "why": " | ".join(r["why"] for r in vals),
            "raw_value": None, "components": components}


def summarize(values, aggregation="all"):
    """Binary conjunction; ratios are explicitly descriptive statistics only."""
    active = [r for r in values if r["status"] != "not_applicable"]
    known = [r for r in active if r["status"] == "scored"]
    if aggregation != "all":
        raise ValueError("Binary reports require aggregation='all'")
    if not active:
        return {"status": "not_applicable", "score": None, "verdict": None,
                "verdict_label": None, "coverage": None, "applicable_count": 0,
                "scored_count": 0, "statistics": {"passed": 0, "failed": 0,
                "unknown": 0, "observed_pass_rate": None}}
    failed = any(known_failure(r) for r in active)
    complete = len(active) == len(known)
    score = 0 if failed else (1 if complete else None)
    passed_count = sum(r["score"] == 1 for r in known)
    return {"status": "scored" if score is not None else "unverifiable",
            "score": score, "verdict": bool(score) if score is not None else None,
            "verdict_label": ("是" if score == 1 else "否") if score is not None else None,
            "coverage": len(known) / len(active), "applicable_count": len(active),
            "scored_count": len(known), "statistics": {
                "passed": passed_count, "failed": sum(r["score"] == 0 for r in known),
                "unknown": len(active) - len(known),
                "observed_pass_rate": passed_count / len(known) if known else None}}


def known_failure(result):
    if result.get("known_failure") is True:
        return True
    if result.get("status") == "scored" and result.get("score", 1) < 1:
        return True
    return any(known_failure(r) for r in result.get("components", {}).values())


def gate(results, flag):
    relevant = [r for r in results if r.get(flag) and r["status"] != "not_applicable"]
    failed = [r["check_id"] for r in relevant if known_failure(r)]
    pending = [r["check_id"] for r in relevant if r["status"] != "scored"]
    if failed:
        state, value = "fail", False
    elif pending:
        state, value = "unverifiable", None
    elif relevant:
        state, value = "pass", True
    else:
        state, value = "not_applicable", None
    return {"status": state, "value": value, "failed_checks": failed,
            "unverifiable_checks": pending, "applicable_count": len(relevant)}


def evaluate(case, config, judge_responses=None, judge_fn: Callable | None = None):
    """judge_fn(request)->JSON/dict, or replay a mapping of check_id to responses.

    No credentials, network requests, or judgments are implicitly created.
    Requests needing a model are returned in judge_requests even in offline mode.
    """
    from .programmatic import HANDLERS as value_handlers
    from .temporal import HANDLERS as temporal_handlers
    from .judge import build_request, validate_response

    metrics, rubrics = validate_config(config)
    handlers = {**value_handlers, **temporal_handlers}
    response_map = judge_responses or {}
    working = copy.deepcopy(case)
    working["evaluation"] = {"checks": {}}
    requests, results = [], []

    def run_programmatic(component):
        handler = handlers.get(component.get("handler"))
        if handler is None:
            return unknown(f"Unknown programmatic handler: {component.get('handler')}")
        try:
            return normalize_result(handler(working, component.get("params", {})))
        except Exception as exc:
            return unknown(f"Evaluator error ({type(exc).__name__}): {exc}")

    def run_judge(spec, component=None):
        j_spec = dict(spec)
        if component is not None:
            j_spec["params"] = component.get("params", {})
        try:
            request = build_request(working, j_spec)
            requests.append(request)
            if spec["id"] in response_map:
                response = response_map[spec["id"]]
            elif judge_fn is not None:
                response = judge_fn(request)
            else:
                return unknown("Judge response pending; exported prompt requires an actual evaluator model.")
            return normalize_result(validate_response(response, working, j_spec))
        except Exception as exc:
            return unknown(f"Judge configuration/response error ({type(exc).__name__}): {exc}")

    for spec in config["checks"]:
        method = spec["method"]
        if not spec.get("applicable", True):
            result = not_applicable(spec["applicability_reason"])
        elif method == "P":
            result = run_programmatic(spec)
        elif method == "J":
            result = run_judge(spec)
        else:
            p_result = run_programmatic(spec["programmatic"])
            # Code failure is decisive; do not spend a Judge call to overwrite it.
            if p_result["status"] == "scored" and p_result["score"] == 0:
                j_result = unknown("Judge skipped: deterministic failure is decisive.")
            else:
                j_result = run_judge(spec, spec["judge"])
            result = combine_hybrid(p_result, j_result)
        result = normalize_result(result)
        result.update({"check_id": spec["id"], "rubric_id": spec["rubric_id"],
                       "metric_id": rubrics[spec["rubric_id"]]["metric_id"],
                       "method": method,
                       "hard": spec.get("hard", False),
                       "required_for_success": spec.get("required_for_success", False)})
        results.append(result)
        working["evaluation"]["checks"][spec["id"]] = result

    rubric_reports = {}
    for rid, rdef in rubrics.items():
        matching = [r for r in results if r["rubric_id"] == rid]
        if matching:
            aggregation = rdef.get("aggregation", "all")
            rubric_reports[rid] = {"rubric_id": rid, "metric_id": rdef["metric_id"],
                                   **summarize(matching, aggregation)}
    metric_reports = {}
    for mid, mdef in metrics.items():
        matching = [r for r in rubric_reports.values() if r["metric_id"] == mid]
        if matching:
            metric_reports[mid] = {"metric_id": mid, "name": mdef.get("name_zh", mdef.get("name", mid)),
                                   **summarize(matching, mdef.get("aggregation", "all")),
                                   "rubrics": [r["rubric_id"] for r in matching]}
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                           allow_nan=False).encode("utf-8")).hexdigest()
    return {"schema_version": "2.0", "scoring_mode": "binary", "task_id": case.get("task", {}).get("id"),
            "reference_version": case.get("reference", {}).get("version"),
            "implementation_fingerprint": implementation_fingerprint(),
            "config_fingerprint": fingerprint, "checks": results,
            "rubrics": rubric_reports, "metrics": metric_reports,
            "task_success": gate(results, "required_for_success"),
            "hard_constraints": gate(results, "hard"), "judge_requests": requests,
            "notes": ["Check, rubric and metric verdicts are binary; N/A and unverifiable are separate statuses.",
                      "A known failure establishes No even with other unknowns; Yes requires all applicable conditions verified.",
                      "Pass rates, resource values and reliability estimates are descriptive statistics, not fractional scores."]}


def reliability(reports, k=1):
    """Finite-sample estimate C(successes,k)/C(known_runs,k), not a new judge."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")
    identities = {(r.get("task_id"), r.get("reference_version"), r.get("config_fingerprint"),
                   r.get("implementation_fingerprint"))
                  for r in reports}
    if len(identities) > 1:
        raise ValueError("Reliability pooling requires identical task/reference/config versions")
    output = {"runs": len(reports), "k": k,
              "assumption": "Comparable repeated runs; estimates describe this sampled population."}
    for field in ("task_success", "hard_constraints"):
        applicable = [r[field] for r in reports if r[field]["status"] != "not_applicable"]
        known = [g for g in applicable if g["value"] is not None]
        successes = sum(g["value"] is True for g in known)
        n = len(known)
        estimate = (math.comb(successes, k) / math.comb(n, k) if n >= k else None)
        complete = len(known) == len(applicable)
        output[field] = {"known_runs": n, "applicable_runs": len(applicable),
                         "coverage": n / len(applicable) if applicable else None,
                         "observed_rate": successes / n if n else None,
                         "observed_pass_k": estimate,
                         "rate": successes / n if n and complete else None,
                         "pass_k": estimate if complete else None}
    return output

