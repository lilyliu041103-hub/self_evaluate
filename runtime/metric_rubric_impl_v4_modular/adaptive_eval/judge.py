"""Provider-independent Judge requests and strict response validation.

No network requests are performed. The caller submits ``system`` and ``user``
to its selected model and passes the returned JSON to ``validate_response``.
"""
from __future__ import annotations
from .resources import resource as _resource, resource_files as _resource_files, PROJECT as _PROJECT

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _unknown(message: str) -> dict:
    return {"status": "unverifiable", "score": None, "verdict": None, "why": message,
            "evidence": [], "raw_value": None}


def _pointer(data: Any, path: str) -> Any:
    """Resolve an RFC 6901 pointer; this API requires a non-root pointer."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("input/evidence paths must be absolute JSON Pointers")
    current = data
    for segment in path[1:].split("/"):
        # Reject invalid ~ escapes instead of silently treating them as names.
        i = 0
        while i < len(segment):
            if segment[i] == "~":
                if i + 1 >= len(segment) or segment[i + 1] not in "01":
                    raise ValueError("invalid JSON Pointer escape")
                i += 1
            i += 1
        part = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not part.isdigit() or str(int(part)) != part:
                raise KeyError(path)
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(path)
    return current


def _params(spec: dict) -> dict:
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return params


def _input_paths(spec: dict) -> list[str]:
    paths = _params(spec).get("input_paths", [])
    if not isinstance(paths, list) or not paths or any(
        not isinstance(p, str) or not p.startswith("/") for p in paths
    ):
        raise ValueError("Judge requires explicit non-empty params.input_paths")
    return list(dict.fromkeys(paths))


def _rubric(spec: dict) -> dict:
    from .core import catalog as load_catalog
    catalog = load_catalog()
    rubric_id = spec.get("rubric_id")
    matches = [r for r in catalog["rubrics"] if r["id"] == rubric_id]
    if len(matches) != 1:
        raise ValueError(f"unknown rubric_id: {rubric_id}")
    return matches[0]


def build_request(case: dict, spec: dict) -> dict:
    """Build an isolated request from an instantiated rubric.

    Example spec::

        {"id": "logic-1", "rubric_id": "AC-2", "params": {
            "criterion": "结论是否从给定前提成立，忽略未展示的内部思维。",
            "input_paths": ["/task/request", "/observation/final_answer"],
            "anchors": {"no": "结论存在明确的逻辑错误",
                        "yes": "当前命题的可观察论证成立"}}}

    The default rubric-specific anchors are used when ``anchors`` is omitted.
    A missing input is explicitly recorded; it is never replaced with a guess.
    Returned keys are check_id, system, user. Configuration errors raise
    ValueError; callers should report a configuration/unverifiable result.
    """
    if spec.get("applicable") is False:
        raise ValueError("Do not submit not-applicable checks to the Judge")
    params = _params(spec)
    rubric = _rubric(spec)
    if not rubric.get("prompt_file"):
        raise ValueError(f"{rubric['id']} has no semantic Judge template")
    criterion = params.get("criterion")
    if not isinstance(criterion, str) or not criterion.strip():
        raise ValueError("Judge requires a task-instantiated params.criterion")
    paths = _input_paths(spec)
    inputs = {}
    for path in paths:
        try:
            inputs[path] = {"present": True, "value": _pointer(case, path)}
        except (KeyError, IndexError, ValueError):
            inputs[path] = {"present": False, "value": None}
    anchors = params.get("anchors")
    if anchors is not None and (not isinstance(anchors, dict) or
                               set(anchors) != {"yes", "no"} or
                               any(not isinstance(v, str) or not v.strip() for v in anchors.values())):
        raise ValueError("anchors must define non-empty text for yes and no only")
    system = (_resource('legacy/prompts/system.txt')).read_text(encoding="utf-8")
    specific = (_resource(rubric["prompt_file"])).read_text(encoding="utf-8")
    payload = {"check_id": spec["id"], "rubric_id": rubric["id"],
               "criterion": criterion, "task_specific_anchors": anchors,
               "allowed_input_paths": paths, "inputs": inputs}
    return {"check_id": spec["id"], "system": system + "\n\n" + specific,
            "user": json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)}


def validate_response(response: str | dict, case: dict, spec: dict) -> dict:
    """Validate binary model output and evidence references.

    Scored answers require a JSON boolean verdict, nonempty why, and at least one
    existing evidence pointer within an explicitly provided input subtree.
    ``not_applicable`` is decided by configuration, never by the model.
    Invalid output returns unverifiable; it never defaults to a passing score.
    """
    if spec.get("applicable") is False:
        return {"status": "not_applicable", "score": None, "verdict": None,
                "why": "任务配置显式声明该检查不适用。", "evidence": [], "raw_value": None}
    try:
        paths = _input_paths(spec)
        if isinstance(response, str):
            response = json.loads(response)
        if not isinstance(response, dict):
            raise ValueError("response must be a JSON object")
        if set(response) - {"status", "verdict", "why", "evidence"}:
            raise ValueError("response contains unsupported fields")
        if not {"status", "verdict", "why", "evidence"}.issubset(response):
            raise ValueError("response is missing required fields")
        status, verdict = response["status"], response["verdict"]
        if status not in {"scored", "unverifiable"}:
            raise ValueError("Judge cannot decide applicability")
        why = response["why"]
        if not isinstance(why, str) or not why.strip():
            raise ValueError("why must be a nonempty string")
        if status == "scored":
            if not isinstance(verdict, bool):
                raise ValueError("scored response requires a JSON boolean verdict, not a number or string")
        elif verdict is not None:
            raise ValueError("unverifiable verdict must be null")
        evidence = response["evidence"]
        if not isinstance(evidence, list) or any(not isinstance(p, str) for p in evidence):
            raise ValueError("evidence must be a list of JSON Pointers")
        if status == "scored" and not evidence:
            raise ValueError("scored response requires evidence")
        for pointer in evidence:
            if not any(pointer == allowed or pointer.startswith(allowed + "/") for allowed in paths):
                raise ValueError("evidence points outside provided inputs")
            _pointer(case, pointer)
        return {"status": status, "score": int(verdict) if verdict is not None else None,
                "verdict": verdict,
                "why": why.strip(), "evidence": list(dict.fromkeys(evidence)), "raw_value": None}
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _unknown(f"Judge 输出无效或配置缺失：{exc}")
