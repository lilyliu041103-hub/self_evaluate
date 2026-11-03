"""End-to-end regressions for semantic-only checks and visible source qualification."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.cli import DEFAULT_ENGINE
from eval_bridge.evidence import EvidenceStore
from eval_bridge.instantiation import freeze_plan
from eval_bridge.planning import prepare, bind_observation, initial_reference_ids
from eval_bridge.scoring import evaluate_prepared, build_group_request
from test_cli_workflow import tool_fixture, tool_candidate


@unittest.skipUnless(DEFAULT_ENGINE.exists(), 'Requires supplied metric runtime')
class ToolPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'input.zip'
        self.source.write_bytes(b'conversion fixture')

    def tearDown(self):
        self.tmp.cleanup()

    def prepared(self, original):
        with patch('agentos_metric_adapter.convert', return_value=(original, {})):
            base = prepare(self.source, self.root / 'prepared', DEFAULT_ENGINE)
        return base, EvidenceStore(self.root / 'prepared')

    def test_semantic_only_tu2_remains_ready_without_tool_schema(self):
        original, schema = tool_fixture()
        original['observation']['events'][0]['payloads']['input']['value'] = {}
        base, store = self.prepared(original)
        response = tool_candidate(base, schema)
        entry = next(x for x in response['rubrics'] if x['rubric_id'] == 'TU-2')
        c = entry['checks'][0]
        entry['checks'] = [c]
        c.update(method='J', prompt='Assess whether this actual lookup limit serves the task.',
                 bindings={}, criterion='Assess the meaning of lookup limit=3 for this task.',
                 evidence_kinds=['task_request', 'trace_payload'],
                 observation_ids=['span:call-1:input'],
                 required_evidence=[['task_request'], ['trace_payload']],
                 source_refs=[{'evidence_id': 'task:request', 'quote': 'Check batch B-TEST-17'}])
        frozen = freeze_plan(base, store, response, {'method': 'synthetic regression fixture'})
        checks, _ = bind_observation(frozen, store, DEFAULT_ENGINE)
        self.assertTrue(checks[0]['input_complete'])
        self.assertFalse(checks[0]['missing_evidence'])
        self.assertNotIn(['tool_contract'], checks[0]['missing_evidence'])
        report = evaluate_prepared(frozen, checks, store.records)
        self.assertEqual(report['execution']['group_count'], 1)
        self.assertEqual(report['execution']['groups'][0]['status'], 'ready')

    def test_source_snapshot_scope_survives_program_scoring_and_request_export(self):
        original, schema = tool_fixture()
        original['observation']['events'][0]['payloads']['input']['value'] = {}
        tool = {'runtime_tool_name': 'lookup', 'description': 'Look up task data',
                'input_schema': schema, 'source_revision': 'revision-test-1',
                'snapshot_id': 'SOURCE-TEST', 'historical_version_match': 'unverified',
                'schema_origin': 'source_declaration_projection', 'source_path': 'tool.py',
                'package_path': 'agent_state/tools/lookup.json', 'package_sha256': 'fixture',
                'declarations': []}
        other = {**tool, 'runtime_tool_name': 'unused', 'package_path': 'agent_state/tools/unused.json'}
        original['reference']['agent_tool_catalog'] = {'revision': {'commit': 'revision-test-1'},
                                                      'tools': [tool, other]}
        base, store = self.prepared(original)
        ids, _ = initial_reference_ids(store, original['task']['request'])
        self.assertIn('agent:tool:lookup', ids)
        self.assertNotIn('agent:tool:unused', ids)
        self.assertIn('agent:tool:unused', store.records)
        response = tool_candidate(base, schema)
        entry = next(x for x in response['rubrics'] if x['rubric_id'] == 'TU-2')
        c = entry['checks'][0]
        entry['checks'] = [c]
        c['criterion'] = '按源码快照 SOURCE-TEST 验证 lookup 参数结构，历史部署版本未核验。'
        c['bindings']['contract_basis'] = 'source_snapshot'
        c['reference_ids'].append('agent:tool:lookup')
        c['observation_ids'].remove('span:s1:tool:0')
        c['source_refs'][1] = {'evidence_id': 'agent:tool:lookup', 'quote': 'lookup'}
        frozen = freeze_plan(base, store, response, {'method': 'synthetic regression fixture'})
        checks, _ = bind_observation(frozen, store, DEFAULT_ENGINE)
        self.assertTrue(checks[0]['input_complete'])
        self.assertTrue(checks[0]['contract_assessment']['has_unverified_source_snapshot'])
        wire = json.loads(build_group_request({'checks': checks, 'evidence': store.records})['user'])
        self.assertTrue(wire['checks'][0]['contract_assessment']['has_unverified_source_snapshot'])
        sys.path.insert(0, str(DEFAULT_ENGINE))
        from adaptive_eval.programmatic import HANDLERS
        source = json.loads((store.bundle / 'audit/original-input.json').read_text(encoding='utf-8'))
        report = evaluate_prepared(frozen, checks, store.records, programs=HANDLERS, original_input=source)
        self.assertEqual(report['execution']['program_call_count'], 1)
        self.assertTrue(report['checks'][c['id']]['verdict'])
        self.assertTrue(report['contract_assessments'][c['id']]['has_unverified_source_snapshot'])


if __name__ == '__main__':
    unittest.main()
