"""Create a provenance-preserving evaluation seed bundle from AgentOS + trace.

No AgentOS code is imported. No database, application or model is started.
Dependencies: pydantic >= 2, typing_extensions (already in the bundled runtime).
"""
from __future__ import annotations
from eval_bridge.resources import literal as _fragment
from eval_bridge.resources import literal as _asset_literal

import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath

from agentos_source_contracts import SourceReader

VERSION = '0.1'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def yaml_text(value, depth=0):
    """Serialize the manifest's plain mappings, lists and JSON scalar values."""
    pad = '  ' * depth
    lines = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines += [pad + k + ':', yaml_text(v, depth + 1)]
            else:
                lines.append(pad + k + ': ' + json.dumps(v, ensure_ascii=False))
    elif isinstance(value, list):
        for v in value:
            if isinstance(v, (dict, list)) and v:
                lines += [pad + '-', yaml_text(v, depth + 1)]
            else:
                lines.append(pad + '- ' + json.dumps(v, ensure_ascii=False))
    return '\n'.join(lines)


def git_metadata(repo, git):
    def run(*args):
        p = subprocess.run([git, '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8')
        return p.stdout.strip() if p.returncode == 0 else None
    status = run('status', '--porcelain')
    return {'commit': run('rev-parse', 'HEAD'), 'branch': run('branch', '--show-current'),
            'working_tree_clean': status == '' if status is not None else None,
            'dirty_paths': status.splitlines() if status else [],
            'historical_run_revision_match': 'unverified'}


class Bundle:
    def __init__(self, root):
        self.root = root.resolve()
        self.refs = []

    def file(self, relative, data):
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
        return relative

    def json(self, relative, obj):
        return self.file(relative, json_bytes(obj))

    def ref(self, relative, **kwargs):
        self.refs.append(relative)
        return {'path': relative, **kwargs}


def skill_frontmatter(text):
    """Extract selected simple metadata; keep raw frontmatter authoritative."""
    match = re.match(r'^---\s*\n(.*?)\n---', text, re.S)
    if not match:
        return {}, None
    raw = match.group(1)
    result = {}
    for key in ('name', 'description', 'display_name', 'version', 'tools'):
        m = re.search(r'^' + key + r':\s*([^\n]*)', raw, re.M)
        if not m:
            continue
        value = m.group(1).strip()
        if value in ('|', '>', '|-', '>-'):
            # Metadata parsing may be partial; the complete original is copied.
            continue
        if key == 'tools':
            try: value = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                value = [s.strip().strip('\"\'') for s in value.strip('[]').split(',') if s.strip()] if value.startswith('[') else None
        else:
            value = value.strip('\"\'')
        result[key] = value
    return result, raw


def inspect_archive(path):
    z = zipfile.ZipFile(path)
    names = z.namelist()
    if len(names) != len(set(names)):
        raise ValueError('duplicate ZIP members')
    for name in names:
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name:
            raise ValueError('unsafe ZIP member: ' + name)
    trace = json.loads(z.read('trace.json'))
    manifest = json.loads(z.read('manifest.json'))
    if trace['trace_id'] != manifest['trace_id']:
        raise ValueError('trace/manifest identity mismatch')
    checked = 0
    for entry in manifest['entries']:
        if entry.get('state') == 'saved':
            if not entry.get('path') or entry['path'] not in names:
                raise ValueError('saved payload missing')
            if sha(z.read(entry['path'])) != entry.get('sha256'):
                raise ValueError('payload hash mismatch: ' + entry['path'])
            checked += 1
    slots = {(e['span_id'], e['slot']): e for e in manifest['entries']}
    return z, trace, manifest, slots, checked


def build(args):
    repo, trace_file, target = args.repo.resolve(), args.trace.resolve(), args.output.resolve()
    if target.exists():
        raise FileExistsError('Refusing to overwrite an existing bundle: ' + str(target))
    target.mkdir(parents=True)
    b = Bundle(target)
    now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    source_revision = git_metadata(repo, args.git)
    z, trace, manifest, slots, checked = inspect_archive(trace_file)
    root_runs = [s for s in trace['spans'] if s['name'] == 'agentos.run.execute' and s['attributes'].get('app.run_kind') == 'meta_chat']
    if len(root_runs) != 1:
        raise ValueError('Expected exactly one meta_chat execution; select a root explicitly for other traces.')
    root = root_runs[0]
    root_id = root['span_id']
    parent = {s['span_id']: s.get('parent_span_id') for s in trace['spans']}
    def descendant(span_id, ancestor):
        seen = set()
        while span_id and span_id not in seen:
            if span_id == ancestor: return True
            seen.add(span_id)
            span_id = parent.get(span_id)
        return False
    model_spans = [s for s in trace['spans'] if s['name'] == 'agentos.llm.call'
                   and s['attributes'].get('app.purpose') == 'chat' and descendant(s['span_id'], root_id)]
    if len(model_spans) != 1:
        raise ValueError(_asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_1'))
    model = model_spans[0]
    model_id = model['span_id']
    def slot(sid, kind):
        entry = slots.get((sid, kind))
        if not entry or entry.get('state') != 'saved':
            raise ValueError(f'Required slot missing: {sid}/{kind}')
        return entry['path']
    request_member, output_member = slot(root_id, 'input'), slot(root_id, 'output')
    request = json.loads(z.read(request_member))
    model_input_member = slot(model_id, 'input')
    model_input = json.loads(z.read(model_input_member))
    metadata = json.loads(z.read(slot(model_id, 'metadata')))
    model_output = json.loads(z.read(slot(model_id, 'output')))
    discovery_spans = [s for s in trace['spans'] if s['name'] == 'agentos.capability.search' and descendant(s['span_id'], root_id)]
    if len(discovery_spans) != 1:
        raise ValueError('Expected one discovery snapshot')
    discovery_member = slot(discovery_spans[0]['span_id'], 'output')
    discovery = json.loads(z.read(discovery_member))
    alternatives = model_input['response_format']['json_schema']['schema']['properties']['action']['anyOf']
    branch = next(v for v in alternatives if 'tool_calls' in v.get('properties', {}))
    wire_tools = branch['properties']['tool_calls']['items']['anyOf']
    exposed = {d['properties']['name']['enum'][0]: d for d in wire_tools}
    redacted_messages = [i for i, m in enumerate(model_input['messages']) if m.get('content') == '[REDACTED]']
    observed_prefix = 'seeds/SEED-001/observations/RUN-001'
    archive_ref = b.ref(observed_prefix + '/trace.zip')
    b.file(archive_ref['path'], trace_file.read_bytes())
    def evidence(member, pointer=None):
        e = {'archive_path': archive_ref['path'], 'member': member}
        if pointer is not None: e['json_pointer'] = pointer
        return e

    # Source declarations are a separate, non-runtime snapshot.
    reader = SourceReader(repo)
    source_tools = reader.export_tools()
    source_prefix = 'capabilities/SOURCE-20261027'
    source_index = []
    for name, tool in sorted(source_tools.items()):
        tool['snapshot_id'] = 'SOURCE-20261027'
        tool['source_revision'] = source_revision['commit']
        tool['source_file_sha256'] = sha((repo / tool['source_path']).read_bytes())
        path = source_prefix + '/tools/' + name + '.json'
        b.json(path, tool)
        source_index.append({'runtime_tool_name': name, 'definition': b.ref(path),
                             'input_schema_available': tool['input_schema'] is not None,
                             'output_schema_available': tool['output_schema'] is not None})
    source_skills, local_skill_by_name = [], {}
    for folder in sorted((repo / 'plugins/skills').iterdir()):
        skill_md = folder / 'SKILL.md'
        if not skill_md.is_file(): continue
        dest = source_prefix + '/skills/' + folder.name
        copied = []
        for p in sorted(folder.rglob('*')):
            if not p.is_file() or p.is_symlink(): continue
            rel = p.relative_to(folder).as_posix()
            dest_path = dest + '/' + rel
            b.file(dest_path, p.read_bytes())
            copied.append({'path': dest_path, 'sha256': sha(p.read_bytes())})
        md, fm = skill_frontmatter(skill_md.read_text(encoding='utf-8-sig'))
        definition = {'snapshot_id': 'SOURCE-20261027', 'name': md.get('name', folder.name),
                      'description': md.get('description'), 'declared_tools': md.get('tools'),
                      'display_name': md.get('display_name'), 'declared_version': md.get('version'),
                      'source_kind': 'repository_guidance_package', 'source_revision': source_revision['commit'],
                      'installation_id': None, 'revision_id': None, 'available_in_run': None,
                      'historical_version_match': 'unverified', 'frontmatter_raw': fm,
                      'instructions': b.ref(dest + '/SKILL.md'), 'files': copied,
                      'note': '原文及原资源路径完整保留；源码存在不证明在目标环境安装或在历史 Run 激活。'}
        b.json(dest + '/skill.json', definition)
        item = {'name': definition['name'], 'definition': b.ref(dest + '/skill.json')}
        source_skills.append(item)
        local_skill_by_name[definition['name']] = item['definition']
    b.json(source_prefix + '/declaration_evidence.json', list(reader.evidence.values()))
    b.json(source_prefix + '/unexecuted_model_methods.json', reader.warnings)
    b.json(source_prefix + '/agents/index.json', _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_2'))
    b.json(source_prefix + '/catalog.json', {
        'snapshot_id': 'SOURCE-20261027', 'kind': 'source_reference', 'captured_at': now,
        'repository_path': str(repo), 'revision': source_revision,
        'scope': _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_3'),
        'completeness': 'partial', 'runtime_authorization_exported': False,
        'schema_projection': {'method': 'AST declarations -> Pydantic JSON Schema',
                              'pydantic_version': __import__('pydantic').__version__,
                              'application_imported': False, 'custom_validators_executed': False,
                              'unknown_expressions': 'Keep null and record gaps, never substitute a permissive schema.'},
        'tools': source_index, 'skills': source_skills,
        'agents': b.ref(source_prefix + '/agents/index.json'),
        'declaration_evidence': b.ref(source_prefix + '/declaration_evidence.json'),
        'unexecuted_model_methods': b.ref(source_prefix + '/unexecuted_model_methods.json')})

    # Snapshot reconstructed solely from the historical trace, with no backfill.
    hist = 'capabilities/SNAPSHOT-001'
    candidates = {x.get('tool_name'): x for x in discovery['tool_candidates']}
    historical_tools = []
    for name, wire in sorted(exposed.items()):
        candidate = candidates.get(name)
        reference = source_prefix + '/tools/' + name + '.json' if name in source_tools else None
        record = {'snapshot_id': 'SNAPSHOT-001', 'runtime_tool_name': name,
                  'resource_id': candidate.get('resource_id') if candidate else None,
                  'description': wire['properties']['name'].get('description'),
                  'version': None, 'input_schema': None, 'output_schema': None,
                  'execution_policy': None, 'authorized_in_run': None,
                  'model_exposed': True, 'exposed_at_span_ids': [model_id],
                  'invocation_observed': False, 'invocation_coverage': 'unknown',
                  'discovery_candidate': candidate, 'contract_status': 'historical_schema_unavailable',
                  'wire_call_schema': wire,
                  'wire_schema_note': '此为 name/arguments 键值数组传输协议，不是原业务参数 JSON Schema。',
                  'source': evidence(model_input_member),
                  'source_reference': b.ref(reference) if reference else None,
                  'source_reference_version_match': 'unverified',
                  'gaps': ['原始 tool_contracts 不在可读消息中，相关消息已被脱敏；不可据此校验历史业务参数。',
                           '未导出完整历史 grant/启停/审批策略，模型暴露不等于授权证明。']}
        path = hist + '/tools/' + name + '.json'
        b.json(path, record)
        historical_tools.append({'runtime_tool_name': name, 'definition': b.ref(path)})
    historical_skills = []
    for candidate in discovery['skill_candidates']:
        name = candidate['name']
        path = hist + '/skills/' + candidate['installation_id'] + '/skill.json'
        b.json(path, {'snapshot_id': 'SNAPSHOT-001', **candidate,
                      'candidate_observed': True, 'activation_observed': False,
                      'activation_coverage': 'unknown', 'instructions': None,
                      'historical_instruction_status': 'not_in_trace',
                      'source': evidence(discovery_member),
                      'source_reference': local_skill_by_name.get(name),
                      'source_reference_version_match': 'unverified'})
        historical_skills.append({'name': name, 'definition': b.ref(path)})
    b.json(hist + '/agents/index.json', {'status': 'observed_candidate_subset',
            'items': discovery['agent_candidates'], 'source': evidence(discovery_member),
            'full_catalog_status': 'unknown', 'note': '空候选只表示此次发现结果为空，不证明 AgentOS 没有可委派 Agent。'})
    b.json(hist + '/catalog.json', {'snapshot_id': 'SNAPSHOT-001', 'kind': 'historical_trace_observation',
            'trace_id': trace['trace_id'], 'run_id': root['attributes'].get('app.run_id'),
            'model_span_id': model_id, 'completeness': 'partial',
            'historical_contract_completeness': 'missing', 'authorization_completeness': 'unknown',
            'tools': historical_tools, 'skills': historical_skills,
            'agents': b.ref(hist + '/agents/index.json'), 'discovery_source': evidence(discovery_member),
            'counts': {'model_exposed_tools': len(exposed), 'discovery_tools': len(candidates),
                       'discovery_skills': len(historical_skills), 'discovery_agents': len(discovery['agent_candidates'])},
            'redacted_message_indices': redacted_messages,
            'source_reference_catalog': b.ref(source_prefix + '/catalog.json'),
            'source_reference_is_historical_snapshot': False})

    # Agent identity is bound to the actual historical root, not an invented published Agent.
    systems = [{'message_index': i, **m} for i, m in enumerate(model_input['messages']) if m.get('role') == 'system']
    b.file('agent/system_prompt.md', systems[0]['content'])
    b.json('agent/system_messages.json', systems)
    current_prompt = repo / 'prompts/runtime/meta_agent_system.prompt.md'
    b.file('agent/source_reference/meta_agent_system.prompt.md', current_prompt.read_bytes())
    b.json('agent/source_reference/revision.json', {'source_path': str(repo), **source_revision,
            'prompt_sha256': sha(current_prompt.read_bytes()),
            'note': '这是 1027 checkout 的提示词模板；agent/system_prompt.md 才是历史模型请求的首条实际 system 消息。'})
    b.json('agent/profile.json', {'profile_id': 'agentos-metaagent-trace-001', 'agent_id': None,
            'name': 'AgentOS MetaAgent', 'agent_type': root['attributes'].get('app.run_kind'),
            'description': systems[0]['content'].split('\n\n')[0],
            'description_source': evidence(model_input_member, '/messages/0/content'),
            'identity_status': 'runtime_role_observed_published_agent_id_unavailable',
            'scope': _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_4'),
            'limitations': {'value': ['只调用本次 Run 暴露的工具', '缺少业务事实或参数时不可编造'],
                            'provenance': 'summary_of_observed_system_prompt'},
            'input_schema': None, 'output_schema': None,
            'system_prompt': b.ref('agent/system_prompt.md'),
            'all_system_messages': b.ref('agent/system_messages.json'),
            'source_reference': b.ref('agent/source_reference/revision.json')})
    runtime = {'snapshot_id': 'RUNTIME-TRACE-001', 'kind': 'historical_model_request',
               'agent_version': None, 'code_revision': None, 'provider': None,
               'model': model_input.get('model'),
               'parameters': {k: model_input[k] for k in ('temperature', 'enable_thinking', 'max_tokens', 'stream', 'stream_options') if k in model_input},
               'protocol': model_input.get('response_format', {}).get('type'),
               'protocol_schema_name': model_input.get('response_format', {}).get('json_schema', {}).get('name'),
               'source': evidence(model_input_member), 'metadata_source': evidence(slot(model_id, 'metadata')),
               'source_checkout': b.ref('agent/source_reference/revision.json'),
               'historical_revision_matches_checkout': 'unknown'}
    b.json('agent/runtime.json', runtime)

    # The raw input/output are exact archive member bytes; extraction adds no facts.
    b.file('seeds/SEED-001/task/request.json', z.read(request_member))
    b.json('seeds/SEED-001/task/attachments/index.json', {'items': request.get('attachments'),
            'source': evidence(request_member, '/attachments'), 'status': 'observed_empty' if request.get('attachments') == [] else 'references_only'})
    b.file(observed_prefix + '/output.md', z.read(output_member))
    b.json(observed_prefix + '/artifacts/index.json', _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_5'))
    counts = Counter(e['state'] for e in manifest['entries'])
    trace_index = {'trace_id': trace['trace_id'], 'business_root_span_id': root_id,
                   'model_span_id': model_id, 'completeness': trace.get('completeness'), 'spans': []}
    for span in trace['spans']:
        sid = span['span_id']
        purpose = span['attributes'].get('app.purpose')
        layer = 'auxiliary' if purpose == 'session_title_generation' else 'business' if descendant(sid, root_id) else 'transport_or_other'
        trace_index['spans'].append({'span_id': sid, 'parent_span_id': span.get('parent_span_id'),
                'name': span['name'], 'purpose': purpose, 'layer': layer,
                'status': span.get('status'), 'outcome': span.get('outcome'),
                'start_time_unix_nano': span.get('start_time_unix_nano'),
                'duration_us': span.get('duration_us'),
                'slots': {k: slots[(sid, k)] for k in ('input', 'output', 'metadata', 'error') if (sid, k) in slots}})
    b.json(observed_prefix + '/trace_index.json', trace_index)
    b.json(observed_prefix + '/run.json', {'observation_id': 'RUN-001', 'seed_id': 'SEED-001',
            'agent_profile': b.ref('agent/profile.json'), 'runtime': b.ref('agent/runtime.json'),
            'capability_snapshot_id': 'SNAPSHOT-001', 'capability_catalog': b.ref(hist + '/catalog.json'),
            'source_reference_snapshot_id': 'SOURCE-20261027',
            'trace_id': trace['trace_id'], 'run_id': root['attributes'].get('app.run_id'),
            'session_id': root['attributes'].get('app.session_id'),
            'business_root_span_id': root_id, 'main_model_span_id': model_id,
            'execution_status': root.get('outcome'), 'answer_quality': 'not_evaluated',
            'trace_completeness': trace.get('completeness'), 'trace_export_status': manifest.get('status'),
            'source_warnings': trace.get('warnings', []), 'duration_ms': root['duration_us'] / 1000,
            'main_model_usage': metadata.get('usage'), 'cost': None,
            'trace': archive_ref, 'trace_index': b.ref(observed_prefix + '/trace_index.json'),
            'output': b.ref(observed_prefix + '/output.md'), 'output_source': evidence(output_member),
            'effective_model_context': evidence(model_input_member),
            'recorded_model_tool_calls': model_output.get('tool_calls'),
            'recorded_tool_invoke_span_count': sum(s['name'] == 'agentos.tool.invoke' for s in trace['spans']),
            'recorded_skill_activation': 'none_observed', 'payload_slot_counts': dict(counts)})
    b.json('seeds/SEED-001/reference/annotations.json', {
            'status': 'unreviewed', 'good_bad_label': None, 'gold_answer': None, 'human_feedback': [],
            'requirements': [{'id': 'REQ-001', 'text': request['user_message'],
                              'source': evidence(request_member, '/user_message'), 'kind': 'user_explicit_verbatim'}],
            'note': '原用户请求可作为要求来源；尚未生成 rubric、评分或根因结论。'})
    b.json('seeds/SEED-001/seed.json', {'seed_id': 'SEED-001',
            'title': 'WM-800 新开班事前质量管控方案',
            'source': {'kind': 'provided_trace_export', 'production_status': 'unverified',
                       'original_filename': trace_file.name, 'environment': manifest.get('environment'),
                       'warnings': manifest.get('warnings', [])},
            'task': b.ref('seeds/SEED-001/task/request.json'),
            'observations': [b.ref(observed_prefix + '/run.json')],
            'reference': b.ref('seeds/SEED-001/reference/annotations.json'),
            'benchmark_case_id': None, 'split': 'unassigned'})
    for kind in ('business', 'ontology'):
        b.json('knowledge/' + kind + '/index.json', {'status': 'not_provided', 'items': [],
                'note': '未提供独立业务规范或本体。Skill 原文位于能力快照，不冒充外部业务标准。'})
    missing_source_tools = sorted(set(exposed) - set(source_tools))
    missing_source_skills = sorted(c['name'] for c in discovery['skill_candidates'] if c['name'] not in local_skill_by_name)
    filename_match = re.search(r'trace-([0-9a-f]{32})-', trace_file.name)
    gaps = [
        {'code': 'historical_tool_contracts_redacted', 'severity': 'partial', 'details': {'message_indices': redacted_messages},
         'impact': '历史业务输入/输出 Schema 无法从此 trace 恢复；不能用传输包装 Schema 代替。'},
        _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_6'),
        _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_7'),
        {'code': 'trace_evidence_gaps', 'severity': 'partial', 'details': {'manifest_status': manifest.get('status'), 'counts': dict(counts)},
         'impact': 'unknown/upload_unconfirmed 是证据状态，不是业务失败；受影响的评估项须返回 unknown。'},
        _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_8'),
        {'code': 'external_tools_missing_from_source', 'severity': 'partial', 'details': missing_source_tools,
         'impact': '外部 MCP/OpenAPI 参数契约需从对应固定版本的运行目录另行导出。'},
        {'code': 'skill_instruction_versions_missing', 'severity': 'partial', 'details': missing_source_skills,
         'impact': '所有历史 Skill 正文均未在该 trace 披露；本地同名 Skill 也只作为未核验版本的参考。'},
        {'code': 'knowledge_not_provided', 'severity': 'info', 'impact': '没有独立 SOP、本体和专家标准答案。'},
    ]
    if filename_match and filename_match.group(1) != trace['trace_id']:
        gaps.append({'code': 'filename_trace_id_mismatch', 'severity': 'warning',
                     'details': {'filename_trace_id': filename_match.group(1), 'canonical_trace_id': trace['trace_id']},
                     'impact': '按 trace.json 与 manifest.json 一致的包内 ID 关联，原始文件不改写。'})
    b.json('data_gaps.json', {'status': 'usable_with_gaps', 'items': gaps})
    package = {'schema_version': VERSION, 'package_id': target.name, 'package_version': '2026.10.27.1',
               'created_at': now, 'purpose': 'benchmark_design_and_offline_review', 'status': 'usable_with_gaps',
               'agent_profile': 'agent/profile.json', 'agent_runtime': 'agent/runtime.json',
               'historical_capability_catalog': hist + '/catalog.json',
               'source_reference_catalog': source_prefix + '/catalog.json',
               'seeds': ['seeds/SEED-001/seed.json'], 'data_gaps': 'data_gaps.json',
               'business_knowledge': 'knowledge/business/index.json', 'ontology': 'knowledge/ontology/index.json',
               'evaluation': _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_9')}
    b.file('package.yaml', yaml_text(package) + '\n')
    # JSON mirror makes ingestion possible with Python's standard library alone.
    b.json('package.json', package)
    b.json('provenance.json', {'source_repository': str(repo), 'source_revision': source_revision,
            'original_trace_path': str(trace_file), 'original_trace_sha256': sha(trace_file.read_bytes()),
            'export_method': 'stdlib ZIP/JSON and static AST declaration projection; no application code execution',
            'exporter_version': VERSION, 'exported_at': now,
            'raw_archive_unchanged': True, 'external_services_accessed': False})
    b.json('reference_index.json', {'path_base': 'package_root', 'paths': sorted(set(b.refs))})
    report = {'status': 'usable_with_gaps', 'source_revision': source_revision['commit'],
              'trace_spans': len(trace['spans']), 'verified_payloads': checked,
              'historical_exposed_tools': len(exposed), 'historical_skill_candidates': len(historical_skills),
              'source_tools': len(source_tools),
              'source_input_schemas': sum(t['input_schema'] is not None for t in source_tools.values()),
              'source_output_schemas': sum(t['output_schema'] is not None for t in source_tools.values()),
              'source_skills_with_full_text': len(source_skills), 'seed_count': 1,
              'missing_source_tools': missing_source_tools, 'missing_source_skills': missing_source_skills}
    b.json('build_report.json', report)
    readme = _fragment('benchmarks/templates/build_agentos_input.json', 'fragment_1').format(v0=(hist), v1=(len(exposed)), v2=(len(historical_skills)), v3=(source_prefix), v4=(len(source_tools)), v5=(len(source_skills)), v6=(report['source_input_schemas']), v7=(report['source_output_schemas']), v8=(root['attributes'].get('app.run_id')), v9=(trace['trace_id']), v10=(len(trace['spans'])), v11=(checked), v12=(source_revision['commit']))
    b.file('README.md', readme)
    z.close()
    return report


def validate(root):
    def read(rel): return json.loads((root / rel).read_text(encoding='utf-8'))
    errors = []
    for p in root.rglob('*.json'):
        try: json.loads(p.read_text(encoding='utf-8'))
        except Exception as exc: errors.append(f'{p.relative_to(root)}: {exc}')
    for rel in read('reference_index.json')['paths']:
        p = (root / rel).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file(): errors.append('invalid reference: ' + rel)
    package = read('package.json')
    if (root / 'package.yaml').read_text(encoding='utf-8') != yaml_text(package) + '\n': errors.append('manifest JSON/YAML mismatch')
    run = read('seeds/SEED-001/observations/RUN-001/run.json')
    zpath = root / run['trace']['path']
    z, trace, manifest, slots, checked = inspect_archive(zpath)
    if sha(zpath.read_bytes()) != read('provenance.json')['original_trace_sha256']: errors.append('raw trace changed')
    rootspan, modelspan = run['business_root_span_id'], run['main_model_span_id']
    if run['trace_id'] != trace['trace_id']: errors.append('run trace identity mismatch')
    if (root / 'seeds/SEED-001/task/request.json').read_bytes() != z.read(slots[(rootspan, 'input')]['path']): errors.append('request differs from raw trace')
    if (root / run['output']['path']).read_bytes() != z.read(slots[(rootspan, 'output')]['path']): errors.append('output differs from raw trace')
    inp = json.loads(z.read(slots[(modelspan, 'input')]['path']))
    def check_evidence(value, document):
        if isinstance(value, dict):
            if 'archive_path' in value and 'member' in value:
                try:
                    if (root / value['archive_path']).resolve() != zpath.resolve():
                        raise ValueError('unexpected evidence archive')
                    data = z.read(value['member'])
                    if 'json_pointer' in value:
                        obj = json.loads(data)
                        pointer = value['json_pointer']
                        if pointer and not pointer.startswith('/'):
                            raise ValueError('invalid JSON pointer')
                        for part in pointer.split('/')[1:]:
                            key = part.replace('~1', '/').replace('~0', '~')
                            obj = obj[int(key)] if isinstance(obj, list) else obj[key]
                except Exception as exc:
                    errors.append(f'evidence reference in {document}: {exc}')
            for child in value.values(): check_evidence(child, document)
        elif isinstance(value, list):
            for child in value: check_evidence(child, document)
    for p in root.rglob('*.json'):
        check_evidence(json.loads(p.read_text(encoding='utf-8')), p.relative_to(root))
    if (root / 'agent/system_prompt.md').read_text(encoding='utf-8') != inp['messages'][0]['content']: errors.append('historical prompt differs')
    hist = read(package['historical_capability_catalog'])
    for item in hist['tools']:
        t = read(item['definition']['path'])
        if t['input_schema'] is not None or t['output_schema'] is not None: errors.append('historical schema silently backfilled')
    for item in hist['skills']:
        t = read(item['definition']['path'])
        if t['instructions'] is not None: errors.append('historical Skill instructions silently backfilled')
    source = read(package['source_reference_catalog'])
    for item in source['skills']:
        skill = read(item['definition']['path'])
        for f in skill['files']:
            if sha((root / f['path']).read_bytes()) != f['sha256']: errors.append('Skill copy mismatch: ' + f['path'])
    ids = {s['span_id'] for s in trace['spans']}
    for entry in manifest['entries']:
        if entry['span_id'] not in ids: errors.append('payload refers to absent span')
    z.close()
    return {'status': 'passed' if not errors else 'failed', 'checks': _asset_literal('benchmarks/definitions/build_agentos_input.json', 'literal_10'),
        'saved_payloads_verified': checked, 'errors': errors,
        'note': '结构与来源校验通过不代表历史契约完整、静态 Schema 等同运行契约或 Agent 回答正确。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--trace', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--git', default='git')
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    if args.validate_only:
        result = validate(args.output.resolve())
        checksum_path = args.output / 'checksums.json'
        if checksum_path.is_file():
            manifest = json.loads(checksum_path.read_text(encoding='utf-8'))
            root = args.output.resolve()
            actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()
                      and p != root / 'checksums.json'}
            if actual != set(manifest['files']):
                result['errors'].append('checksum inventory mismatch')
            for rel, expected in manifest['files'].items():
                path = (root / rel).resolve()
                if not path.is_relative_to(root) or not path.is_file() or sha(path.read_bytes()) != expected:
                    result['errors'].append('checksum mismatch: ' + rel)
            result['checks'].append('complete_package_sha256_inventory')
        else:
            result['errors'].append('checksums.json missing')
        result['status'] = 'failed' if result['errors'] else 'passed'
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'passed' else 1
    if not args.repo or not args.trace: parser.error('--repo and --trace are required for building')
    report = build(args)
    root = args.output.resolve()
    verification = validate(root)
    (root / 'validation_report.json').write_bytes(json_bytes(verification))
    if verification['status'] != 'passed':
        print(json.dumps(verification, ensure_ascii=False, indent=2))
        return 1
    hashes = {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in sorted(root.rglob('*')) if p.is_file()}
    (root / 'checksums.json').write_bytes(json_bytes({'algorithm': 'sha256', 'excludes': ['checksums.json'], 'files': hashes}))
    zip_path = root.with_suffix('.zip')
    if zip_path.exists(): raise FileExistsError(zip_path)
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob('*')):
            if p.is_file(): z.write(p, (Path(root.name) / p.relative_to(root)).as_posix())
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip() is not None: raise ValueError('export ZIP CRC failed')
        for rel, digest in hashes.items():
            if sha(z.read(root.name + '/' + rel)) != digest: raise ValueError('packaged file hash mismatch: ' + rel)
    print(json.dumps({'output': str(root), 'zip': str(zip_path), 'report': report, 'validation': verification}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
