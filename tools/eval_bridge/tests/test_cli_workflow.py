"""Exercise run-specific instantiation and scoring with synthetic model responses."""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.cli import DEFAULT_ENGINE, main
from eval_bridge.evidence import EvidenceStore, write_json
from eval_bridge.planning import prepare, bind_observation, verify_plan
from test_evidence_pipeline import fixture


def candidate(plan):
    result = {'rubrics': [{'rubric_id': p['id'], 'checks': [],
                           'unresolved': ['Synthetic fixture leaves this rubric unresolved.']}
                          for p in plan['profiles']]}
    target = next(r for r in result['rubrics'] if r['rubric_id'] == 'TC-1')
    target['unresolved'] = []
    target['checks'] = [{
        'id': 'TC-1.batch-result', 'criterion': 'Check batch B-TEST-17', 'method': 'J',
        'prompt': 'Determine whether the delivered answer checks batch B-TEST-17.',
        'bindings': {}, 'evidence_kinds': ['task_request', 'final_answer'],
        'reference_ids': ['task:request'], 'observation_ids': ['run:final_answer'],
       
        'required_evidence': [['task_request'], ['final_answer']], 'when': None,
        'source_refs': [{'evidence_id': 'task:request', 'quote': 'Check batch B-TEST-17'}]}]
    return result


def tool_fixture():
    original = fixture()
    schema = {'type': 'object', 'properties': {'limit': {'type': 'integer'}},
              'required': ['limit'], 'additionalProperties': False}
    original['observation']['events'][0]['payloads']['input'] = {
        'source': 'payloads/s1/input.json', 'sha256': 'synthetic-schema',
        'value': {'tools': [{'type': 'function', 'function': {
            'name': 'lookup', 'parameters': schema}}]}}
    for number, value in enumerate((3, 'invalid-limit'), 1):
        span_id = 'call-' + str(number)
        original['observation']['events'].append({
            'span_id': span_id, 'name': 'agentos.tool.call',
            'attributes': {'app.tool_name': 'lookup'}, 'payloads': {
                'input': {'source': f'payloads/{span_id}/input.json', 'sha256': span_id,
                          'value': {'id': span_id, 'tool': 'lookup', 'arguments': {'limit': value}}},
                'output': {'source': f'payloads/{span_id}/output.json', 'sha256': span_id + '-out',
                           'value': {'rows': []}}}})
    return original, schema


def tool_candidate(plan, schema):
    result = candidate(plan)
    next(r for r in result['rubrics'] if r['rubric_id'] == 'TC-1').update(
        checks=[], unresolved=['Synthetic fixture has no delivered answer.'])
    target = next(r for r in result['rubrics'] if r['rubric_id'] == 'TU-2')
    target['unresolved'] = []
    target['checks'] = [{
        'id': f'TU-2.call-{number}.protocol',
        'criterion': f'Validate only protocol and argument schema of actual lookup call-{number}.',
        'method': 'P', 'prompt': None,
        'bindings': {'call_path': f'/observation/events/{number}/payloads/input/value',
                     'schema': copy.deepcopy(schema), 'required_fields': ['id', 'tool', 'arguments'],
                     'schema_only': True},
        'evidence_kinds': ['task_request', 'tool_contract', 'trace_event', 'trace_payload'],
        'reference_ids': ['task:request'],
        'observation_ids': [f'span:call-{number}', f'span:call-{number}:input', 'span:s1:tool:0'],
        'required_evidence': [['tool_contract'], ['trace_payload']],
        'when': None,
        'source_refs': [{'evidence_id': 'task:request', 'quote': 'Check batch B-TEST-17'},
                        {'evidence_id': 'span:s1:tool:0', 'quote': 'lookup'}]}
        for number in (1, 2)]
    return result


@unittest.skipUnless(DEFAULT_ENGINE.exists(), 'Requires supplied v4.1 engine')
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / 'bundle'
        self.source = self.root / 'source.zip'
        self.source.write_bytes(b'synthetic source')
        original = fixture()
        original['observation']['final_answer'] = 'OBSERVATION_SENTINEL B-TEST-17'
        self.base = self.prepare(self.bundle, original)
        self.roles = self.root / 'roles.json'
        write_json(self.roles, {r: {'api_key': 'synthetic-key', 'model': r + '-model',
                                  'base_url': 'https://' + r + '.invalid'}
                               for r in ('instantiate', 'score')})

    def prepare(self, destination, original):
        with patch('agentos_metric_adapter.convert', return_value=(original, {})):
            return prepare(self.source, destination, DEFAULT_ENGINE)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, command, out, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main([command, '--bundle', str(self.bundle), '--out', str(self.root / out),
                         '--engine', str(DEFAULT_ENGINE), *map(str, extra)])

    def test_static_template_cannot_be_scored_live_or_implicitly(self):
        with self.assertRaises(SystemExit):
            self.cli('score', 'implicit')
        with self.assertRaises(SystemExit):
            self.cli('score', 'live', '--template-preview', '--model-roles', self.roles)
        self.assertEqual(self.cli('score', 'preview', '--template-preview'), 0)
        report = json.loads((self.root / 'preview/report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['execution']['mode'], 'template_preview')
        self.assertEqual(report['execution']['judge_call_count'], 0)

    def test_offline_instantiation_exports_observations_and_no_frozen_plan(self):
        self.assertEqual(self.cli('instantiate', 'export'), 0)
        self.assertFalse((self.root / 'export/plan.json').exists())
        request = (self.root / 'export/instantiate.request.json').read_text(encoding='utf-8')
        self.assertIn('OBSERVATION_SENTINEL', request)
        status = json.loads((self.root / 'export/status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['api_calls'], 0)

    def model_factory(self, generated=None):
        responses = candidate(self.base) if generated is None else generated
        captured = []
        class SyntheticClient:
            def __init__(self, config, store, log_dir, rounds, *, role, response_cache=True):
                if role not in ('instantiate', 'score'):
                    raise AssertionError('No review model may be created')
                self.config, self.role, self.calls = config, role, 0
                self.response_cache = response_cache

            def statistics(self):
                return {'role': self.role, 'model': self.config['model'],
                        'base_url': self.config['base_url'], 'api_calls': self.calls,
                        'response_cache_enabled': self.response_cache}

            def __call__(self, request):
                self.calls += 1
                captured.append((self.role, copy.deepcopy(request)))
                if self.role == 'instantiate':
                    return copy.deepcopy(responses)
                payload = json.loads(request['user'])
                return {'results': [{'check_id': c['check_id'], 'status': 'scored', 'verdict': True,
                                     'reason': 'run:final_answer 中的交付内容满足该检查要求。'}
                                    for c in payload['checks']]}
        return SyntheticClient, captured

    def test_runtime_instantiation_then_independent_score_has_no_review(self):
        client, calls = self.model_factory()
        with patch('eval_bridge.client.ModelClient', client):
            self.assertEqual(self.cli('instantiate', 'instantiated', '--model-roles', self.roles), 0)
            path = self.root / 'instantiated/plan.json'
            frozen = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(frozen['schema_version'], 'agentos-rubric-plan/4')
            self.assertEqual(frozen['instantiation']['validation']['method'], 'local')
            self.assertNotIn('review', frozen['instantiation'])
            store = EvidenceStore(self.bundle)
            verify_plan(frozen, store, DEFAULT_ENGINE)
            checks, _ = bind_observation(frozen, store, DEFAULT_ENGINE)
            self.assertEqual(checks[0]['id'], 'TC-1.batch-result')
            self.assertEqual(checks[0]['observation_ids'], ['run:final_answer'])
            self.assertNotIn('span:s1:output', checks[0]['evidence_ids'])
            self.assertIn('B-TEST-17', checks[0]['prompt'])
            self.assertIn(next(p for p in frozen['profiles'] if p['id'] == 'TC-1')['how']['judge_rules'], checks[0]['prompt'])
            self.assertEqual(self.cli('score', 'scored', '--plan', path, '--model-roles', self.roles), 0)
        self.assertEqual([role for role, _ in calls], ['instantiate', 'score'])
        for role, request in calls:
            self.assertIn('OBSERVATION_SENTINEL', json.dumps(request))
        self.assertFalse(list((self.root / 'instantiated').glob('review*')))
        report = json.loads((self.root / 'scored/report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['rubrics']['TC-1']['verdict'])
        self.assertEqual(report['checks']['TC-1.batch-result']['reason'],
                         'run:final_answer 中的交付内容满足该检查要求。')
        self.assertEqual(report['rubrics']['TC-1']['check_ids'], ['TC-1.batch-result'])
        self.assertEqual(report['rubrics']['TC-1']['failed_check_ids'], [])
        self.assertEqual(report['metrics']['TC']['failed_rubric_ids'], [])
        self.assertEqual(len(report['rubrics']), 22)
        self.assertEqual(report['rubrics']['AC-3']['status'], 'unverifiable')
        self.assertEqual(report['execution']['client']['model'], 'score-model')
        status = json.loads((self.root / 'instantiated/status.json').read_text(encoding='utf-8'))
        self.assertEqual(set(status['roles']), {'instantiate'})
        self.assertEqual(status['roles']['instantiate']['api_calls'], 1)

    def test_legacy_instantiation_note_does_not_override_cli_check_score(self):
        generated = candidate(self.base)
        target = next(r for r in generated['rubrics'] if r['rubric_id'] == 'TC-1')
        target['unresolved'] = ['Other unobserved delivery requirements might exist.']
        client, calls = self.model_factory(generated)
        with patch('eval_bridge.client.ModelClient', client):
            self.assertEqual(self.cli('instantiate', 'instantiated', '--model-roles', self.roles), 0)
            path = self.root / 'instantiated/plan.json'
            self.assertEqual(self.cli('score', 'scored', '--plan', path, '--model-roles', self.roles), 0)
        report = json.loads((self.root / 'scored/report.json').read_text(encoding='utf-8'))
        self.assertEqual([role for role, _ in calls], ['instantiate', 'score'])
        self.assertTrue(report['checks']['TC-1.batch-result']['verdict'])
        self.assertTrue(report['rubrics']['TC-1']['verdict'])
        self.assertEqual(report['execution']['rubric_diagnostics']['TC-1']['code'], 'instantiation_notes')

    def test_local_validation_failure_keeps_response_without_frozen_plan(self):
        invalid = candidate(self.base)
        invalid['rubrics'].pop()
        client, calls = self.model_factory(invalid)
        with patch('eval_bridge.client.ModelClient', client), self.assertRaises(SystemExit):
            self.cli('instantiate', 'rejected', '--model-roles', self.roles)
        self.assertEqual([role for role, _ in calls], ['instantiate', 'instantiate'])
        self.assertTrue((self.root / 'rejected/attempts/01.response.json').exists())
        self.assertTrue((self.root / 'rejected/attempts/02.response.json').exists())
        self.assertFalse((self.root / 'rejected/plan.json').exists())
        self.assertTrue((self.root / 'rejected/instantiate.response.json').exists())
        self.assertFalse((self.root / 'rejected/review.response.json').exists())
        status = json.loads((self.root / 'rejected/status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['stage'], 'not_frozen')
        self.assertEqual(status['phase'], 'local_validation')
        self.assertRegex(status['validation_error'], '22|missing')

    def test_transport_error_records_phase_without_exception_text(self):
        base_client, _ = self.model_factory()
        credential = 'synthetic-transport-secret'
        class FailingClient(base_client):
            def __call__(self, request):
                raise RuntimeError('Do not persist API credential: ' + credential)
        with patch('eval_bridge.client.ModelClient', FailingClient), self.assertRaises(RuntimeError):
            self.cli('instantiate', 'transport-error', '--model-roles', self.roles)
        text = (self.root / 'transport-error/status.json').read_text(encoding='utf-8')
        status = json.loads(text)
        self.assertEqual(status['phase'], 'model_call')
        self.assertEqual(status['error_type'], 'RuntimeError')
        self.assertNotIn('validation_error', status)
        self.assertNotIn(credential, text)
        self.assertNotIn('Do not persist', text)
        self.assertFalse((self.root / 'transport-error/plan.json').exists())

    def test_runtime_plan_cannot_be_reused_on_changed_observation(self):
        from eval_bridge.instantiation import freeze_plan
        frozen = freeze_plan(self.base, EvidenceStore(self.bundle), candidate(self.base))
        original = fixture()
        original['observation']['final_answer'] = 'Different answer to the same task'
        second = self.root / 'second'
        self.prepare(second, original)
        with self.assertRaisesRegex(ValueError, 'run|Run|observation|Observation'):
            bind_observation(frozen, EvidenceStore(second), DEFAULT_ENGINE)

    def test_old_task_plan_cannot_bypass_runtime_instantiation(self):
        legacy = copy.deepcopy(self.base)
        legacy.update(schema_version='agentos-rubric-plan/2', stage='frozen')
        path = self.root / 'legacy.json'
        write_json(path, legacy)
        with patch('eval_bridge.client.ModelClient', side_effect=AssertionError('Must fail before API setup')):
            with self.assertRaises(SystemExit):
                self.cli('score', 'old-schema', '--plan', path, '--model-roles', self.roles)

    def test_two_actual_tool_calls_get_separate_program_checks(self):
        original, schema = tool_fixture()
        self.bundle = self.root / 'tools'
        self.base = self.prepare(self.bundle, original)
        client, calls = self.model_factory(tool_candidate(self.base, schema))
        with patch('eval_bridge.client.ModelClient', client):
            self.assertEqual(self.cli('instantiate', 'tool-plan', '--model-roles', self.roles), 0)
            path = self.root / 'tool-plan/plan.json'
            frozen = json.loads(path.read_text(encoding='utf-8'))
            checks, _ = bind_observation(frozen, EvidenceStore(self.bundle), DEFAULT_ENGINE)
            self.assertEqual(len(checks), 2)
            self.assertIn('span:call-1:input', checks[0]['evidence_ids'])
            self.assertNotIn('span:call-2:input', checks[0]['evidence_ids'])
            self.assertIn('span:call-2:input', checks[1]['evidence_ids'])
            self.assertNotIn('span:call-1:input', checks[1]['evidence_ids'])
            self.assertEqual(self.cli('score', 'tool-score', '--plan', path, '--model-roles', self.roles), 0)
        self.assertEqual([role for role, _ in calls], ['instantiate'])
        self.assertIn('call-1', calls[0][1]['user'])
        self.assertIn('call-2', calls[0][1]['user'])
        report = json.loads((self.root / 'tool-score/report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['checks']['TU-2.call-1.protocol']['verdict'])
        self.assertFalse(report['checks']['TU-2.call-2.protocol']['verdict'])
        self.assertFalse(report['rubrics']['TU-2']['verdict'])
        self.assertEqual(report['execution']['program_call_count'], 2)
        self.assertEqual(report['execution']['judge_call_count'], 0)

    def test_same_frozen_plan_exports_baseline_and_isolated_requests(self):
        from eval_bridge.instantiation import freeze_plan
        generated = candidate(self.base)
        tc_check = next(r for r in generated['rubrics'] if r['rubric_id'] == 'TC-1')['checks'][0]
        ac_check = copy.deepcopy(tc_check)
        ac_check.update(id='AC-1.batch-fact', criterion='The delivered batch identifier is B-TEST-17.',
                        prompt='Verify the delivered batch identifier against the task request.')
        next(r for r in generated['rubrics'] if r['rubric_id'] == 'AC-1').update(
            checks=[ac_check], unresolved=[])
        frozen = freeze_plan(self.base, EvidenceStore(self.bundle), generated)
        path = self.root / 'frozen.json'
        write_json(path, frozen)
        for layout in ('none', 'shared', 'packs'):
            exports = {}
            for mode in ('baseline', 'metric'):
                out = mode + '-' + layout
                self.assertEqual(self.cli('score', out, '--plan', path, '--scoring-mode', mode,
                                         '--common-evidence-mode', layout), 0)
                exports[mode] = json.loads((self.root / out / 'requests.json').read_text(encoding='utf-8'))
                report = json.loads((self.root / out / 'report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['plan_hash'], frozen['plan_hash'])
                self.assertEqual(report['execution']['judge_call_count'], 0)
                for request in exports[mode]:
                    self.assertLessEqual(request['request_chars'], 64000)
                    self.assertGreater(request['input_request_chars'], len(request['system']) + len(request['user']))
                if layout == 'packs':
                    from eval_bridge.evidence import digest
                    directory = self.root / out / 'evidence-packs'
                    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
                    self.assertEqual(manifest['schema_version'], 'score-evidence-packs/1')
                    self.assertEqual(len(list(directory.glob('*.json'))), len(manifest['packs']) + 1)
                    stored = {}
                    for pack in manifest['packs']:
                        content = json.loads((directory / (pack['pack_id'] + '.json')).read_text(encoding='utf-8'))
                        self.assertEqual(content.pop('pack_id'), pack['pack_id'])
                        self.assertEqual(digest(content), pack['pack_id'])
                        stored[pack['pack_id']] = content['evidence']
                    for request in exports[mode]:
                        records = {eid: record for pid in request['evidence_pack_ids']
                                   for eid, record in stored[pid].items()}
                        self.assertEqual(records, json.loads(request['user'])['evidence'])
            self.assertEqual(len(exports['baseline']), 1)
            self.assertEqual(len(exports['metric']), 2)
            self.assertTrue(all(len(r['metric_ids']) == 1 for r in exports['metric']))
            if layout in ('shared', 'packs'):
                all_requests = exports['baseline'] + exports['metric']
                self.assertEqual(len({r['common_prefix_hash'] for r in all_requests}), 1)
                self.assertEqual(len({r['user'][:r['common_prefix_chars']] for r in all_requests}), 1)

    def test_live_score_can_disable_local_response_cache(self):
        client, _ = self.model_factory()
        with patch('eval_bridge.client.ModelClient', client):
            self.cli('instantiate', 'instantiated', '--model-roles', self.roles)
            self.cli('score', 'uncached', '--plan', self.root / 'instantiated/plan.json',
                     '--model-roles', self.roles, '--no-response-cache')
        report = json.loads((self.root / 'uncached/report.json').read_text(encoding='utf-8'))
        self.assertFalse(report['execution']['client']['response_cache_enabled'])
        self.assertFalse(report['execution']['response_cache_enabled'])
        self.assertEqual(report['execution']['common_evidence_mode'], 'packs')
        self.assertTrue((self.root / 'uncached/evidence-packs/manifest.json').is_file())

    def test_zero_retrieval_reserve_requires_tools_disabled(self):
        with self.assertRaises(SystemExit):
            self.cli('score', 'invalid-budget', '--template-preview', '--tool-reserve-chars', 0)
        self.assertEqual(self.cli('score', 'no-tools', '--template-preview',
                                 '--tool-reserve-chars', 0, '--max-tool-rounds', 0), 0)


if __name__ == '__main__':
    unittest.main()
