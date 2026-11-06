"""Repackage the verified AgentOS seed in a Penguin-inspired layout (offline only)."""
from eval_bridge.resources import literal as _fragment
from eval_bridge.resources import literal as _asset_literal
import hashlib
import json
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

from build_agentos_input import yaml_text, inspect_archive

ROOT = Path(__file__).resolve().parents[1]
import os
if not os.environ.get('AIMS_REPACKAGE_INPUT') or not os.environ.get('AIMS_REPACKAGE_OUTPUT'):
    raise RuntimeError('Set AIMS_REPACKAGE_INPUT and AIMS_REPACKAGE_OUTPUT explicitly')
OLD = Path(os.environ['AIMS_REPACKAGE_INPUT']).resolve()
NEW = Path(os.environ['AIMS_REPACKAGE_OUTPUT']).resolve()
PIN = '079a07f407375b3401bdf541790183a142565ee1'


def digest(b):
    return hashlib.sha256(b).hexdigest()


def read(p):
    return json.loads(p.read_text(encoding='utf-8'))


def write(rel, value):
    p = NEW / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    p.write_text(value, encoding='utf-8', newline='\n')


MAPPING = _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_1')


def remap(s):
    for before, after in MAPPING:
        if s.startswith(before):
            return after + s[len(before):]
    return s


def rewrite(obj):
    if isinstance(obj, str):
        return remap(obj)
    if isinstance(obj, list):
        return [rewrite(v) for v in obj]
    if isinstance(obj, dict):
        return {k: rewrite(v) for k, v in obj.items()}
    return obj


def build():
    assert not NEW.exists(), 'Never overwrite an existing package'
    # Validate the previous package before relying on its source projection.
    previous = read(OLD / 'checksums.json')['files']
    for rel, expected in previous.items():
        assert digest((OLD / rel).read_bytes()) == expected, rel
    NEW.mkdir(parents=True)
    for p in OLD.rglob('*'):
        if not p.is_file():
            continue
        rel = p.relative_to(OLD).as_posix()
        dest = remap(rel)
        if dest == rel:
            continue
        target = NEW / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        # Keep raw task, skill content and archive byte-identical; only generated metadata rebased.
        skill_resource = rel.startswith('capabilities/SOURCE-20261027/skills/') and p.name != 'skill.json'
        raw_task = rel == 'seeds/SEED-001/task/request.json'
        if p.suffix == '.json' and not skill_resource and not raw_task:
            write(dest, rewrite(read(p)))
        else:
            shutil.copyfile(p, target)

    profile = read(NEW / 'agent_state/profile.json')
    profile['scope_note'] = '职责文字源于历史提示词；本目录工具和 Skill 来自 1027 源码，历史版本对应关系未验证。'
    profile['system_prompt'] = {'path': 'agent_state/source_reference/meta_agent_system.prompt.md'}
    profile['historical_system_messages'] = profile.pop('all_system_messages')
    write('agent_state/profile.json', profile)
    write('agent_state/AGENTS.md', '# AgentOS Meta Agent\n\n' + profile['description'] + _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_2'))
    config = {
        'name': 'AgentOS Meta Agent', 'description': profile['description'], 'version': 1,
        'system_prompt': (NEW / 'agent_state/source_reference/meta_agent_system.prompt.md').read_text(encoding='utf-8'),
        'tools': {'builtin': [], 'mcpServers': []},
        'x_agentos': {
            'purpose': 'offline_evaluation_input', 'executable_adapter_configured': False,
            'source_snapshot_id': 'SOURCE-20261027',
            'source_revision': read(NEW / 'agent_state/source_reference/revision.json')['commit'],
            'contract_catalog': {'path': 'agent_state/catalog.json'},
            'historical_runtime': {'path': 'observations/RUN-001/context/runtime.json'},
            'historical_version_match': 'unverified',
            'version_note': 'version=1 是本包的状态版本，不是未知的历史 AgentOS 版本。',
            'tools_note': 'Penguin 可执行工具注册为空；AgentOS 静态契约由 contract_catalog 引用，不能直接执行。',
            'runtime_note': '历史模型参数在 observations 中保留，不推断当前运行参数。'
        }
    }
    write('agent_state/system_config.yaml', yaml_text(config) + '\n')
    write('agent_state/system_config.json', config)

    request = read(NEW / 'benchmark/CASE-001/statement/request.json')
    statement = '# WM-800 新开班事前质量管控方案\n\n' + request['user_message'] + _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_3')
    write('benchmark/CASE-001/statement/README.md', statement)
    write('benchmark/benchmark_config.toml', _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_4'))
    criteria = _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_5')
    rubric = _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_6')
    for cid, title, points, requirement, scoring in criteria:
        rubric += f'### {cid} {title}（{points} 分）\n\n{requirement}\n\n{scoring}\n\n'
    rubric += _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_7')
    write('benchmark/CASE-001/rubric/README.md', rubric)
    write('benchmark/CASE-001/rubric/criteria.json', {'status': 'draft', 'maximum_score': 100,
          'gold_answer': None, 'calibrated': False, 'authorship': 'assistant_derived_from_original_request',
          'created_after_historical_run': True,
          'criteria': [{'id': a, 'title': b, 'max_points': c, 'requirement': d, 'scoring': e} for a,b,c,d,e in criteria]})
    run = read(NEW / 'observations/RUN-001/run.json')
    run.update(_asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_8'))
    write('observations/RUN-001/run.json', run)
    write('data_gaps.json', rewrite(read(OLD / 'data_gaps.json')))
    write('provenance/original_export.json', read(OLD / 'provenance.json'))
    write('provenance/repackage.json', {'date': '2026-11-01', 'source_package': str(OLD),
          'penguin_reference_commit': PIN, 'schema_compatibility': 'layout_and_selected_fields_only',
          'source_checkout': 'D:/code/agentos_1027/agentos',
          'source_checkout_commit_verified_at_repackaging': config['x_agentos']['source_revision'],
          'benchmark_import_ready': False, 'reason': 'draft rubric, no calibration; agent tool execution adapter not implemented',
          'trace_unchanged': True, 'original_package_unchanged': True})
    package = _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_9')
    write('package.yaml', yaml_text(package) + '\n')
    write('package.json', package)
    write('README.md', _fragment('benchmarks/templates/repackage_agentos_penguin.json', 'fragment_1').format(v0=(PIN)))
    return config, criteria


def validate(config, criteria):
    checked_refs = 0
    archive, trace, manifest, slots, checked_payloads = inspect_archive(NEW / 'observations/RUN-001/trace.zip')
    def visit(v):
        nonlocal checked_refs
        if isinstance(v, dict):
            if 'path' in v and isinstance(v['path'], str) and v['path'].startswith(
                    ('agent_state/', 'benchmark/', 'observations/', 'provenance/', 'data_gaps.json')):
                p = (NEW / v['path']).resolve()
                # Trace slot entries use ZIP-local payload paths, not package paths.
                if not v['path'].startswith(('payloads/', 'trace.json', 'manifest.json')):
                    assert p.is_relative_to(NEW.resolve()) and p.exists(), v['path']
                    checked_refs += 1
            if 'archive_path' in v and 'member' in v:
                assert v['archive_path'] == 'observations/RUN-001/trace.zip'
                b = archive.read(v['member'])
                if 'json_pointer' in v:
                    obj = json.loads(b)
                    for part in v['json_pointer'].split('/')[1:]:
                        key = part.replace('~1', '/').replace('~0', '~')
                        obj = obj[int(key)] if isinstance(obj, list) else obj[key]
            for child in v.values():
                visit(child)
        elif isinstance(v, list):
            for child in v:
                visit(child)
    for p in NEW.rglob('*.json'):
        # Original task and copied Skill resources are evidence, not package manifests.
        rel = p.relative_to(NEW).as_posix()
        value = read(p)
        if rel.startswith('agent_state/skills/') and p.name != 'skill.json':
            continue
        if rel.startswith('provenance/') or rel.endswith('statement/request.json'):
            continue
        visit(value)
    manifest = tomllib.loads((NEW / 'benchmark/benchmark_config.toml').read_text(encoding='utf-8'))
    assert manifest['id'] == 'benchmark' and manifest['status'] == 'draft' and manifest['runs'] == 1
    assert sum(c[2] for c in criteria) == 100
    assert (NEW / 'agent_state/system_config.yaml').read_text(encoding='utf-8') == yaml_text(config) + '\n'
    for old, new in _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_10'):
        assert (OLD / old).read_bytes() == (NEW / new).read_bytes()
    for p in (NEW / 'agent_state/skills').glob('*/skill.json'):
        for f in read(p)['files']:
            assert digest((NEW / f['path']).read_bytes()) == f['sha256']
    for p in (NEW / 'observations/RUN-001/context/observed_tools_and_skills/tools').glob('*.json'):
        value = read(p)
        assert value['input_schema'] is None and value['output_schema'] is None
    public = (NEW / 'benchmark/CASE-001/statement/README.md').read_text(encoding='utf-8')
    assert 'rubric' not in public and 'output.md' not in public and 'trace.zip' not in public
    assert not (NEW / 'capabilities').exists()
    archive.close()
    if (NEW / 'checksums.json').exists():
        hashes = read(NEW / 'checksums.json')['files']
        actual = {p.relative_to(NEW).as_posix() for p in NEW.rglob('*')
                  if p.is_file() and p != NEW / 'checksums.json'}
        assert actual == set(hashes), 'Package file inventory changed'
        with zipfile.ZipFile(NEW.parent / 'eval-input.zip') as z:
            assert z.testzip() is None
            assert set(z.namelist()) == {'eval-input/' + p for p in actual | {'checksums.json'}}
            for rel, h in hashes.items():
                assert digest((NEW / rel).read_bytes()) == h, rel
                assert digest(z.read('eval-input/' + rel)) == h, rel
            assert z.read('eval-input/checksums.json') == (NEW / 'checksums.json').read_bytes()
        print(json.dumps({'status': 'passed', 'file_hashes_verified': len(hashes),
                          'saved_payloads_verified': checked_payloads,
                          'archive_and_directory_identical': True}, indent=2))
        return
    write('validation_report.json', {'status': 'passed', 'resolved_file_references': checked_refs,
        'saved_payloads_verified': checked_payloads, 'rubric_total': 100,
        'checks': _asset_literal('benchmarks/definitions/repackage_agentos_penguin.json', 'literal_11'),
        'scope': '离线输入包的结构与证据校验；非 Penguin 运行兼容性测试，也非业务答案正确性认证。'})
    hashes = {p.relative_to(NEW).as_posix(): digest(p.read_bytes()) for p in sorted(NEW.rglob('*')) if p.is_file()}
    write('checksums.json', {'algorithm': 'sha256', 'excludes': ['checksums.json'], 'files': hashes})
    zip_path = NEW.parent / 'eval-input.zip'
    assert not zip_path.exists()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(NEW.rglob('*')):
            if p.is_file():
                z.write(p, 'eval-input/' + p.relative_to(NEW).as_posix())
    with zipfile.ZipFile(zip_path) as z:
        assert z.testzip() is None
        for rel, h in hashes.items():
            assert digest(z.read('eval-input/' + rel)) == h
    print(json.dumps({'package': str(NEW), 'zip': str(zip_path), 'files': len(hashes)+1,
                      'validation': read(NEW / 'validation_report.json')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    if '--validate-only' in sys.argv:
        config = read(NEW / 'agent_state/system_config.json')
        criteria = [(c['id'], c['title'], c['max_points']) for c in
                    read(NEW / 'benchmark/CASE-001/rubric/criteria.json')['criteria']]
        validate(config, criteria)
    else:
        validate(*build())
