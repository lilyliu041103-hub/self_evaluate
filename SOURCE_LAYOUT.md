# Evaluation source and editable resources

The maintained evaluation source root is this directory. External AgentOS and
industrial_brain copies are read-only references. The original adaptive-eval-workspace
is historical; current tools are under tools/ and third_party/IndustryBench/.

- tools/eval_bridge/: preparation, instantiation, evidence and scoring implementation.
- runtime/metric_rubric_impl_v4_modular/adaptive_eval/: runtime implementation.
- metrics/metrics.yaml: metric catalog; rubrics/templates/: rubric source YAML.
- rubrics/bridge/: evidence coverage/policies; rubrics/task-profile/: capability gates.
- prompts/: runtime, legacy, bridge, client and IndustryBench instructions.
- benchmarks/definitions/ and templates/: task contents and annotation/generator assets.
- configs/task-profiles/: task-specific capability profiles.

Required resources fail explicitly if missing; no embedded-content fallback is provided.
Resources are still needed locally even if excluded from Git. Ignore examples in
configs/resource-ignore.example.txt do not change tracking or authorize publication.
Schema keys, protocol enums and executable validation logic remain in Python.
Resource changes invalidate implementation/adapter fingerprints; re-prepare and
re-instantiate historical plans before using the new layout.

Benchmark builders require AIMS_BENCHMARK_WORKSPACE to point at an authorized
run directory. They read/write its outputs/ subtree, preserving historical relative
suite naming. Importing these old authoring scripts may execute generation;
invoke them as standalone tools in that workspace, never as evaluator libraries.
Repackaging requires AIMS_REPACKAGE_INPUT and AIMS_REPACKAGE_OUTPUT.
IndustryBench scoring requires --samples, --answers and --output; set
AIMS_REQUEST_TEMP to an authorized existing temporary directory for curl bodies.
Preflight requires explicit --output-dir and source revision arguments. It can
contact network services and download a dataset, so it is not an offline test.

build_agentos_input.py and agentos_source_contracts.py require Pydantic 2;
IndustryBench has its own dependency declaration. Optional dependencies were installed with user approval; see requirements-tools.lock.txt. No
real model calls are part of this migration. No upstream .git or dataset is copied.
