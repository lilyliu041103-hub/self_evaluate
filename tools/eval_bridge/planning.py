"""Local evidence preparation and binding of validated run-specific checks."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from pathlib import Path

from .evidence import EvidenceStore, build_evidence, canonical, digest, make_index, write_json
from .profiles import load_profiles, required_evidence_for_method


def engine_fingerprint(engine):
    engine = Path(engine)
    files = sorted([*engine.glob('adaptive_eval/*.py'), *engine.glob('configs/**/*.yaml'),
                    *engine.glob('prompts/**/*.txt')])
    return digest({p.relative_to(engine).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


def adapter_fingerprint():
    package = Path(__file__).parent
    files = [*package.glob('*.py'), package.parent / 'agentos_metric_adapter.py']
    return digest({p.relative_to(package.parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(files)})


def reference_fingerprint(records, task, reference=None, original_reference=None):
    return digest({'task': task, 'reference_catalog': reference,
                   'original_reference': original_reference,
                   'evidence': {eid: r['content_hash'] for eid, r in records.items()
                                            if r['scope'] in ('task', 'reference')}})


def run_fingerprint(store):
    """Bind instantiated objects and raw JSON Pointers to this exact execution."""
    original = json.loads((store.bundle / 'audit/original-input.json').read_text(encoding='utf-8'))
    return digest({'observation': original.get('observation', {}),
                   'evidence': {eid: record['content_hash'] for eid, record in store.records.items()
                                if record['scope'] == 'observation'}})


def prepare(source, out, engine, observation=None, task_id=None, bindings=None, methods=None):
    # Import the existing ZIP adapter so checksum verification and answer provenance stay consistent.
    from agentos_metric_adapter import convert
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError('Preparation needs an empty output directory; use a new version directory.')
    original, audit = convert(source, observation, task_id)
    records, compact, catalog = build_evidence(original)
    profiles = load_profiles(Path(engine))
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, dict) or set(bindings) - {p['id'] for p in profiles}:
        raise ValueError('Bindings must map known rubric IDs to parameter dictionaries')
    for rid, parameters in bindings.items():
        if not isinstance(parameters, dict):
            raise ValueError('Each rubric binding must be an object: ' + rid)
    if bindings:
        # This is explicitly supplied evaluator configuration, never an inferred budget.
        original['reference']['configured_program_bindings'] = copy.deepcopy(bindings)
        compact['reference']['configured_program_bindings'] = copy.deepcopy(bindings)
        catalog['reference']['configured_program_bindings'] = copy.deepcopy(bindings)
        if bindings.get('BC-1'):
            budget = {'id': 'config:BC-1:budget', 'scope': 'reference', 'kind': 'budget',
                      'data': copy.deepcopy(bindings['BC-1']),
                      'origin': {'pointer': '/reference/configured_program_bindings/BC-1',
                                 'source': 'explicit --bindings configuration'}}
            budget['content_hash'] = digest(budget)
            records[budget['id']] = budget
    methods = methods or {}
    by_id = {p['id']: p for p in profiles}
    if not isinstance(methods, dict) or set(methods) - set(by_id):
        raise ValueError('Methods must map known rubric IDs to P/J/H')
    for rid, method in methods.items():
        if method not in by_id[rid]['how']['allowed_methods']:
            raise ValueError('Method forbidden by rubric: ' + rid)
    plan = {'schema_version': 'agentos-rubric-plan/1', 'stage': 'template', 'task_id': original['task']['id'],
            'task_fingerprint': reference_fingerprint(records, original['task'], catalog['reference'], original['reference']),
            'engine_fingerprint': engine_fingerprint(engine),
            'adapter_fingerprint': adapter_fingerprint(),
            'profiles_fingerprint': digest(profiles), 'profiles': profiles,
            'program_bindings': bindings,
            'methods': methods,
            'selection_policy': {'small_table_limit': 40, 'focus': 'exact identifier-column values present in task request; no status/version filtering'},
            'preparation': {'method': 'fixed_library_contracts', 'model_calls': 0,
                            'note': 'No task-specific model-generated rubric or gold. Fixed rubric boundaries and methods are retained; missing program bindings remain unverifiable.'}}
    plan['plan_hash'] = digest(plan)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / 'evidence.json', records)
    write_json(out / 'index.json', make_index(records))
    write_json(out / 'task.json', catalog)
    write_json(out / 'task.compact.json', compact)
    write_json(out / 'plan.json', plan)
    write_json(out / 'audit/original-input.json', original)
    write_json(out / 'audit/conversion.json', audit)
    shutil.copyfile(source, out / 'audit/source-eval-input.zip')
    old_bytes = len(canonical({'task': original['task'], 'reference': original['reference']}).encode('utf-8'))
    new_bytes = len(canonical(compact).encode('utf-8'))
    write_json(out / 'preparation-summary.json', {
        'task_id': plan['task_id'], 'rubric_count': len(profiles), 'model_calls': 0,
        'evidence_count': len(records), 'table_row_count': sum(r['kind'] == 'table_row' for r in records.values()),
        'original_task_compact_json_bytes': old_bytes, 'new_task_compact_json_bytes': new_bytes,
        'byte_reduction_fraction': 1 - new_bytes / old_bytes,
        'note': 'Byte sizes are measured locally; they are not token counts or measured API cost savings.',
        'plan_hash': plan['plan_hash']})
    files = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in out.rglob('*') if p.is_file()}
    write_json(out / 'manifest.json', {'schema_version': 'agentos-evidence-bundle/1',
                                     'task_fingerprint': plan['task_fingerprint'], 'files': files})
    EvidenceStore(out).verify()
    return plan


def verify_plan(plan, store, engine):
    store.verify()
    body = {k: v for k, v in plan.items() if k != 'plan_hash'}
    if plan.get('schema_version') not in ('agentos-rubric-plan/1', 'agentos-rubric-plan/4') or digest(body) != plan.get('plan_hash'):
        raise ValueError('Invalid or modified plan')
    catalog = json.loads((store.bundle / 'task.json').read_text(encoding='utf-8'))
    task = catalog['task']
    original = json.loads((store.bundle / 'audit/original-input.json').read_text(encoding='utf-8'))
    if plan['task_fingerprint'] != reference_fingerprint(store.records, task, catalog['reference'], original['reference']):
        raise ValueError('Task/reference changed; prepare a new plan')
    profiles = load_profiles(Path(engine))
    if plan['engine_fingerprint'] != engine_fingerprint(engine) or plan['profiles_fingerprint'] != digest(profiles):
        raise ValueError('Rubric library or evidence contracts changed; prepare a new plan')
    if plan.get('adapter_fingerprint') != adapter_fingerprint():
        raise ValueError('Adapter implementation changed; prepare a new plan')
    if plan['profiles'] != profiles:
        raise ValueError('Frozen profiles do not match the configured library')
    if plan['schema_version'] == 'agentos-rubric-plan/4':
        if plan.get('run_fingerprint') != run_fingerprint(store):
            raise ValueError('Run/observation changed; instantiate this execution again')
        from .instantiation import verify_instantiated
        verify_instantiated(plan, store)
    return task


def initial_reference_ids(store, request, small_table_limit=40):
    """Exact-key focus keeps every revision/void/pending record of the target batch.

    Large unrelated tables stay queryable; the selection explicitly records that
    it is a starting evidence view, never a claim of exhaustive source coverage.
    """
    by_table = {}
    ids = []
    active_tools = {r.get('tool_name') or r.get('data', {}).get('tool')
                    for r in store.records.values()
                    if r.get('id', '').startswith(('run:tool_call:', 'run:tool_attempt:')) and isinstance(r.get('data'), dict)
                    and isinstance(r.get('tool_name') or r['data'].get('tool'), str)}
    tool_selection = []
    for eid, record in store.records.items():
        if record['scope'] not in ('task', 'reference'):
            continue
        if record['kind'] == 'table_row':
            by_table.setdefault(record['table_id'], []).append(eid)
        elif record['kind'] == 'table_schema' and 'styles_xml' in record['data']:
            continue
        elif record['kind'] == 'tool_contract':
            name = record['data'].get('runtime_tool_name', record['data'].get('name'))
            chosen = name in active_tools
            tool_selection.append({'id': eid, 'tool_name': name, 'initially_selected': chosen,
                                   'mode': 'observed_tool' if chosen else 'query_required'})
            if chosen:
                ids.append(eid)
        else:
            ids.append(eid)
    focus_values = set()
    for eids in by_table.values():
        for eid in eids:
            record = store.records[eid]
            columns = store.records[record['schema_id']]['data']['columns']
            id_columns = [i for i, name in enumerate(columns) if re.search(r'id|编号|记录号|批次号', name, re.I)]
            for value in (record['data']['values'][i] for i in id_columns):
                if isinstance(value, str) and len(value) >= 4 and value in request:
                    focus_values.add(value)
    selection = []
    for table, eids in by_table.items():
        matched = [eid for eid in eids if any(v in focus_values for v in store.records[eid]['data']['values']
                                             if isinstance(v, str))]
        if matched:
            chosen, mode = matched, 'exact_task_value_match'
        elif len(eids) <= small_table_limit:
            chosen, mode = eids, 'whole_small_table'
        else:
            chosen, mode = [], 'query_required'
        ids.extend(chosen)
        selection.append({'table_id': table, 'selected': len(chosen), 'available': len(eids),
                          'mode': mode, 'all_rows_included': len(chosen) == len(eids)})
    return ids, {'focus_values': sorted(focus_values), 'tables': selection, 'tool_contracts': tool_selection,
                 'note': 'Initial view only. All omitted rows remain queryable; unviewed data cannot establish absence or completeness.'}


def contract_assessment(specification, records):
    """Qualify every bound tool reference, even when omitted from source_refs."""
    contracts = []
    ids = (specification.get('reference_ids', []) + specification.get('observation_ids', []) +
           [s['evidence_id'] for s in specification.get('source_refs', [])])
    for eid in dict.fromkeys(ids):
        record = records.get(eid, {})
        if record.get('kind') != 'tool_contract':
            continue
        data = record['data']
        snapshot = eid.startswith('agent:tool:')
        contracts.append({'evidence_id': eid,
                          'tool_name': data.get('runtime_tool_name', data.get('name')),
                          'basis': 'source_snapshot' if snapshot else 'runtime_record',
                          'source_revision': data.get('source_revision'),
                          'snapshot_id': data.get('snapshot_id'),
                          'historical_version_match': data.get('historical_version_match', 'not_asserted'),
                          'schema_origin': data.get('schema_origin'),
                          'contract_level': data.get('contract_level'),
                          'source_path': data.get('source_path')})
    if not contracts:
        return None
    return {'contracts': contracts,
            'has_unverified_source_snapshot': any(c['basis'] == 'source_snapshot' and
                c['historical_version_match'] not in ('verified', 'matched') for c in contracts),
            'scope_note': 'Source-snapshot checks assess the named reference version. They do not establish the deployed historical version or execution authorization.'}


def bind_observation(plan, store, engine):
    """Execute precise observation bindings chosen during run instantiation."""
    task = verify_plan(plan, store, engine)
    initial_ids, selection = initial_reference_ids(store, task['request'],
                                                   plan['selection_policy']['small_table_limit'])
    initial_set = set(initial_ids)
    checks = []
    profiles = {profile['id']: profile for profile in plan['profiles']}
    runtime = plan['schema_version'] == 'agentos-rubric-plan/4'
    specifications = plan.get('task_checks') if runtime else [
        {'id': p['id'], 'rubric_id': p['id'], 'metric_id': p['metric_id'],
         'method': plan.get('methods', {}).get(p['id'], p['how']['default_method']),
         'criterion': p['what'], 'prompt': None,
         'evidence_kinds': p['where'], 'reference_ids': [],
         'required_evidence': p['required_evidence'],
         'bindings': plan['program_bindings'].get(p['id'], {}), 'when': None}
        for p in plan['profiles']]
    for specification in specifications:
        profile = profiles[specification['rubric_id']]
        kinds = set(specification['evidence_kinds'])
        if runtime:
            selected = list(dict.fromkeys(specification['reference_ids'] + specification['observation_ids']))
        else:
            # Legacy static templates remain available only as explicit offline diagnostics.
            reference_set = initial_set | set(specification['reference_ids'])
            selected = [eid for eid, r in store.records.items() if r['kind'] in kinds and
                        (r['scope'] == 'observation' or eid in reference_set) and
                        (r['kind'] != 'trace_payload' or r.get('slot') in ('output', 'error'))]
        for eid in list(dict.fromkeys(selected + specification['reference_ids'])):
            if eid not in selected:
                selected.append(eid)
            schema = store.records[eid].get('schema_id')
            if schema and schema not in selected:
                selected.append(schema)
            span = store.records[eid].get('span_id')
            header = 'span:' + span if span else None
            if header in store.records and header not in selected:
                selected.append(header)
        # Every request knows task scope and source completeness even for process rubrics.
        for eid in ('task:request', 'run:status'):
            if eid in store.records and eid not in selected:
                selected.append(eid)
        available = {store.records[eid]['kind'] for eid in selected}
        required = required_evidence_for_method(profile, specification['method']) + specification['required_evidence']
        if profile['id'] == 'TU-2' and specification['bindings'].get('protocol_only') is True:
            required = profile['required_evidence'] + specification['required_evidence']
        missing = [alternatives for alternatives in required
                   if not set(alternatives) & available]
        missing = [list(item) for item in dict.fromkeys(tuple(item) for item in missing)]
        prompts = [profile['how'].get('judge_rules'), specification.get('prompt')]
        prompt = '\n\n'.join(p for p in prompts if p) or None
        checks.append({'id': specification['id'], 'rubric_id': profile['id'], 'metric_id': profile['metric_id'],
                       'method': specification['method'], 'criterion': specification['criterion'],
                       'prompt': prompt, 'how': profile['how'],
                       'group': profile['group'], 'evidence_ids': selected,
                       'input_complete': not missing, 'missing_evidence': missing,
                       'bindings': copy.deepcopy(specification['bindings']),
                       'observation_ids': copy.deepcopy(specification.get('observation_ids', [])),
                       'when': copy.deepcopy(specification.get('when')),
                       'source_refs': copy.deepcopy(specification.get('source_refs', [])),
                       'contract_assessment': contract_assessment(specification, store.records),
                       'selection': ({'mode': 'run_instance',
                                      'observation_ids': copy.deepcopy(specification['observation_ids']),
                                      'trace_policy': 'Only explicitly bound records, their schemas/span headers, task request and run status. Follow references or query supplemental evidence; a bounded packet cannot prove absence.'}
                                     if runtime else {**selection, 'mode': 'static_template_preview',
                                      'trace_policy': 'All event headers; output/error payload views. Inputs, metadata and large fields remain retrievable by evidence_ref or query. This initial packet is not the full trace.'}),
                       'applicability_policy': profile['applicability_policy']})
    return checks, ({'mode': 'run_instance', 'run_fingerprint': plan['run_fingerprint'],
                     'check_count': len(checks), 'note': 'Each instance keeps its own observation IDs.'}
                    if runtime else selection)
