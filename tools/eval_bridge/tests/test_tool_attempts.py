"""Regressions for preserving invalid, failed and unexecuted evaluation objects."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.tool_observations import normalize_tool_observations
from eval_bridge.tool_attempts import score_attempt
from eval_bridge.evidence import build_evidence, EvidenceStore
from eval_bridge.cli import DEFAULT_ENGINE
from eval_bridge.planning import prepare, bind_observation
from eval_bridge.instantiation import freeze_plan, tool_contract_bindings, validate_candidate
from eval_bridge.scoring import evaluate_prepared
from test_tool_observations import fixture, event, action, history, CHANNEL, wire_schema
from test_tool_binding_v3 import history_fixture
import test_tool_binding_v3 as binding_fixtures


class ToolAttemptTests(unittest.TestCase):
    def test_formal_action_without_result_is_preserved(self):
        original = fixture([CHANNEL])
        original['observation']['events'][0]['payloads']['output'] = {
            'source': 'out.json', 'sha256': 'test', 'value': {'content': json.dumps(action(None))}}
        before = copy.deepcopy(original['observation']['events'])
        records, _, _ = build_evidence(original)
        attempts = original['observation']['tool_attempts']
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['execution_evidence'], 'not_observed')
        self.assertEqual(original['observation']['events'], before)
        self.assertIn('run:tool_attempt:0', records)
        self.assertIn('span:history:output', records)

    def test_bad_encodings_and_missing_fields_are_not_dropped(self):
        invalids = [
            {'id': 'c1', 'function': {'name': 'lookup', 'arguments': '{bad'}},
            {'id': 'c1', 'function': {'name': 'lookup', 'arguments': '{"text":"a","text":"b"}'}},
            {'id': 'c1', 'function': {'name': 'lookup', 'arguments': [
                {'name': 'text', 'value_json': '"a"'}, {'name': 'text', 'value_json': '"b"'}]}},
            {'id': 'c1', 'function': {'arguments': {}}},
            {'function': {'name': 'lookup', 'arguments': {}}},
            {'id': 7, 'function': {'name': 'lookup', 'arguments': {}}},
            {'id': 'c1', 'function': {'name': 'lookup'}},
            'invalid call',
        ]
        for raw in invalids:
            with self.subTest(raw=raw):
                original = fixture([CHANNEL])
                original['observation']['events'][0]['payloads']['output'] = {
                    'value': {'tool_calls': [raw]}, 'source': 'out', 'sha256': 'test'}
                normalize_tool_observations(original)
                attempt = original['observation']['tool_attempts'][0]
                self.assertEqual(attempt['raw'], raw)
                self.assertTrue(attempt['protocol_errors'])
                handler = Mock(side_effect=AssertionError('Malformed protocol must be decided before schema'))
                result = score_attempt(original, {'call_path': '/observation/tool_attempts/0', 'protocol_only': True}, handler)
                self.assertFalse(result['verdict'])

    def test_unparseable_action_envelope_is_retained(self):
        original = fixture([CHANNEL])
        e = original['observation']['events'][0]
        e['payloads']['input']['value']['response_format'] = wire_schema()
        raw = '{"action":{"kind":"tool_calls",'
        e['payloads']['output'] = {'value': {'content': raw}, 'source': 'out', 'sha256': 'test'}
        normalize_tool_observations(original)
        self.assertEqual(original['observation']['tool_attempts'][0]['raw'], raw)
        self.assertIn('invalid_action_json', original['observation']['tool_attempts'][0]['protocol_errors'])

    def test_malformed_response_schema_does_not_hide_formal_output(self):
        original = fixture([CHANNEL])
        e = original['observation']['events'][0]
        e['payloads']['input']['value']['response_format'] = {
            'type': 'json_schema', 'json_schema': {'schema': {'properties': {'action': {'anyOf': None}}}}}
        e['payloads']['output'] = {'value': {'content': json.dumps(action(None))}, 'source': 'out', 'sha256': 'test'}
        normalize_tool_observations(original)
        self.assertEqual(len(original['observation']['tool_attempts']), 1)

    def test_empty_formal_action_remains_an_evaluation_object(self):
        original = fixture([CHANNEL])
        raw = {'action': {'kind': 'tool_calls', 'tool_calls': []}}
        original['observation']['events'][0]['payloads']['output'] = {
            'value': {'content': json.dumps(raw)}, 'source': 'out', 'sha256': 'test'}
        normalize_tool_observations(original)
        self.assertEqual(len(original['observation']['tool_attempts']), 1)
        self.assertEqual(original['observation']['tool_attempts'][0]['raw'], raw['action'])

    def test_missing_capture_is_retained_as_unknown(self):
        original = fixture([CHANNEL])
        e = event('missing-input', {})
        e.update(name='agentos.tool.call', attributes={'app.tool.name': 'lookup'}, payloads={})
        original['observation']['events'].append(e)
        normalize_tool_observations(original)
        attempt = original['observation']['tool_attempts'][0]
        self.assertFalse(attempt['raw_available'])
        result = score_attempt(original, {'call_path': '/observation/tool_attempts/0', 'protocol_only': True}, Mock())
        self.assertEqual(result['status'], 'unverifiable')

    def test_duplicate_id_conflicts_keep_both_occurrences(self):
        original = fixture([CHANNEL,
            {'role': 'assistant', 'content': json.dumps(action('same', {'text': 'a'}))},
            {'role': 'assistant', 'content': json.dumps(action('same', {'text': 'b'}))}])
        normalize_tool_observations(original)
        attempts = original['observation']['tool_attempts']
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len({a['evaluation_id'] for a in attempts}), 2)
        self.assertTrue(all('conflicting_call_id' in a['association_errors'] for a in attempts))

    def test_mismatched_return_keeps_call_and_return_pointer(self):
        original = fixture(history(result_name='other'))
        normalize_tool_observations(original)
        attempt = original['observation']['tool_attempts'][0]
        self.assertIn('mismatched_result_tool', attempt['association_errors'])
        self.assertTrue(attempt['provenance']['result_pointers'])

    def test_error_result_is_preserved(self):
        original = fixture(history(status='failed'))
        normalize_tool_observations(original)
        attempt = original['observation']['tool_attempts'][0]
        self.assertEqual(attempt['result_status'], 'failed')
        self.assertEqual(attempt['execution_evidence'], 'linked_tool_result')

    def test_direct_event_without_valid_input_keeps_error_payload(self):
        original = fixture([CHANNEL])
        e = event('bad-tool', None)
        e.update(name='agentos.tool.call', attributes={'app.tool.name': 'lookup'})
        e['payloads']['error'] = {'value': {'message': 'rejected'}, 'source': 'error', 'sha256': 'test'}
        original['observation']['events'].append(e)
        records, _, _ = build_evidence(original)
        attempt = original['observation']['tool_attempts'][0]
        self.assertEqual(attempt['raw'], None)
        self.assertEqual(attempt['result_status'], 'error')
        self.assertIn('span:bad-tool:error', records)
        self.assertTrue(attempt['protocol_errors'])

    def test_reasoning_and_quoted_tool_results_are_not_formal_actions(self):
        original = fixture([CHANNEL])
        original['observation']['events'][0]['payloads']['output'] = {
            'value': {'content': 'Here is an example.', 'reasoning': json.dumps(action())}, 'source': 'out', 'sha256': 'test'}
        normalize_tool_observations(original)
        self.assertEqual(original['observation']['tool_attempts'], [])

    def test_complete_inventory_is_recomputed_and_idempotent(self):
        original = history_fixture(False)
        normalize_tool_observations(original)
        expected = copy.deepcopy(original)
        self.assertEqual(len(original['observation']['tool_attempts']), 1)
        self.assertEqual(len(original['observation']['tool_attempts'][0]['provenance']['occurrences']), 2)
        original['observation']['tool_attempts'] = [{'evaluation_id': 'forged'}]
        normalize_tool_observations(original)
        self.assertEqual(original, expected)


@unittest.skipUnless(DEFAULT_ENGINE.exists(), 'Requires engine')
class AttemptPipelineTests(unittest.TestCase):
    def test_unexecuted_nested_parameters_reach_program_and_fail_rubric(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source.zip'
            source.write_bytes(b'mocked')
            original = history_fixture(False)
            raw = {'action': {'kind': 'tool_calls', 'tool_calls': [{'name': 'context_grep',
                'arguments': [{'name': 'context_grep', 'value_json': '{"text":"rules"}'}]}]}}
            original['observation']['events'].append(event('bad-output', {'messages': [CHANNEL]},
                output={'content': json.dumps(raw)}, time=30))
            with patch('agentos_metric_adapter.convert', return_value=(original, {})):
                plan = prepare(source, root / 'bundle', DEFAULT_ENGINE)
            store = EvidenceStore(root / 'bundle')
            links = tool_contract_bindings(store)
            self.assertEqual(len(links), 2)
            self.assertTrue(all(link['preferred_schema_ids'] for link in links))
            candidate, base_check = binding_fixtures.ToolBindingV3Tests.candidate(self, plan, store, snapshot=True)
            target = next(r for r in candidate['rubrics'] if r['rubric_id'] == 'TU-2')
            target['checks'] = []
            for index in range(2):
                check = copy.deepcopy(base_check)
                check['id'] = f'TU-2-{index}'
                check['bindings']['call_path'] = f'/observation/tool_attempts/{index}'
                check['observation_ids'] = [f'run:tool_attempt:{index}']
                target['checks'].append(check)
            frozen = freeze_plan(plan, store, candidate)
            checks, _ = bind_observation(frozen, store, DEFAULT_ENGINE)
            sys.path.insert(0, str(DEFAULT_ENGINE))
            from adaptive_eval.programmatic import HANDLERS
            report = evaluate_prepared(frozen, checks, store.records, programs=HANDLERS, original_input=original)
            self.assertTrue(report['checks']['TU-2-0']['verdict'])
            self.assertFalse(report['checks']['TU-2-1']['verdict'])
            self.assertFalse(report['rubrics']['TU-2']['verdict'])

    def test_protocol_defect_without_business_schema_can_be_scored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source.zip'
            source.write_bytes(b'mocked')
            original = fixture([CHANNEL])
            original['observation']['events'][0]['payloads']['output'] = {
                'value': {'tool_calls': [{'id': 'broken', 'function': {'arguments': '{bad'}}]},
                'source': 'out', 'sha256': 'test'}
            with patch('agentos_metric_adapter.convert', return_value=(original, {})):
                plan = prepare(source, root / 'bundle', DEFAULT_ENGINE)
            store = EvidenceStore(root / 'bundle')
            profile = next(p for p in plan['profiles'] if p['id'] == 'TU-2')
            check = {'id': 'protocol', 'criterion': 'The formal tool action has a valid protocol envelope.',
                'method': 'P', 'prompt': None,
                'bindings': {'call_path': '/observation/tool_attempts/0', 'protocol_only': True, 'schema_only': True},
                'evidence_kinds': ['trace_payload'], 'reference_ids': [], 'observation_ids': ['run:tool_attempt:0'],
                'required_evidence': [], 'when': None,
                'source_refs': [{'evidence_id': 'rubric:TU-2', 'quote': profile['how']['definition']}]}
            candidate = {'rubrics': [{'rubric_id': p['id'], 'checks': [check] if p['id'] == 'TU-2' else [],
                                      'unresolved': [] if p['id'] == 'TU-2' else ['Not supplied.']} for p in plan['profiles']]}
            frozen = freeze_plan(plan, store, candidate)
            checks, _ = bind_observation(frozen, store, DEFAULT_ENGINE)
            self.assertTrue(checks[0]['input_complete'])
            report = evaluate_prepared(frozen, checks, store.records,
                programs={'call_arguments': Mock(side_effect=AssertionError('Should use protocol failure'))}, original_input=original)
            self.assertFalse(report['rubrics']['TU-2']['verdict'])


if __name__ == '__main__':
    unittest.main()
