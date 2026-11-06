"""Deterministic, provenance-preserving tool views derived from original trace events.

The execution view links calls to runtime records. The evaluation inventory
also retains formal actions without execution, invalid parameters and conflicts.
Every inventory object points back to its original trace payloads.
"""
from __future__ import annotations
from .resources import literal as _fragment

import copy
import json


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _decoded(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value, object_pairs_hook=_pairs,
                          parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except (ValueError, TypeError):
        return None


def decode_arguments(value):
    """Decode only the declared native object/JSON or strict name/value_json wire form."""
    if isinstance(value, str):
        value = _decoded(value)
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if not isinstance(value, list):
        return None
    result = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {'name', 'value_json'}:
            return None
        name, encoded = item['name'], item['value_json']
        if not isinstance(name, str) or not name or name in result or not isinstance(encoded, str):
            return None
        try:
            result[name] = json.loads(encoded, object_pairs_hook=_pairs,
                                      parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
        except (ValueError, TypeError):
            return None
    return result


def _tool(item):
    if not isinstance(item, dict):
        return None
    function = item.get('function', item)
    if not isinstance(function, dict):
        return None
    name = function.get('name')
    if not isinstance(name, str) or not name.strip():
        return None
    result = {'name': name, 'description': function.get('description', ''), 'contract_level': 'capability'}
    for key in ('parameters', 'input_schema', 'inputSchema'):
        schema = function.get(key)
        if isinstance(schema, (dict, bool)):
            result.update(parameters=copy.deepcopy(schema), contract_level='business')
            break
    if 'output_schema' in function:
        result['output_schema'] = copy.deepcopy(function['output_schema'])
    return result


def _strict_calls(value):
    """Top-level action only; do not search quoted text, reasoning, or tool outputs."""
    value = _decoded(value)
    if not isinstance(value, dict):
        return []
    action = value.get('action')
    if not isinstance(action, dict) or action.get('kind') != 'tool_calls':
        return []
    calls = action.get('tool_calls')
    return calls if isinstance(calls, list) else []


def _call(item):
    if not isinstance(item, dict):
        return None
    function = item.get('function', item)
    if not isinstance(function, dict):
        return None
    name = function.get('name', item.get('tool'))
    args = decode_arguments(function.get('arguments'))
    call_id = item.get('tool_call_id', item.get('id'))
    if not isinstance(name, str) or not name or args is None:
        return None
    if call_id is not None and (not isinstance(call_id, str) or not call_id):
        return None
    return {'id': call_id, 'tool': name, 'arguments': args}


def _time(event):
    value = event.get('start_time_unix_nano')
    return int(value) if (type(value) is int or isinstance(value, str) and value.isdigit()) else None


def _model_event(event):
    name = str(event.get('name', '')).lower()
    attrs = event.get('attributes', {})
    return ('llm' in name or 'model' in name) and not (attrs.get('app.tool.name') or attrs.get('app.tool_name'))


def _contract_inputs(value, pointer, event_index):
    found = []
    tools = value.get('tools', [])
    if isinstance(tools, list):
        for ti, item in enumerate(tools):
            if contract := _tool(item):
                contract['provenance'] = {'source_event_index': event_index,
                    'source_pointer': f'{pointer}/tools/{ti}', 'source_type': 'input_tools'}
                found.append(contract)
    messages = value.get('messages', [])
    if not isinstance(messages, list):
        messages = []
    # The runtime protocol explicitly identifies the plain-message contract
    # channel. An arbitrary user JSON object is not sufficient on its own.
    for mi, message in enumerate(messages):
        following = messages[mi + 1] if mi + 1 < len(messages) else {}
        has_contract_channel = (isinstance(following, dict) and following.get('role') == 'system'
            and isinstance(following.get('content'), str) and 'tool_contracts' in following['content'])
        if not has_contract_channel or not isinstance(message, dict) or message.get('role') != 'user':
            continue
        data = _decoded(message.get('content'))
        if not isinstance(data, dict) or set(data) != {'type', 'tools'} or data.get('type') != 'tool_contracts' \
                or not isinstance(data['tools'], list):
            continue
        for ti, item in enumerate(data['tools']):
            if contract := _tool(item):
                contract['provenance'] = {'source_event_index': event_index,
                    'source_pointer': f'{pointer}/messages/{mi}/content',
                    'decoded_pointer': f'/tools/{ti}', 'source_type': 'message_tool_contracts'}
                found.append(contract)
    # AgentOS strict action schemas expose useful names/descriptions even when
    # their argument grammar is only the name/value_json transport format.
    response = value.get('response_format')
    if isinstance(response, dict) and response.get('type') == 'json_schema':
        wrapper = response.get('json_schema', {})
        schema = wrapper.get('schema', {}) if isinstance(wrapper, dict) else {}
        properties = schema.get('properties', {}) if isinstance(schema, dict) else {}
        action = properties.get('action', {}) if isinstance(properties, dict) else {}
        alternatives = action.get('anyOf', []) if isinstance(action, dict) else []
        for ai, alternative in enumerate(alternatives if isinstance(alternatives, list) else []):
            props = alternative.get('properties', {}) if isinstance(alternative, dict) else {}
            kind = props.get('kind', {})
            if not isinstance(kind, dict) or kind.get('enum') != ['tool_calls']:
                continue
            call_schema = props.get('tool_calls', {})
            items = call_schema.get('items', {}) if isinstance(call_schema, dict) else {}
            branches = items.get('anyOf', []) if isinstance(items, dict) else []
            for bi, branch in enumerate(branches if isinstance(branches, list) else []):
                bp = branch.get('properties', {}) if isinstance(branch, dict) else {}
                name_schema = bp.get('name', {}) if isinstance(bp, dict) else {}
                names = name_schema.get('enum', []) if isinstance(name_schema, dict) else []
                if not isinstance(names, list) or len(names) != 1 or not isinstance(names[0], str):
                    continue
                source = (_fragment('prompts/bridge/fragments/tool_observations.json', 'fragment_1').format(v0=(pointer), v1=(ai), v2=(bi)))
                found.append({'name': names[0], 'description': name_schema.get('description', ''),
                    'contract_level': 'capability', 'wire_call_schema': copy.deepcopy(branch),
                    'provenance': {'source_event_index': event_index, 'source_pointer': source,
                        'source_type': 'strict_action_schema',
                        'note': 'Wire schema is not the original business parameter schema.'}})
    return found


def normalize_tool_observations(original):
    """Rebuild derived arrays from raw events; injected pre-normalized data is ignored."""
    observation = original.setdefault('observation', {})
    events = observation.get('events', [])
    contracts, calls, rejected, proposals = [], {}, [], []

    def add_call(call, provenance):
        if not call['id']:
            return
        key = call['id']
        if key in calls:
            previous = calls[key]
            if previous is None:
                return
            if any(previous[k] != call[k] for k in ('tool', 'arguments')):
                calls[key] = None
                rejected.append({'call_id': key, 'reason': 'conflicting_call_id'})
                return
            old = previous['provenance']
            for field in ('source_pointers', 'result_pointers'):
                old[field] = list(dict.fromkeys(old.get(field, []) + provenance.get(field, [])))
            old['observed_in_event_indices'] = sorted(set(old.get('observed_in_event_indices', [old['source_event_index']])
                                                         + [provenance['source_event_index']]))
            return
        calls[key] = {**copy.deepcopy(call), 'provenance': provenance}

    for ei, event in enumerate(events):
        payloads = event.get('payloads', {})
        value = payloads.get('input', {}).get('value')
        pointer = f'/observation/events/{ei}/payloads/input/value'
        if not isinstance(value, dict):
            continue
        attrs = event.get('attributes', {})
        tool_name = attrs.get('app.tool.name') or attrs.get('app.tool_name')
        event_name = str(event.get('name', '')).lower()
        model_request = _model_event(event)
        if model_request:
            contracts.extend(_contract_inputs(value, pointer, ei))
        if isinstance(tool_name, str) and tool_name and 'llm' not in event_name and 'model' not in event_name:
            direct = _call(value)
            if direct and direct['tool'] == tool_name and direct['id']:
                result_pointers = [f'/observation/events/{ei}/payloads/{slot}/value'
                                   for slot in ('output', 'error') if slot in payloads]
                add_call(direct, {'source_event_index': ei, 'source_pointers': [pointer],
                    'result_pointers': result_pointers, 'execution_evidence': 'direct_tool_event',
                    'tool_event_span_id': event.get('span_id'), 'invocation_event_index': ei})
        messages = value.get('messages', [])
        if not model_request or not isinstance(messages, list):
            continue
        runtime_action_channel = any(isinstance(m, dict) and m.get('role') == 'system'
            and isinstance(m.get('content'), str) and 'tool_result' in m['content']
            and ('action' in m['content'] or 'tool_calls' in m['content']) for m in messages)
        pending, invalid_ids = {}, set()
        for mi, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            mp = f'{pointer}/messages/{mi}'
            if message.get('role') == 'assistant':
                native = message.get('tool_calls', [])
                items = native if isinstance(native, list) else []
                if runtime_action_channel:
                    items = items + _strict_calls(message.get('content'))
                for raw in items:
                    call = _call(raw)
                    if not call or not call['id']:
                        if isinstance(raw, dict):
                            bad_id = raw.get('tool_call_id', raw.get('id'))
                            if isinstance(bad_id, str) and bad_id:
                                invalid_ids.add(bad_id)
                        continue
                    cid = call['id']
                    if cid in pending and pending[cid][0] != call:
                        invalid_ids.add(cid)
                    pending[cid] = (call, mp, mi)
                continue
            result = None
            if message.get('role') == 'tool' and isinstance(message.get('tool_call_id'), str):
                result = {'tool_call_id': message['tool_call_id'], 'name': message.get('name'),
                          'status': 'recorded', 'content': message.get('content')}
            elif runtime_action_channel and message.get('role') == 'user':
                candidate = _decoded(message.get('content'))
                if isinstance(candidate, dict) and candidate.get('type') == 'tool_result' \
                        and set(candidate) <= {'type', 'tool_call_id', 'name', 'status', 'content', 'error', 'is_error'} \
                        and isinstance(candidate.get('name'), str) \
                        and candidate.get('status') in ('success', 'error', 'failed', 'failure'):
                    result = candidate
            if not result:
                continue
            cid = result.get('tool_call_id')
            if cid not in pending or cid in invalid_ids:
                continue
            call, cp, ci = pending[cid]
            if result.get('name') not in (None, call['tool']):
                invalid_ids.add(cid)
                rejected.append({'call_id': cid, 'reason': 'mismatched_result_tool', 'source_event_index': ei})
                continue
            add_call(call, {'source_event_index': ei, 'source_pointers': [cp], 'result_pointers': [mp],
                'message_index': ci, 'result_message_index': mi, 'execution_evidence': 'linked_tool_result',
                'result_status': result.get('status'),
                'invocation_event_index': None,
                'temporal_note': _fragment('prompts/bridge/fragments/tool_observations.json', 'fragment_2')})
        for cid in invalid_ids:
            calls[cid] = None

    # Link only a one-to-one prior model action and recorded call/result pair.
    # Equal name/arguments and temporal ordering alone are insufficient when a
    # proposal could explain multiple later call IDs, or vice versa.
    for ei, event in enumerate(events):
        if not _model_event(event):
            continue
        value = event.get('payloads', {}).get('output', {}).get('value')
        if not isinstance(value, dict):
            continue
        native = value.get('tool_calls', [])
        native = native if isinstance(native, list) else []
        candidates = [(raw, f'/observation/events/{ei}/payloads/output/value/tool_calls/{ni}')
                      for ni, raw in enumerate(native)]
        candidates.extend((raw, f'/observation/events/{ei}/payloads/output/value/content')
                          for raw in _strict_calls(value.get('content')))
        for raw, source_pointer in candidates:
            if call := _call(raw):
                proposals.append((ei, call, source_pointer))
    candidates_by_call, candidates_by_proposal = {}, {}
    for call in (item for item in calls.values() if item is not None):
        p = call['provenance']
        if p['execution_evidence'] != 'linked_tool_result':
            continue
        seen_time = _time(events[p['source_event_index']])
        matches = [pi for pi, (ei, proposal, _) in enumerate(proposals) if proposal['tool'] == call['tool']
                   and proposal['arguments'] == call['arguments']
                   and proposal['id'] in (None, call['id'])
                   and seen_time is not None and _time(events[ei]) is not None
                   and _time(events[ei]) < seen_time]
        candidates_by_call[call['id']] = matches
        for pi in matches:
            candidates_by_proposal.setdefault(pi, []).append(call['id'])
    for cid, matches in candidates_by_call.items():
        if len(matches) == 1 and len(candidates_by_proposal[matches[0]]) == 1:
            ei, _, source_pointer = proposals[matches[0]]
            p = calls[cid]['provenance']
            p['observed_action_event_index'] = ei
            p['invocation_event_index'] = ei
            p['action_source_pointer'] = source_pointer

    observation['tool_calls'] = [item for item in calls.values() if item is not None]
    observation['tool_contracts'] = contracts
    observation['tool_normalization'] = {'version': 'agentos-tool-observations/1',
        'call_count': len(observation['tool_calls']), 'contract_count': len(contracts), 'rejected': rejected,
        'note': _fragment('prompts/bridge/fragments/tool_observations.json', 'fragment_3')}
    from .tool_attempts import build_tool_attempts
    observation['tool_attempts'] = build_tool_attempts(observation)
    observation['tool_normalization']['attempt_count'] = len(observation['tool_attempts'])
    observation['tool_normalization']['version'] = 'agentos-tool-observations/2'
    return observation
