"""Uniform task-specific check contract and deterministic plan validation.

These checks validate structure/provenance/coverage links. Semantic completeness
and atomicity require the independent review step, not keyword heuristics.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from pathlib import Path
from .library import load_library
from .programmatic import HANDLERS as VALUE_HANDLERS, pointer
from .temporal import HANDLERS as TEMPORAL_HANDLERS

ROOT = Path(__file__).resolve().parents[1]
CHECK_KEYS = {"id", "metric_id", "rubric_id", "method", "criterion",
              "requirement_ids", "code", "prompt", "bindings", "input_paths",
              "when", "hard", "required_for_success"}
BUILTIN_CODES = set(VALUE_HANDLERS) | set(TEMPORAL_HANDLERS)
GENERATION_KEYS = {"requirements", "assignments", "checks", "unresolved"}


class PlanError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def task_context(input_data):
    """Never include candidate observations in requirement generation."""
    task, reference = input_data.get("task"), input_data.get("reference", {})
    if not isinstance(task, dict) or not isinstance(reference, dict):
        raise PlanError("task/reference must be objects")
    for key in ("id", "request"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise PlanError(f"task.{key} must be a nonempty string")
    return copy.deepcopy({"task": task, "reference": reference})


def _nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{name} must be a nonempty string")


def _exact_keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PlanError(f"{name} requires exactly these fields: {sorted(keys)}")


def _path(path, roots=("task", "reference", "observation")):
    if not isinstance(path, str) or not any(path == "/" + root or path.startswith("/" + root + "/") for root in roots):
        raise PlanError(f"Invalid or disallowed input path: {path!r}")
    # Reject non-RFC6901 escapes; actual observation may not exist yet.
    import re
    if re.search(r"~(?![01])", path):
        raise PlanError("Invalid JSONPointer escape")


def _source(context, source):
    _exact_keys(source, {"path", "quote"}, "source_ref")
    _path(source["path"], ("task", "reference"))
    _nonempty(source["quote"], "source_ref.quote")
    try:
        value = pointer(context, source["path"])
    except ValueError as exc:
        raise PlanError("Source pointer does not exist") from exc
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if source["quote"] not in text:
        raise PlanError("Source quote must occur verbatim at the source path")


def _requirements(context, requirements):
    if not isinstance(requirements, list) or not requirements:
        raise PlanError("A nonempty task must declare its requirements")
    requirement_ids, texts = set(), set()
    for requirement in requirements:
        _exact_keys(requirement, {"id", "text", "source_refs"}, "requirement")
        _nonempty(requirement["id"], "requirement.id")
        _nonempty(requirement["text"], "requirement.text")
        normalized_text = "".join(requirement["text"].split()).casefold()
        if requirement["id"] in requirement_ids or normalized_text in texts:
            raise PlanError("Duplicate requirement id or identical requirement text")
        requirement_ids.add(requirement["id"])
        texts.add(normalized_text)
        if not isinstance(requirement["source_refs"], list) or not requirement["source_refs"]:
            raise PlanError("Every requirement needs a task/reference source")
        for source in requirement["source_refs"]:
            _source(context, source)
    return requirement_ids


def _assignments(assignments, requirement_ids, rubrics):
    if not isinstance(assignments, list):
        raise PlanError("assignments must be a list")
    assigned = {}
    for item in assignments:
        _exact_keys(item, {"requirement_id", "metric_id", "rubric_id"}, "assignment")
        for key in item:
            _nonempty(item[key], "assignment." + key)
        requirement_id, rubric_id = item["requirement_id"], item["rubric_id"]
        if requirement_id not in requirement_ids or rubric_id not in rubrics:
            raise PlanError("Assignment must use an existing requirement and rubric")
        if item["metric_id"] != rubrics[rubric_id]["metric_id"]:
            raise PlanError("Assignment metric does not own its rubric")
        if requirement_id in assigned:
            raise PlanError("A requirement must have exactly one primary rubric assignment")
        assigned[requirement_id] = item
    return assigned


def _unresolved(items, requirement_ids, metrics, assigned):
    if not isinstance(items, list):
        raise PlanError("unresolved must be a list")
    unresolved_ids = set()
    for item in items:
        _exact_keys(item, {"requirement_id", "metric_id", "missing"}, "unresolved item")
        _nonempty(item["requirement_id"], "unresolved.requirement_id")
        _nonempty(item["metric_id"], "unresolved.metric_id")
        requirement_id = item["requirement_id"]
        if requirement_id not in requirement_ids or item["metric_id"] not in metrics:
            raise PlanError("Unresolved item must identify an existing requirement and metric")
        if requirement_id in unresolved_ids:
            raise PlanError("Combine missing prerequisites in one unresolved entry per requirement")
        if requirement_id in assigned and item["metric_id"] != assigned[requirement_id]["metric_id"]:
            raise PlanError("Unresolved item metric differs from the primary assignment")
        if not isinstance(item["missing"], list) or not item["missing"]:
            raise PlanError("Unresolved requirements must identify missing configuration")
        for missing in item["missing"]:
            _nonempty(missing, "unresolved.missing")
        unresolved_ids.add(requirement_id)
    return unresolved_ids


def validate_routing(input_data, routing):
    """Check one primary owner per extracted requirement, before specialization."""
    _exact_keys(routing, {"requirements", "assignments", "unresolved"}, "routing")
    library = load_library()
    rubrics = {r["id"]: r for r in library["rubrics"]}
    metrics = {m["id"] for m in library["metrics"]}
    requirement_ids = _requirements(task_context(input_data), routing["requirements"])
    assigned = _assignments(routing["assignments"], requirement_ids, rubrics)
    unresolved_ids = _unresolved(routing["unresolved"], requirement_ids, metrics, assigned)
    if set(assigned) & unresolved_ids:
        raise PlanError("Routing must either assign a requirement or mark it unresolved")
    if requirement_ids != set(assigned) | unresolved_ids:
        raise PlanError("Every requirement must be assigned once or explicitly unresolved")
    return copy.deepcopy(routing)


def _prompt_text(value):
    _nonempty(value, "check.prompt")
    text = value.strip()
    # v4 accepts instructions, never a filename to load as executable policy.
    if (text.startswith(("/", "./", "../", ".\\", "..\\", "prompts/", "prompts\\", "~"))
        or re.match(r"^[A-Za-z]:[/\\]", text)
        or (not re.search(r"\s", text) and text.lower().endswith(
            (".txt", ".md", ".json", ".yaml", ".yml", ".py")))):
        raise PlanError("check.prompt must contain task-specific scoring instructions, not a file path")


def validate_plan(input_data, candidate, approved_codes=None):
    context = task_context(input_data)
    if not isinstance(candidate, dict):
        raise PlanError("Plan must be an object")
    for key in GENERATION_KEYS:
        if not isinstance(candidate.get(key), list):
            raise PlanError(f"Plan.{key} must be a list")
    library = load_library()
    rubrics = {r["id"]: r for r in library["rubrics"]}
    metrics = {m["id"] for m in library["metrics"]}
    codes = BUILTIN_CODES | set(approved_codes or ())
    requirement_ids = _requirements(context, candidate["requirements"])
    assigned = _assignments(candidate["assignments"], requirement_ids, rubrics)
    check_ids, covered, propositions = set(), set(), set()
    for check in candidate["checks"]:
        _exact_keys(check, CHECK_KEYS, "check")
        _nonempty(check["id"], "check.id")
        _nonempty(check["criterion"], "check.criterion")
        _nonempty(check["metric_id"], "check.metric_id")
        _nonempty(check["rubric_id"], "check.rubric_id")
        if check["id"] in check_ids:
            raise PlanError("Duplicate check id")
        check_ids.add(check["id"])
        rid = check["rubric_id"]
        if rid not in rubrics or check["metric_id"] != rubrics[rid]["metric_id"]:
            raise PlanError("Unknown rubric or incorrect metric ownership")
        refs = check["requirement_ids"]
        if (not isinstance(refs, list) or len(refs) != 1
                or not isinstance(refs[0], str) or refs[0] not in requirement_ids):
            raise PlanError("Each atomic check must reference exactly one declared requirement")
        assignment = assigned.get(refs[0])
        if assignment is None or assignment["rubric_id"] != rid or assignment["metric_id"] != check["metric_id"]:
            raise PlanError("Check must preserve the requirement's primary rubric assignment")
        covered.update(refs)
        if not isinstance(check["hard"], bool) or not isinstance(check["required_for_success"], bool):
            raise PlanError("hard/required_for_success must be booleans")
        if not isinstance(check["bindings"], dict):
            raise PlanError("bindings must always be an object")
        paths = check["input_paths"]
        if not isinstance(paths, list) or not paths:
            raise PlanError("input_paths must be a nonempty unique list")
        for path in paths:
            _path(path)
        if len(set(paths)) != len(paths):
            raise PlanError("input_paths must be unique")
        method = check["method"]
        if method not in rubrics[rid]["allowed_methods"]:
            raise PlanError("method must be permitted by the rubric configuration")
        if method in {"P", "H"}:
            if not isinstance(check["code"], str) or check["code"] not in codes:
                raise PlanError("code must identify a registered evaluator, not generated executable text")
            if check["code"] in BUILTIN_CODES and check["code"] != rubrics[rid]["programmatic_handler"]:
                raise PlanError("Built-in evaluator does not match the rubric template")
            if not check["bindings"]:
                raise PlanError("Programmatic instances require explicit task-specific bindings")
            if check["code"] == "action_grounding":
                from .temporal import validate_grounding_params
                try:
                    validate_grounding_params(check["bindings"], method)
                except ValueError as exc:
                    raise PlanError(str(exc)) from exc
        elif check["code"] is not None or check["bindings"]:
            raise PlanError("J-only instances use code=null and bindings={}")
        if method in {"J", "H"}:
            _prompt_text(check["prompt"])
        elif check["prompt"] is not None:
            raise PlanError("P-only instances use prompt=null")
        when = check["when"]
        if when is not None:
            _exact_keys(when, {"path", "op", "expected"}, "when")
            _path(when["path"])
            if when["op"] not in {"eq", "ne", "in", "gt", "gte", "lt", "lte", "contains", "exists"}:
                raise PlanError("Unsupported applicability predicate")
            if when["path"].startswith("/observation"):
                trusted = context["reference"].get("trusted_observation_paths", [])
                if not any(when["path"] == p or when["path"].startswith(p + "/") for p in trusted):
                    raise PlanError("Applicability cannot depend on untrusted agent self-reports")
        proposition = ("".join(check["criterion"].split()).casefold(), digest(when))
        if proposition in propositions:
            raise PlanError("Same proposition appears more than once; reuse its result")
        propositions.add(proposition)
    unresolved_ids = _unresolved(candidate["unresolved"], requirement_ids, metrics, assigned)
    if requirement_ids - covered - unresolved_ids:
        raise PlanError("Every extracted requirement needs a check or explicit unresolved entry")
    # These fields are the only part accepted from the generation model.
    body = copy.deepcopy({k: candidate[k] for k in ("requirements", "assignments", "checks", "unresolved")})
    from .core import implementation_fingerprint
    return {"schema_version": "4.0", "task_id": context["task"]["id"],
            "library_fingerprint": implementation_fingerprint(),
            "task_fingerprint": digest(context), "plan_hash": digest(body), **body}


def assert_plan_usable(input_data, plan, approved_codes=None):
    normalized = validate_plan(input_data, plan, approved_codes)
    if any(plan.get(k) != normalized[k] for k in ("schema_version", "task_id", "task_fingerprint", "plan_hash", "library_fingerprint")):
        raise PlanError("Task/reference or plan changed; instantiate and review again")
    review = plan.get("review", {})
    if review.get("accepted") is not True or review.get("plan_hash") != normalized["plan_hash"]:
        raise PlanError("The exact plan must pass the independent instantiation review before evaluation")
    return normalized

