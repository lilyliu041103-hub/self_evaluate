"""Lossless inventory of formal tool actions, including invalid and unexecuted ones."""
from __future__ import annotations

import copy
import hashlib
import json

from .tool_observations import _decoded, _model_event, decode_arguments


def _action_items(value, pointer, strict_channel=False):
    """Return raw occurrences with their locations, without requiring valid fields."""
    if not isinstance(value, dict):
        if strict_channel and value is not None:
            yield value, pointer, '', 'action_envelope', ['invalid_action_envelope']
        return
    native = value.get('tool_calls')
    if native is not None:
        if isinstance(native, list):
            for index, item in enumerate(native):
                yield item, pointer + '/tool_calls/' + str(index), '', 'native', []
        else:
            yield native, pointer + '/tool_calls', '', 'action_envelope', ['tool_calls_not_array']
    content = value.get('content')
    decoded = _decoded(content)
    if decoded is None and isinstance(content, str) and content.strip() and strict_channel:
        yield content, pointer + '/content', '', 'action_envelope', ['invalid_action_json']
        return
    if not isinstance(decoded, dict):
        if strict_channel and content is not None:
            yield content, pointer + '/content', '', 'action_envelope', ['invalid_action_envelope']
        return
    action = decoded.get('action')
    if not isinstance(action, dict):
        if strict_channel:
            yield decoded, pointer + '/content', '', 'action_envelope', ['invalid_action_envelope']
        return
    if action.get('kind') != 'tool_calls':
        if 'tool_calls' in action:
            yield action, pointer + '/content', '/action', 'action_envelope', ['invalid_action_kind']
        return
    calls = action.get('tool_calls')
    if not isinstance(calls, list):
        yield calls, pointer + '/content', '/action/tool_calls', 'action_envelope', ['tool_calls_not_array']
        return
    if not calls:
        yield action, pointer + '/content', '/action', 'action_envelope', []
        return
    for index, item in enumerate(calls):
        yield item, pointer + '/content', '/action/tool_calls/' + str(index), 'strict_action', []


def build_tool_attempts(observation):
    """Keep each raw occurrence. Merge only copies linked by verified call provenance."""
    attempts = []
    events = observation.get('events', [])

    def add(raw, pointer, decoded_pointer, protocol, errors, event_index, tool_hint=None):
        location = pointer + '#' + decoded_pointer
        evaluation_id = 'attempt-' + hashlib.sha256(location.encode()).hexdigest()[:20]
        function = raw.get('function', raw) if isinstance(raw, dict) else {}
        function = function if isinstance(function, dict) else {}
        name = function.get('name', function.get('tool'))
        reported_id = raw.get('tool_call_id', raw.get('id')) if isinstance(raw, dict) else None
        arguments = decode_arguments(function.get('arguments'))
        issues = list(errors)
        if protocol != 'action_envelope':
            if not isinstance(raw, dict):
                issues.append('call_not_object')
            if not isinstance(name, str) or not name.strip():
                issues.append('missing_or_invalid_tool_name')
            if arguments is None:
                issues.append('missing_or_invalid_arguments_encoding')
            elif not isinstance(arguments, dict):
                issues.append('arguments_not_object')
            if protocol in ('native', 'tool_event') and (not isinstance(reported_id, str) or not reported_id.strip()):
                issues.append('missing_or_invalid_call_id')
        if tool_hint and name and name != tool_hint:
            issues.append('event_tool_name_mismatch')
        event = events[event_index]
        slot = 'output' if '/payloads/output/' in pointer else 'input'
        payload = event.get('payloads', {}).get(slot, {})
        raw_available = 'value' in payload and payload.get('complete') is not False and payload.get('truncated') is not True
        pointers = [f'/observation/events/{event_index}/payloads/{slot}/value'
                    for slot in event.get('payloads', {})]
        attempts.append({
            'id': evaluation_id, 'evaluation_id': evaluation_id,
            'reported_call_id': copy.deepcopy(reported_id), 'tool': name if isinstance(name, str) else None,
            'tool_hint': tool_hint, 'arguments': arguments, 'raw': copy.deepcopy(raw),
            'raw_available': raw_available,
            'protocol': protocol, 'protocol_errors': list(dict.fromkeys(issues)),
            'association_errors': [], 'execution_evidence': 'not_observed',
            'provenance': {'source_event_index': event_index,
                           'invocation_event_index': event_index if protocol == 'tool_event' or '/payloads/output/' in pointer else None,
                           'source_pointers': [pointer], 'payload_pointers': pointers,
                           'occurrences': [{'pointer': pointer, 'decoded_pointer': decoded_pointer,
                                            'raw': copy.deepcopy(raw)}], 'result_pointers': []}})
        if protocol == 'tool_event':
            attempts[-1]['execution_evidence'] = 'direct_tool_event'
            attempts[-1]['provenance']['result_pointers'] = [p for p in pointers if '/input/' not in p]
            attempts[-1]['result_status'] = 'error' if 'error' in event.get('payloads', {}) else 'recorded' if 'output' in event.get('payloads', {}) else 'unavailable'

    for index, event in enumerate(events):
        payloads = event.get('payloads', {})
        value = payloads.get('input', {}).get('value')
        attrs = event.get('attributes', {})
        tool_hint = attrs.get('app.tool.name') or attrs.get('app.tool_name')
        base = f'/observation/events/{index}/payloads'
        if tool_hint and not _model_event(event):
            add(value, base + '/input/value', '', 'tool_event', [], index, tool_hint)
        if not _model_event(event):
            continue
        response_format = value.get('response_format', {}) if isinstance(value, dict) else {}
        wrapper = response_format.get('json_schema', {}) if isinstance(response_format, dict) else {}
        schema = wrapper.get('schema', {}) if isinstance(wrapper, dict) else {}
        properties = schema.get('properties', {}) if isinstance(schema, dict) else {}
        action_schema = properties.get('action', {}) if isinstance(properties, dict) else {}
        variants = action_schema.get('anyOf', []) if isinstance(action_schema, dict) else []
        strict = False
        for variant in variants if isinstance(variants, list) else []:
            fields = variant.get('properties', {}) if isinstance(variant, dict) else {}
            kind = fields.get('kind', {}) if isinstance(fields, dict) else {}
            if isinstance(kind, dict) and kind.get('enum') == ['tool_calls']:
                strict = True
                break
        output = payloads.get('output', {}).get('value')
        for raw, pointer, decoded, protocol, errors in _action_items(output, base + '/output/value', strict):
            add(raw, pointer, decoded, protocol, errors, index)
        messages = value.get('messages', []) if isinstance(value, dict) else []
        for mi, message in enumerate(messages if isinstance(messages, list) else []):
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                continue
            for raw, pointer, decoded, protocol, errors in _action_items(message, base + f'/input/value/messages/{mi}'):
                add(raw, pointer, decoded, protocol, errors, index)

    # Link execution evidence without requiring it for admission to this inventory.
    merged = set()
    for ci, call in enumerate(observation.get('tool_calls', [])):
        provenance = call['provenance']
        action_pointer = provenance.get('action_source_pointer')
        source_pointers = provenance.get('source_pointers', [])
        matches = [a for a in attempts if a['tool'] == call['tool'] and a['arguments'] == call['arguments']
                   and any(any(o['pointer'] == p or o['pointer'].startswith(p + '/') for p in source_pointers)
                           or o['pointer'] == action_pointer
                           for o in a['provenance']['occurrences'])
                   and a['reported_call_id'] in (None, call['id'])]
        if not matches:
            continue
        primary = next((a for a in matches if action_pointer in a['provenance']['source_pointers']), matches[0])
        primary['id'] = call['id']
        primary['execution_evidence'] = provenance['execution_evidence']
        primary['result_status'] = provenance.get('result_status')
        primary['linked_call_index'] = ci
        primary['provenance']['invocation_event_index'] = provenance.get('invocation_event_index')
        primary['provenance']['result_pointers'] = copy.deepcopy(provenance.get('result_pointers', []))
        for other in matches:
            if other is primary:
                continue
            primary['provenance']['occurrences'].extend(other['provenance']['occurrences'])
            primary['provenance']['source_pointers'].extend(other['provenance']['source_pointers'])
            primary['provenance']['payload_pointers'].extend(other['provenance']['payload_pointers'])
            merged.add(other['evaluation_id'])
    attempts = [a for a in attempts if a['evaluation_id'] not in merged]
    by_id = {}
    for attempt in attempts:
        cid = attempt['reported_call_id']
        if isinstance(cid, str) and cid:
            by_id.setdefault(cid, []).append(attempt)
    for group in by_id.values():
        identities = {json.dumps([a['tool'], a['arguments']], sort_keys=True) for a in group}
        if len(identities) > 1:
            for attempt in group:
                attempt['association_errors'].append('conflicting_call_id')
    # Preserve mismatched returns with their call even when execution linking failed.
    for attempt in attempts:
        cid = attempt['reported_call_id']
        if not isinstance(cid, str) or not cid:
            continue
        for index, event in enumerate(events):
            value = event.get('payloads', {}).get('input', {}).get('value')
            messages = value.get('messages', []) if isinstance(value, dict) else []
            messages = messages if isinstance(messages, list) else []
            runtime_channel = any(isinstance(m, dict) and m.get('role') == 'system'
                                  and 'tool_result' in str(m.get('content', ''))
                                  for m in messages)
            for mi, message in enumerate(messages if isinstance(messages, list) else []):
                if not isinstance(message, dict):
                    continue
                result = message if message.get('role') == 'tool' else _decoded(message.get('content'))
                if not isinstance(result, dict) or result.get('tool_call_id') != cid:
                    continue
                if message.get('role') != 'tool' and result.get('type') != 'tool_result':
                    continue
                if message.get('role') != 'tool' and not runtime_channel:
                    continue
                pointer = f'/observation/events/{index}/payloads/input/value/messages/{mi}'
                attempt['provenance']['result_pointers'].append(pointer)
                if result.get('name') not in (None, attempt['tool']):
                    attempt['association_errors'].append('mismatched_result_tool')
        for key in ('source_pointers', 'result_pointers', 'payload_pointers'):
            attempt['provenance'][key] = list(dict.fromkeys(attempt['provenance'][key]))
        attempt['association_errors'] = list(dict.fromkeys(attempt['association_errors']))
    return attempts


def score_attempt(case, bindings, handler):
    """Assess raw protocol defects before passing decoded arguments to the handler."""
    path = bindings.get('call_path', '')
    prefix = '/observation/tool_attempts/'
    if not path.startswith(prefix):
        return handler(case, bindings)
    attempt = case['observation']['tool_attempts'][int(path[len(prefix):])]
    if not attempt['raw_available']:
        return {'status': 'unverifiable', 'verdict': None}
    if attempt['protocol_errors'] or attempt['association_errors']:
        return {'status': 'scored', 'verdict': False}
    if bindings.get('protocol_only') is True:
        return {'status': 'scored', 'verdict': True}
    return handler(case, bindings)
