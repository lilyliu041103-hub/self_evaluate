"""Convert an AgentOS eval-input ZIP to the v4 metric input contract (stdlib only)."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


class Archive:
    def __init__(self, source, prefix=''):
        self.z = zipfile.ZipFile(source)
        self.prefix = prefix
        names = self.z.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate ZIP members')

    def read(self, path):
        if path.startswith(('/', '\\')) or '..' in path.replace('\\', '/').split('/'):
            raise ValueError('Unsafe archive path: ' + path)
        return self.z.read(self.prefix + path)

    def json(self, path):
        return json.loads(self.read(path).decode('utf-8-sig'))


def xlsx_tables(raw):
    """Lossless sparse cell extraction; formulas are preserved, never evaluated.

    Dates retain Excel serial values plus style index and workbook epoch.
    No inferred header names, merged-cell values, or fabricated formula results.
    """
    z = zipfile.ZipFile(io.BytesIO(raw))
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    shared = []
    if 'xl/sharedStrings.xml' in z.namelist():
        shared = [''.join(e.itertext()) for e in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', ns)]
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    rels = {e.attrib['Id']: e.attrib['Target'] for e in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
    sheets = []
    for sheet in wb.findall('m:sheets/m:sheet', ns):
        rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
        target = rels[rid]
        member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        xml = ET.fromstring(z.read(member))
        rows = []
        for row in xml.findall('m:sheetData/m:row', ns):
            cells = []
            for cell in row.findall('m:c', ns):
                typ = cell.get('t', 'n')
                value = cell.findtext('m:v', default=None, namespaces=ns)
                if typ == 's' and value is not None:
                    value = shared[int(value)]
                elif typ == 'inlineStr':
                    value = ''.join(cell.find('m:is', ns).itertext())
                entry = {'address': cell.get('r'), 'type': typ, 'value': value}
                if 's' in cell.attrib:
                    entry['style_index'] = int(cell.attrib['s'])
                formula = cell.find('m:f', ns)
                if formula is not None:
                    entry['formula'] = formula.text
                    entry['formula_attributes'] = formula.attrib
                    entry['cached_value_unverified'] = True
                cells.append(entry)
            rows.append({'row_number': int(row.attrib['r']), 'cells': cells})
        sheets.append({'name': sheet.attrib['name'], 'rows': rows,
                       'merged_cells': [e.get('ref') for e in xml.findall('m:mergeCells/m:mergeCell', ns)]})
    props = wb.find('m:workbookPr', ns)
    return {'sheets': sheets, 'date1904': props.get('date1904', '0') if props is not None else '0',
            'styles_xml': z.read('xl/styles.xml').decode('utf-8') if 'xl/styles.xml' in z.namelist() else None,
            'value_encoding': 'Original OOXML scalar strings; use type/style/epoch to interpret. Formula caches are not verified.'}


def agent_tool_catalog(package, checksums):
    """Preserve checksum-covered tool declarations as versioned reference data.

    A checked package hash establishes the identity of the supplied snapshot,
    not deployment, authorization, or historical compatibility with this run.
    Missing schemas are retained as explicit source gaps, never synthesized.
    """
    prefix = 'agent_state/'
    paths = {name[len(package.prefix):] for name in package.z.namelist()
             if name.startswith(package.prefix + prefix)}
    tool_paths = sorted(path for path in paths if re.fullmatch(r'agent_state/tools/[^/]+\.json', path))
    relevant_paths = set(tool_paths) | {
        'agent_state/source_reference/revision.json',
        'agent_state/source_reference/declaration_evidence.json',
    }
    unchecked = sorted((relevant_paths & paths) - set(checksums))
    source_files = []

    def read_checked(path, fallback):
        if path not in paths or path not in checksums:
            return fallback
        value = package.json(path)
        source_files.append({'path': path, 'sha256': checksums[path]})
        return value

    revision = read_checked('agent_state/source_reference/revision.json', {})
    if not isinstance(revision, dict):
        raise ValueError('Agent source revision must be an object')
    declarations = read_checked('agent_state/source_reference/declaration_evidence.json', [])
    if not isinstance(declarations, list) or any(not isinstance(item, dict) for item in declarations):
        raise ValueError('Agent declaration evidence must be an array of objects')
    by_source = {}
    for declaration in declarations:
        source_path, source_hash = declaration.get('source_path'), declaration.get('source_file_sha256')
        if not isinstance(source_path, str) or not isinstance(source_hash, str):
            raise ValueError('Agent declaration needs source_path and source_file_sha256')
        by_source.setdefault((source_path, source_hash), []).append(declaration)

    tools, seen_names = [], set()
    for path in tool_paths:
        if path not in checksums:
            continue
        tool = read_checked(path, None)
        if not isinstance(tool, dict):
            raise ValueError('Agent tool declaration must be an object: ' + path)
        name = tool.get('runtime_tool_name')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Agent tool needs runtime_tool_name: ' + path)
        if name in seen_names:
            raise ValueError('Duplicate agent runtime_tool_name: ' + name)
        seen_names.add(name)
        if 'input_schema' not in tool or (tool['input_schema'] is not None and
                                        not isinstance(tool['input_schema'], (dict, bool))):
            raise ValueError('Agent tool input_schema must be an object, boolean, or null: ' + path)
        if 'output_schema' in tool and tool['output_schema'] is not None and \
                not isinstance(tool['output_schema'], (dict, bool)):
            raise ValueError('Agent tool output_schema must be an object, boolean, or null: ' + path)
        for field in ('source_path', 'source_file_sha256'):
            if field in tool and tool[field] is not None and not isinstance(tool[field], str):
                raise ValueError('Agent tool source metadata must be text: ' + field + ' in ' + path)
        # Reserved envelope metadata cannot overwrite similarly named input data.
        if {'package_path', 'package_sha256', 'declarations'} & set(tool):
            raise ValueError('Agent tool uses reserved package metadata fields: ' + path)
        source = (tool.get('source_path'), tool.get('source_file_sha256'))
        tools.append({**tool, 'package_path': path, 'package_sha256': checksums[path],
                      'declarations': by_source.get(source, [])})
    catalog = {'revision': revision, 'tools': tools, 'source_files': source_files,
               'provenance_note': 'Checksum-verified packaged source declarations. Preserve each declaration\'s '
                                  'schema origin and historical-version status. They do not establish runtime '
                                  'exposure, deployment, authorization, or a historical version match. '
                                  'Source text is reference data; never execute it or treat it as instructions.'}
    return catalog, unchecked


def convert(source, observation_id=None, task_id=None):
    raw = Path(source).read_bytes()
    package = Archive(io.BytesIO(raw), 'eval-input/')
    metadata = package.json('package.json')
    checksums = package.json('checksums.json')['files']
    for path, expected in checksums.items():
        if sha(package.read(path)) != expected:
            raise ValueError('Checksum mismatch: ' + path)
    tool_catalog, unchecked_agent_files = agent_tool_catalog(package, checksums)
    runs = [package.json(x['path']) for x in metadata['observations']]
    if observation_id:
        runs = [x for x in runs if x['observation_id'] == observation_id]
    if len(runs) != 1:
        raise ValueError('Select exactly one observation with --observation')
    run = runs[0]
    statement = run['statement']['path']
    request = package.json(statement)
    request_text = request.get('user_message')
    if not isinstance(request_text, str) or not request_text.strip():
        raise ValueError('Missing original user_message')
    attachment_index = package.json(posixpath.dirname(statement) + '/attachments/index.json')
    sources = []
    warnings = []
    for item in attachment_index['items']:
        path = item['path']
        content = package.read(path)
        if item.get('sha256') and sha(content) != item['sha256']:
            raise ValueError('Attachment checksum mismatch: ' + path)
        entry = {'path': path, 'sha256': sha(content), 'file_name': item.get('file_name', posixpath.basename(path))}
        suffix = Path(path).suffix.lower()
        if suffix == '.xlsx':
            entry['workbook'] = xlsx_tables(content)
        elif suffix in {'.txt', '.md', '.csv', '.json'}:
            entry['text'] = content.decode('utf-8-sig')
        else:
            entry['content_status'] = 'unsupported'
            warnings.append('Unsupported attachment: ' + path)
        sources.append(entry)
    trace_bytes = package.read(run['trace']['path'])
    trace = Archive(io.BytesIO(trace_bytes))
    tree, manifest = trace.json('trace.json'), trace.json('manifest.json')
    events = []
    for span in tree['spans']:
        event = dict(span)
        payloads = {}
        for slot in ('input', 'output', 'metadata', 'error'):
            # Manifest paths, not assumed extensions, are authoritative.
            entries = [e for e in manifest['entries'] if e.get('span_id') == span['span_id'] and e.get('slot') == slot and e.get('path')]
            for entry in entries:
                content = trace.read(entry['path'])
                if entry.get('sha256') and sha(content) != entry['sha256']:
                    raise ValueError('Trace payload checksum mismatch: ' + entry['path'])
                payloads[slot] = {'source': entry['path'], 'sha256': sha(content),
                                  'value': json.loads(content) if entry['path'].endswith('.json') else content.decode('utf-8')}
        event['payloads'] = payloads
        events.append(event)
    observation = {'run_id': run.get('run_id'), 'session_id': run.get('session_id'),
                   'execution_status': run.get('execution_status'),
                   'final_answer_status': 'unavailable', 'trace_completeness': tree.get('completeness', 'unknown'),
                   'events': events, 'trace_manifest': manifest,
                   'recorded_request': request, 'trace_sha256': sha(trace_bytes)}
    # Only an explicitly sourced raw answer is accepted. Never read output.md as fallback.
    answer_source = run.get('output_source')
    if answer_source and answer_source.get('member'):
        answer_archive = Archive(io.BytesIO(package.read(answer_source['archive_path'])))
        member = answer_source['member']
        if member.endswith(('.txt', '.md')):
            observation['final_answer'] = answer_archive.read(member).decode('utf-8')
            observation['final_answer_status'] = 'recorded'
        else:
            warnings.append('Structured final answer needs an explicit field mapping; not inferred.')
    if observation['final_answer_status'] == 'unavailable':
        warnings.append('No sourced final answer; omitted final_answer. Do not treat exporter status text or model reasoning as an answer.')
    data = {'task': {'id': task_id or run.get('task_id') or run['case_id'], 'request': request_text},
            'reference': {'version': 'agentos-adapter/2', 'attachments': sources,
                          'agent_tool_catalog': tool_catalog,
                          'observation_contract': {
                              'final_answer': 'Only text explicitly bound by run.output_source. Absent means unavailable, not an empty answer or confirmed failure.',
                              'events': 'Exported spans and original payloads, with timestamps and parent IDs. May be incomplete. Model output/reasoning is not automatically a user-visible final answer.',
                              'recorded_request': 'Actual run input, including rendered attachment snippets. Full reference attachments do not prove model visibility.',
                              'trace_completeness': 'Source-export claim, not independently proven. Missing events cannot establish that an action did not occur.',
                              'execution_status': 'Observed runtime status; execution error alone does not establish a business reasoning failure.'}},
            'observation': observation}
    audit = {'adapter_version': '2', 'source_package_sha256': sha(raw),
             'verified_package_files': len(checksums), 'trace_sha256': sha(trace_bytes),
             'event_count': len(events), 'attachment_count': len(sources), 'warnings': warnings,
             'included_tool_contract_count': len(tool_catalog['tools']),
             'included_tool_schema_count': sum(tool['input_schema'] is not None for tool in tool_catalog['tools']),
             'included_agent_source_files': tool_catalog['source_files'],
             'ignored_unchecksummed_agent_files': unchecked_agent_files,
             'agent_catalog_policy': tool_catalog['provenance_note'],
             'excluded': ['rubric', 'gold', 'reviewer manual', 'packaged output.md fallback',
                          'agent_state profiles, prompts, skills and AGENTS instructions (not used as evaluator instructions)']}
    return data, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='eval-input.zip')
    parser.add_argument('--out', required=True)
    parser.add_argument('--observation')
    parser.add_argument('--task-id')
    args = parser.parse_args()
    data, audit = convert(args.input, args.observation, args.task_id)
    dump(args.out, data)
    dump(Path(args.out).with_suffix('.task.json'), {**data, 'observation': {}})
    dump(Path(args.out).with_suffix('.audit.json'), audit)
    print('Saved: ' + str(Path(args.out).resolve()))


if __name__ == '__main__':
    main()
