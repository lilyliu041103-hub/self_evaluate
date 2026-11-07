"""Instantiate concrete checks for one execution and validate them locally."""
from __future__ import annotations
from .resources import literal as _fragment
from .resources import text as _resource_text, literal as _resource_literal

import copy
import difflib
import json
import math
import re

from .evidence import canonical, digest


_REFERENCE_SCOPES = frozenset({'task', 'reference'})


class SourceQuoteError(ValueError):
    """Actionable feedback only; approximate matches never satisfy validation."""

    def __init__(self, rubric_id, check_id, source_index, evidence_id, quote, data):
        # Compare bounded original sentences, preserving their exact characters.
        # Avoid difflib over arbitrarily large evidence payloads or generated quotes.
        preview = quote[:500]
        best, score = None, 0.0
        for value in _strings(data):
            for match in re.finditer(r'[^。！？\n]{1,1000}[。！？\n]?', value[:40000]):
                candidate = match.group(0)
                similarity = difflib.SequenceMatcher(None, preview, candidate, autojunk=False).ratio()
                if similarity > score:
                    best, score = candidate, similarity
        edits = []
        if best is not None and score >= 0.5:
            for tag, i, j, a, b in difflib.SequenceMatcher(None, preview, best, autojunk=False).get_opcodes():
                if tag != 'equal':
                    edits.append({'operation': tag, 'quote_range': [i, j],
                                  'quoted': preview[i:j], 'source': best[a:b]})
        else:
            best = None
        self.diagnostic = {'code': 'source_quote_not_verbatim', 'rubric_id': rubric_id,
                           'check_id': check_id, 'source_ref_index': source_index,
                           'evidence_id': evidence_id, 'quote': preview,
                           'quote_truncated': len(quote) > len(preview),
                           'source_excerpt': best, 'character_edits': edits[:20],
                           'hint': _fragment('prompts/bridge/fragments/instantiation.json', 'fragment_1')}
        super().__init__('Source quote is not verbatim: ' + canonical(self.diagnostic))


_CHECK_KEYS = frozenset({
    'id', 'criterion', 'method', 'prompt', 'bindings', 'evidence_kinds',
    'reference_ids', 'observation_ids', 'required_evidence', 'when', 'source_refs',
})
_WHEN_OPS = frozenset({'eq', 'ne', 'in', 'gt', 'gte', 'lt', 'lte', 'contains', 'exists'})
_REPAIR_ROUNDS = 2

INSTANTIATION_SYSTEM = _resource_text('prompts/bridge/instantiation_system.txt')


def _exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(label + ' must have exactly: ' + ', '.join(sorted(keys)))


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be a nonempty string')
    return value


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


_QUOTE_TRANSLATION = str.maketrans({
    '“': '"', '”': '"', '「': '"', '」': '"', '『': '"', '』': '"', '＂': '"',
    '‘': "'", '’': "'", '`': "'", '´': "'",
})


def _canon_quote(text):
    """Fold typographic quotes and drop whitespace, nothing else.

    The contract forbids paraphrase; it does not require reproducing the exact
    quote glyph or the line wrapping the source happened to use. Whitespace is
    removed rather than collapsed so a quote reflowed at any column still
    matches, while one differing character still fails.
    """
    folded = str(text).translate(_QUOTE_TRANSLATION)
    return re.sub(r'\s+', '', folded)


def _quote_matches(quote, data):
    """Verbatim match first; accept only quote/whitespace folding as a fallback."""
    if not str(quote).strip():
        return False
    values = list(_strings(data))
    if any(quote in value for value in values) or quote in canonical(data):
        return True
    target = _canon_quote(quote)
    if not target:
        return False
    if any(target in _canon_quote(value) for value in values):
        return True
    return target in _canon_quote(canonical(data))

def _profiles(plan):
    profiles = plan.get('profiles')
    if not isinstance(profiles, list) or len(profiles) != 22:
        raise ValueError('Instantiation requires all 22 rubric profiles')
    result = {p['id']: p for p in profiles}
    if len(result) != 22:
        raise ValueError('Duplicate rubric profiles')
    return result


def _original(store):
    original = json.loads((store.bundle / 'audit/original-input.json').read_text(encoding='utf-8'))
    if not all(isinstance(original.get(key), dict) for key in ('task', 'reference', 'observation')):
        raise ValueError('Run input must contain task, reference and observation objects')
    return original


def _contract_view(record):
    """Project contract metadata, keeping parameter schemas intact (including $defs)."""
    data = record['data']
    function = data.get('function', data)
    keys = ('name', 'runtime_tool_name', 'contract_level', 'schema_origin',
            'source_revision', 'snapshot_id', 'historical_version_match', 'source_path',
            'provenance', 'scope_note', 'applicability_note', 'reference_scope')
    projected = {key: copy.deepcopy(data[key]) for key in keys if key in data}
    if function is not data:
        projected['name'] = function.get('name')
    for key in ('parameters', 'input_schema', 'inputSchema', 'wire_call_schema'):
        if key in function:
            projected[key] = copy.deepcopy(function[key])
    description = function.get('description', '')
    if isinstance(description, str):
        projected['description'] = description[:600]
    view = {key: copy.deepcopy(record[key]) for key in ('id', 'scope', 'kind', 'origin')}
    view.update(data=projected, evidence_ref=record['id'], content_not_inlined=True,
                projection='contract_fields', schema_inlined=bool(list(_schema_contracts(projected))))
    return view


def _packet(records, ids, budget):
    """Bound data and omission metadata together; large records get marked previews."""
    selected, omitted, total, omitted_total = [], [], 0, 0
    data_budget = budget - min(3000, budget // 8)
    for eid in ids:
        record = records[eid]
        serialized, view = canonical(record), record
        if record['kind'] == 'tool_contract' and isinstance(record['data'], dict):
            view = _contract_view(record)
            if len(canonical(view)) > data_budget:
                # No partial JSON Schema: expose a mandatory read of the original.
                view = {k: copy.deepcopy(record[k]) for k in ('id', 'scope', 'kind', 'origin')}
                view.update(evidence_ref=eid, content_not_inlined=True, schema_inlined=False,
                            read_required=True, reason='contract_exceeds_packet_budget')
        elif len(serialized) > 8000:
            view = {k: copy.deepcopy(record[k]) for k in ('id', 'scope', 'kind', 'origin')}
            view.update(data_preview=canonical(record['data'])[:1800], evidence_ref=eid,
                        content_not_inlined=True, original_characters=len(serialized))
        size = len(canonical(view))
        if total + size <= data_budget:
            selected.append(copy.deepcopy(view))
            total += size
        else:
            omitted_total += 1
            if len(canonical(omitted)) < 1800:
                omitted.append(eid)
    result = {'records': selected, 'omitted_ids': omitted, 'omitted_count': omitted_total,
              'omitted_ids_complete': len(omitted) == omitted_total,
              'complete': not omitted_total and not any(r.get('content_not_inlined') for r in selected),
              'available_count': len(ids), 'character_budget': budget}
    while len(canonical(result)) > budget and result['omitted_ids']:
        result['omitted_ids'].pop()
        result['omitted_ids_complete'] = False
    while len(canonical(result)) > budget and result['records']:
        result['records'].pop()
        result['omitted_count'] += 1
        result['complete'] = False
        result['omitted_ids_complete'] = False
    return result


def _context(plan, store):
    from .planning import initial_reference_ids
    catalog = json.loads((store.bundle / 'task.json').read_text(encoding='utf-8'))
    reference_ids, selection = initial_reference_ids(
        store, catalog['task'].get('request', ''), plan.get('selection_policy', {}).get('small_table_limit', 40))
    active_tools = {r.get('data', {}).get('tool') for eid, r in store.records.items()
                    if eid.startswith(('run:tool_call:', 'run:tool_attempt:')) and isinstance(r.get('data', {}).get('tool'), str)}
    active_tools.discard(None)
    links = tool_contract_bindings(store)
    contract_ids = list(dict.fromkeys(c['evidence_id'] for link in links for c in link['contracts']))
    reference_ids = list(dict.fromkeys(
        [eid for eid in contract_ids if store.records[eid]['scope'] in _REFERENCE_SCOPES] + reference_ids))
    priority = {'run_status': 0, 'final_answer': 1, 'trace_event': 4,
                'runtime_request': 2, 'interaction': 6, 'trace_payload': 7,
                'artifact': 8, 'resource_usage': 9, 'trace_payload_part': 10}
    answer = store.records.get('run:final_answer', {}).get('data')
    chat_input_ids = {r['data'].get('payload_refs', {}).get('input')
                      for r in store.records.values() if r['kind'] == 'trace_event'
                      and r['data'].get('name') == 'agentos.llm.call'
                      and r['data'].get('attributes', {}).get('app.purpose') == 'chat'}

    def evidence_priority(eid):
        record = store.records[eid]
        if eid.startswith(('run:tool_call:', 'run:tool_attempt:')):
            rank = 2
        elif record['kind'] == 'trace_payload' and isinstance(answer, str) and record['data'] == answer:
            rank = 1
        elif eid in chat_input_ids:
            rank = 3
        elif record['kind'] == 'tool_contract':
            data = record.get('data', {})
            function = data.get('function', data)
            name = function.get('name', function.get('runtime_tool_name')) if isinstance(function, dict) else None
            # A large menu of available tools must not crowd out actual calls.
            rank = 3 if name in active_tools else 11
        else:
            rank = priority.get(record['kind'], 12)
        return rank, 0 if record.get('slot') == 'input' else 1

    observation_ids = sorted((eid for eid, r in store.records.items() if r['scope'] == 'observation'),
                             key=evidence_priority)
    observation_ids = list(dict.fromkeys(
        [eid for eid in ('run:status', 'run:final_answer') if eid in store.records] +
        [link['call_evidence_id'] for link in links] +
        [eid for eid in contract_ids if store.records[eid]['scope'] == 'observation'] + observation_ids))
    link_view = {'calls': [], 'omitted_count': 0, 'complete': True}
    for link in links:
        link_view['calls'].append(link)
        if len(canonical(link_view)) > 7900:
            link_view['calls'].pop()
            link_view['omitted_count'] += 1
            link_view['complete'] = False
    return {
        'task': copy.deepcopy(catalog['task']), 'reference': copy.deepcopy(catalog['reference']),
        'profiles': copy.deepcopy(plan['profiles']), 'configured_methods': copy.deepcopy(plan.get('methods', {})),
        'evidence': {'reference': _packet(store.records, reference_ids, 22000),
                     'observation': _packet(store.records, observation_ids, 34000),
                     'tool_bindings': link_view,
                     'run_inventory': _run_inventory(store),
                     'reference_selection': selection,
                     'note': 'Bounded views. Previews/omissions are incomplete; query/read by ID before deciding.'},
        'evidence_access': {'allowed_scopes': ['task', 'reference', 'observation'],
                            'note': 'Query scope/kind and read IDs; origin.pointer addresses audit/original-input.json.'},
    }


def _run_inventory(store, budget=16000):
    """Expose event/payload locators across the run without copying large values."""
    counts, events, contracts = {}, [], {}
    groups = {
        'deliveries': {'scope': 'observation', 'kinds': ['final_answer', 'artifact']},
        'interactions': {'scope': 'observation', 'kinds': ['interaction']},
        'tool_attempts': {'scope': 'observation', 'kinds': ['trace_payload']},
        'error_payloads': {'scope': 'observation', 'kinds': ['trace_payload']},
        'resource_usage': {'scope': 'observation', 'kinds': ['resource_usage']},
        'budgets': {'scope': 'reference', 'kinds': ['budget']},
    }
    group_ids = {name: [] for name in groups}
    kind_groups = {'final_answer': 'deliveries', 'artifact': 'deliveries',
                   'interaction': 'interactions', 'resource_usage': 'resource_usage'}
    for record in store.records.values():
        if record['scope'] == 'reference' and record['kind'] == 'budget':
            group_ids['budgets'].append(record['id'])
        if record['scope'] != 'observation':
            continue
        kind = record['kind']
        counts[kind] = counts.get(kind, 0) + 1
        if kind in kind_groups:
            group_ids[kind_groups[kind]].append(record['id'])
        if record['id'].startswith('run:tool_attempt:'):
            group_ids['tool_attempts'].append(record['id'])
        if record.get('slot') == 'error':
            group_ids['error_payloads'].append(record['id'])
        if kind == 'trace_event':
            data = record['data']
            events.append({'evidence_id': record['id'], **{
                key: copy.deepcopy(data[key]) for key in
                ('name', 'parent_span_id', 'start_time_unix_nano', 'end_time_unix_nano',
                 'status', 'outcome', 'payload_refs') if key in data}})
            if data.get('attributes', {}).get('app.purpose'):
                events[-1]['purpose'] = data['attributes']['app.purpose']
        elif kind == 'tool_contract':
            level = record['data'].get('contract_level', 'unspecified')
            contracts[level] = contracts.get(level, 0) + 1
    locators = {}
    for name, query in groups.items():
        ids = group_ids[name]
        selected = []
        for eid in ids:
            if len(canonical(selected + [eid])) > budget // 24:
                break
            selected.append(eid)
        locators[name] = {'ids': selected, 'available_count': len(ids),
                          'omitted_count': len(ids) - len(selected), 'complete': len(ids) == len(selected),
                          'query': query}
    locators['tool_attempts']['selection_note'] = 'Query returns a superset; inspect IDs with prefix run:tool_attempt:.'
    locators['error_payloads']['selection_note'] = 'Query returns a superset; inspect error slots. A payload locator alone does not confirm a fault.'
    result = {'record_counts': counts, 'contract_counts_by_level': contracts, 'locators': locators,
              'events': [], 'event_count': len(events), 'omitted_event_count': 0,
              'complete': all(item['complete'] for item in locators.values()),
              'recorded_request_id': 'run:request' if 'run:request' in store.records else None,
              'note': _fragment('prompts/bridge/fragments/instantiation.json', 'fragment_2')}
    for event in events:
        result['events'].append(event)
        if len(canonical(result)) > budget - 100:
            result['events'].pop()
            result['omitted_event_count'] += 1
            result['complete'] = False
    return result


def build_instantiation_request(base_plan, store):
    _profiles(base_plan)
    return {'stage': 'instantiate', 'system': INSTANTIATION_SYSTEM, 'user': canonical(_context(base_plan, store))}


def _tokens(path):
    if not isinstance(path, str) or not path.startswith('/') or re.search(r'~(?![01])', path):
        raise ValueError('Invalid JSON Pointer: ' + repr(path))
    return [token.replace('~1', '/').replace('~0', '~') for token in path[1:].split('/')]


def _resolve(value, tokens):
    for token in tokens:
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and str(int(token)) == token and int(token) < len(value):
            value = value[int(token)]
        else:
            raise KeyError(token)
    return value


def _path_value(path, original, *, actual=False, trusted=False):
    tokens = _tokens(path)
    if tokens[0] == 'evaluation' and not trusted:
        if len(tokens) != 4 or tokens[1] != 'checks' or tokens[3] not in ('score', 'status'):
            raise ValueError('Invalid evaluation dependency path: ' + path)
        return None
    if tokens[0] not in ('task', 'reference', 'observation'):
        raise ValueError('Forbidden program/applicability path: ' + path)
    if actual and tokens[0] != 'observation':
        raise ValueError('Actual observation cannot be replaced by task/reference: ' + path)
    if trusted and tokens[0] == 'observation':
        contract = original['reference'].get('observation_contract', {})
        trusted_paths = contract.get('trusted_observation_paths', []) if isinstance(contract, dict) else []
        if not isinstance(trusted_paths, list) or path not in trusted_paths:
            raise ValueError('Applicability needs an explicitly trusted observation path: ' + path)
    try:
        return _resolve(original, tokens)
    except KeyError as exc:
        raise ValueError('Path does not exist in this execution input: ' + path) from exc


def _binding_paths(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('schema', 'protocol_schema'):
                continue
            if key == 'path' or key.endswith('_path'):
                yield key, child
            elif key == 'variables' and isinstance(child, dict):
                for path in child.values():
                    yield 'reference_variable', path
            else:
                yield from _binding_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _binding_paths(child)


def _covered_observation(path, observations):
    for record in observations:
        origin = record.get('origin', {}).get('pointer', '')
        if origin == '/observation':
            tokens = _tokens(path)
            if len(tokens) > 1 and isinstance(record['data'], dict) and tokens[1] in record['data']:
                return True
        elif origin.startswith('/observation/') and (path == origin or path.startswith(origin + '/')):
            return True
    return False


def _schema_contracts(data):
    """Recognize declared tool parameter schemas; arbitrary example objects do not qualify."""
    if not isinstance(data, dict):
        return
    if data.get('contract_level') == 'capability':
        return
    function = data.get('function', data)
    if isinstance(function, dict):
        name = function.get('name', function.get('runtime_tool_name'))
        for key in ('parameters', 'input_schema', 'inputSchema'):
            if key in function:
                yield name, function[key]
    # A reference can explicitly contain a bare JSON Schema.
    if data.get('type') == 'object' and any(key in data for key in ('properties', 'required', '$schema')):
        yield None, data


def _event_time(event):
    value = event.get('start_time_unix_nano')
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _verified_tool_views(original):
    """Recompute derived objects, never trust model-supplied normalization metadata."""
    from .tool_observations import normalize_tool_observations
    regenerated = copy.deepcopy(original)
    normalize_tool_observations(regenerated)
    return regenerated['observation']


def _derived_object(original, views, key, index):
    try:
        supplied = original['observation'][key][index]
        actual = views[key][index]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('TU-2 missing deterministic tool adaptation') from exc
    if supplied != actual:
        raise ValueError('TU-2 normalized tool evidence differs from raw trace')
    return actual


def _contract_event(record, original, views=None):
    tokens = _tokens(record.get('origin', {}).get('pointer', ''))
    if len(tokens) == 3 and tokens[:2] == ['observation', 'tool_contracts'] and tokens[2].isdigit():
        views = _verified_tool_views(original) if views is None else views
        data = _derived_object(original, views, 'tool_contracts', int(tokens[2]))
        if record['data'] != data:
            raise ValueError('TU-2 normalized contract record differs from its source')
        index = data.get('provenance', {}).get('source_event_index')
        if type(index) is not int or not 0 <= index < len(original['observation']['events']):
            raise ValueError('TU-2 contract source event is missing')
        return original['observation']['events'][index]
    if len(tokens) != 8 or tokens[:2] != ['observation', 'events'] or not tokens[2].isdigit() or \
            tokens[3:7] != ['payloads', 'input', 'value', 'tools'] or not tokens[7].isdigit():
        raise ValueError('TU-2 observed contract needs its original input.tools or deterministic runtime source')
    try:
        return original['observation']['events'][int(tokens[2])]
    except IndexError as exc:
        raise ValueError('TU-2 contract source event is missing') from exc


def _available_runtime_contracts(tool_name, call_event, records, original, views=None):
    """Use explicit span ancestry or a non-conflicting available contract.

    Timestamps rule out future declarations. Without causal association, a set
    of different same-name schemas is ambiguous even if one is newer: unrelated
    concurrent model calls can expose different tool versions.
    """
    call_time = _event_time(call_event) if call_event is not None else None
    event_by_span = {event.get('span_id'): event for event in original['observation']['events']}
    ancestors = {call_event.get('span_id'): 0} if call_event is not None else {}
    parent = call_event.get('parent_span_id') if call_event is not None else None
    while parent and parent in event_by_span and parent not in ancestors:
        ancestors[parent] = len(ancestors)
        parent = event_by_span[parent].get('parent_span_id')
    available, future_contracts = [], []
    for eid, record in records.items():
        if record['scope'] != 'observation' or record['kind'] != 'tool_contract':
            continue
        for name, declared in _schema_contracts(record['data']):
            if name != tool_name:
                continue
            source_event = _contract_event(record, original, views)
            source_time = _event_time(source_event)
            future = call_time is not None and source_time is not None and source_time > call_time
            if future:
                future_contracts.append((eid, declared))
            else:
                available.append((eid, declared, ancestors.get(source_event.get('span_id'))))
    related = [item for item in available if item[2] is not None]
    if related:
        nearest = min(item[2] for item in related)
        available = [item for item in related if item[2] == nearest]
    return available, future_contracts


def _validate_contract_applicability(schema, tool_name, call_event, cited, records, original, views=None):
    if call_event is None:
        raise ValueError('TU-2 runtime schema needs the call invocation association; history capture is not invocation time')
    available, future = _available_runtime_contracts(tool_name, call_event, records, original, views)
    if any(eid in cited and declared == schema for eid, declared in future):
        raise ValueError('TU-2 cannot use a future tool schema for this call')
    if not any(eid in cited and declared == schema for eid, declared, _ in available):
        raise ValueError('TU-2 requires an applicable cited tool contract')
    if len({canonical(item[1]) for item in available}) != 1:
        raise ValueError('TU-2 has ambiguous same-name tool schemas; keep this check unresolved')


def _source_snapshot(record):
    return record.get('kind') == 'tool_contract' and record.get('scope') == 'reference' and (
        record.get('origin', {}).get('pointer', '').startswith('/reference/agent_tool_catalog/tools/')
        or record.get('data', {}).get('schema_origin') == 'source_declaration_projection')


def _validate_snapshot_scope(check, records):
    """A reference snapshot is usable without relabeling it as historical deployment."""
    # A bound reference is delivered to the judge even when it is omitted from
    # source_refs. Scope qualification must therefore follow all bound data,
    # not just the model's smaller list of normative citations.
    bound_ids = dict.fromkeys(check['reference_ids'] + check['observation_ids']
                             + [source['evidence_id'] for source in check['source_refs']])
    snapshots = [records[eid] for eid in bound_ids if eid in records and _source_snapshot(records[eid])]
    if not snapshots:
        return
    criterion = check['criterion']
    qualified = 'source snapshot' in criterion.lower() or '源码快照' in criterion or '源代码快照' in criterion
    for record in snapshots:
        data = record['data']
        identities = [data.get(key) for key in ('source_revision', 'snapshot_id') if isinstance(data.get(key), str) and data[key]]
        if not qualified or not identities or not any(value in criterion for value in identities):
            raise ValueError('Source snapshot rule needs explicit snapshot scope and revision/snapshot_id in criterion')
    if check['method'] in ('P', 'H') and check['bindings'].get('contract_basis') != 'source_snapshot':
        raise ValueError('Source snapshot P/H requires contract_basis=source_snapshot')


def _call_context(call_path, original, views=None):
    if not isinstance(call_path, str):
        raise ValueError('TU-2 program requires the actual call_path')
    tokens = _tokens(call_path)
    views = _verified_tool_views(original) if views is None else views
    if len(tokens) == 3 and tokens[:2] == ['observation', 'tool_attempts'] and tokens[2].isdigit():
        call = _derived_object(original, views, 'tool_attempts', int(tokens[2]))
        index = call['provenance']['invocation_event_index']
        event = original['observation']['events'][index] if type(index) is int else None
        return call, event, views
    if len(tokens) == 3 and tokens[:2] == ['observation', 'tool_calls'] and tokens[2].isdigit():
        call = _derived_object(original, views, 'tool_calls', int(tokens[2]))
        provenance = call.get('provenance', {})
        if provenance.get('execution_evidence') not in ('linked_tool_result', 'direct_tool_event'):
            raise ValueError('TU-2 normalized call lacks execution evidence')
        index = provenance.get('invocation_event_index', provenance.get('observed_action_event_index'))
        if index is None and provenance.get('execution_evidence') == 'direct_tool_event':
            index = provenance.get('source_event_index')
        event = original['observation']['events'][index] if type(index) is int and 0 <= index < len(original['observation']['events']) else None
        tool_name = call.get('tool')
    else:
        if len(tokens) != 6 or tokens[:2] != ['observation', 'events'] or not tokens[2].isdigit() or \
                tokens[3:] != ['payloads', 'input', 'value']:
            raise ValueError(_fragment('prompts/bridge/fragments/instantiation.json', 'fragment_3'))
        event = original['observation']['events'][int(tokens[2])]
        attrs = event.get('attributes', {})
        tool_name = attrs.get('app.tool.name') or attrs.get('app.tool_name')
        event_name = str(event.get('name', '')).lower()
        if not tool_name or 'llm' in event_name or 'model' in event_name:
            raise ValueError('TU-2 needs a tool execution/attempt marker, not a model action proposal')
    call = _path_value(call_path, original, actual=True)
    if not isinstance(call, dict):
        raise ValueError('Use the preserved tool_attempt record for malformed raw input')
    call = copy.deepcopy(call)
    for attempt in views.get('tool_attempts', []):
        if call_path in attempt['provenance']['source_pointers'] or (
                tokens[:2] == ['observation', 'tool_calls'] and attempt.get('linked_call_index') == int(tokens[2])):
            call['evaluation_id'] = attempt['evaluation_id']
            break
    return call, event, views


def _validate_tool_call(check, original, records):
    bindings = check['bindings']
    call, event, views = _call_context(bindings.get('call_path'), original)
    if bindings.get('protocol_only') is True:
        if not bindings['call_path'].startswith('/observation/tool_attempts/') or bindings.get('schema_only') is not True:
            raise ValueError('Protocol-only checks require a preserved attempt and schema_only:true')
        return
    tool_name = call.get('tool') or call.get('tool_hint')
    schema = bindings.get('schema')
    source_contracts, reference_match, snapshot_match, cited = [], False, False, set()
    for source in check['source_refs']:
        record = records.get(source['evidence_id'])
        if record and (record['scope'] in _REFERENCE_SCOPES or record['kind'] == 'tool_contract'):
            contracts = list(_schema_contracts(record['data']))
            source_contracts.extend(contracts)
            if record['scope'] in _REFERENCE_SCOPES and any(
                    declared == schema and (name is None or name == tool_name) for name, declared in contracts):
                reference_match = True
                snapshot_match = snapshot_match or _source_snapshot(record)
            elif record['scope'] == 'observation':
                cited.add(record['id'])
    if not isinstance(schema, (dict, bool)) or not any(
            declared == schema and (name is None or name == tool_name) for name, declared in source_contracts):
        raise ValueError('TU-2 schema must exactly match a cited contract for this tool')
    basis = bindings.get('contract_basis')
    if basis not in (None, 'runtime', 'source_snapshot'):
        raise ValueError('Unsupported TU-2 contract_basis')
    if basis == 'source_snapshot' and not snapshot_match:
        raise ValueError('Source snapshot basis needs its matching cited snapshot contract')
    if reference_match:
        available, _ = _available_runtime_contracts(tool_name, event, records, original, views)
        if any(declared != schema for _, declared, _ in available):
            raise ValueError('TU-2 reference contract conflicts with an applicable runtime tool schema')
    else:
        _validate_contract_applicability(schema, tool_name, event, cited, records, original, views)


def tool_contract_bindings(store):
    """Resolve candidates using the same call provenance and applicability as validation.

    Names select candidates only. Conflicts and unknown invocation associations
    never become an automatic deployment claim or a newest-version selection.
    """
    original, records = _original(store), store.records
    views = _verified_tool_views(original)
    result, seen = [], set()
    candidates = [(eid, record) for eid, record in records.items() if eid.startswith('run:tool_attempt:')]
    if not candidates:
        candidates = sorted(records.items(), key=lambda item: not item[0].startswith('run:tool_call:'))
    for eid, record in candidates:
        data = record.get('data')
        if record.get('scope') != 'observation' or not isinstance(data, dict):
            continue
        path = record.get('origin', {}).get('pointer', '')
        try:
            call, event, _ = _call_context(path, original, views)
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        identity = call.get('evaluation_id') or path
        if identity in seen:
            continue
        seen.add(identity)
        tool_name = call.get('tool') or call.get('tool_hint')
        available, future = _available_runtime_contracts(tool_name, event, records, original, views) if tool_name else ([], [])
        available = [item for item in available if isinstance(item[1], (dict, bool))]
        conflict = len({canonical(item[1]) for item in available}) > 1
        contracts = []
        for cid, schema, _ in available:
            contracts.append({'evidence_id': cid, 'basis': 'runtime', 'business_schema': True,
                              'applicability': 'invocation_unlinked' if event is None else
                              ('runtime_conflict' if conflict else 'available_runtime')})
        for cid, _ in future:
            contracts.append({'evidence_id': cid, 'basis': 'runtime', 'business_schema': True,
                              'applicability': 'future'})
        snapshots = []
        for cid, contract in records.items():
            if contract['kind'] != 'tool_contract':
                continue
            value = contract['data']
            function = value.get('function', value)
            if not tool_name or function.get('runtime_tool_name', function.get('name')) != tool_name:
                continue
            if value.get('contract_level') == 'capability':
                contracts.append({'evidence_id': cid, 'basis': 'runtime' if contract['scope'] == 'observation' else 'reference',
                                  'business_schema': False, 'applicability': 'capability_only'})
            if _source_snapshot(contract):
                for name, schema in _schema_contracts(value):
                    if name == tool_name and isinstance(schema, (dict, bool)):
                        snapshots.append((cid, schema))
                        contracts.append({'evidence_id': cid, 'basis': 'source_snapshot', 'business_schema': True,
                                          'applicability': 'conflicts_with_runtime' if any(s != schema for _, s, _ in available)
                                          else 'named_reference_only',
                                          'source_revision': value.get('source_revision'),
                                          'snapshot_id': value.get('snapshot_id'),
                                          'historical_version_match': value.get('historical_version_match')})
        preferred = [cid for cid, _, _ in available] if event is not None and not conflict else []
        if not available and len({canonical(s) for _, s in snapshots}) == 1:
            preferred = [cid for cid, _ in snapshots if any(records[cid]['data'].get(k)
                         for k in ('source_revision', 'snapshot_id'))]
        # Business schemas precede potentially long capability menus.
        contracts.sort(key=lambda c: (c['evidence_id'] not in preferred, not c['business_schema']))
        result.append({'call_evidence_id': eid, 'call_path': path, 'tool': tool_name,
                       'protocol_errors': call.get('protocol_errors', []),
                       'association_errors': call.get('association_errors', []),
                       'execution_evidence': call.get('execution_evidence'),
                       'contracts': contracts, 'preferred_schema_ids': list(dict.fromkeys(preferred)),
                       'scope_note': 'Source snapshots evaluate only their named version, not historical deployment.'})
    return result


class ToolSchemaCoverageError(ValueError):
    def __init__(self, gaps):
        self.gaps = gaps
        super().__init__('TU-2 omitted tool attempts or available business-schema checks: ' + canonical(gaps))


def _tool_schema_gaps(base_plan, store, candidate):
    """Check deterministic schema opportunities, not the wording of model prose."""
    profile = _profiles(base_plan)['TU-2']
    configured = base_plan.get('methods', {}).get('TU-2')
    if configured == 'J' or not set(profile['how']['allowed_methods']) & {'P', 'H'}:
        return []
    entry = next(r for r in candidate['rubrics'] if r['rubric_id'] == 'TU-2')
    checked_paths = {c['bindings'].get('call_path') for c in entry['checks'] if c['method'] in ('P', 'H')}
    original = _original(store)
    checked_calls = set()
    checked_schemas = set()
    for path in checked_paths:
        call, event, _ = _call_context(path, original)
        checked_calls.add(call.get('evaluation_id') or path)
        if any(c['bindings'].get('call_path') == path and not c['bindings'].get('protocol_only')
               for c in entry['checks'] if c['method'] in ('P', 'H')):
            checked_schemas.add(call.get('evaluation_id') or path)
    gaps = []
    for link in tool_contract_bindings(store):
        call, event, _ = _call_context(link['call_path'], original)
        key = call.get('evaluation_id') or link['call_path']
        observed = any(link['call_evidence_id'] in c['observation_ids'] for c in entry['checks'])
        has_defect = bool(link['protocol_errors'] or link['association_errors'])
        requires_schema = bool(link['preferred_schema_ids']) and not has_defect
        if (has_defect and key not in checked_calls) or (requires_schema and key not in checked_schemas) or (
                not has_defect and not requires_schema and not observed):
            gaps.append({k: link[k] for k in ('call_evidence_id', 'call_path', 'tool', 'preferred_schema_ids', 'scope_note')})
    return gaps


def _validate_numeric_oracle(bindings, original):
    configured = original['reference'].get('configured_program_bindings', {}).get('AC-3', {})
    if not isinstance(configured, dict):
        raise ValueError('AC-3 explicit program configuration must be an object')
    modes = [key for key in ('expected_path', 'expected', 'expression') if key in bindings]
    if len(modes) != 1:
        raise ValueError('AC-3 requires exactly one trusted expected_path, expected, or expression')
    mode = modes[0]
    if mode == 'expected_path':
        if _tokens(bindings[mode])[0] not in _REFERENCE_SCOPES:
            raise ValueError('AC-3 expected_path must use trusted task/reference truth')
    elif mode == 'expected':
        if 'expected' not in configured or canonical(bindings['expected']) != canonical(configured['expected']):
            raise ValueError(_fragment('prompts/bridge/fragments/instantiation.json', 'fragment_4'))
    else:
        if 'expression' not in configured or bindings['expression'] != configured['expression'] or \
                canonical(bindings.get('variables', {})) != canonical(configured.get('variables', {})):
            raise ValueError('AC-3 expression and variables must match an explicit reference recipe')
    for key in ('abs_tol', 'rel_tol'):
        value = bindings.get(key, 0)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('AC-3 tolerance must be a finite nonnegative number')
        if key in configured:
            if value != configured[key]:
                raise ValueError('AC-3 tolerance must match explicit reference configuration: ' + key)
        elif value != 0:
            raise ValueError('AC-3 nonzero tolerance needs explicit reference configuration: ' + key)
    for key, default in (('expected_unit', None), ('actual_unit', None), ('unit_conversions', {})):
        value = bindings.get(key, default)
        if canonical(value) != canonical(configured.get(key, default)):
            raise ValueError('AC-3 units/conversions must match explicit reference configuration: ' + key)


def _validate_bindings(check, original, records, profile):
    bindings, dependencies = check['bindings'], set()
    observations = [records[eid] for eid in check['observation_ids']]
    for key, path in _binding_paths(bindings):
        _path_value(path, original, actual=key in ('actual_path', 'actual_unit_path', 'call_path'))
        tokens = _tokens(path)
        if key in ('reference_variable', 'limit_path') and tokens[0] not in _REFERENCE_SCOPES:
            raise ValueError('Reference formula or budget cannot use observations: ' + path)
        if tokens[0] == 'observation' and not _covered_observation(path, observations):
            raise ValueError('Observation binding needs its target record in observation_ids: ' + path)
        if tokens[0] == 'evaluation':
            dependencies.add(tokens[2])
    if profile['id'] == 'AC-3':
        actual_path = bindings.get('actual_path')
        if not isinstance(actual_path, str):
            raise ValueError('AC-3 needs an actual numeric observation path')
        actual = _path_value(actual_path, original, actual=True)
        if type(actual) not in (int, float) or not math.isfinite(actual):
            raise ValueError('AC-3 needs an actual numeric field; prose requires an extractor and stays unresolved')
        _validate_numeric_oracle(bindings, original)
    if profile['id'] == 'TU-2' and check['method'] in ('P', 'H'):
        _validate_tool_call(check, original, records)
    return dependencies


def _string_list(value, label, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(label + ' must be a ' + ('nonempty ' if nonempty else '') + 'list')
    for item in value:
        _text(item, label)
    if len(set(value)) != len(value):
        raise ValueError(label + ' must not contain duplicates')
    return value


def validate_candidate(base_plan, store, response):
    """Validate declarative output against this execution and fixed rubric contracts."""
    _exact(response, {'rubrics'}, 'Candidate')
    profiles, original, records = _profiles(base_plan), _original(store), store.records
    if not isinstance(response['rubrics'], list):
        raise ValueError('Candidate rubrics must be a list')
    by_rubric, ids, dependencies = {}, set(), {}
    for entry in response['rubrics']:
        _exact(entry, {'rubric_id', 'checks', 'unresolved'}, 'Rubric entry')
        rid = _text(entry['rubric_id'], 'rubric_id')
        if rid not in profiles or rid in by_rubric:
            raise ValueError('Unknown or duplicate rubric: ' + rid)
        profile = profiles[rid]
        _string_list(entry['unresolved'], rid + '.unresolved')
        if not isinstance(entry['checks'], list) or (not entry['checks'] and not entry['unresolved']):
            raise ValueError(rid + ' requires checks or explicit unresolved prerequisites')
        for check in entry['checks']:
            _exact(check, _CHECK_KEYS, 'Check')
            cid = _text(check['id'], 'check.id')
            if cid in ids or '/' in cid or '~' in cid:
                raise ValueError('Duplicate or unsafe check ID: ' + cid)
            ids.add(cid)
            _text(check['criterion'], cid + '.criterion')
            method = check['method']
            if method not in profile['how']['allowed_methods']:
                raise ValueError('Method forbidden by rubric ' + rid)
            if base_plan.get('methods', {}).get(rid, method) != method:
                raise ValueError('Method differs from configured method for ' + rid)
            if not isinstance(check['bindings'], dict):
                raise ValueError(cid + '.bindings must be an object')
            if method == 'J' and check['bindings']:
                raise ValueError('J check must have empty bindings: ' + cid)
            if method in ('P', 'H') and not check['bindings']:
                raise ValueError('P/H check requires program bindings: ' + cid)
            if method == 'P':
                if check['prompt'] is not None:
                    raise ValueError('P check prompt must be null: ' + cid)
            else:
                _text(check['prompt'], cid + '.prompt')
                if check['prompt'] in (profile['how'].get('judge_rules'), profile['how'].get('instantiate_prompt')):
                    raise ValueError('Prompt must be instantiated for a concrete object: ' + cid)
            allowed_kinds = set(profile['where']) | {'trace_payload_part', 'run_status'}
            _string_list(check['evidence_kinds'], cid + '.evidence_kinds', nonempty=True)
            if set(check['evidence_kinds']) - allowed_kinds:
                raise ValueError('Evidence kind outside rubric scope: ' + cid)
            for key, scopes in (('reference_ids', _REFERENCE_SCOPES), ('observation_ids', {'observation'})):
                _string_list(check[key], cid + '.' + key)
                for eid in check[key]:
                    if eid not in records or records[eid]['scope'] not in scopes:
                        raise ValueError(key + ' contains an unknown or wrong-scope record: ' + eid)
            for eid in check['observation_ids']:
                if records[eid]['kind'] not in allowed_kinds or records[eid]['kind'] not in check['evidence_kinds']:
                    raise ValueError('Observed record kind is outside the check evidence scope: ' + eid)
            if not isinstance(check['required_evidence'], list):
                raise ValueError('required_evidence must be AND groups of OR alternatives: ' + cid)
            for group in check['required_evidence']:
                _string_list(group, cid + '.required_evidence group', nonempty=True)
                if set(group) - set(check['evidence_kinds']):
                    raise ValueError('Required evidence must be included in evidence_kinds: ' + cid)
            if check['when'] is not None:
                _exact(check['when'], {'path', 'op', 'expected'}, 'when')
                _text(check['when']['op'], 'when.op')
                if check['when']['op'] not in _WHEN_OPS:
                    raise ValueError('Unsupported applicability operator: ' + cid)
                _path_value(check['when']['path'], original, trusted=True)
            if not isinstance(check['source_refs'], list) or not check['source_refs']:
                raise ValueError('Every check needs source_refs: ' + cid)
            reference_source = False
            for source_index, source in enumerate(check['source_refs']):
                _exact(source, {'evidence_id', 'quote'}, 'source_ref')
                eid, quote = _text(source['evidence_id'], 'source_ref.evidence_id'), _text(source['quote'], 'source_ref.quote')
                if eid == 'rubric:' + rid:
                    data = profile
                elif eid in records and eid in check['reference_ids']:
                    data, reference_source = records[eid]['data'], True
                elif eid in records and eid in check['observation_ids'] and records[eid]['kind'] == 'tool_contract':
                    data = records[eid]['data']
                else:
                    raise ValueError('Rule source must be task/reference/rubric or an observed tool_contract: ' + eid)
                if not _quote_matches(quote, data):
                    raise SourceQuoteError(rid, cid, source_index, eid, quote, data)
            if rid in ('TC-1', 'EG-3') and not reference_source:
                raise ValueError(rid + ' requires a task/reference rule source: ' + cid)
            if rid in ('AC-1', 'AC-2') and not any(
                    records[eid]['kind'] in ('final_answer', 'artifact')
                    for eid in check['observation_ids']):
                raise ValueError(rid + ' requires delivered observation_ids: ' + cid)
            if rid in ('TU-1', 'TU-2', 'TU-3') and not any(
                    records[eid]['kind'] in ('trace_event', 'trace_payload', 'trace_payload_part')
                    for eid in check['observation_ids']):
                raise ValueError(rid + ' requires tool observation_ids: ' + cid)
            if rid in ('TU-1', 'TU-2'):
                _validate_snapshot_scope(check, records)
            dependencies[cid] = _validate_bindings(check, original, records, profile)
        by_rubric[rid] = copy.deepcopy(entry)
    if set(by_rubric) != set(profiles):
        raise ValueError('Candidate must cover all 22 rubrics; missing: ' + ', '.join(sorted(set(profiles) - set(by_rubric))))
    for cid, targets in dependencies.items():
        if targets - ids or cid in targets:
            raise ValueError('Missing or self-referencing check dependency: ' + cid)
    visited, visiting = set(), set()

    def visit(cid):
        if cid in visiting:
            raise ValueError('Cyclic check dependency: ' + cid)
        if cid not in visited:
            visiting.add(cid)
            for target in dependencies[cid]:
                visit(target)
            visiting.remove(cid)
            visited.add(cid)

    for cid in dependencies:
        visit(cid)
    result = {'rubrics': [by_rubric[p['id']] for p in base_plan['profiles']]}
    canonical(result)
    return result


def _flatten(plan, candidate):
    profiles = _profiles(plan)
    checks = [dict(copy.deepcopy(check), rubric_id=entry['rubric_id'], metric_id=profiles[entry['rubric_id']]['metric_id'])
              for entry in candidate['rubrics'] for check in entry['checks']]
    unresolved = [{'rubric_id': entry['rubric_id'], 'missing': copy.deepcopy(entry['unresolved'])}
                  for entry in candidate['rubrics'] if entry['unresolved']]
    return checks, unresolved


def _validation(candidate):
    return {'method': 'local', 'rubric_count': len(candidate['rubrics']),
            'check_count': sum(len(entry['checks']) for entry in candidate['rubrics']),
            'coverage_scope': _fragment('prompts/bridge/fragments/instantiation.json', 'fragment_5'),
            'checks': ['schema', 'coverage', 'methods', 'sources', 'run_bindings', 'dependencies']}


def freeze_plan(base_plan, store, candidate, provenance=None):
    from .planning import run_fingerprint
    candidate = validate_candidate(base_plan, store, candidate)
    plan = copy.deepcopy(base_plan)
    plan.update(schema_version='agentos-rubric-plan/4', stage='frozen', run_fingerprint=run_fingerprint(store))
    plan['task_checks'], plan['unresolved'] = _flatten(plan, candidate)
    plan['instantiation'] = {'candidate': candidate, 'candidate_hash': digest(candidate),
                             'validation': _validation(candidate), 'provenance': copy.deepcopy(provenance)}
    plan['plan_hash'] = digest({k: v for k, v in plan.items() if k != 'plan_hash'})
    return plan


def instantiate_plan(base_plan, store, generator_fn, request_sink=None, provenance=None, degrade=False,
                     normalise=False):
    """One generation stage, with bounded correction from local diagnostics.

    When local validation pinpoints individual rubrics, the correction asks the
    model to replace only those rubric entries instead of rewriting the whole
    candidate, and it may repeat while the failures keep shrinking. A response
    that is not a repair payload is still read as a full candidate, so the
    original one-shot contract remains valid.
    """
    request = build_instantiation_request(base_plan, store)
    repairing = False
    previous = None
    iteration = 0
    repairs = 0
    last_errors = None
    edits = []

    def _provenance():
        meta = copy.deepcopy(provenance) if isinstance(provenance, dict) else {}
        if normalise:
            meta['normalisation'] = {'applied': bool(edits), 'edits': copy.deepcopy(edits)}
        return meta
    while True:
        iteration += 1
        if request_sink is not None:
            if callable(request_sink):
                request_sink(copy.deepcopy(request))
            else:
                request_sink.append(copy.deepcopy(request))
        response = generator_fn(copy.deepcopy(request))
        merged = _merge_repair(previous, response) if repairing else None
        candidate = merged if merged is not None else response
        if normalise:
            candidate = _normalise_candidate(base_plan, store, candidate, edits)
        try:
            candidate = validate_candidate(base_plan, store, candidate)
        except ValueError as exc:
            diagnostics = _candidate_diagnostics(base_plan, store, candidate)
            progress = last_errors is None or len(diagnostics) < last_errors
            if iteration == 1 or (repairs < _REPAIR_ROUNDS and progress):
                target = _repair_request(base_plan, request, candidate, diagnostics)
                if target is not None:
                    if iteration > 1:
                        repairs += 1
                    last_errors = len(diagnostics)
                    repairing, previous, request = True, candidate, target
                    continue
            if iteration == 1:
                body = json.loads(request['user'])
                body['local_feedback'] = {'code': 'local_validation_failed', 'error': str(exc),
                    'check_errors': diagnostics,
                    'instruction': _fragment('prompts/bridge/fragments/instantiation.json', 'fragment_6')}
                body['previous_candidate'] = candidate
                request = {**request, 'user': canonical(body)}
                continue
            if degrade:
                return _freeze_degraded(base_plan, store, candidate, exc, _provenance())
            raise
        gaps = _tool_schema_gaps(base_plan, store, candidate)
        if not gaps:
            return freeze_plan(base_plan, store, candidate, _provenance())
        if iteration > 1:
            if degrade:
                return _freeze_degraded(base_plan, store, candidate, ToolSchemaCoverageError(gaps), _provenance())
            raise ToolSchemaCoverageError(gaps)
        repairing, previous = False, None
        body = json.loads(request['user'])
        body.pop('repair', None)
        body['local_feedback'] = {'code': 'available_tool_schema_omitted', 'gaps': gaps,
            'instruction': _fragment('prompts/bridge/fragments/instantiation.json', 'fragment_7')}
        body['previous_candidate'] = candidate
        request = {**request, 'user': canonical(body)}


def _normalise_candidate(base_plan, store, candidate, edits=None):
    """Repair mechanically invalid evidence wiring without touching the semantics.

    Drops evidence kinds and citations that cannot resolve in this execution,
    declares task/reference records that a check already cites, derives the kind
    of each declared observation, and turns a check that keeps no usable
    citation into an explicit unresolved gap instead of a failed generation.
    """
    if not isinstance(candidate, dict) or not isinstance(candidate.get('rubrics'), list):
        return candidate
    profiles, records = _profiles(base_plan), store.records
    normalised = copy.deepcopy(candidate)
    for entry in normalised['rubrics']:
        if not isinstance(entry, dict):
            continue
        profile = profiles.get(entry.get('rubric_id'))
        if profile is None or not isinstance(entry.get('checks'), list):
            continue
        allowed = set(profile['where']) | {'trace_payload_part', 'run_status'}
        kept, dropped = [], []
        for check in entry['checks']:
            if not isinstance(check, dict) or set(check) != set(_CHECK_KEYS):
                kept.append(check)
                continue
            kinds = [kind for kind in check['evidence_kinds'] if kind in allowed]
            references = [eid for eid in check['reference_ids']
                          if eid in records and records[eid]['scope'] in _REFERENCE_SCOPES]
            observations = []
            for eid in check['observation_ids']:
                if eid not in records or records[eid]['scope'] != 'observation':
                    continue
                kind = records[eid]['kind']
                if kind not in allowed:
                    continue
                observations.append(eid)
                if kind not in kinds:
                    kinds.append(kind)
            sources = []
            for source in check['source_refs']:
                if not isinstance(source, dict):
                    continue
                eid = source.get('evidence_id')
                if eid == 'rubric:' + entry['rubric_id']:
                    sources.append(source)
                elif eid in records and (records[eid]['scope'] in _REFERENCE_SCOPES
                                         or records[eid]['kind'] == 'tool_contract'):
                    sources.append(source)
                    if records[eid]['scope'] in _REFERENCE_SCOPES and eid not in references:
                        references.append(eid)
            before = (list(check['evidence_kinds']), list(check['reference_ids']),
                      list(check['observation_ids']), list(check['source_refs']),
                      copy.deepcopy(check['required_evidence']))
            groups = [[kind for kind in group if kind in kinds]
                      for group in check['required_evidence'] if isinstance(group, list)]
            check['evidence_kinds'] = kinds
            check['reference_ids'] = references
            check['observation_ids'] = observations
            check['required_evidence'] = [group for group in groups if group]
            check['source_refs'] = sources
            if edits is not None and before != (kinds, references, observations,
                                                sources, check['required_evidence']):
                edits.append({'rubric_id': entry['rubric_id'], 'check_id': str(check.get('id')),
                              'evidence_kinds': sorted(set(before[0]) - set(kinds)),
                              'reference_ids': sorted(set(references) - set(before[1])),
                              'observation_ids': sorted(set(before[2]) - set(observations)),
                              'source_refs': len(before[3]) - len(sources),
                              'required_evidence': len(before[4]) - len(check['required_evidence'])})
            if not kinds or not sources or not (references or observations):
                dropped.append(str(check.get('id')))
                if edits is not None:
                    edits.append({'rubric_id': entry['rubric_id'], 'check_id': str(check.get('id')),
                                  'dropped': True})
                continue
            kept.append(check)
        entry['checks'] = kept
        if dropped:
            notes = entry.get('unresolved')
            notes = list(notes) if isinstance(notes, list) else []
            notes.append('机械性归一化：' + '、'.join(dropped) + ' 没有可解析的证据引用，已作为缺口保留。')
            entry['unresolved'] = notes
    return normalised


def _repair_targets(base_plan, candidate, diagnostics):
    """Rubrics whose entries must be regenerated, with what the model needs to see.

    A rubric is a repair target when one of its checks failed local validation,
    or when the entry carries neither checks nor an explicit unresolved reason.
    """
    entries = {}
    if isinstance(candidate, dict) and isinstance(candidate.get('rubrics'), list):
        for entry in candidate['rubrics']:
            if isinstance(entry, dict) and isinstance(entry.get('rubric_id'), str):
                entries[entry['rubric_id']] = entry
    targets = []
    for profile in base_plan['profiles']:
        rubric_id = profile['id']
        entry = entries.get(rubric_id) or {}
        checks = entry.get('checks') if isinstance(entry.get('checks'), list) else []
        unresolved = entry.get('unresolved') if isinstance(entry.get('unresolved'), list) else []
        failures = [item for item in diagnostics if item.get('rubric_id') == rubric_id]
        empty = not checks and not unresolved
        if not failures and not empty:
            continue
        targets.append({'rubric_id': rubric_id,
                        'checks': copy.deepcopy(checks),
                        'unresolved': copy.deepcopy(unresolved),
                        'failures': copy.deepcopy(failures),
                        'empty': empty})
    return targets


def _repair_request(base_plan, request, candidate, diagnostics):
    """Build a correction request that asks only for the failing rubrics.

    Returns None when no rubric can be attributed, so the caller falls back to
    the whole-candidate correction.
    """
    if not isinstance(candidate, dict):
        return None
    targets = _repair_targets(base_plan, candidate, diagnostics)
    if not targets:
        return None
    body = json.loads(request['user'])
    body.pop('previous_candidate', None)
    body['local_feedback'] = {'code': 'local_validation_failed',
        'error': 'Only the rubrics listed under repair.rubrics failed local validation.',
        'check_errors': diagnostics,
        'instruction': _fragment('prompts/bridge/fragments/instantiation.json', 'fragment_6')}
    body['repair'] = {'mode': 'rubrics_only',
        'instruction': _fragment('prompts/bridge/fragments/instantiation.json', 'fragment_8'),
        'rubrics': targets}
    system = _fragment('prompts/bridge/fragments/instantiation.json', 'fragment_9') + request['system']
    return {**request, 'system': system, 'user': canonical(body)}


def _merge_repair(previous, response):
    """Splice repaired rubric entries back into the previous candidate.

    Returns None when the response is not a repair payload, or when an entry
    cannot be placed by rubric id.
    """
    if not isinstance(response, dict) or set(response) != {'repair'}:
        return None
    payload = response['repair']
    if not isinstance(payload, dict) or set(payload) != {'rubrics'}:
        return None
    entries = payload['rubrics']
    if not isinstance(entries, list) or not entries:
        return None
    merged = copy.deepcopy(previous)
    by_id = {}
    for entry in merged.get('rubrics') or []:
        if isinstance(entry, dict) and isinstance(entry.get('rubric_id'), str):
            by_id[entry['rubric_id']] = entry
    for replacement in entries:
        if not isinstance(replacement, dict):
            return None
        rubric_id = replacement.get('rubric_id')
        if rubric_id not in by_id:
            return None
        checks = replacement.get('checks') or []
        if not isinstance(checks, list) or not all(isinstance(item, dict) for item in checks):
            return None
        target = by_id[rubric_id]
        kept = {item['id']: item for item in target.get('checks') or []
                if isinstance(item, dict) and isinstance(item.get('id'), str)}
        for item in checks:
            kept.pop(item.get('id'), None)
        target['checks'] = checks + list(kept.values())
        if isinstance(replacement.get('unresolved'), list):
            target['unresolved'] = replacement['unresolved']
    return merged


def _prune_candidate(base_plan, store, candidate, error):
    """Keep checks that pass isolated validation and turn the rest into explicit gaps."""
    profiles = _profiles(base_plan)
    dropped, by_rubric = [], {}
    entries = candidate.get("rubrics") if isinstance(candidate, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("rubric_id")
        if rid not in profiles or rid in by_rubric:
            continue
        unresolved = [item for item in (entry.get("unresolved") or []) if isinstance(item, str)]
        checks = []
        for check in entry.get("checks") or []:
            if not isinstance(check, dict):
                continue
            isolated = {"rubrics": [{"rubric_id": key, "checks": [check] if key == rid else [],
                                     "unresolved": ["Isolated local validation only."]} for key in profiles]}
            try:
                validate_candidate(base_plan, store, isolated)
            except ValueError as exc:
                dropped.append({"rubric_id": rid, "check_id": check.get("id"), "error": str(exc)})
                unresolved.append("check " + str(check.get("id")) + " dropped after local validation: " + str(exc))
                continue
            checks.append(check)
        if not checks and not unresolved:
            unresolved.append("Kept as unresolved after local validation: " + str(error))
        by_rubric[rid] = {"rubric_id": rid, "checks": checks, "unresolved": unresolved}
    for rid in profiles:
        if rid not in by_rubric:
            by_rubric[rid] = {"rubric_id": rid, "checks": [],
                              "unresolved": ["Not generated; local validation failed: " + str(error)]}
    for _ in range(12):
        trial = {"rubrics": [by_rubric[profile["id"]] for profile in base_plan["profiles"]]}
        try:
            return validate_candidate(base_plan, store, trial), dropped
        except ValueError as exc:
            message = str(exc)
            target = None
            for marker in ("Cyclic check dependency: ", "check dependency: "):
                if marker in message:
                    target = message.split(marker, 1)[1].strip()
                    break
            if not target:
                return None, dropped
            removed = False
            for entry in by_rubric.values():
                kept = [check for check in entry["checks"] if check.get("id") != target]
                if len(kept) != len(entry["checks"]):
                    entry["checks"] = kept
                    entry["unresolved"].append("check " + target + " dropped for a broken dependency: " + message)
                    removed = True
            if not removed:
                return None, dropped
    return None, dropped


def _freeze_degraded(base_plan, store, candidate, error, provenance):
    """Freeze a partial plan: valid checks survive, rejected ones become unresolved notes."""
    degraded, dropped = _prune_candidate(base_plan, store, candidate, error)
    if degraded is None:
        raise error
    meta = copy.deepcopy(provenance) if isinstance(provenance, dict) else {}
    meta["degradation"] = {"applied": True, "reason": str(error), "dropped_count": len(dropped),
                           "dropped_checks": dropped}
    return freeze_plan(base_plan, store, degraded, meta)


def _candidate_diagnostics(base_plan, store, candidate):
    """Report independent check errors together, so a bounded retry can fix them.

    Cross-check dependency errors are validated on the complete candidate, not
    inferred from these deliberately isolated diagnostics.
    """
    errors = []
    if not isinstance(candidate, dict) or not isinstance(candidate.get('rubrics'), list):
        return errors
    profiles = _profiles(base_plan)
    for entry in candidate['rubrics']:
        if not isinstance(entry, dict) or entry.get('rubric_id') not in profiles or not isinstance(entry.get('checks'), list):
            continue
        rid = entry['rubric_id']
        for check in entry['checks']:
            isolated = {'rubrics': [{'rubric_id': key, 'checks': [check] if key == rid else [],
                                     'unresolved': ['Isolated local validation only.']} for key in profiles]}
            try:
                validate_candidate(base_plan, store, isolated)
            except ValueError as exc:
                if 'check dependency' in str(exc):
                    continue
                diagnostic = {'rubric_id': rid, 'check_id': check.get('id') if isinstance(check, dict) else None,
                              'error': str(exc), 'allowed_evidence_kinds': profiles[rid]['where'] + ['trace_payload_part', 'run_status']}
                if isinstance(exc, SourceQuoteError):
                    diagnostic['source_quote'] = exc.diagnostic
                errors.append(diagnostic)
            if len(errors) >= 30:
                return errors
    return errors


def verify_instantiated(plan, store):
    from .planning import run_fingerprint
    if plan.get('schema_version') != 'agentos-rubric-plan/4' or plan.get('stage') != 'frozen':
        raise ValueError('Scoring requires a locally validated run-specific frozen plan')
    if digest({k: v for k, v in plan.items() if k != 'plan_hash'}) != plan.get('plan_hash'):
        raise ValueError('Invalid or modified frozen plan')
    if plan.get('run_fingerprint') != run_fingerprint(store):
        raise ValueError('Run/observation changed; instantiate this execution again')
    instantiation = plan.get('instantiation')
    _exact(instantiation, {'candidate', 'candidate_hash', 'validation', 'provenance'}, 'Instantiation')
    candidate = validate_candidate(plan, store, instantiation['candidate'])
    if candidate != instantiation['candidate'] or digest(candidate) != instantiation['candidate_hash']:
        raise ValueError('Instantiated candidate hash or normalization changed')
    if instantiation['validation'] != _validation(candidate):
        raise ValueError('Local validation record changed')
    checks, unresolved = _flatten(plan, candidate)
    if checks != plan.get('task_checks') or unresolved != plan.get('unresolved'):
        raise ValueError('Frozen task checks differ from the validated candidate')
    return True
