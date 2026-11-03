"""Local evidence storage. The model sees compact views, never the whole audit dump."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from .tool_observations import normalize_tool_observations


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def column_number(address):
    match = re.match(r'^([A-Z]+)[1-9][0-9]*$', address or '')
    if not match:
        raise ValueError('Invalid XLSX cell address: ' + str(address))
    result = 0
    for char in match[1]:
        result = result * 26 + ord(char) - ord('A') + 1
    return result - 1


def build_evidence(original):
    normalize_tool_observations(original)
    records = {}
    tables = []
    documents = []
    tool_catalog = []

    def add(eid, scope, kind, data, origin, **meta):
        if eid in records:
            raise ValueError('Duplicate evidence ID: ' + eid)
        record = dict(id=eid, scope=scope, kind=kind, data=data, origin=origin, **meta)
        record['content_hash'] = digest(record)
        records[eid] = record
        return eid

    def split_large(eid, value, origin, span_id=None):
        """Keep field boundaries and pointers. No LLM summary or silent truncation."""
        if not isinstance(value, dict):
            return value
        compact = {}
        for key, child in value.items():
            if len(canonical(child)) <= 1600 and key not in ('reasoning', 'reasoning_content'):
                compact[key] = child
                continue
            pointer_token = key.replace('~', '~0').replace('/', '~1')
            child_id = eid + ':field:' + quote(key, safe='-_')
            child_origin = dict(origin, pointer=origin['pointer'] + '/' + pointer_token)
            add(child_id, 'observation', 'trace_payload_part', child, child_origin, span_id=span_id)
            compact[key] = {'evidence_ref': child_id, 'characters': len(canonical(child)),
                            'content_not_inlined': True}
            if isinstance(child, list):
                parts = []
                for i, part in enumerate(child):
                    part_id = child_id + ':' + str(i)
                    part_origin = dict(child_origin, pointer=child_origin['pointer'] + '/' + str(i))
                    add(part_id, 'observation', 'trace_payload_part', part, part_origin, span_id=span_id)
                    parts.append(part_id)
                compact[key]['item_refs'] = parts
        return compact

    add('task:request', 'task', 'task_request', original['task'], {'pointer': '/task'})
    agent_catalog = original['reference'].get('agent_tool_catalog', {})
    for ti, tool in enumerate(agent_catalog.get('tools', [])):
        name = tool['runtime_tool_name']
        eid = 'agent:tool:' + quote(name, safe='-_')
        data = copy.deepcopy(tool)
        data['contract_level'] = ('business' if isinstance(tool.get('input_schema'), (dict, bool)) else 'capability')
        data.setdefault('historical_version_match', 'unverified')
        data['reference_scope'] = 'packaged_source_snapshot'
        data['applicability_note'] = ('This source snapshot is a declared reference, not proof of the tool version, '
                                    'deployment, or permission in the observed execution. Keep its historical_version_match visible.')
        add(eid, 'reference', 'tool_contract', data,
            {'pointer': f'/reference/agent_tool_catalog/tools/{ti}',
             'package_path': tool.get('package_path'), 'package_sha256': tool.get('package_sha256')},
            tool_name=name, contract_level=data['contract_level'])
        tool_catalog.append({'id': eid, 'name': name, 'contract_level': data['contract_level'],
                             'source_revision': tool.get('source_revision'),
                             'historical_version_match': tool.get('historical_version_match', 'unverified')})
    for ai, attachment in enumerate(original['reference'].get('attachments', [])):
        source = {'path': attachment['path'], 'sha256': attachment['sha256']}
        base_pointer = f'/reference/attachments/{ai}'
        if 'text' in attachment:
            eid = 'doc:' + digest(attachment['path'])[:16]
            add(eid, 'reference', 'document', {'text': attachment['text']},
                dict(source, pointer=base_pointer + '/text'))
            documents.append({'evidence_id': eid, 'file_name': attachment['file_name']})
        if 'workbook' not in attachment:
            continue
        workbook = attachment['workbook']
        for si, sheet in enumerate(workbook['sheets']):
            table_id = 'table:' + digest([attachment['path'], sheet['name']])[:16]
            rows = sheet['rows']
            width = max((column_number(c['address']) + 1 for r in rows for c in r['cells']), default=0)
            header = [None] * width
            # Preserve the first physical row as a header candidate, not a proven schema.
            if rows:
                for c in rows[0]['cells']:
                    header[column_number(c['address'])] = c.get('value')
            columns = [str(v) if v is not None else f'column_{i+1}' for i, v in enumerate(header)]
            key_column = next((i for i, h in enumerate(columns) if re.search(r'id|编号|记录号', h, re.I)), None)
            schema = {'table_id': table_id, 'file_name': attachment['file_name'], 'sheet': sheet['name'],
                      'columns': columns, 'header_row_number': rows[0]['row_number'] if rows else None,
                      'header_assumption': 'first physical row; original cells remain available for verification',
                      'row_count': max(0, len(rows) - 1), 'date1904': workbook.get('date1904', '0'),
                      'merged_cells': sheet.get('merged_cells', []),
                      'value_encoding': 'OOXML scalar strings; per-row types distinguish numbers, strings, booleans and errors; no formula recalculation.'}
            schema_id = table_id + ':schema'
            add(schema_id, 'reference', 'table_schema', schema,
                dict(source, pointer=base_pointer + f'/workbook/sheets/{si}'))
            seen = {}
            compact_rows = []
            for ri, row in enumerate(rows[1:], 1):
                values = [None] * width
                types = {}
                formulas = {}
                styles = {}
                for cell in row['cells']:
                    col = column_number(cell['address'])
                    values[col] = cell.get('value')
                    if cell.get('type', 'n') not in ('s', 'inlineStr', 'str'):
                        types[str(col)] = cell.get('type', 'n')
                    if 'formula' in cell:
                        formulas[str(col)] = {k: v for k, v in cell.items() if k in
                                             ('formula', 'formula_attributes', 'cached_value_unverified')}
                    if 'style_index' in cell:
                        styles[str(col)] = cell['style_index']
                business_id = values[key_column] if key_column is not None else None
                key = str(business_id) if business_id is not None else 'row-' + str(row['row_number'])
                seen[key] = seen.get(key, 0) + 1
                eid = table_id + ':row:' + quote(key, safe='-_') + ':' + str(seen[key])
                data = {'table_id': table_id, 'values': values}
                if types:
                    data['types'] = types
                if formulas:
                    data['formulas'] = formulas
                if styles:
                    # Styles can change date/percent interpretation, so retain their references.
                    data['styles'] = styles
                add(eid, 'reference', 'table_row', data,
                    dict(source, pointer=base_pointer + f'/workbook/sheets/{si}/rows/{ri}',
                         sheet=sheet['name'], row_number=row['row_number']),
                    table_id=table_id, business_id=business_id, schema_id=schema_id)
                compact_rows.append({'id': eid, **data})
            # Styles are one shared record, not repeated per cell or request.
            if workbook.get('styles_xml'):
                style_id = table_id + ':styles'
                add(style_id, 'reference', 'table_schema', {'table_id': table_id, 'styles_xml': workbook['styles_xml']},
                    dict(source, pointer=base_pointer + '/workbook/styles_xml'))
                records[schema_id]['data']['styles_evidence_id'] = style_id
                base_record = {k: v for k, v in records[schema_id].items() if k != 'content_hash'}
                records[schema_id]['content_hash'] = digest(base_record)
            tables.append({'id': table_id, 'schema_id': schema_id, 'sheet': sheet['name'],
                           'file_name': attachment['file_name'], 'columns': columns, 'rows': compact_rows})

    observation = original.get('observation', {})
    status = {k: v for k, v in observation.items() if k not in
              ('events', 'trace_manifest', 'recorded_request', 'final_answer', 'tool_calls', 'tool_contracts', 'tool_attempts')}
    status['payload_coverage'] = {}
    for entry in observation.get('trace_manifest', {}).get('entries', []):
        state = entry.get('state', 'unknown')
        status['payload_coverage'][state] = status['payload_coverage'].get(state, 0) + 1
    add('run:status', 'observation', 'run_status', status, {'pointer': '/observation'})
    if 'final_answer' in observation:
        add('run:final_answer', 'observation', 'final_answer', observation['final_answer'],
            {'pointer': '/observation/final_answer'})
    if 'recorded_request' in observation:
        compact_request = split_large('run:request', observation['recorded_request'],
                                      {'pointer': '/observation/recorded_request'})
        add('run:request', 'observation', 'runtime_request', compact_request,
            {'pointer': '/observation/recorded_request'})
        for hi, item in enumerate(observation['recorded_request'].get('history', [])):
            add(f'interaction:{hi}', 'observation', 'interaction', item,
                {'pointer': f'/observation/recorded_request/history/{hi}'})
    for ei, event in enumerate(observation.get('events', [])):
        span = event['span_id']
        summary = {k: event[k] for k in ('span_id', 'parent_span_id', 'name', 'status', 'outcome',
                                       'start_time_unix_nano', 'end_time_unix_nano') if k in event}
        wanted_attributes = {'app.operation_code', 'app.purpose', 'app.tool_name', 'app.tool.name',
                             'error.type', 'error.message', 'app.model_alias', 'app.run_kind'}
        summary['attributes'] = {k: v for k, v in event.get('attributes', {}).items() if k in wanted_attributes}
        raw_id = 'span:' + span + ':raw_metadata'
        add(raw_id, 'observation', 'trace_payload_part',
            {k: v for k, v in event.items() if k != 'payloads'},
            {'pointer': f'/observation/events/{ei}'}, span_id=span)
        summary['raw_metadata_ref'] = raw_id
        summary['payload_refs'] = {}
        for slot, payload in event.get('payloads', {}).items():
            eid = f'span:{span}:{slot}'
            value = payload['value']
            origin = {'archive_path': 'trace.zip', 'member': payload['source'], 'sha256': payload['sha256'],
                      'pointer': f'/observation/events/{ei}/payloads/{slot}/value'}
            view = split_large(eid, value, origin, span_id=span)
            add(eid, 'observation', 'trace_payload', view, origin, span_id=span, slot=slot)
            summary['payload_refs'][slot] = eid
            if slot == 'input' and isinstance(value, dict) and isinstance(value.get('tools'), list):
                for ti, tool in enumerate(value['tools']):
                    add(f'span:{span}:tool:{ti}', 'observation', 'tool_contract', tool,
                        {'pointer': f'/observation/events/{ei}/payloads/{slot}/value/tools/{ti}'}, span_id=span)
            if isinstance(value, dict) and isinstance(value.get('usage'), dict):
                add(f'span:{span}:{slot}:usage', 'observation', 'resource_usage',
                    {'scope': 'span:' + span, 'usage': value['usage'], 'note': 'Not an execution-wide total; overlapping scopes must not be added.'},
                    {'pointer': f'/observation/events/{ei}/payloads/{slot}/value/usage'}, span_id=span)
        add('span:' + span, 'observation', 'trace_event', summary,
            {'pointer': f'/observation/events/{ei}'}, span_id=span)

    for ai, attempt in enumerate(observation.get('tool_attempts', [])):
        event = observation['events'][attempt['provenance']['source_event_index']]
        add(f'run:tool_attempt:{ai}', 'observation', 'trace_payload', copy.deepcopy(attempt),
            {'pointer': f'/observation/tool_attempts/{ai}',
             'source_pointers': attempt['provenance']['source_pointers'],
             'result_pointers': attempt['provenance']['result_pointers'],
             'payload_pointers': attempt['provenance']['payload_pointers']},
            span_id=event.get('span_id'), tool_name=attempt['tool'] or attempt['tool_hint'],
            slot='tool_attempt')
    for ti, contract in enumerate(observation.get('tool_contracts', [])):
        source_index = contract['provenance']['source_event_index']
        event = observation['events'][source_index]
        add(f'run:tool_contract:{ti}', 'observation', 'tool_contract', copy.deepcopy(contract),
            {'pointer': f'/observation/tool_contracts/{ti}',
             'source_pointer': contract['provenance']['source_pointer']},
            span_id=event.get('span_id'), tool_name=contract['name'],
            contract_level=contract['contract_level'])
    for ci, call in enumerate(observation.get('tool_calls', [])):
        event = observation['events'][call['provenance']['source_event_index']]
        add(f'run:tool_call:{ci}', 'observation', 'trace_payload', copy.deepcopy(call),
            {'pointer': f'/observation/tool_calls/{ci}',
             'source_pointers': copy.deepcopy(call['provenance']['source_pointers']),
             'result_pointers': copy.deepcopy(call['provenance']['result_pointers'])},
            span_id=event.get('span_id'), tool_name=call['tool'], slot='normalized_tool_call')

    compact = {'task': original['task'], 'reference': {
        'version': 'agentos-evidence-view/1', 'tables': tables,
        'documents': [{'id': x['evidence_id'], 'file_name': x['file_name'],
                       'text': records[x['evidence_id']]['data']['text']} for x in documents],
        'tool_catalog': tool_catalog,
        'tool_catalog_provenance': {'revision': agent_catalog.get('revision', {}),
                                   'note': agent_catalog.get('provenance_note')},
        'observation_contract': original['reference'].get('observation_contract', {})}}
    task_catalog = {'task': original['task'], 'reference': {
        'tables': [{k: v for k, v in t.items() if k != 'rows'} | {'row_count': len(t['rows'])} for t in tables],
        'documents': documents, 'tool_catalog': tool_catalog,
        'tool_catalog_provenance': {'revision': agent_catalog.get('revision', {}),
                                   'note': agent_catalog.get('provenance_note')},
        'observation_contract': original['reference'].get('observation_contract', {})}}
    return records, compact, task_catalog


class EvidenceStore:
    def __init__(self, bundle):
        self.bundle = Path(bundle)
        self.records = json.loads((self.bundle / 'evidence.json').read_text(encoding='utf-8'))
        self.index = json.loads((self.bundle / 'index.json').read_text(encoding='utf-8'))
        self.original = None

    def verify(self):
        manifest = json.loads((self.bundle / 'manifest.json').read_text(encoding='utf-8'))
        for name, expected in manifest['files'].items():
            target = (self.bundle / name).resolve()
            if not target.is_relative_to(self.bundle.resolve()):
                raise ValueError('Manifest path escape')
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError('Bundle file changed: ' + name)
        for eid, item in self.records.items():
            if eid != item['id'] or digest({k: v for k, v in item.items() if k != 'content_hash'}) != item['content_hash']:
                raise ValueError('Evidence changed: ' + eid)
        return manifest

    def select(self, scope=None, kinds=None, table_id=None, field=None, equals=None,
               contains=None, limit=50, offset=0):
        if not isinstance(limit, int) or not 1 <= limit <= 200 or not isinstance(offset, int) or offset < 0:
            raise ValueError('limit must be 1..200; offset must be nonnegative')
        matched = []
        for eid, meta in self.index.items():
            if scope and meta['scope'] != scope:
                continue
            if kinds and meta['kind'] not in kinds:
                continue
            if table_id and meta.get('table_id') != table_id:
                continue
            record = self.records[eid]
            data = record['data']
            if field is not None or equals is not None:
                if meta['kind'] != 'table_row':
                    continue
                columns = self.records[meta['schema_id']]['data']['columns']
                if field is None:
                    vals = data['values']
                else:
                    indices = [i for i, c in enumerate(columns) if c == field]
                    if not indices:
                        continue
                    vals = [data['values'][i] for i in indices]
                if not any(v == equals for v in vals):
                    continue
            if contains and contains not in canonical(data):
                continue
            matched.append(eid)
        end = offset + limit
        return {'ids': matched[offset:end], 'total_matches': len(matched), 'offset': offset,
                'next_offset': end if end < len(matched) else None,
                'returned_all_matches': offset == 0 and end >= len(matched),
                'coverage_note': 'Matches within provided evidence only; source trace may be incomplete.'}

    def resolve(self, evidence_id):
        if evidence_id in self.records:
            return [evidence_id]
        return [eid for eid, m in self.index.items() if m.get('business_id') == evidence_id]

    def read(self, ids, max_chars=40000, offset=None):
        if not isinstance(ids, list) or not ids or len(ids) > 200:
            raise ValueError('ids must contain 1..200 evidence IDs')
        if not isinstance(max_chars, int) or not 100 <= max_chars <= 100000:
            raise ValueError('max_chars must be 100..100000')
        if offset is not None:
            if type(offset) is not int or offset < 0 or len(ids) != 1:
                raise ValueError('Paged reads require one exact evidence ID and a nonnegative offset')
            if ids[0] not in self.records:
                raise ValueError('Paged reads require an exact evidence ID')
            record = self.records[ids[0]]
            serialized = canonical(record['data'])
            next_offset = min(offset + max_chars, len(serialized))
            if offset > len(serialized):
                raise ValueError('Page offset exceeds evidence length')
            has_more = next_offset < len(serialized)
            return {'evidence_id': record['id'], 'origin': copy.deepcopy(record['origin']),
                    'encoding': 'JSON text of record.data; concatenate fragments in offset order',
                    'text_fragment': serialized[offset:next_offset], 'offset': offset,
                    'total_characters': len(serialized), 'next_offset': next_offset if has_more else None,
                    'complete': offset == 0 and not has_more,
                    'next_read_arguments': {'ids': ids, 'offset': next_offset, 'max_chars': max_chars}
                    if has_more else None}
        resolved, missing = [], []
        for eid in ids:
            matches = self.resolve(eid)
            if not matches:
                missing.append(eid)
            for match in matches:
                if match not in resolved:
                    resolved.append(match)
                schema_id = self.index[match].get('schema_id')
                if schema_id and schema_id not in resolved:
                    resolved.append(schema_id)
        kept, omitted, size = [], [], 0
        for eid in resolved:
            record = self.records[eid]
            length = len(canonical(record))
            if size + length > max_chars:
                omitted.append({'id': eid, 'characters': length, 'reason': 'read_budget_exceeded',
                                'next_read_arguments': {'ids': [eid], 'offset': 0, 'max_chars': max_chars}})
                continue
            kept.append(record)
            size += length
        return {'records': copy.deepcopy(kept), 'missing_ids': missing, 'omitted': omitted,
                'complete': not missing and not omitted}

    def read_original(self, evidence_id):
        matches = self.resolve(evidence_id)
        if len(matches) != 1:
            raise ValueError('Raw lookup needs one unambiguous evidence ID')
        if self.original is None:
            self.original = json.loads((self.bundle / 'audit/original-input.json').read_text(encoding='utf-8'))
        pointer = self.records[matches[0]]['origin']['pointer']
        value = self.original
        for token in pointer.strip('/').split('/'):
            token = token.replace('~1', '/').replace('~0', '~')
            value = value[int(token)] if isinstance(value, list) else value[token]
        return copy.deepcopy(value)


def make_index(records):
    return {eid: {k: v for k, v in r.items() if k not in ('data', 'origin')}
            for eid, r in records.items()}
