"""CLI for task instantiation, review, and metric-only binary evaluation."""
from __future__ import annotations

import argparse
import importlib
import json
import re
from pathlib import Path

from .core import load_json
from .instantiation import (accept_generation, accept_review,
                            build_instantiation_request, build_review_request,
                            build_rubric_request, accept_routing, assemble_generation,
                            instantiate)
from .unified import evaluate_metrics


def _client(spec):
    """Load only the trusted callback explicitly named by the CLI user.

    Generated plans cannot select Python modules. A client is a synchronous
    callable fn(request)->JSON/dict supplied by the integrating application.
    """
    if spec is None:
        return None
    if not isinstance(spec, str) or spec.count(":") != 1:
        raise ValueError("Client must use module:callable syntax")
    module_name, attr = spec.split(":", 1)
    identifier = r"[A-Za-z_]\w*"
    if not re.fullmatch(identifier + r"(?:\." + identifier + r")*", module_name) or not re.fullmatch(identifier, attr):
        raise ValueError("Client must identify an importable module and a function name")
    callback = getattr(importlib.import_module(module_name), attr)
    if not callable(callback):
        raise ValueError("The explicitly selected client is not callable")
    return callback


def _write(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return target


def _write_requests(path, requests):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n"
                              for request in requests), encoding="utf-8")


def parser():
    result = argparse.ArgumentParser(description="Instantiate task-specific rubrics, then evaluate binary metrics")
    commands = result.add_subparsers(dest="command", required=True)

    def command(name, description):
        sub = commands.add_parser(name, help=description)
        sub.add_argument("--input", required=True, help="Unified task/observation/reference JSON")
        sub.add_argument("--out", required=True, help="Output JSON file")
        return sub

    command("prepare", "Export the shared requirement extraction/routing request")
    rubric = command("prepare-rubric", "Export an assigned rubric's instantiation request")
    rubric.add_argument("--routing", required=True, help="Actual routing response JSON")
    rubric.add_argument("--rubric", required=True, help="Assigned rubric ID, e.g. TC-1")
    assemble = command("assemble", "Validate and join routed per-rubric model responses")
    assemble.add_argument("--routing", required=True)
    assemble.add_argument("--instances", required=True, help="JSON mapping rubric IDs to their responses")
    review = command("review-request", "Validate a generated plan and export its review request")
    review.add_argument("--generated", required=True)
    compile_command = command("compile", "Validate generation and review responses, then freeze the plan")
    compile_command.add_argument("--generated", required=True)
    compile_command.add_argument("--review", required=True)
    inst = command("instantiate", "Generate and review through explicitly supplied model callbacks")
    inst.add_argument("--client", required=True, help="Trusted synchronous Python module:function")
    inst.add_argument("--reviewer", help="Optional separate reviewer module:function")
    evaluate = command("evaluate", "Evaluate a reviewed plan; output metric verdicts only")
    evaluate.add_argument("--plan", required=True)
    evaluate.add_argument("--judge-results", help="Actual model responses keyed by check ID")
    evaluate.add_argument("--client", help="Optional trusted scoring callback module:function")
    evaluate.add_argument("--requests", help="Export scoring requests as JSONL")
    run = command("run", "Instantiate, save the frozen plan, and evaluate")
    run.add_argument("--client", required=True, help="Trusted generation/scoring callback module:function")
    run.add_argument("--reviewer", help="Optional separate reviewer module:function")
    run.add_argument("--plan-out", required=True)
    return result


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        data = load_json(args.input)
        if args.command == "prepare":
            output = build_instantiation_request(data)
        elif args.command == "prepare-rubric":
            routing = accept_routing(data, load_json(args.routing))
            output = build_rubric_request(data, routing, args.rubric)
        elif args.command == "assemble":
            routing = accept_routing(data, load_json(args.routing))
            output = assemble_generation(data, routing, load_json(args.instances))
            # Emit the generator contract only; compile attaches review and hashes.
            output = {key: output[key] for key in ("requirements", "assignments", "checks", "unresolved")}
        elif args.command == "review-request":
            candidate = accept_generation(data, load_json(args.generated))
            output = build_review_request(data, candidate)
        elif args.command == "compile":
            candidate = accept_generation(data, load_json(args.generated))
            output = accept_review(data, candidate, load_json(args.review))
        elif args.command == "instantiate":
            output = instantiate(data, _client(args.client), reviewer_fn=_client(args.reviewer))
        elif args.command == "evaluate":
            requests = []
            output = evaluate_metrics(data, load_json(args.plan), judge_fn=_client(args.client),
                                      judge_responses=load_json(args.judge_results) if args.judge_results else None,
                                      request_sink=requests)
            if args.requests:
                _write_requests(args.requests, requests)
        else:
            callback = _client(args.client)
            plan = instantiate(data, callback, reviewer_fn=_client(args.reviewer))
            _write(args.plan_out, plan)
            output = evaluate_metrics(data, plan, judge_fn=callback)
        target = _write(args.out, output)
    except (ValueError, OSError, ImportError, AttributeError, TypeError) as exc:
        cli.error(str(exc))
    print(f"Saved: {target.resolve()}")
    return 0


if __name__ == "__main__":
    main()
