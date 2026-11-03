"""Keep business schemas visible and detect omitted structural checks."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.cli import DEFAULT_ENGINE
from eval_bridge.evidence import EvidenceStore, canonical
from eval_bridge.instantiation import (
    build_instantiation_request, instantiate_plan, tool_contract_bindings,
    ToolSchemaCoverageError, _packet, _candidate_diagnostics,
)
from eval_bridge.planning import prepare
from test_tool_binding_v3 import history_fixture
import test_tool_binding_v3 as fixtures


@unittest.skipUnless(DEFAULT_ENGINE.exists(), 'Requires supplied engine')
class ToolContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.zip'
        self.source.write_bytes(b'conversion mocked')

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self, original):
        with patch('agentos_metric_adapter.convert', return_value=(original, {})):
            plan = prepare(self.source, self.root / 'bundle', DEFAULT_ENGINE)
        return plan, EvidenceStore(self.root / 'bundle')

    def candidate(self, plan, store, **kwargs):
        return fixtures.ToolBindingV3Tests.candidate(self, plan, store, **kwargs)

    def test_large_description_does_not_hide_schema_or_defs(self):
        original = history_fixture(False)
        source = original['reference']['agent_tool_catalog']['tools'][0]
        source['description'] = 'verbose capability description ' * 2000
        source['input_schema']['$defs'] = {'query': {'type': 'string', 'minLength': 1}}
        source['input_schema']['properties']['text'] = {'$ref': '#/$defs/query'}
        plan, store = self.bundle(original)
        body = json.loads(build_instantiation_request(plan, store)['user'])
        view = next(r for r in body['evidence']['reference']['records'] if r['id'] == 'agent:tool:context_grep')
        self.assertEqual(view['data']['input_schema'], source['input_schema'])
        self.assertEqual(view['data']['historical_version_match'], 'unverified')
        self.assertTrue(view['schema_inlined'])
        self.assertLess(len(canonical(view)), 3000)
        self.assertEqual(body['evidence']['tool_bindings']['calls'][0]['preferred_schema_ids'],
                         ['agent:tool:context_grep'])

    def test_oversized_schema_is_not_sliced(self):
        original = history_fixture(False)
        source = original['reference']['agent_tool_catalog']['tools'][0]
        source['input_schema']['enum'] = ['x' * 30000]
        _, store = self.bundle(original)
        packet = _packet(store.records, ['agent:tool:context_grep'], 4000)
        view = packet['records'][0]
        self.assertTrue(view['read_required'])
        self.assertFalse(view['schema_inlined'])
        self.assertNotIn('data_preview', view)
        self.assertFalse(packet['complete'])
        self.assertLessEqual(len(canonical(packet)), 4000)

    def test_runtime_contract_wins_and_conflicting_snapshot_is_marked(self):
        original = history_fixture()
        original['reference']['agent_tool_catalog']['tools'][0]['input_schema']['required'] = ['text', 'path']
        _, store = self.bundle(original)
        link = tool_contract_bindings(store)[0]
        self.assertTrue(all(e.startswith('run:tool_contract:') for e in link['preferred_schema_ids']))
        snapshot = next(c for c in link['contracts'] if c['basis'] == 'source_snapshot')
        self.assertEqual(snapshot['applicability'], 'conflicts_with_runtime')

    def test_conflicting_runtime_schemas_are_not_resolved_by_order(self):
        original = history_fixture()
        messages = original['observation']['events'][0]['payloads']['input']['value']['messages']
        body = json.loads(messages[1]['content'])
        other = copy.deepcopy(body['tools'][0])
        other['parameters']['required'] = ['text', 'path']
        body['tools'].append(other)
        messages[1]['content'] = json.dumps(body)
        _, store = self.bundle(original)
        link = tool_contract_bindings(store)[0]
        self.assertEqual(link['preferred_schema_ids'], [])
        self.assertTrue(any(c['applicability'] == 'runtime_conflict' for c in link['contracts']))

    def test_future_contract_is_not_selected_as_runtime_basis(self):
        original = history_fixture()
        future = copy.deepcopy(original['observation']['events'][0])
        future.update(span_id='future', start_time_unix_nano=30)
        future['payloads'].pop('output')
        original['observation']['events'][0]['payloads']['input']['value']['messages'] = []
        original['observation']['events'].append(future)
        _, store = self.bundle(original)
        link = tool_contract_bindings(store)[0]
        self.assertEqual(link['preferred_schema_ids'], ['agent:tool:context_grep'])
        self.assertTrue(any(c['applicability'] == 'future' for c in link['contracts']))

    def test_missing_invocation_does_not_claim_runtime_match(self):
        original = history_fixture()
        original['observation']['events'][0]['payloads'].pop('output')
        _, store = self.bundle(original)
        link = tool_contract_bindings(store)[0]
        self.assertEqual(link['preferred_schema_ids'], [])
        self.assertTrue(any(c['applicability'] == 'invocation_unlinked' for c in link['contracts']))

    def test_omitted_schema_gets_one_correction_with_concrete_evidence_ids(self):
        plan, store = self.bundle(history_fixture(False))
        first, semantic = self.candidate(plan, store, snapshot=True, method='J')
        target = next(r for r in first['rubrics'] if r['rubric_id'] == 'TU-2')
        target['unresolved'] = ['No business schema was found.']
        fixed, structural = self.candidate(plan, store, snapshot=True)
        semantic['id'] = 'semantic'
        next(r for r in fixed['rubrics'] if r['rubric_id'] == 'TU-2')['checks'].append(semantic)
        generator = Mock(side_effect=[first, fixed])
        requests = []
        frozen = instantiate_plan(plan, store, generator, requests)
        self.assertEqual(generator.call_count, 2)
        feedback = json.loads(requests[1]['user'])['local_feedback']
        self.assertEqual(feedback['gaps'][0]['preferred_schema_ids'], ['agent:tool:context_grep'])
        self.assertEqual(len(frozen['task_checks']), 2)
        self.assertEqual(frozen['unresolved'], [x for x in frozen['unresolved'] if x['rubric_id'] != 'TU-2'])

    def test_repeated_omission_does_not_freeze(self):
        plan, store = self.bundle(history_fixture(False))
        candidate, _ = self.candidate(plan, store, snapshot=True, method='J')
        generator = Mock(return_value=candidate)
        with self.assertRaises(ToolSchemaCoverageError):
            instantiate_plan(plan, store, generator)
        self.assertEqual(generator.call_count, 2)

    def test_explicit_semantic_only_configuration_does_not_force_program(self):
        plan, store = self.bundle(history_fixture(False))
        plan['methods']['TU-2'] = 'J'
        candidate, _ = self.candidate(plan, store, snapshot=True, method='J')
        generator = Mock(return_value=candidate)
        instantiate_plan(plan, store, generator)
        generator.assert_called_once()

    def test_local_diagnostics_identify_multiple_independent_errors(self):
        plan, store = self.bundle(history_fixture(False))
        candidate, check = self.candidate(plan, store, snapshot=True)
        first = copy.deepcopy(check)
        first['id'] = 'bad-scope'
        first['criterion'] = 'Claims historical deployment without snapshot qualification.'
        second = copy.deepcopy(check)
        second['id'] = 'bad-evidence'
        second['observation_ids'].append('run:status')
        next(r for r in candidate['rubrics'] if r['rubric_id'] == 'TU-2')['checks'] = [first, second]
        errors = _candidate_diagnostics(plan, store, candidate)
        self.assertEqual({e['check_id'] for e in errors}, {'bad-scope', 'bad-evidence'})

    def test_absent_schema_does_not_allow_silent_object_omission(self):
        original = history_fixture(False)
        original['reference'].pop('agent_tool_catalog')
        plan, store = self.bundle(original)
        candidate = {'rubrics': [{'rubric_id': p['id'], 'checks': [], 'unresolved': ['No prerequisites.']}
                                for p in plan['profiles']]}
        generator = Mock(return_value=candidate)
        with self.assertRaises(ToolSchemaCoverageError):
            instantiate_plan(plan, store, generator)
        self.assertEqual(generator.call_count, 2)


if __name__ == '__main__':
    unittest.main()
