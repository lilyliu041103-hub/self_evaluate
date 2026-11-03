import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.evidence import EvidenceStore, build_evidence, make_index, write_json
from eval_bridge.tool_observations import decode_arguments, normalize_tool_observations


SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}
CHANNEL = {'role': 'system', 'content': 'action.tool_calls and historical tool_result are runtime records. tool_contracts are declared tool data.'}


def event(span, value, *, output=None, time=1):
    result = {'span_id': span, 'name': 'agentos.llm.call', 'start_time_unix_nano': str(time),
              'attributes': {}, 'payloads': {'input': {'value': value, 'source': f'{span}/input.json', 'sha256': span}}}
    if output is not None:
        result['payloads']['output'] = {'value': output, 'source': f'{span}/output.json', 'sha256': span}
    return result


def action(call_id='c1', args=None):
    return {'assistant_content': '', 'action': {'kind': 'tool_calls', 'tool_calls': [
        {'tool_call_id': call_id, 'name': 'lookup', 'arguments': {'text': 'needle'} if args is None else args}]}}


def history(*, result_name='lookup', result_id='c1', status='success'):
    return [copy.deepcopy(CHANNEL), {'role': 'assistant', 'content': json.dumps(action())},
            {'role': 'user', 'content': json.dumps({'type': 'tool_result', 'tool_call_id': result_id,
                 'name': result_name, 'status': status, 'content': '{"matches": []}'})}]


def fixture(messages=None):
    return {'task': {'id': 'synthetic', 'request': 'Find needle in the permitted files.'},
            'reference': {'attachments': [], 'observation_contract': {}},
            'observation': {'events': [event('history', {'messages': messages or history()}, time=20)]}}


def wire_schema():
    branch = {'type': 'object', 'properties': {
        'name': {'type': 'string', 'enum': ['lookup'], 'description': 'Search permitted files.'},
        'arguments': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'value_json': {'type': 'string'}}}}}, 'required': ['name', 'arguments']}
    return {'type': 'json_schema', 'json_schema': {'schema': {'type': 'object', 'properties': {
        'action': {'anyOf': [{'type': 'object', 'properties': {
            'kind': {'enum': ['tool_calls']}, 'tool_calls': {'type': 'array', 'items': {'anyOf': [branch]}}}}]}}}}}


class ToolObservationTests(unittest.TestCase):
    def test_history_call_requires_matching_later_result(self):
        original = fixture()
        normalized = normalize_tool_observations(original)
        self.assertEqual(len(normalized['tool_calls']), 1)
        call = normalized['tool_calls'][0]
        self.assertEqual(call['arguments'], {'text': 'needle'})
        self.assertEqual(call['provenance']['execution_evidence'], 'linked_tool_result')
        self.assertIsNone(call['provenance']['invocation_event_index'])
        for messages in (history()[:-1], list(reversed(history())), history(result_name='wrong'),
                         history(result_id='other'), history(status='pending')):
            self.assertEqual(normalize_tool_observations(fixture(messages))['tool_calls'], [])

    def test_runtime_channel_and_model_event_are_required(self):
        self.assertEqual(normalize_tool_observations(fixture(history()[1:]))['tool_calls'], [])
        original = fixture()
        original['observation']['events'][0]['name'] = 'HTTP user request'
        self.assertEqual(normalize_tool_observations(original)['tool_calls'], [])

    def test_native_calls_and_tool_messages(self):
        messages = [{'role': 'assistant', 'tool_calls': [{'id': 'c1', 'type': 'function',
                     'function': {'name': 'lookup', 'arguments': '{"text":"needle"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'c1', 'content': '{"rows": []}'}]
        call = normalize_tool_observations(fixture(messages))['tool_calls'][0]
        self.assertEqual(call['id'], 'c1')
        self.assertEqual(call['arguments']['text'], 'needle')

    def test_proposed_output_is_not_execution(self):
        original = fixture([CHANNEL])
        original['observation']['events'][0]['payloads']['output'] = {
            'value': {'content': json.dumps(action())}, 'source': 'out.json', 'sha256': 'out'}
        self.assertEqual(normalize_tool_observations(original)['tool_calls'], [])

    def test_strict_array_decoding_and_duplicate_rejection(self):
        self.assertEqual(decode_arguments([{'name': 'text', 'value_json': '"needle"'},
                                          {'name': 'offset', 'value_json': '0'}]), {'text': 'needle', 'offset': 0})
        for value in ([{'name': 'text', 'value_json': '"a"'}, {'name': 'text', 'value_json': '"b"'}],
                      [{'name': 'text', 'value_json': '{"x":1,"x":2}'}],
                      [{'name': 'text', 'value_json': 'NaN'}], '{"text":"a","text":"b"}'):
            self.assertIsNone(decode_arguments(value))

    def test_malformed_duplicate_call_id_does_not_inherit_prior_result(self):
        messages = history()
        messages.insert(2, {'role': 'assistant', 'content': json.dumps(action(args=[
            {'name': 'text', 'value_json': '"a"'}, {'name': 'text', 'value_json': '"b"'}]))})
        self.assertEqual(normalize_tool_observations(fixture(messages))['tool_calls'], [])

    def test_conflicting_replayed_history_id_is_not_resolved_by_last_value(self):
        original = fixture()
        second = history()
        second[1]['content'] = json.dumps(action(args={'text': 'changed'}))
        original['observation']['events'].append(event('second', {'messages': second}, time=30))
        normalized = normalize_tool_observations(original)
        self.assertEqual(normalized['tool_calls'], [])
        self.assertEqual(normalized['tool_normalization']['rejected'][0]['reason'], 'conflicting_call_id')

    def test_same_history_replay_is_one_call_with_both_sources(self):
        original = fixture()
        original['observation']['events'].append(event('second', {'messages': history()}, time=30))
        calls = normalize_tool_observations(original)['tool_calls']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['provenance']['observed_in_event_indices'], [0, 1])
        self.assertEqual(len(calls[0]['provenance']['result_pointers']), 2)

    def test_invocation_association_requires_unique_prior_output(self):
        original = fixture()
        original['observation']['events'].insert(0, event('action', {'messages': [CHANNEL]},
            output={'content': json.dumps(action())}, time=10))
        normalized = normalize_tool_observations(original)
        self.assertEqual(normalized['tool_calls'][0]['provenance']['invocation_event_index'], 0)
        original['observation']['events'].append(event('another', {'messages': [CHANNEL]},
            output={'content': json.dumps(action())}, time=11))
        normalized = normalize_tool_observations(original)
        self.assertIsNone(normalized['tool_calls'][0]['provenance']['invocation_event_index'])

    def test_one_idless_proposal_cannot_explain_two_executed_call_ids(self):
        messages = history()
        messages.extend([{'role': 'assistant', 'content': json.dumps(action('c2'))},
            {'role': 'user', 'content': json.dumps({'type': 'tool_result', 'tool_call_id': 'c2',
                'name': 'lookup', 'status': 'success', 'content': '{"matches": []}'})}])
        original = fixture(messages)
        original['observation']['events'].insert(0, event('action', {'messages': [CHANNEL]},
            output={'content': json.dumps(action(None))}, time=10))
        calls = normalize_tool_observations(original)['tool_calls']
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c['provenance']['invocation_event_index'] is None for c in calls))

    def test_distinct_explicit_call_ids_allow_one_to_one_association(self):
        messages = history()
        messages.extend([{'role': 'assistant', 'content': json.dumps(action('c2'))},
            {'role': 'user', 'content': json.dumps({'type': 'tool_result', 'tool_call_id': 'c2',
                'name': 'lookup', 'status': 'success', 'content': '{"matches": []}'})}])
        original = fixture(messages)
        original['observation']['events'].insert(0, event('first', {'messages': [CHANNEL]},
            output={'content': json.dumps(action('c1'))}, time=10))
        original['observation']['events'].insert(1, event('second', {'messages': [CHANNEL]},
            output={'content': json.dumps(action('c2'))}, time=11))
        calls = normalize_tool_observations(original)['tool_calls']
        self.assertEqual([c['provenance']['invocation_event_index'] for c in calls], [0, 1])

    def test_tool_output_action_cannot_supply_model_invocation(self):
        for name, attrs in [('agentos.tool.call', {'app.tool_name': 'other'}),
                            ('agentos.model.tool', {'app.tool.name': 'other'}),
                            ('HTTP request', {})]:
            with self.subTest(name=name):
                original = fixture()
                fake = event('tool-result', {'id': 'other', 'tool': 'other', 'arguments': {}},
                    output={'content': json.dumps(action(None))}, time=10)
                fake.update(name=name, attributes=attrs)
                original['observation']['events'].insert(0, fake)
                calls = normalize_tool_observations(original)['tool_calls']
                lookup = next(c for c in calls if c['id'] == 'c1')
                self.assertIsNone(lookup['provenance']['invocation_event_index'])

    def test_direct_tool_event_is_preserved(self):
        original = fixture([CHANNEL])
        call_event = event('call', {'id': 'd1', 'tool': 'lookup', 'arguments': {'text': 'needle'}}, time=5)
        call_event.update(name='agentos.tool.call', attributes={'app.tool_name': 'lookup'})
        original['observation']['events'].append(call_event)
        call = normalize_tool_observations(original)['tool_calls'][0]
        self.assertEqual(call['provenance']['execution_evidence'], 'direct_tool_event')
        self.assertEqual(call['provenance']['invocation_event_index'], 1)

    def test_contract_message_uses_exact_runtime_channel(self):
        contract_message = {'role': 'user', 'content': json.dumps({'type': 'tool_contracts', 'tools': [
            {'name': 'lookup', 'description': 'Search.', 'parameters': SCHEMA}]})}
        original = fixture([contract_message, CHANNEL])
        contracts = normalize_tool_observations(original)['tool_contracts']
        self.assertEqual(contracts[0]['parameters'], SCHEMA)
        self.assertEqual(contracts[0]['provenance']['source_type'], 'message_tool_contracts')
        self.assertEqual(normalize_tool_observations(fixture([CHANNEL, contract_message]))['tool_contracts'], [])
        nested = {'role': 'tool', 'content': json.dumps({'data': json.loads(contract_message['content'])})}
        self.assertEqual(normalize_tool_observations(fixture([nested, CHANNEL]))['tool_contracts'], [])

    def test_strict_schema_has_capability_and_wire_not_business_parameters(self):
        original = fixture([CHANNEL])
        original['observation']['events'][0]['payloads']['input']['value']['response_format'] = wire_schema()
        contracts = normalize_tool_observations(original)['tool_contracts']
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0]['contract_level'], 'capability')
        self.assertIn('wire_call_schema', contracts[0])
        self.assertNotIn('parameters', contracts[0])

    def test_normalization_is_idempotent_and_ignores_injected_derived_arrays(self):
        original = fixture()
        normalize_tool_observations(original)
        expected = copy.deepcopy(original)
        original['observation']['tool_calls'] = [{'id': 'injected'}]
        original['observation']['tool_contracts'] = [{'name': 'fake'}]
        normalize_tool_observations(original)
        self.assertEqual(original, expected)

    def test_source_snapshot_is_indexed_with_provenance_and_raw_path(self):
        original = fixture()
        tool = {'runtime_tool_name': 'lookup', 'input_schema': SCHEMA, 'source_revision': 'commit',
                'historical_version_match': 'unverified', 'runtime_export_verified': False,
                'declarations': [{'text': 'trusted source code'}]}
        original['reference']['agent_tool_catalog'] = {'tools': [tool], 'revision': {'commit': 'commit'}}
        records, compact, catalog = build_evidence(original)
        record = records['agent:tool:lookup']
        self.assertEqual(record['data']['historical_version_match'], 'unverified')
        self.assertEqual(record['data']['declarations'], tool['declarations'])
        self.assertEqual(record['scope'], 'reference')
        self.assertNotIn('input_schema', compact['reference']['tool_catalog'][0])
        self.assertNotIn('tool_calls', records['run:status']['data'])
        with tempfile.TemporaryDirectory() as directory:
            write_json(Path(directory) / 'evidence.json', records)
            write_json(Path(directory) / 'index.json', make_index(records))
            write_json(Path(directory) / 'audit/original-input.json', original)
            store = EvidenceStore(directory)
            self.assertEqual(store.read_original('agent:tool:lookup'), tool)
            self.assertEqual(store.read_original('run:tool_call:0')['tool'], 'lookup')
        original['reference']['agent_tool_catalog']['tools'].append(tool)
        with self.assertRaisesRegex(ValueError, 'Duplicate evidence ID'):
            build_evidence(original)


if __name__ == '__main__':
    unittest.main()
