"""Tool history, contract provenance and source-baseline binding regressions."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.cli import DEFAULT_ENGINE
from eval_bridge.evidence import EvidenceStore, write_json
from eval_bridge.instantiation import validate_candidate, freeze_plan
from eval_bridge.planning import prepare, bind_observation
from test_evidence_pipeline import fixture


SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string'}},
          'required': ['text'], 'additionalProperties': False}
REVISION = 'synthetic-revision-1'


def history_fixture(runtime_contract=True):
    original = fixture()
    source = {'runtime_tool_name': 'context_grep', 'description': 'Search authorized files.',
              'input_schema': copy.deepcopy(SCHEMA), 'schema_origin': 'source_declaration_projection',
              'historical_version_match': 'unverified', 'runtime_export_verified': False,
              'source_revision': REVISION, 'snapshot_id': 'SOURCE-SYNTHETIC',
              'package_path': 'agent_state/tools/context_grep.json', 'package_sha256': 'synthetic',
              'declarations': []}
    original['reference']['agent_tool_catalog'] = {'revision': {'commit': REVISION}, 'tools': [source]}
    wire = {'action': {'kind': 'tool_calls', 'tool_calls': [
        {'name': 'context_grep', 'arguments': [{'name': 'text', 'value_json': '"effective date"'}]}]}}
    invocation_messages = [{'role': 'system', 'content': 'tool_contracts contains runtime definitions.'}]
    if runtime_contract:
        invocation_messages.append({'role': 'user', 'content': json.dumps({
            'type': 'tool_contracts', 'tools': [{'name': 'context_grep',
                'description': 'Search authorized files.', 'parameters': SCHEMA}]})})
        invocation_messages.append({'role': 'system', 'content': 'tool_contracts contains runtime definitions.'})
    history = [
        {'role': 'system', 'content': 'action tool_calls and tool_result are runtime records.'},
        {'role': 'assistant', 'content': json.dumps({'action': {'kind': 'tool_calls', 'tool_calls': [
            {'name': 'context_grep', 'tool_call_id': 'call-one', 'arguments': {'text': 'effective date'}}]}})},
        {'role': 'user', 'content': json.dumps({'type': 'tool_result', 'name': 'context_grep',
            'tool_call_id': 'call-one', 'status': 'success', 'content': {'matches': []}})},
    ]

    def payload(span, slot, value):
        return {'source': f'{span}/{slot}.json', 'sha256': 'synthetic', 'value': value}

    original['observation']['events'] = [
        {'span_id': 'invoke', 'name': 'agentos.llm.call', 'start_time_unix_nano': 10, 'attributes': {},
         'payloads': {'input': payload('invoke', 'input', {'messages': invocation_messages}),
                      'output': payload('invoke', 'output', {'content': json.dumps(wire)})}},
        {'span_id': 'history', 'name': 'agentos.llm.call', 'start_time_unix_nano': 20, 'attributes': {},
         'payloads': {'input': payload('history', 'input', {'messages': history})}},
    ]
    original['observation'].pop('final_answer', None)
    return original


@unittest.skipUnless(DEFAULT_ENGINE.exists(), 'Requires supplied v4.1 engine')
class ToolBindingV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'fixture.zip'
        self.source.write_bytes(b'conversion mocked')
        self.number = 0

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self, original):
        self.number += 1
        directory = self.root / f'bundle-{self.number}'
        with patch('agentos_metric_adapter.convert', return_value=(original, {})):
            plan = prepare(self.source, directory, DEFAULT_ENGINE)
        return plan, EvidenceStore(directory)

    def candidate(self, plan, store, *, snapshot=False, method='P', rid='TU-2'):
        source_id = 'agent:tool:context_grep' if snapshot else next(
            eid for eid, r in store.records.items() if eid.startswith('run:tool_contract:')
            and r['data'].get('contract_level') == 'business')
        call_id = 'run:tool_call:0'
        criterion = 'Validate context_grep call-one against its business parameter schema.'
        if snapshot:
            criterion = f'按源码快照 {REVISION} 检查 context_grep call-one 的参数。'
        check = {'id': 'TU-object-1', 'criterion': criterion, 'method': method,
                 'prompt': None if method == 'P' else 'Check the concrete context_grep operation.',
                 'bindings': {} if method == 'J' else {'call_path': '/observation/tool_calls/0',
                     'schema': copy.deepcopy(SCHEMA), 'schema_only': True},
                 'evidence_kinds': ['task_request', 'tool_contract', 'trace_payload'],
                 'reference_ids': ['task:request'] + ([source_id] if snapshot else []),
                 'observation_ids': [call_id] + ([] if snapshot else [source_id]),
                 'required_evidence': [['trace_payload']], 'when': None,
                 'source_refs': [{'evidence_id': 'task:request', 'quote': 'Check batch B-TEST-17'},
                                 {'evidence_id': source_id, 'quote': 'context_grep'}]}
        if snapshot and method != 'J':
            check['bindings']['contract_basis'] = 'source_snapshot'
        proposed = {'rubrics': [{'rubric_id': p['id'], 'checks': [check] if p['id'] == rid else [],
            'unresolved': [] if p['id'] == rid else ['Other fixture requirements not supplied.']}
            for p in plan['profiles']]}
        return proposed, check

    def test_history_pair_and_prior_message_contract_can_bind_without_final_answer(self):
        plan, store = self.bundle(history_fixture())
        candidate, _ = self.candidate(plan, store)
        frozen = freeze_plan(plan, store, candidate)
        checks, _ = bind_observation(frozen, store, DEFAULT_ENGINE)
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0]['input_complete'])
        self.assertEqual(checks[0]['bindings']['call_path'], '/observation/tool_calls/0')

    def test_snapshot_is_usable_with_explicit_baseline_and_preserved_version_status(self):
        plan, store = self.bundle(history_fixture(runtime_contract=False))
        candidate, _ = self.candidate(plan, store, snapshot=True)
        validate_candidate(plan, store, candidate)
        self.assertEqual(store.records['agent:tool:context_grep']['data']['historical_version_match'], 'unverified')

    def test_snapshot_cannot_silently_claim_historical_contract_compliance(self):
        plan, store = self.bundle(history_fixture(runtime_contract=False))
        for change in ('criterion', 'basis'):
            candidate, check = self.candidate(plan, store, snapshot=True)
            if change == 'criterion':
                check['criterion'] = 'The actual historical tool call follows its deployed schema.'
            else:
                check['bindings'].pop('contract_basis')
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'snapshot'):
                validate_candidate(plan, store, candidate)

    def test_tu1_snapshot_judgment_requires_the_same_scope_qualification(self):
        plan, store = self.bundle(history_fixture(runtime_contract=False))
        candidate, check = self.candidate(plan, store, snapshot=True, method='J', rid='TU-1')
        validate_candidate(plan, store, candidate)
        check['criterion'] = 'Tool is compatible with the historical deployed definition.'
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            validate_candidate(plan, store, candidate)

    def test_bound_snapshot_cannot_escape_scope_by_omitting_normative_citation(self):
        plan, store = self.bundle(history_fixture())
        for rid in ('TU-1', 'TU-2'):
            candidate, check = self.candidate(plan, store, method='J', rid=rid)
            check['reference_ids'].append('agent:tool:context_grep')
            # The runtime contract is correctly cited, but the extra source
            # snapshot will also be delivered to the judge and remains scoped.
            self.assertNotIn('agent:tool:context_grep',
                             [s['evidence_id'] for s in check['source_refs']])
            with self.subTest(rid=rid), self.assertRaisesRegex(ValueError, 'snapshot'):
                validate_candidate(plan, store, candidate)
            check['reference_ids'].remove('agent:tool:context_grep')
            validate_candidate(plan, store, candidate)

    def test_snapshot_cannot_override_conflicting_available_runtime_schema(self):
        original = history_fixture()
        contract_message = original['observation']['events'][0]['payloads']['input']['value']['messages'][1]
        value = json.loads(contract_message['content'])
        value['tools'][0]['parameters']['properties']['text']['type'] = 'integer'
        contract_message['content'] = json.dumps(value)
        plan, store = self.bundle(original)
        candidate, _ = self.candidate(plan, store, snapshot=True)
        with self.assertRaisesRegex(ValueError, 'conflicts.*runtime'):
            validate_candidate(plan, store, candidate)

    def test_history_capture_contract_is_too_late_for_an_earlier_call(self):
        original = history_fixture(runtime_contract=False)
        value = original['observation']['events'][1]['payloads']['input']['value']
        value['messages'][0]['content'] += ' tool_contracts contains runtime definitions.'
        value['messages'].append({'role': 'user', 'content': json.dumps({'type': 'tool_contracts', 'tools': [
            {'name': 'context_grep', 'parameters': SCHEMA}]})})
        value['messages'].append({'role': 'system', 'content': 'tool_contracts contains runtime definitions.'})
        plan, store = self.bundle(original)
        candidate, _ = self.candidate(plan, store)
        with self.assertRaisesRegex(ValueError, 'future tool schema'):
            validate_candidate(plan, store, candidate)

    def test_unmatched_history_cannot_manufacture_a_call(self):
        original = history_fixture()
        original['observation']['events'][1]['payloads']['input']['value']['messages'].pop()
        plan, store = self.bundle(original)
        self.assertNotIn('run:tool_call:0', store.records)
        candidate, _ = self.candidate(plan, store)
        with self.assertRaisesRegex(ValueError, 'unknown|wrong-scope'):
            validate_candidate(plan, store, candidate)

    def test_derived_view_tampering_is_rejected_even_if_pointer_exists(self):
        plan, store = self.bundle(history_fixture())
        path = store.bundle / 'audit/original-input.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        original['observation']['tool_calls'][0]['arguments']['text'] = 'forged value'
        write_json(path, original)
        candidate, _ = self.candidate(plan, store)
        with self.assertRaisesRegex(ValueError, 'differs from raw trace'):
            validate_candidate(plan, store, candidate)

    def test_tu2_independent_semantics_does_not_require_a_complete_schema(self):
        plan, store = self.bundle(history_fixture(runtime_contract=False))
        candidate, check = self.candidate(plan, store, snapshot=True, method='J')
        check.update(criterion='The search text addresses the batch-assessment task.',
                     evidence_kinds=['task_request', 'trace_payload'], reference_ids=['task:request'],
                     source_refs=[{'evidence_id': 'task:request', 'quote': 'Check batch B-TEST-17'}])
        validate_candidate(plan, store, candidate)
        frozen = freeze_plan(plan, store, candidate)
        checks, _ = bind_observation(frozen, store, DEFAULT_ENGINE)
        self.assertTrue(checks[0]['input_complete'])

    def test_capability_wire_schema_cannot_substitute_for_business_parameters(self):
        original = history_fixture(runtime_contract=False)
        branch = {'type': 'object', 'properties': {
            'name': {'enum': ['context_grep'], 'description': 'Search authorized files.'},
            'arguments': {'type': 'array', 'items': {'type': 'object'}}}}
        original['observation']['events'][0]['payloads']['input']['value']['response_format'] = {
            'type': 'json_schema', 'json_schema': {'schema': {'type': 'object', 'properties': {
                'action': {'anyOf': [{'properties': {'kind': {'enum': ['tool_calls']},
                    'tool_calls': {'items': {'anyOf': [branch]}}}}]}}}}}
        plan, store = self.bundle(original)
        candidate, check = self.candidate(plan, store, snapshot=True)
        contract_id = next(eid for eid, r in store.records.items()
            if eid.startswith('run:tool_contract:') and r['data'].get('contract_level') == 'capability')
        check.update(criterion='Validate the business parameters of call-one.', reference_ids=['task:request'],
                     observation_ids=['run:tool_call:0', contract_id],
                     source_refs=[{'evidence_id': contract_id, 'quote': 'context_grep'}])
        check['bindings'].pop('contract_basis')
        check['bindings']['schema'] = copy.deepcopy(branch)
        with self.assertRaisesRegex(ValueError, 'exactly match a cited contract'):
            validate_candidate(plan, store, candidate)

    def test_capture_time_does_not_substitute_for_unknown_invocation_time(self):
        original = history_fixture()
        original['observation']['events'][0]['payloads'].pop('output')
        plan, store = self.bundle(original)
        candidate, _ = self.candidate(plan, store)
        with self.assertRaisesRegex(ValueError, 'invocation association'):
            validate_candidate(plan, store, candidate)


if __name__ == '__main__':
    unittest.main()
