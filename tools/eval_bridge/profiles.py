"""Fixed evidence contracts for the 22 rubric templates in metric v4.2.

Profiles describe where an evaluator should look; they never assert task facts,
infer missing trace events, select a new scoring method, or supply gold answers.
The source YAML remains the authority on scoring boundaries and P/J/H methods.
"""
from __future__ import annotations
from .resources import text as _resource_text, literal as _resource_literal

import copy
import hashlib
from pathlib import Path

import yaml


PROFILE_VERSION = "agentos-rubric-evidence/3"
EVIDENCE_KINDS = frozenset({
    "task_request", "table_schema", "table_row", "document", "run_status",
    "final_answer", "artifact", "trace_event", "trace_payload",
    "runtime_request", "tool_contract", "resource_usage", "budget", "interaction",
})

_COMMON_POLICY = (
    _resource_text('rubrics/bridge/_common_policy.txt')
)


# Coverage guides enumerate objects under the original rubric; they do not
# create task requirements or change scoring methods and aggregation.
_COVERAGE = _resource_literal('rubrics/bridge/profiles.json', '_COVERAGE')

# All entries are task-independent. OR within each required_evidence group,
# AND across groups. The dispatcher must retain the original rubric boundaries
# when selecting formal actions, execution attempts and associated trace data.
_SPECS = _resource_literal('rubrics/bridge/profiles.json', '_SPECS')

_REQUIRED_TEMPLATE_FIELDS = {
    "id", "metric_id", "name", "definition", "boundary", "inputs",
    "default_method", "allowed_methods", "instantiate_prompt", "judge_rules",
    "programmatic_handler", "program_contract",
}


def load_profiles(engine_dir: Path) -> list[dict]:
    """Read only trusted template YAML and return one evidence profile per rubric.

    No candidate observation or task input is accepted by this function. A new
    or removed rubric fails explicitly until it has a reviewed evidence profile.
    Source hashes allow callers to freeze and invalidate prepared plans.
    """
    engine_dir = Path(engine_dir)
    from .resource_paths import engine_resource
    rubric_dir = engine_resource(engine_dir, 'configs/rubrics')
    templates = {}
    for source in sorted(rubric_dir.glob("*.yaml")):
        raw = source.read_bytes()
        template = yaml.safe_load(raw.decode("utf-8-sig"))
        if not isinstance(template, dict) or not _REQUIRED_TEMPLATE_FIELDS <= template.keys():
            raise ValueError(f"Incomplete rubric template: {source.name}")
        rid = template["id"]
        if rid != source.stem or rid in templates:
            raise ValueError(f"Rubric ID/file mismatch or duplicate: {source.name}")
        methods = template["allowed_methods"]
        if (not isinstance(methods, list) or not methods
                or any(method not in {"P", "J", "H"} for method in methods)
                or template["default_method"] not in methods):
            raise ValueError(f"Invalid source method contract: {rid}")
        templates[rid] = (template, raw, source)
    if set(templates) != set(_SPECS):
        missing = sorted(set(_SPECS) - set(templates))
        unexpected = sorted(set(templates) - set(_SPECS))
        raise ValueError(f"Rubric/profile coverage mismatch: missing={missing}, unexpected={unexpected}")
    if set(_COVERAGE) != set(templates):
        raise ValueError('Instantiation coverage guides must cover exactly the configured rubrics')

    result = []
    for rid in sorted(templates):
        template, raw, source = templates[rid]
        spec = copy.deepcopy(_SPECS[rid])
        coverage = copy.deepcopy(_COVERAGE[rid])
        if (set(coverage) != {'unit', 'include', 'bind', 'boundary'}
                or any(not isinstance(coverage[key], str) or not coverage[key].strip()
                       for key in ('unit', 'bind', 'boundary'))
                or not isinstance(coverage['include'], list) or not coverage['include']
                or any(not isinstance(item, str) or not item.strip() for item in coverage['include'])):
            raise ValueError('Invalid instantiation coverage guide: ' + rid)
        kinds = set(spec["where"])
        if not kinds <= EVIDENCE_KINDS or len(kinds) != len(spec["where"]):
            raise ValueError(f"Unknown or duplicate evidence kind in profile: {rid}")
        if any(not alternatives or not set(alternatives) <= kinds for alternatives in spec["required_evidence"]):
            raise ValueError(f"Required evidence is not present in where: {rid}")
        profile = {
            "id": rid,
            "metric_id": template["metric_id"],
            "name": template.get("name_zh", template["name"]),
            "what": template["definition"],
            "where": spec["where"],
            "how": copy.deepcopy(template),
            "group": spec["group"],
            "required_evidence": spec["required_evidence"],
            "applicability_policy": spec["policy"] + _COMMON_POLICY,
            "instantiation_coverage": coverage,
            "profile_version": PROFILE_VERSION,
            "source_path": __import__('os').path.relpath(source, engine_dir).replace('\\', '/'),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
        }
        if spec.get('required_evidence_by_method'):
            profile['required_evidence_by_method'] = spec['required_evidence_by_method']
        result.append(profile)
    return result


def required_evidence_for_method(profile, method):
    """Semantic-only TU-2 checks do not inherit a protocol/schema prerequisite."""
    return copy.deepcopy(profile['required_evidence'] +
                         profile.get('required_evidence_by_method', {}).get(method, []))
