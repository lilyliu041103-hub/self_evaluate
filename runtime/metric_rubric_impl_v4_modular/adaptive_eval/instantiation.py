"""Route requirements once, specialize each rubric, review and freeze the plan."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .contracts import (GENERATION_KEYS, PlanError, task_context,
                        validate_plan, validate_routing)
from .library import load_library, load_rubric

ROOT = Path(__file__).resolve().parents[1]


def _decode(response):
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError as exc:
            raise PlanError("Model response must be valid JSON") from exc
    if not isinstance(response, dict):
        raise PlanError("Model response must be a JSON object")
    return response


def _schema():
    return json.loads((ROOT / "configs/instance.schema.json").read_text(encoding="utf-8"))


def _request(stage, context, system, content, **extra):
    return {"stage": stage, "task_id": context["task"]["id"], **extra,
            "system": system,
            "user": json.dumps(content, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}


def _subschema(*keys):
    schema = _schema()
    return {**schema, "required": list(keys),
            "properties": {key: schema["properties"][key] for key in keys}}


def _rubric_index(library):
    """All ownership boundaries, without generation prompts or evaluator details."""
    return [{key: rubric[key] for key in
             ("id", "metric_id", "name", "name_zh", "definition", "boundary", "inputs")}
            for rubric in library["rubrics"]]


def build_routing_request(input_data):
    """Read task/reference only; ask once for requirements and unique owners."""
    context = task_context(input_data)
    library = load_library()
    index = _rubric_index(library)
    return _request("route", context,
                    (ROOT / "prompts/routing.txt").read_text(encoding="utf-8"),
                    {**context, "metrics": library["metrics"], "rubric_index": index,
                     "output_schema": _subschema("requirements", "assignments", "unresolved")})


def build_instantiation_request(input_data):
    """Compatibility name: v4's first generation stage is routing."""
    return build_routing_request(input_data)


def _validate_schema(response, schema):
    import jsonschema
    try:
        jsonschema.Draft202012Validator(schema).validate(response)
    except jsonschema.ValidationError as exc:
        raise PlanError("Generated data does not match the uniform schema: " + exc.message) from exc


def accept_routing(input_data, response):
    response = _decode(response)
    _validate_schema(response, _subschema("requirements", "assignments", "unresolved"))
    return validate_routing(input_data, response)


def build_rubric_request(input_data, routing, rubric_id):
    """Pass one configured rubric and only the requirements assigned to it."""
    routing = accept_routing(input_data, routing)
    context = task_context(input_data)
    rubric = load_rubric(rubric_id)
    assignments = [a for a in routing["assignments"] if a["rubric_id"] == rubric_id]
    if not assignments:
        raise PlanError("Cannot instantiate a rubric with no assigned requirements")
    ids = {a["requirement_id"] for a in assignments}
    requirements = [r for r in routing["requirements"] if r["id"] in ids]
    system = ((ROOT / "prompts/instantiation.txt").read_text(encoding="utf-8")
              + "\n\n当前 rubric 的实例化规则：\n" + rubric["instantiate_prompt"])
    # The local instruction already appears in system; do not send it twice.
    rubric_context = {key: value for key, value in rubric.items() if key != "instantiate_prompt"}
    return _request("instantiate", context, system,
                    {**context, "rubric": rubric_context, "requirements": requirements,
                     "assignments": assignments,
                     "output_schema": _subschema("checks", "unresolved")}, rubric_id=rubric_id)


def accept_rubric_generation(input_data, routing, rubric_id, response):
    """A local generator cannot rewrite requirements or take another rubric's work."""
    routing = accept_routing(input_data, routing)
    response = _decode(response)
    _validate_schema(response, _subschema("checks", "unresolved"))
    assignments = [a for a in routing["assignments"] if a["rubric_id"] == rubric_id]
    if not assignments:
        raise PlanError("No requirements are assigned to this rubric")
    ids = {a["requirement_id"] for a in assignments}
    local = {"requirements": [r for r in routing["requirements"] if r["id"] in ids],
             "assignments": assignments, **response}
    validate_plan(input_data, local)
    return copy.deepcopy(response)


def assemble_generation(input_data, routing, responses_by_rubric):
    """Combine all local responses; cross-rubric duplicate IDs/propositions fail."""
    routing = accept_routing(input_data, routing)
    expected = {a["rubric_id"] for a in routing["assignments"]}
    if not isinstance(responses_by_rubric, dict) or set(responses_by_rubric) != expected:
        raise PlanError("Provide exactly one response for every assigned rubric")
    candidate = {"requirements": routing["requirements"], "assignments": routing["assignments"],
                 "checks": [], "unresolved": copy.deepcopy(routing["unresolved"])}
    for rubric_id in sorted(expected):
        local = accept_rubric_generation(input_data, routing, rubric_id, responses_by_rubric[rubric_id])
        candidate["checks"].extend(local["checks"])
        candidate["unresolved"].extend(local["unresolved"])
    return accept_generation(input_data, candidate)


def accept_generation(input_data, response):
    response = _decode(response)
    if set(response) != GENERATION_KEYS:
        raise PlanError("Generator must return requirements/assignments/checks/unresolved only")
    _validate_schema(response, _schema())
    return validate_plan(input_data, response)


def build_review_request(input_data, candidate):
    plan = validate_plan(input_data, candidate)
    context = task_context(input_data)
    library = load_library()
    selected_ids = {item["rubric_id"] for item in plan["assignments"]}
    # Keep the full index for auditing omissions/misrouting, but only include
    # execution details for rubrics the candidate actually uses. The reviewer
    # evaluates the resulting plan, not the instructions used to generate it.
    selected = [{key: value for key, value in rubric.items() if key != "instantiate_prompt"}
                for rubric in library["rubrics"] if rubric["id"] in selected_ids]
    return _request("review", context,
                    (ROOT / "prompts/instantiation_review.txt").read_text(encoding="utf-8"),
                    {**context, "metrics": library["metrics"],
                     "rubric_index": _rubric_index(library), "rubric_library": selected, "plan": plan})


def accept_review(input_data, candidate, response, provenance="provided_responses"):
    plan = validate_plan(input_data, candidate)
    review = _decode(response)
    if (set(review) != {"accepted", "issues"} or not isinstance(review["accepted"], bool)
            or not isinstance(review["issues"], list)):
        raise PlanError("Review response requires accepted:boolean and issues:list")
    for issue in review["issues"]:
        if not isinstance(issue, dict) or set(issue) != {"code", "requirement_id", "check_id", "message"}:
            raise PlanError("Review issues must follow the documented schema")
        if not isinstance(issue["code"], str) or not isinstance(issue["message"], str):
            raise PlanError("Review issue code/message must be strings")
        if any(issue[k] is not None and not isinstance(issue[k], str) for k in ("requirement_id", "check_id")):
            raise PlanError("Review issue IDs must be strings or null")
    if not review["accepted"] or review["issues"]:
        raise PlanError("Instantiation review did not approve this plan: " + json.dumps(review["issues"], ensure_ascii=False))
    plan["review"] = {"accepted": True, "plan_hash": plan["plan_hash"], "provenance": provenance}
    return plan


def instantiate(input_data, generator_fn, reviewer_fn=None, request_sink=None):
    """Call route -> one instantiate per selected rubric -> review.

    No model is bundled and no fixture/regex decomposition is substituted.
    A callback receives stage/system/user (plus rubric_id for local generation).
    None of these requests includes the candidate observation.
    """
    if not callable(generator_fn):
        raise PlanError("A generator model callback is required for natural-language instantiation")
    if reviewer_fn is not None and not callable(reviewer_fn):
        raise PlanError("reviewer_fn must be a callable")

    def invoke(request, callback):
        if request_sink is not None:
            if callable(request_sink):
                request_sink(copy.deepcopy(request))
            else:
                request_sink.append(copy.deepcopy(request))
        return callback(copy.deepcopy(request))

    routing = accept_routing(input_data, invoke(build_routing_request(input_data), generator_fn))
    responses = {}
    for rubric_id in sorted({a["rubric_id"] for a in routing["assignments"]}):
        request = build_rubric_request(input_data, routing, rubric_id)
        responses[rubric_id] = accept_rubric_generation(
            input_data, routing, rubric_id, invoke(request, generator_fn))
    candidate = assemble_generation(input_data, routing, responses)
    review_request = build_review_request(input_data, candidate)
    review_response = invoke(review_request, reviewer_fn or generator_fn)
    return accept_review(input_data, candidate, review_response, provenance="model_callbacks")
