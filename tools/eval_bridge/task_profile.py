# -*- coding: utf-8 -*-
"""Task-level capability profile -> rubric applicability.

Applicability is decided from what the task asks for, never from "the run did
not happen to produce this event".  A requirement marked ``required`` keeps the
rubric applicable even when no evidence exists, so a missing capability shows up
as a coverage gap instead of silently disappearing.
"""

import hashlib
import json
import os

PROFILE_SCHEMA = "agentos-task-profile/1"
LEVELS = ("required", "optional", "not_required", "unspecified")
REQUIREMENTS = ("tool_use", "human_approval", "error_recovery", "budget", "planning",
                "knowledge_retrieval")

GATES = {
    "AC-1": None, "AC-2": None, "AC-3": None, "TC-1": None, "EE-4": None,
    "EG-1": None, "EG-2": None, "EG-3": None,
    "TU-1": "tool_use", "TU-2": "tool_use", "TU-3": "tool_use",
    "PE-1": "planning", "PE-2": "planning", "PE-3": "planning",
    "ER-1": "error_recovery", "ER-2": "error_recovery", "ER-3": "error_recovery",
    "HC-1": "human_approval", "HC-3": "human_approval", "HC-4": "human_approval",
    "HC-2": None,
    "BC-1": "budget",
}

# Which rubric families prove a capability actually appeared in a run.
EVIDENCE_OF = {
    "tool_use": ("TU-1", "TU-2", "TU-3"),
    "planning": ("PE-1", "PE-2", "PE-3"),
    "error_recovery": ("ER-1", "ER-2", "ER-3"),
    "human_approval": ("HC-1", "HC-2", "HC-3", "HC-4"),
    "budget": ("BC-1",),
}


class ProfileError(ValueError):
    pass


def validate(profile):
    if not isinstance(profile, dict):
        raise ProfileError("task profile must be an object")
    if profile.get("schema") != PROFILE_SCHEMA:
        raise ProfileError("unsupported schema: %r" % (profile.get("schema"),))
    if not isinstance(profile.get("task_id"), str) or not profile["task_id"]:
        raise ProfileError("task_id is required")
    reqs = profile.get("requirements")
    if not isinstance(reqs, dict):
        raise ProfileError("requirements must be an object")
    unknown = set(reqs) - set(REQUIREMENTS)
    if unknown:
        raise ProfileError("unknown requirement keys: " + ", ".join(sorted(unknown)))
    for key, item in reqs.items():
        if not isinstance(item, dict):
            raise ProfileError("requirement %s must be an object" % key)
        level = item.get("level")
        if level not in LEVELS:
            raise ProfileError("requirement %s has invalid level: %r" % (key, level))
        if level != "unspecified":
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise ProfileError("requirement %s needs a non-empty reason" % key)
            basis = item.get("basis")
            if not isinstance(basis, list) or not basis or not all(isinstance(b, str) and b for b in basis):
                raise ProfileError("requirement %s needs a non-empty basis list" % key)
    return profile


def load(path):
    with open(path, encoding="utf-8-sig") as handle:
        profile = json.load(handle)
    return validate(profile)


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def decide(rid, profile, observed):
    gate = GATES.get(rid, None) if rid in GATES else None
    if rid not in GATES:
        return {"status": "applicable", "reason": "rubric 不在能力门控表内，按交付物适用",
                "gate": None, "level": None}
    if gate is None:
        return {"status": "applicable", "reason": "该 rubric 只要求有交付物，与特定能力要求无关",
                "gate": None, "level": None}
    item = (profile.get("requirements") or {}).get(gate) or {}
    level = item.get("level", "unspecified")
    seen = gate in observed
    if level in ("required", "optional"):
        reason = ((item.get("reason") or "任务要求此项能力") +
                  ("（required，缺证据按覆盖缺口处理）" if level == "required" else "（optional，出现即评）"))
        return {"status": "applicable", "gate": gate, "level": level, "observed": seen,
                "reason": reason, "basis": item.get("basis", [])}
    if level == "not_required":
        entry = {"status": "not_applicable", "gate": gate, "level": level, "observed": seen,
                 "reason": (item.get("reason") or "任务不要求此项能力"),
                 "basis": item.get("basis", [])}
        if seen:
            entry["note"] = "画像判定不要求，但本轮确实实例化了该类检查；按画像排除，仅留痕"
        return entry
    return {"status": "undetermined", "gate": gate, "level": level, "observed": seen,
            "reason": "任务画像未声明 " + gate, "basis": []}


def rubric_applicability(profile, rubric_ids=None, observed=()):
    validate(profile)
    observed = set(observed or ())
    ids = list(rubric_ids) if rubric_ids else list(GATES)
    return {rid: decide(rid, profile, observed) for rid in ids}


def observed_gates_from_checks(checks):
    """A capability was exercised if any check in its family was instantiated."""
    rubrics = {c.get("rubric_id") for c in checks or ()}
    return {gate for gate, rids in EVIDENCE_OF.items() if rubrics & set(rids)}


def observed_gates_from_report(report):
    rub = report.get("rubrics") or {}
    return {gate for gate, rids in EVIDENCE_OF.items()
            if any((rub.get(rid) or {}).get("verdict") is not None for rid in rids)}


def aggregate(verdicts):
    if any(v is False for v in verdicts):
        return False
    if verdicts and all(v is True for v in verdicts):
        return True
    return None


def summarize(applicability):
    counts = {}
    for entry in (applicability or {}).values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts
