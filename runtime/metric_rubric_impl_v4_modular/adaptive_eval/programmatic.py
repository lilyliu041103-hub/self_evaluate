"""Deterministic, evidence-based validators. No agent-reported scores are trusted.

All handlers accept (case, params) and return the same result dictionary. JSON
Pointers address the full case, not a selected event. Configuration is trusted
evaluator input; observed data is produced by a trace/environment adapter.
"""
from __future__ import annotations

import ast
import json
import math
import operator
from typing import Any


class MissingEvidence(ValueError):
    """A required observation or oracle is absent."""


def pointer(data: Any, path: str) -> Any:
    """Resolve RFC 6901-style pointers; invalid paths never silently return None."""
    if path == "":
        return data
    if not isinstance(path, str) or not path.startswith("/"):
        raise MissingEvidence(f"Invalid JSON Pointer: {path!r}")
    current = data
    for token in path[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and str(int(token)) == token:
            try:
                current = current[int(token)]
            except IndexError as exc:
                raise MissingEvidence(f"Missing evidence: {path}") from exc
        else:
            raise MissingEvidence(f"Missing evidence: {path}")
    return current


def result(score: float | None, why: str, evidence=(), raw_value=None, status=None):
    return {
        "status": status or ("scored" if score is not None else "unverifiable"),
        "score": score,
        "why": why,
        "evidence": list(dict.fromkeys(evidence)),
        "raw_value": raw_value,
    }


def unknown(why, evidence=(), raw_value=None):
    return result(None, why, evidence, raw_value)


def na(why):
    return result(None, why, status="not_applicable")


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Expected a finite number, got {value!r}")
    return float(value)


def equal(left, right):
    # JSON booleans are not the numbers 0/1; Python otherwise conflates these.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right


def trusted_reference(path):
    if not isinstance(path, str) or not path.startswith(("/reference/", "/task/")):
        raise ValueError("Oracle path must address trusted /reference/ or /task/ data")
    return path


def aggregate(items, mode="all"):
    """Binary conjunction; pass rates are diagnostic statistics, never scores."""
    evidence = [p for item in items for p in item["evidence"]]
    active = [item for item in items if item["status"] != "not_applicable"]
    scores = [item["score"] for item in active if item["status"] == "scored"]
    passed = sum(score == 1 for score in scores)
    failed = sum(score == 0 for score in scores)
    raw = {"items": items, "scored_count": len(scores), "total_count": len(items),
           "applicable_count": len(active), "passed": passed, "failed": failed,
           "unknown_count": len(active) - len(scores),
           "coverage": len(scores) / len(active) if active else None,
           "observed_pass_rate": passed / len(scores) if scores else None}
    if mode != "all":
        return unknown("Binary checks require aggregation='all'; legacy mean aggregation is rejected.", evidence, raw)
    if not active:
        return result(None, "No applicable checks were configured.", evidence, raw, status="not_applicable")
    if any(isinstance(s, bool) or s not in (0, 1) for s in scores):
        return unknown("A component returned a non-binary score.", evidence, raw)
    failures = [item["why"] for item in active if item["status"] == "scored" and item["score"] == 0]
    if failures:
        combined = result(0.0, "; ".join(failures), evidence, raw)
        combined["known_failure"] = True
        return combined
    if len(scores) != len(active):
        combined = unknown("At least one required check lacks evidence; see item results.", evidence, raw)
        return combined
    return result(1.0, "All configured checks passed.", evidence, raw)


def _assertion(case, item):
    path = item.get("actual_path")
    op = item.get("op", "eq")
    refs = [path] if isinstance(path, str) else []
    label = item.get("id", path or "assertion")
    try:
        try:
            actual = pointer(case, path)
            present = True
        except MissingEvidence:
            if op != "exists":
                raise
            parent_flag = item.get("complete_parent_path")
            if not parent_flag or pointer(case, parent_flag) is not True:
                raise MissingEvidence("Absence is not failure without an explicit complete-parent flag")
            refs.append(parent_flag)
            actual, present = None, False
        if "expected_path" in item:
            expected_path = item["expected_path"]
            if expected_path == path:
                raise ValueError("An assertion cannot use its actual value as its own oracle")
            expected = pointer(case, expected_path)
            refs.append(expected_path)
        elif "expected" in item:
            expected = item["expected"]
        elif op == "exists":
            expected = True
        else:
            raise ValueError("Missing expected or expected_path")
        if op == "exists":
            if not isinstance(expected, bool):
                raise ValueError("exists expects a boolean")
            passed = present == expected
        elif op == "json_valid":
            if not isinstance(expected, bool):
                raise ValueError("json_valid expects a boolean")
            valid = isinstance(actual, str)
            if valid:
                def reject_constant(value):
                    raise ValueError("Non-JSON numeric constant: " + value)
                try:
                    json.loads(actual, parse_constant=reject_constant)
                except (ValueError, TypeError):
                    valid = False
            passed = valid == expected
        elif op in {"eq", "ne"}:
            passed = equal(actual, expected)
            if op == "ne":
                passed = not passed
        elif op in {"gt", "lt", "gte", "lte"}:
            left, right = finite_number(actual), finite_number(expected)
            passed = {"gt": operator.gt, "lt": operator.lt,
                      "gte": operator.ge, "lte": operator.le}[op](left, right)
        elif op == "approx":
            tolerance = finite_number(item.get("tolerance", 0.0))
            rel_tol = finite_number(item.get("rel_tol", 0.0))
            if tolerance < 0 or rel_tol < 0:
                raise ValueError("Tolerances cannot be negative")
            passed = math.isclose(finite_number(actual), finite_number(expected), abs_tol=tolerance, rel_tol=rel_tol)
        elif op == "in":
            if not isinstance(expected, (list, tuple)):
                raise ValueError("in expects a list of allowed values")
            passed = any(equal(actual, value) for value in expected)
        elif op == "contains":
            if isinstance(actual, str) and isinstance(expected, str):
                passed = expected in actual
            elif isinstance(actual, list):
                passed = any(equal(value, expected) for value in actual)
            elif isinstance(actual, dict) and isinstance(expected, str):
                passed = expected in actual
            else:
                raise ValueError("contains expects a string, list, or object key")
        elif op == "set_equal":
            if not isinstance(actual, list) or not isinstance(expected, list):
                raise ValueError("set_equal expects two lists")
            passed = all(any(equal(a, b) for b in expected) for a in actual) and all(any(equal(b, a) for a in actual) for b in expected)
        else:
            raise ValueError(f"Unsupported assertion operator: {op}")
        raw = {"id": label, "actual": actual, "expected": expected, "op": op}
        return result(float(passed), f"{label}: {'passed' if passed else 'failed'} ({op}).", refs, raw)
    except (MissingEvidence, ValueError, TypeError, KeyError) as exc:
        return unknown(f"{label}: {exc}", refs)


def assertions(case, params):
    """params={items:[{id,actual_path,op,expected|expected_path}],aggregation:'all'}.

    Supported operators: eq/ne/in/contains/gt/lt/gte/lte/approx/exists/set_equal/json_valid.
    json_valid parses the actual final text directly; it does not accept an
    agent's self-reported format flag and rejects non-JSON NaN/Infinity constants.
    exists may score missing data only with complete_parent_path pointing to a
    trusted adapter's True completeness flag. expected_path is evaluator-selected:
    it can address reference facts OR a trusted tool result/environment snapshot,
    but cannot equal actual_path. The adapter must distinguish observed agent
    claims from trusted observations; the evaluator must choose the latter.
    Missing observed values otherwise return Unverifiable, not False or zero.
    """
    items = params.get("items")
    if not isinstance(items, list):
        return unknown("assertions.items must be a list")
    return aggregate([_assertion(case, item) for item in items], params.get("aggregation", "all"))


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _safe_expression(expression, variables):
    if not isinstance(expression, str) or len(expression) > 1000:
        raise ValueError("Invalid or oversized expression")
    root = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(root)) > 100:
        raise ValueError("Expression exceeds AST node limit")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return finite_number(node.value)
        if isinstance(node, ast.Name) and node.id in variables:
            return finite_number(variables[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return finite_number(_OPS[type(node.op)](walk(node.left), walk(node.right)))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return walk(node.operand) if isinstance(node.op, ast.UAdd) else -walk(node.operand)
        raise ValueError("Only numbers, named oracle variables and + - * / are allowed")
    return walk(root)


def numeric(case, params):
    """Compare numeric answer to a trusted oracle, never execute arbitrary code.

    params={actual_path:'/observation/answer_structured/total',
      expression:'quantity * price',variables:{quantity:'/task/quantity',price:'/reference/price'},
      abs_tol:0.01,rel_tol:0,actual_unit_path:'/observation/answer_structured/unit',
      expected_unit:'kg',unit_conversions:{g:{scale:0.001,offset:0}}}
    Instead of expression use expected_path:'/reference/total' or literal expected.
    Conversion maps an observed unit directly into expected_unit.
    """
    refs = []
    try:
        actual_path = params["actual_path"]
        refs.append(actual_path)
        observed_value = pointer(case, actual_path)
        try:
            actual = finite_number(observed_value)
        except ValueError:
            # This checker verifies arithmetic, not an unrequested output format.
            # A failed/missing text-to-number adapter cannot establish wrong math.
            return unknown("No reliably extracted finite numeric answer; validate any explicit output-format requirement separately.",
                           refs, {"actual": observed_value})
        if "expected_path" in params:
            ep = trusted_reference(params["expected_path"])
            refs.append(ep)
            expected = finite_number(pointer(case, ep))
        elif "expected" in params:
            expected = finite_number(params["expected"])
        elif "expression" in params:
            variables = {}
            for name, path in params.get("variables", {}).items():
                refs.append(trusted_reference(path))
                variables[name] = finite_number(pointer(case, path))
            expected = _safe_expression(params["expression"], variables)
        else:
            raise ValueError("A trusted expected value or reference expression is required")
        observed = actual
        expected_unit = params.get("expected_unit")
        if expected_unit is not None:
            if "actual_unit_path" in params:
                refs.append(params["actual_unit_path"])
                unit = pointer(case, params["actual_unit_path"])
            elif "actual_unit" in params:
                unit = params["actual_unit"]
            else:
                raise MissingEvidence("Expected unit is configured but observed unit is unavailable")
            if unit != expected_unit:
                conversion = params.get("unit_conversions", {}).get(unit)
                if conversion is None:
                    return unknown(f"No trusted conversion from observed unit {unit!r} to {expected_unit!r}; unit support is insufficient to establish a calculation error.",
                                   refs, {"actual": observed, "expected": expected})
                actual = actual * finite_number(conversion["scale"]) + finite_number(conversion.get("offset", 0))
        abs_tol = finite_number(params.get("abs_tol", 0))
        rel_tol = finite_number(params.get("rel_tol", 0))
        if abs_tol < 0 or rel_tol < 0:
            raise ValueError("Tolerances cannot be negative")
        passed = math.isclose(finite_number(actual), expected, abs_tol=abs_tol, rel_tol=rel_tol)
        raw = {"observed": observed, "converted_actual": actual, "expected": expected, "absolute_error": abs(actual - expected)}
        return result(float(passed), "Numeric value and configured unit/tolerance passed." if passed else f"Numeric mismatch: actual={actual}, expected={expected}.", refs, raw)
    except (MissingEvidence, ValueError, TypeError, KeyError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        return unknown(str(exc), refs)


def tool_selection(case, params):
    """params={call_path:'/observation/events/0',allowed_tools:['read_sensor']}.

    Alternative: required_capabilities:['sensor.read'],
    tool_capabilities:{read_sensor:['sensor.read']}. If both are supplied, both
    must pass. Tool specifications are supplied by the evaluator, not the agent.
    """
    refs = []
    try:
        path = params["call_path"]
        refs.append(path)
        call = pointer(case, path)
        tool = call["tool"]
        if not isinstance(tool, str) or not tool:
            raise ValueError("Call tool must be a nonempty string")
        conditions = []
        if "allowed_tools" in params:
            allowed = params["allowed_tools"]
            if not isinstance(allowed, list):
                raise ValueError("allowed_tools must be an explicit list")
            conditions.append(tool in allowed)
        if "required_capabilities" in params:
            catalog = params.get("tool_capabilities", {})
            if tool not in catalog:
                raise MissingEvidence(f"No trusted capability specification for {tool}")
            conditions.append(set(params["required_capabilities"]) <= set(catalog[tool]))
        if not conditions:
            raise MissingEvidence("No allowed tool list or capability requirement supplied")
        passed = all(conditions)
        return result(float(passed), f"Tool {tool!r} {'matches' if passed else 'does not match'} the configured operation.", refs, {"tool": tool})
    except (MissingEvidence, ValueError, KeyError, TypeError) as exc:
        return unknown(str(exc), refs)


def call_arguments(case, params):
    """params={call_path:'/observation/events/0',schema:{...JSON Schema...},
      required_fields:['id','tool','arguments'],semantic_assertions:[...assertions items...]}

    Validates protocol fields and JSON Schema (with format checking), then task
    semantics. schema_only:true explicitly restricts this programmatic component
    to syntax, for a standalone protocol/schema check or a Hybrid check with a
    separate semantic judge. Without either
    semantic_assertions or schema_only:true it cannot claim semantic correctness.
    Optional protocol_schema validates the entire normalized call envelope,
    including additional transport fields. Without it the built-in envelope
    contract only covers nonempty id/tool, object arguments, and field presence.
    """
    refs = []
    try:
        path = params["call_path"]
        refs.append(path)
        call = pointer(case, path)
        if not isinstance(call, dict):
            return result(0.0, "Call is not an object.", refs)
        missing = [name for name in params.get("required_fields", ["id", "tool", "arguments"]) if name not in call]
        if missing:
            return result(0.0, f"Missing protocol fields: {missing}.", refs, {"missing_fields": missing})
        if not isinstance(call.get("tool"), str) or not call["tool"].strip() or not isinstance(call.get("arguments"), dict):
            return result(0.0, "Tool must be a nonempty string and arguments must be an object.", refs)
        if "id" in params.get("required_fields", ["id", "tool", "arguments"]) and (not isinstance(call["id"], str) or not call["id"]):
            return result(0.0, "Normalized call id must be a nonempty string.", refs)
        schema = params.get("schema")
        if not isinstance(schema, (dict, bool)):
            raise MissingEvidence("A trusted JSON Schema is required")
        messages = []
        try:
            import jsonschema
            format_checker = jsonschema.FormatChecker()

            def supported_schema(node):
                if isinstance(node, dict):
                    for key in ("$ref", "$dynamicRef"):
                        if key in node and (not isinstance(node[key], str) or not node[key].startswith("#")):
                            raise ValueError("Only local schema references are supported; no remote fetch is performed")
                    if "format" in node and node["format"] not in format_checker.checkers:
                        raise ValueError(f"Unsupported format checker: {node['format']!r}; format was not verified")
                    # Traverse actual subschemas, not enum/default/example data.
                    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
                        for value in node.get(key, {}).values():
                            supported_schema(value)
                    for key in ("additionalProperties", "unevaluatedProperties", "propertyNames",
                                "additionalItems", "unevaluatedItems", "contains", "not", "if", "then", "else"):
                        if key in node:
                            supported_schema(node[key])
                    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
                        for value in node.get(key, []):
                            supported_schema(value)
                    if "items" in node:
                        for value in node["items"] if isinstance(node["items"], list) else [node["items"]]:
                            supported_schema(value)
                    for value in node.get("dependencies", {}).values():
                        if isinstance(value, (dict, bool)):
                            supported_schema(value)
            schema_targets = [("arguments", schema, call["arguments"])]
            if "protocol_schema" in params:
                schema_targets.append(("protocol", params["protocol_schema"], call))
            for scope, selected_schema, value in schema_targets:
                if not isinstance(selected_schema, (dict, bool)):
                    raise ValueError(f"{scope} schema must be an object or boolean")
                supported_schema(selected_schema)
                if isinstance(selected_schema, dict) and "$schema" in selected_schema and jsonschema.validators.validator_for(selected_schema, default=None) is None:
                    raise ValueError("Unsupported JSON Schema dialect; validation was not performed")
                validator_class = jsonschema.validators.validator_for(selected_schema)
                validator_class.check_schema(selected_schema)
                validator = validator_class(selected_schema, format_checker=format_checker)
                errors = sorted(validator.iter_errors(value), key=lambda err: str(list(err.path)))
                messages += [f"{scope}/{'/'.join(map(str, err.path))}: {err.message}" for err in errors]
        except ImportError:
            return unknown("jsonschema is not installed; schema was not validated.", refs)
        except Exception as exc:
            if messages:
                failed = result(0.0, "; ".join(messages) + f"; additional schema validation unavailable: {exc}", refs,
                                {"schema_errors": messages, "unverified_schema": str(exc)})
                failed["known_failure"] = True
                return failed
            return unknown(f"Cannot apply trusted schema: {exc}", refs)
        if messages:
            return result(0.0, "; ".join(messages), refs, {"schema_errors": messages})
        if params.get("semantic_assertions"):
            semantic = assertions(case, {"items": params["semantic_assertions"], "aggregation": "all"})
            semantic["evidence"] = list(dict.fromkeys(refs + semantic["evidence"]))
            return semantic
        if params.get("schema_only") is True:
            return result(1.0, "Protocol and JSON Schema passed; semantic correctness is outside this component's scope.", refs, {"scope": "schema_only"})
        return unknown("Schema passed, but no semantic oracle was configured; schema validity is not argument correctness.", refs)
    except (MissingEvidence, ValueError, KeyError, TypeError) as exc:
        return unknown(str(exc), refs)




def budget(case, params):
    """params={items:[{resource:'tool_calls',actual_path:'/observation/resources/tool_calls',
      limit:10,scope:'task'}]}. limit_path may reference trusted task/reference data.

    Tool/stage/fault scopes require a matching resource adapter path and a
    scope_path whose value equals scope, e.g. tool:pump with
    actual_path=/observation/resource_scopes/pump/retries and
    scope_path=/observation/resource_scopes/pump/scope. This prevents silently
    using a task total as a per-tool count. All applicable budgets must pass.
    """
    items = params.get("items")
    if not isinstance(items, list):
        return unknown("budget.items must be a list")
    if not items:
        return na("No budgets apply to this task.")
    checks = []
    for item in items:
        refs = []
        try:
            resource, scope = item["resource"], item.get("scope", "task")
            path = item["actual_path"]
            refs.append(path)
            actual = finite_number(pointer(case, path))
            if "limit_path" in item:
                lp = trusted_reference(item["limit_path"])
                refs.append(lp)
                limit = finite_number(pointer(case, lp))
            else:
                limit = finite_number(item["limit"])
            if actual < 0 or limit < 0:
                raise ValueError("Budget actual/limit cannot be negative")
            if scope != "task":
                sp = item.get("scope_path")
                if not sp:
                    raise MissingEvidence("Non-task resource counts require a scope_path adapter declaration")
                refs.append(sp)
                if pointer(case, sp) != scope:
                    raise ValueError("Resource adapter scope does not match budget scope")
            passed = actual <= limit
            raw = {"resource": resource, "scope": scope, "actual": actual, "limit": limit, "excess": max(0.0, actual - limit)}
            checks.append(result(float(passed), f"{resource} [{scope}]: actual={actual}, limit={limit}; {'within budget' if passed else 'budget exceeded'}.", refs, raw))
        except (MissingEvidence, ValueError, KeyError, TypeError) as exc:
            checks.append(unknown(str(exc), refs))
    return aggregate(checks, "all")


HANDLERS = {
    "assertions": assertions,
    "numeric": numeric,
    "tool_selection": tool_selection,
    "call_arguments": call_arguments,
    "budget": budget,
}
