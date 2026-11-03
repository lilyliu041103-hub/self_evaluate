"""Load the editable metric index and one uniform YAML file per rubric.

The YAML files are the v4 source of truth. No cache or generated catalog is
consulted, so edits are visible on the next load and can be fingerprinted by
the plan contract. YAML uses a safe loader and rejects duplicate mapping keys.
"""
from __future__ import annotations

from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUBRIC_KEYS = {
    "id", "metric_id", "name", "name_zh", "definition", "boundary", "inputs",
    "default_method", "allowed_methods", "instantiate_prompt", "judge_rules",
    "programmatic_handler", "program_contract",
}
METRIC_KEYS = {"id", "name", "name_zh", "layer", "aggregation", "rubrics", "description"}


class LibraryError(ValueError):
    """The editable library is inconsistent or cannot be executed as declared."""


class _UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML with duplicate-key rejection instead of silent overwrites."""


def _mapping(loader, node, deep=False):
    # Expanding YAML merges before checking also rejects ambiguous overrides.
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise LibraryError("YAML mapping keys must be scalar values") from exc
        if duplicate:
            raise LibraryError(f"Duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _read(path):
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.load(stream, Loader=_UniqueSafeLoader)
    except (OSError, yaml.YAMLError, RecursionError) as exc:
        raise LibraryError(f"Cannot load {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise LibraryError(f"{path.name} must contain a YAML mapping")
    return value


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise LibraryError(f"{label} requires exactly: {sorted(expected)}")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise LibraryError(f"{label} must be a nonempty string")


def _strings(value, label):
    if not isinstance(value, list) or not value:
        raise LibraryError(f"{label} must be a nonempty list")
    for item in value:
        _text(item, label)
    if len(set(value)) != len(value):
        raise LibraryError(f"{label} must not contain duplicates")


def load_library(root=None):
    """Return {version, aggregation_policy, metrics, rubrics} from YAML files.

    ``root`` is the package directory, useful for explicit alternate libraries
    and tests. The metric index declares the complete set of required rubric
    files; absent files, extra files and inconsistent ownership are rejected.
    """
    package = Path(root) if root is not None else ROOT
    index = _read(package / "configs" / "metrics.yaml")
    _keys(index, {"version", "aggregation_policy", "metrics"}, "metrics index")
    _text(index["version"], "library version")
    if not isinstance(index["aggregation_policy"], dict):
        raise LibraryError("aggregation_policy must be an object")
    if index["aggregation_policy"].get("metric_default") != "all":
        raise LibraryError("Binary metric aggregation must be 'all'")
    if not isinstance(index["metrics"], list) or not index["metrics"]:
        raise LibraryError("metrics must be a nonempty list")
    metric_ids, declarations = set(), {}
    for metric in index["metrics"]:
        _keys(metric, METRIC_KEYS, "metric")
        for key in ("id", "name", "name_zh", "description"):
            _text(metric[key], "metric." + key)
        mid = metric["id"]
        if not re.fullmatch(r"[A-Z][A-Z0-9]*", mid) or mid in metric_ids:
            raise LibraryError("Invalid or duplicate metric ID: " + mid)
        metric_ids.add(mid)
        if type(metric["layer"]) is not int or metric["layer"] < 1:
            raise LibraryError("metric.layer must be a positive integer")
        if metric["aggregation"] != "all":
            raise LibraryError("Every metric must use binary 'all' aggregation")
        _strings(metric["rubrics"], "metric.rubrics")
        for rid in metric["rubrics"]:
            if not re.fullmatch(re.escape(mid) + r"-[1-9][0-9]*", rid):
                raise LibraryError(f"Rubric ID {rid!r} does not belong to metric {mid!r}")
            if rid in declarations:
                raise LibraryError("Rubric declared more than once: " + rid)
            declarations[rid] = mid
    rubric_dir = package / "configs" / "rubrics"
    files = sorted(rubric_dir.glob("*.yaml"))
    if {path.stem for path in files} != set(declarations):
        missing = sorted(set(declarations) - {path.stem for path in files})
        extra = sorted({path.stem for path in files} - set(declarations))
        raise LibraryError(f"Rubric files differ from metric index: missing={missing}, extra={extra}")
    if list(rubric_dir.glob("*.yml")):
        raise LibraryError("Use .yaml consistently; .yml files are not loaded")
    from .programmatic import HANDLERS as value_handlers
    from .temporal import HANDLERS as temporal_handlers
    handlers = {**value_handlers, **temporal_handlers}
    by_id = {}
    for path in files:
        rubric = _read(path)
        _keys(rubric, RUBRIC_KEYS, path.name)
        for key in ("id", "metric_id", "name", "name_zh", "definition", "boundary", "instantiate_prompt"):
            _text(rubric[key], path.name + "." + key)
        rid = rubric["id"]
        if rid != path.stem or rid in by_id:
            raise LibraryError("Rubric filename must match its unique ID: " + path.name)
        if rubric["metric_id"] != declarations[rid]:
            raise LibraryError("Incorrect metric ownership: " + rid)
        _strings(rubric["inputs"], rid + ".inputs")
        handler, contract, judge = (rubric[k] for k in ("programmatic_handler", "program_contract", "judge_rules"))
        if handler is None:
            if contract is not None:
                raise LibraryError("A rubric without a program cannot declare a program contract: " + rid)
        else:
            if not isinstance(handler, str) or handler not in handlers:
                raise LibraryError("Unknown registered program: " + str(handler))
            _text(contract, rid + ".program_contract")
        if judge is not None:
            _text(judge, rid + ".judge_rules")
        supported = ({"P"} if handler else set()) | ({"J"} if judge else set())
        if handler and judge:
            supported.add("H")
        _strings(rubric["allowed_methods"], rid + ".allowed_methods")
        allowed = set(rubric["allowed_methods"])
        if not allowed <= supported:
            raise LibraryError("Allowed methods need corresponding program/Judge configuration: " + rid)
        _text(rubric["default_method"], rid + ".default_method")
        if rubric["default_method"] not in allowed:
            raise LibraryError("Default method is not an allowed method: " + rid)
        by_id[rid] = rubric
    # Stable index order is used for routing prompts and reproducible exports.
    return {**index, "rubrics": [by_id[rid] for rid in declarations]}


def load_rubric(rubric_id, root=None):
    """Return one validated rubric; loading always validates the full index."""
    for rubric in load_library(root)["rubrics"]:
        if rubric["id"] == rubric_id:
            return rubric
    raise LibraryError("Unknown rubric: " + str(rubric_id))
