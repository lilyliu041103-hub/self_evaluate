"""Metric-only binary Judge protocol: no generated rationale or evidence list.

The model receives the same four-key payload used by every program evaluator:
task, observation, reference, check. Data is projected to explicit input_paths.
"""
from __future__ import annotations
from .resources import resource as _resource, resource_files as _resource_files, PROJECT as _PROJECT

import copy
import json
from pathlib import Path

from .judge import _pointer

ROOT = Path(__file__).resolve().parent.parent


def unknown():
    return {"status": "unverifiable", "verdict": None}


def normalize_binary(value, allow_na=False):
    """Validate a minimal result, rejecting old score/why/evidence protocols."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return unknown()
    if not isinstance(value, dict) or set(value) != {"status", "verdict"}:
        return unknown()
    status, verdict = value["status"], value["verdict"]
    if status == "scored" and isinstance(verdict, bool):
        return {"status": status, "verdict": verdict}
    if status == "unverifiable" and verdict is None:
        return unknown()
    if allow_na and status == "not_applicable" and verdict is None:
        return {"status": status, "verdict": None}
    return unknown()


def validate_response(response):
    """Judge cannot decide applicability; it returns only a boolean or unknown."""
    return normalize_binary(response)


def _put(target, source, parts, value):
    """Build an isolated JSON subtree while preserving selected array indices."""
    current = target
    original = source
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(original, dict):
            original_child = original.get(part)
        elif isinstance(original, list) and part.isdigit() and int(part) < len(original):
            original_child = original[int(part)]
        else:
            original_child = None
        if isinstance(current, list):
            if not part.isdigit() or str(int(part)) != part:
                raise ValueError("Invalid array index in selected input path")
            index = int(part)
            if index > 100000:
                raise ValueError("Selected array index is too large")
            while len(current) <= index:
                current.append(None)
            if last:
                current[index] = copy.deepcopy(value)
            else:
                if current[index] is None:
                    current[index] = [] if isinstance(original_child, list) else {}
                current = current[index]
        elif isinstance(current, dict):
            if last:
                current[part] = copy.deepcopy(value)
            else:
                if part not in current or current[part] is None:
                    current[part] = [] if isinstance(original_child, list) else {}
                current = current[part]
        else:
            raise ValueError("Selected input paths conflict structurally")
        original = original_child


def project_payload(payload):
    """Keep the public four-key shape and mark unavailable selected values."""
    if set(payload) != {"task", "observation", "reference", "check"}:
        raise ValueError("Evaluator input must use the uniform four-key payload")
    check = payload["check"]
    paths = check.get("input_paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("Judge requires explicit input_paths")
    projected = {"task": {}, "observation": {}, "reference": {},
                 "check": copy.deepcopy(check)}
    # Parents are projected first; a selected child cannot erase a selected parent.
    for path in sorted(set(paths), key=lambda p: (p.count("/"), p)):
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("input_paths must contain absolute JSON Pointers")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
        if parts[0] not in {"task", "observation", "reference"}:
            raise ValueError("Only task/observation/reference may be selected")
        try:
            value = _pointer(payload, path)
        except (KeyError, IndexError):
            value = {"$missing": True}
        _put(projected, payload, parts, value)
    return projected


def build_request(payload):
    """Compose the fixed protocol, rubric rules and frozen task instructions.

    The task-specific prompt is inline text in user.check.prompt, not a path
    to load or a replacement for the fixed binary evaluation protocol.
    """
    from .library import load_rubric
    from .contracts import _prompt_text
    projected = project_payload(payload)
    check = payload["check"]
    prompt = check.get("prompt")
    _prompt_text(prompt)
    rubric = load_rubric(check["rubric_id"])
    if check.get("metric_id") != rubric["metric_id"] or check.get("method") not in {"J", "H"}:
        raise ValueError("Judge check must identify its rubric and parent metric")
    if check["method"] not in rubric["allowed_methods"] or not rubric["judge_rules"]:
        raise ValueError("The rubric does not support this Judge method")
    system = (_resource('prompts/scoring/system.txt')).read_text(encoding="utf-8")
    specific = rubric["judge_rules"]
    return {"stage": "evaluate", "check_id": check["id"],
            "system": system + "\n\n" + specific,
            "user": json.dumps(projected, ensure_ascii=False, allow_nan=False)}
