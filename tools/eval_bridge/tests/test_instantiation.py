import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.evidence import EvidenceStore, build_evidence, canonical, digest, make_index, write_json
from eval_bridge.instantiation import (
    build_instantiation_request, freeze_plan, instantiate_plan, validate_candidate, verify_instantiated,
    _run_inventory, SourceQuoteError, _candidate_diagnostics,
)


RUBRICS = 'AC-1 AC-2 AC-3 BC-1 EE-4 EG-1 EG-2 EG-3 ER-1 ER-2 ER-3 HC-1 HC-2 HC-3 HC-4 PE-1 PE-2 PE-3 TC-1 TU-1 TU-2 TU-3'.split()
TOOL_SCHEMA = {'type': 'object', 'properties': {'batch': {'type': 'string'}}, 'required': ['batch'],
               'additionalProperties': False}


def profiles():
    return [{'id': rid, 'metric_id': rid.split('-')[0], 'what': 'Fixed definition for ' + rid,
             'where': ['task_request', 'document', 'table_schema', 'table_row', 'final_answer',
                       'trace_event', 'trace_payload', 'tool_contract', 'run_status'],
             'how': {'allowed_methods': ['P'] if rid in ('AC-3', 'BC-1') else ['J', 'P', 'H'],
                     'default_method': 'P' if rid in ('AC-3', 'BC-1') else 'J',
                     'judge_rules': 'Generic template judging rule',
                     'instantiate_prompt': 'Generic instantiation instruction',
                     'program_contract': 'Existing handler only',
                     'programmatic_handler': 'call_arguments' if rid == 'TU-2' else 'assertions'},
             'required_evidence': [['task_request'], ['final_answer']],
             'group': 'delivery', 'applicability_policy': 'Require reliable evidence'} for rid in RUBRICS]


def fixture():
    def payload(name, value):
        return {'source': name + '.json', 'sha256': name, 'value': value}
    return {
        'task': {'id': 'T-FIXTURE', 'request': 'Check batch B-TEST-17 and submit a Chinese summary.'},
        'reference': {'observation_contract': {}, 'expected_total': 42, 'attachments': [
            {'path': 'rules.txt', 'file_name': 'rules.txt', 'sha256': 'rules',
             'text': 'Compare approved measurements for batch B-TEST-17.'},
            {'path': 'input.xlsx', 'file_name': 'input.xlsx', 'sha256': 'sheet', 'workbook': {
                'sheets': [{'name': 'data', 'rows': [
                    {'row_number': 1, 'cells': [{'address': 'A1', 'value': 'record_id'}, {'address': 'B1', 'value': 'total'}]},
                    {'row_number': 2, 'cells': [{'address': 'A2', 'value': 'B-TEST-17'}, {'address': 'B2', 'value': '42'}]},
                ]}]}}]},
        'observation': {'run_id': 'run-1', 'execution_status': 'done', 'trace_completeness': 'partial',
            'final_answer': 'The total for B-TEST-17 is 42.', 'events': [
                {'span_id': 's-model', 'name': 'agentos.llm.call', 'attributes': {}, 'payloads': {
                    'input': payload('model-in', {'tools': [{'type': 'function', 'function': {
                        'name': 'read_batch', 'parameters': copy.deepcopy(TOOL_SCHEMA)}}]}),
                    'output': payload('model-out', {'id': 'proposal', 'tool': 'read_batch',
                                                  'arguments': {'batch': 'B-TEST-17'}})}},
                {'span_id': 's-tool', 'name': 'agentos.tool.call', 'attributes': {'app.tool.name': 'read_batch'},
                 'payloads': {'input': payload('tool-in', {'id': 'call-1', 'tool': 'read_batch',
                                                          'arguments': {'batch': 'B-TEST-17'}}),
                              'output': payload('tool-out', {'total': 42})}},
                {'span_id': 's-tool2', 'name': 'agentos.tool.call', 'attributes': {'app.tool_name': 'read_batch'},
                 'payloads': {'input': payload('tool2-in', {'id': 'call-2', 'tool': 'read_batch',
                                                           'arguments': {'batch': 17}})}}]}}


def task_check(cid='deliver-summary', method='J'):
    return {
        'id': cid, 'criterion': 'Deliver the requested Chinese summary for batch B-TEST-17.',
        'method': method, 'prompt': None if method == 'P' else 'Check that this delivered summary covers B-TEST-17 in Chinese.',
        'bindings': {} if method == 'J' else {'items': [{'actual_path': '/observation/final_answer',
                                                       'op': 'contains', 'expected': 'B-TEST-17'}]},
        'evidence_kinds': ['task_request', 'document', 'final_answer'],
        'reference_ids': ['task:request'], 'observation_ids': ['run:final_answer'],
        'required_evidence': [['task_request'], ['final_answer']], 'when': None,
        'source_refs': [{'evidence_id': 'task:request', 'quote': 'submit a Chinese summary'}],
    }


def tool_check(index=1):
    suffix = '' if index == 1 else '2'
    return {
        'id': 'validate-call-' + str(index), 'criterion': 'Validate the read_batch arguments of actual call-' + str(index),
        'method': 'P', 'prompt': None,
        'bindings': {'call_path': '/observation/events/' + str(index) + '/payloads/input/value',
                     'schema': copy.deepcopy(TOOL_SCHEMA), 'required_fields': ['id', 'tool', 'arguments'], 'schema_only': True},
        'evidence_kinds': ['task_request', 'tool_contract', 'trace_payload'],
        'reference_ids': ['task:request'],
        'observation_ids': ['span:s-model:tool:0', 'span:s-tool' + suffix + ':input'],
        'required_evidence': [['tool_contract'], ['trace_payload']], 'when': None,
        'source_refs': [{'evidence_id': 'span:s-model:tool:0', 'quote': 'read_batch'}],
    }


def numeric_check():
    check = task_check('numeric-result', 'P')
    check.update(observation_ids=['span:s-tool:output'], evidence_kinds=['task_request', 'trace_payload'],
                 required_evidence=[['trace_payload']])
    check['bindings'] = {'actual_path': '/observation/events/1/payloads/output/value/total',
                         'expected_path': '/reference/expected_total', 'abs_tol': 0}
    return check


def candidate():
    return {'rubrics': [{'rubric_id': rid, 'checks': [task_check()] if rid == 'TC-1' else [],
                         'unresolved': [] if rid == 'TC-1' else ['Missing prerequisites for ' + rid]}
                        for rid in RUBRICS]}


def entry(value, rid='TC-1'):
    return next(item for item in value['rubrics'] if item['rubric_id'] == rid)


class InstantiationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name)
        self.original = fixture()
        self.rebuild()
        self.plan = {'schema_version': 'agentos-rubric-plan/1', 'stage': 'template', 'profiles': profiles(),
                     'methods': {}, 'program_bindings': {}, 'task_id': 'T-FIXTURE',
                     'selection_policy': {'small_table_limit': 40}}
        self.plan['plan_hash'] = digest(self.plan)

    def rebuild(self):
        records, _, catalog = build_evidence(self.original)
        write_json(self.bundle / 'task.json', catalog)
        write_json(self.bundle / 'evidence.json', records)
        write_json(self.bundle / 'index.json', make_index(records))
        write_json(self.bundle / 'audit/original-input.json', self.original)
        self.store = EvidenceStore(self.bundle)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quote_feedback_identifies_missing_chinese_quotes_without_accepting_them(self):
        self.original['task']['request'] = '“初报”是待复核记录，不能作为当前事实。'
        self.rebuild()
        proposed = candidate()
        check = entry(proposed)['checks'][0]
        check['source_refs'] = [{'evidence_id': 'task:request', 'quote': '初报是待复核记录，不能作为当前事实。'}]
        original = copy.deepcopy(proposed)
        with self.assertRaises(SourceQuoteError) as error:
            validate_candidate(self.plan, self.store, proposed)
        detail = error.exception.diagnostic
        self.assertEqual(detail['check_id'], check['id'])
        self.assertEqual(detail['rubric_id'], 'TC-1')
        self.assertEqual(detail['source_ref_index'], 0)
        self.assertEqual(detail['source_excerpt'], self.original['task']['request'])
        self.assertEqual([edit['source'] for edit in detail['character_edits']], ['“', '”'])
        self.assertEqual(proposed, original)
        diagnostics = _candidate_diagnostics(self.plan, self.store, proposed)
        self.assertEqual(diagnostics[0]['source_quote'], detail)
        check['source_refs'][0]['quote'] = self.original['task']['request']
        validate_candidate(self.plan, self.store, proposed)

    def test_unrelated_quote_has_no_suggested_replacement(self):
        error = SourceQuoteError('TC-1', 'c1', 0, 'task:request', 'invented rule', '完全无关的中文原文。')
        self.assertIsNone(error.diagnostic['source_excerpt'])
        self.assertEqual(error.diagnostic['character_edits'], [])

    def test_numeric_difference_is_diagnosed_but_still_rejected(self):
        self.original['task']['request'] = '不良产品24件。'
        self.rebuild()
        proposed = candidate()
        entry(proposed)['checks'][0]['source_refs'] = [{'evidence_id': 'task:request', 'quote': '不良产品32件。'}]
        with self.assertRaises(SourceQuoteError) as error:
            validate_candidate(self.plan, self.store, proposed)
        self.assertEqual(error.exception.diagnostic['source_excerpt'], '不良产品24件。')

    def test_context_contains_real_observations_inputs_schema_and_source_pointers(self):
        request = build_instantiation_request(self.plan, self.store)
        self.assertEqual(set(request), {'stage', 'system', 'user'})
        body = json.loads(request['user'])
        observed = {r['id']: r for r in body['evidence']['observation']['records']}
        self.assertEqual(body['evidence_access']['allowed_scopes'], ['task', 'reference', 'observation'])
        self.assertEqual(observed['run:final_answer']['data'], self.original['observation']['final_answer'])
        self.assertEqual(observed['run:status']['data']['execution_status'], 'done')
        self.assertIn('span:s-tool', observed)
        self.assertIn('span:s-model:tool:0', observed)
        self.assertEqual(observed['span:s-tool:input']['data']['arguments'], {'batch': 'B-TEST-17'})
        self.assertEqual(observed['span:s-tool:input']['origin']['pointer'], '/observation/events/1/payloads/input/value')
        self.assertEqual(len(body['profiles']), 22)

    def test_context_is_bounded_and_reports_preview_and_omission(self):
        self.original['observation']['final_answer'] = 'x' * 90000
        template = self.original['observation']['events'][1]
        for number in range(100):
            event = copy.deepcopy(template)
            event['span_id'] = 'extra-' + str(number)
            event['payloads']['input']['value']['extra'] = 'large text ' * 1000
            self.original['observation']['events'].append(event)
        self.rebuild()
        body = json.loads(build_instantiation_request(self.plan, self.store)['user'])
        reference, observation = body['evidence']['reference'], body['evidence']['observation']
        self.assertLessEqual(len(canonical(reference)), 24000)
        self.assertLessEqual(len(canonical(observation)), 40000)
        answer = next(r for r in observation['records'] if r['id'] == 'run:final_answer')
        self.assertTrue(answer['content_not_inlined'])
        self.assertEqual(answer['evidence_ref'], 'run:final_answer')
        self.assertGreater(observation['omitted_count'], 0)
        self.assertFalse(observation['complete'])

    def test_prompt_limits_checks_to_supplied_targets_and_rules(self):
        system = build_instantiation_request(self.plan, self.store)['system']
        self.assertIn('The evaluation scope is the supplied input', system)
        self.assertIn('Concrete binding gaps may\ncoexist with other valid checks', system)
        self.assertIn('Rubric aggregation uses only the generated check results', system)
        self.assertIn('absent optional policies add no obligation', system)
        self.assertIn('final-answer section\nheadings alone do not establish a plan', system)

    def test_run_inventory_exposes_all_event_payloads_and_contract_counts(self):
        body = json.loads(build_instantiation_request(self.plan, self.store)['user'])
        inventory = body['evidence']['run_inventory']
        self.assertTrue(inventory['complete'])
        self.assertEqual(inventory['event_count'], 3)
        self.assertEqual(inventory['omitted_event_count'], 0)
        events = {event['evidence_id']: event for event in inventory['events']}
        self.assertEqual(events['span:s-tool']['payload_refs']['output'], 'span:s-tool:output')
        self.assertEqual(inventory['record_counts']['tool_contract'],
                         sum(r['kind'] == 'tool_contract' and r['scope'] == 'observation'
                             for r in self.store.records.values()))
        self.assertEqual(sum(inventory['contract_counts_by_level'].values()),
                         inventory['record_counts']['tool_contract'])

    def test_run_inventory_bounds_large_traces_and_reports_omitted_events(self):
        template = self.original['observation']['events'][1]
        for i in range(200):
            event = copy.deepcopy(template)
            event['span_id'] = 'extra-' + str(i)
            self.original['observation']['events'].append(event)
        self.rebuild()
        inventory = json.loads(build_instantiation_request(self.plan, self.store)['user'])['evidence']['run_inventory']
        self.assertLessEqual(len(canonical(inventory)), 16000)
        self.assertFalse(inventory['complete'])
        self.assertEqual(len(inventory['events']) + inventory['omitted_event_count'], 203)

    def test_prompt_requires_target_coverage_and_available_evidence_reads(self):
        system = build_instantiation_request(self.plan, self.store)['system']
        for requirement in ('including delivered conclusions', 'evidence-use pairs',
                            'prerequisite completion/result availability',
                            'public plans, progress messages and explicit action arrangements',
                            'next_read_arguments', 'not just the first example'):
            self.assertIn(requirement, system)
        self.assertIn('zero tool\nattempts does not imply zero data dependencies', system)
        self.assertIn("each profile's instantiation_coverage", system)
        self.assertIn('all supplied turns', system)
        self.assertIn('run_inventory.locators', system)

    def test_inventory_surfaces_interactions_errors_attempts_usage_and_budgets(self):
        self.original['observation']['recorded_request'] = {
            'user_message': 'Stop after drafting',
            'history': [{'role': 'user', 'content': 'Prepare two changes'},
                        {'role': 'user', 'content': 'Withdraw the second change'}]}
        events = self.original['observation']['events']
        events[1]['payloads']['error'] = {
            'source': 'error.json', 'sha256': 'error', 'value': {'code': 'timeout'}}
        events[0]['payloads']['metadata'] = {
            'source': 'usage.json', 'sha256': 'usage', 'value': {'usage': {'total_tokens': 120}}}
        self.rebuild()
        self.store.records['config:BC-1:budget'] = {
            'id': 'config:BC-1:budget', 'kind': 'budget', 'scope': 'reference',
            'data': {'limit': 1000}, 'origin': {'pointer': '/reference/budget'}}
        self.store.records['artifact:report'] = {
            'id': 'artifact:report', 'kind': 'artifact', 'scope': 'observation',
            'data': {'file_name': 'report.json'}, 'origin': {'pointer': '/observation/artifact'}}
        inventory = _run_inventory(self.store)
        groups = inventory['locators']
        self.assertEqual(groups['interactions']['ids'], ['interaction:0', 'interaction:1'])
        self.assertEqual(groups['error_payloads']['ids'], ['span:s-tool:error'])
        self.assertEqual(groups['resource_usage']['ids'], ['span:s-model:metadata:usage'])
        self.assertEqual(groups['budgets']['ids'], ['config:BC-1:budget'])
        self.assertIn('artifact:report', groups['deliveries']['ids'])
        self.assertEqual(set(groups['tool_attempts']['ids']),
                         {eid for eid in self.store.records if eid.startswith('run:tool_attempt:')})
        self.assertTrue(inventory['complete'])
        self.assertIn('does not confirm a fault', groups['error_payloads']['selection_note'])

    def test_locator_omissions_are_counted_and_remain_queryable(self):
        self.original['observation']['recorded_request'] = {
            'history': [{'role': 'user', 'content': 'decision ' + str(i)} for i in range(250)]}
        self.rebuild()
        inventory = _run_inventory(self.store)
        group = inventory['locators']['interactions']
        self.assertEqual(group['available_count'], 250)
        self.assertEqual(len(group['ids']) + group['omitted_count'], 250)
        self.assertFalse(group['complete'])
        self.assertFalse(inventory['complete'])
        self.assertLessEqual(len(canonical(inventory)), 16000)
        found, offset = [], 0
        while offset is not None:
            page = self.store.select(**group['query'], offset=offset, limit=100)
            found.extend(page['ids'])
            offset = page['next_offset']
        self.assertEqual(len(found), 250)

    def test_named_program_binding_gap_is_retained_with_other_valid_results(self):
        proposed = candidate()
        entry(proposed, 'AC-3').update(
            checks=[numeric_check()],
            unresolved=['Second delivered result has no declared numeric extraction path.'])
        plan = freeze_plan(self.plan, self.store, proposed)
        self.assertIn('numeric-result', [check['id'] for check in plan['task_checks']])
        missing = next(item for item in plan['unresolved'] if item['rubric_id'] == 'AC-3')
        self.assertEqual(missing['missing'], ['Second delivered result has no declared numeric extraction path.'])
        self.assertIn('semantic object coverage', plan['instantiation']['validation']['coverage_scope'])

    def test_main_model_input_and_delivered_payload_precede_bulk_event_headers(self):
        template = self.original['observation']['events'][1]
        for i in range(80):
            event = copy.deepcopy(template)
            event['span_id'] = 'extra-' + str(i)
            self.original['observation']['events'].append(event)
        event = copy.deepcopy(self.original['observation']['events'][0])
        event['span_id'] = 'late-chat'
        event['attributes']['app.purpose'] = 'chat'
        event['payloads']['input']['value'] = {'messages': [{'role': 'user', 'content': 'Actual task context'}]}
        event['payloads']['output']['value'] = self.original['observation']['final_answer']
        self.original['observation']['events'].append(event)
        self.rebuild()
        body = json.loads(build_instantiation_request(self.plan, self.store)['user'])
        ids = [r['id'] for r in body['evidence']['observation']['records']]
        self.assertIn('span:late-chat:input', ids)
        self.assertIn('span:late-chat:output', ids)
        self.assertLess(ids.index('span:late-chat:output'), ids.index('span:late-chat:input'))
        self.assertFalse(body['evidence']['observation']['complete'])

    def test_single_generation_freezes_real_tool_calls_without_another_model(self):
        proposed = candidate()
        entry(proposed, 'TU-2').update(checks=[tool_check(1), tool_check(2)], unresolved=[])
        generator = Mock(return_value=proposed)
        requests, before = [], copy.deepcopy(self.plan)
        frozen = instantiate_plan(self.plan, self.store, generator, requests, {'model': 'generator'})
        generator.assert_called_once()
        self.assertEqual([r['stage'] for r in requests], ['instantiate'])
        self.assertEqual(self.plan, before)
        self.assertEqual(frozen['schema_version'], 'agentos-rubric-plan/4')
        self.assertEqual(frozen['stage'], 'frozen')
        self.assertEqual(set(frozen['instantiation']), {'candidate', 'candidate_hash', 'validation', 'provenance'})
        self.assertEqual(frozen['instantiation']['validation']['method'], 'local')
        self.assertEqual(len(frozen['task_checks']), 3)
        self.assertTrue(verify_instantiated(frozen, self.store))
        engine = Path(__file__).resolve().parents[3] / 'runtime/metric_rubric_impl_v4_modular/adaptive_eval/programmatic.py'
        if engine.exists():
            spec = importlib.util.spec_from_file_location('fixture_programmatic', engine)
            programmatic = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(programmatic)
            checks = entry(proposed, 'TU-2')['checks']
            self.assertEqual([programmatic.call_arguments(self.original, c['bindings'])['score'] for c in checks], [1.0, 0.0])

    def test_all_rubrics_required_exactly_once(self):
        for mode in ('missing', 'duplicate', 'empty'):
            proposed = candidate()
            if mode == 'missing':
                proposed['rubrics'].pop()
            elif mode == 'duplicate':
                proposed['rubrics'][-1] = copy.deepcopy(proposed['rubrics'][0])
            else:
                proposed['rubrics'][0]['unresolved'] = []
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_candidate(self.plan, self.store, proposed)

    def test_p_only_and_explicit_methods_are_preserved(self):
        proposed = candidate()
        entry(proposed, 'AC-3')['checks'] = [task_check('numeric', 'J')]
        with self.assertRaisesRegex(ValueError, 'Method forbidden'):
            validate_candidate(self.plan, self.store, proposed)
        self.plan['methods']['TC-1'] = 'P'
        with self.assertRaisesRegex(ValueError, 'configured method'):
            validate_candidate(self.plan, self.store, candidate())

    def test_sources_cannot_turn_an_agent_answer_into_a_rule(self):
        for source in ({'evidence_id': 'run:final_answer', 'quote': '42'},
                       {'evidence_id': 'task:request', 'quote': 'made up requirement'},
                       {'evidence_id': 'rubric:AC-3', 'quote': 'Fixed definition'}):
            proposed = candidate()
            entry(proposed)['checks'][0]['source_refs'] = [source]
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_candidate(self.plan, self.store, proposed)

    def test_observation_ids_are_exact_in_run_and_scope_checked(self):
        for key, eid in (('reference_ids', 'run:final_answer'), ('observation_ids', 'task:request'),
                         ('observation_ids', 'other-run:answer')):
            proposed = candidate()
            entry(proposed)['checks'][0][key].append(eid)
            with self.subTest(key=key, eid=eid), self.assertRaisesRegex(ValueError, 'wrong-scope'):
                validate_candidate(self.plan, self.store, proposed)

    def test_task_requirement_survives_without_matching_output(self):
        proposed = candidate()
        check = entry(proposed)['checks'][0]
        check['observation_ids'] = []
        frozen = freeze_plan(self.plan, self.store, proposed)
        self.assertEqual(frozen['task_checks'][0]['observation_ids'], [])
        check['source_refs'] = [{'evidence_id': 'rubric:TC-1', 'quote': 'Fixed definition for TC-1'}]
        with self.assertRaisesRegex(ValueError, 'task/reference rule source'):
            validate_candidate(self.plan, self.store, proposed)

    def test_delivered_claim_requires_answer_not_arbitrary_observation(self):
        for rid in ('AC-1', 'AC-2'):
            proposed = candidate()
            check = task_check('claim-' + rid)
            entry(proposed, rid)['checks'] = [check]
            check['observation_ids'] = []
            with self.subTest(rid=rid), self.assertRaisesRegex(ValueError, 'delivered observation_ids'):
                validate_candidate(self.plan, self.store, proposed)
            check['evidence_kinds'].append('trace_payload')
            check['observation_ids'] = ['span:s-tool:output']
            with self.assertRaisesRegex(ValueError, 'delivered observation_ids'):
                validate_candidate(self.plan, self.store, proposed)
            check['observation_ids'] = ['run:final_answer']
            validate_candidate(self.plan, self.store, proposed)

    def test_tool_contract_alone_does_not_identify_a_tool_action(self):
        proposed = candidate()
        check = tool_check()
        check['observation_ids'] = ['span:s-model:tool:0']
        entry(proposed, 'TU-2')['checks'] = [check]
        with self.assertRaisesRegex(ValueError, 'tool observation_ids'):
            validate_candidate(self.plan, self.store, proposed)

    def test_legacy_target_label_is_rejected(self):
        proposed = candidate()
        entry(proposed)['checks'][0]['target_type'] = 'obligation'
        with self.assertRaisesRegex(ValueError, 'must have exactly'):
            validate_candidate(self.plan, self.store, proposed)

    def test_bindings_resolve_actual_fields_without_old_contract_declarations(self):
        self.assertEqual(self.original['reference']['observation_contract'], {})
        proposed = candidate()
        entry(proposed)['checks'] = [task_check(method='P')]
        validate_candidate(self.plan, self.store, proposed)
        for path in ('/observation/parsed_answer/total', '/observation/resources/tokens', '/reference/expected_total'):
            entry(proposed)['checks'][0]['bindings']['items'][0]['actual_path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_candidate(self.plan, self.store, proposed)

    def test_original_reference_cell_path_resolves_but_compact_catalog_does_not(self):
        proposed = candidate()
        check = task_check(method='P')
        check['bindings']['items'][0]['expected_path'] = '/reference/attachments/1/workbook/sheets/0/rows/1/cells/1/value'
        entry(proposed)['checks'] = [check]
        validate_candidate(self.plan, self.store, proposed)
        check['bindings']['items'][0]['expected_path'] = '/reference/tables/0/row_count'
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            validate_candidate(self.plan, self.store, proposed)

    def test_observation_pointer_cannot_switch_to_another_target(self):
        proposed = candidate()
        check = tool_check(1)
        check['bindings']['call_path'] = '/observation/events/2/payloads/input/value'
        entry(proposed, 'TU-2')['checks'] = [check]
        with self.assertRaisesRegex(ValueError, 'target record'):
            validate_candidate(self.plan, self.store, proposed)
        check['observation_ids'].append('run:status')
        check['evidence_kinds'].append('run_status')
        with self.assertRaisesRegex(ValueError, 'target record'):
            validate_candidate(self.plan, self.store, proposed)

    def test_numeric_field_is_usable_but_prose_and_self_gold_are_rejected(self):
        proposed = candidate()
        check = numeric_check()
        entry(proposed, 'AC-3')['checks'] = [check]
        validate_candidate(self.plan, self.store, proposed)
        check['bindings']['expected_path'] = check['bindings']['actual_path']
        with self.assertRaisesRegex(ValueError, 'trusted task/reference'):
            validate_candidate(self.plan, self.store, proposed)
        check['bindings']['expected_path'] = '/reference/expected_total'
        check['bindings']['actual_path'] = '/observation/final_answer'
        check['observation_ids'].append('run:final_answer')
        check['evidence_kinds'].append('final_answer')
        with self.assertRaisesRegex(ValueError, 'actual numeric field'):
            validate_candidate(self.plan, self.store, proposed)

    def test_copied_answer_cannot_become_literal_expected(self):
        self.original['observation']['events'][1]['payloads']['output']['value']['total'] = 999
        self.original['reference']['expected_total'] = 4
        self.rebuild()
        proposed, check = candidate(), numeric_check()
        check['bindings'].pop('expected_path')
        check['bindings']['expected'] = 999
        entry(proposed, 'AC-3')['checks'] = [check]
        with self.assertRaisesRegex(ValueError, 'literal expected needs explicit'):
            validate_candidate(self.plan, self.store, proposed)
        self.original['reference']['configured_program_bindings'] = {'AC-3': {'expected': 4}}
        self.rebuild()
        with self.assertRaisesRegex(ValueError, 'literal expected needs explicit'):
            validate_candidate(self.plan, self.store, proposed)
        check['bindings']['expected'] = 4
        validate_candidate(self.plan, self.store, proposed)

    def test_numeric_formula_requires_explicit_matching_recipe(self):
        proposed, check = candidate(), numeric_check()
        check['bindings'].pop('expected_path')
        check['bindings']['expression'] = '42'
        entry(proposed, 'AC-3')['checks'] = [check]
        with self.assertRaisesRegex(ValueError, 'explicit reference recipe'):
            validate_candidate(self.plan, self.store, proposed)
        recipe = {'expression': 'quantity * price', 'variables': {'quantity': '/reference/quantity', 'price': '/reference/price'}}
        self.original['reference'].update(quantity=2, price=21, configured_program_bindings={'AC-3': recipe})
        self.rebuild()
        check['bindings'].update(copy.deepcopy(recipe))
        validate_candidate(self.plan, self.store, proposed)
        check['bindings']['variables']['price'] = '/reference/expected_total'
        with self.assertRaisesRegex(ValueError, 'explicit reference recipe'):
            validate_candidate(self.plan, self.store, proposed)

    def test_numeric_tolerances_and_units_cannot_be_invented(self):
        for injected in ({'abs_tol': 1000000}, {'rel_tol': 1000000}, {'expected_unit': 'kg'},
                         {'actual_unit': 'kg'}, {'unit_conversions': {'g': {'scale': 0}}}):
            proposed, check = candidate(), numeric_check()
            check['bindings'].update(injected)
            entry(proposed, 'AC-3')['checks'] = [check]
            with self.subTest(injected=injected), self.assertRaisesRegex(ValueError, 'explicit reference configuration'):
                validate_candidate(self.plan, self.store, proposed)
        self.original['reference']['configured_program_bindings'] = {'AC-3': {'abs_tol': 0.01}}
        self.rebuild()
        proposed, check = candidate(), numeric_check()
        entry(proposed, 'AC-3')['checks'] = [check]
        check['bindings']['abs_tol'] = 0.01
        validate_candidate(self.plan, self.store, proposed)
        check['bindings']['abs_tol'] = 0.02
        with self.assertRaisesRegex(ValueError, 'tolerance must match'):
            validate_candidate(self.plan, self.store, proposed)

    def test_tool_schema_from_a_future_event_is_rejected(self):
        # Event array order deliberately disagrees with timestamps.
        self.original['observation']['events'][0]['start_time_unix_nano'] = '200'
        self.original['observation']['events'][1]['start_time_unix_nano'] = '100'
        self.rebuild()
        proposed = candidate()
        entry(proposed, 'TU-2')['checks'] = [tool_check()]
        with self.assertRaisesRegex(ValueError, 'future tool schema'):
            validate_candidate(self.plan, self.store, proposed)

    def test_conflicting_tool_schemas_require_causal_association(self):
        newer = copy.deepcopy(self.original['observation']['events'][0])
        newer['span_id'] = 's-new-schema'
        declared = newer['payloads']['input']['value']['tools'][0]['function']['parameters']
        declared['properties']['batch']['type'] = 'integer'
        self.original['observation']['events'].append(newer)
        self.rebuild()
        proposed, check = candidate(), tool_check()
        entry(proposed, 'TU-2')['checks'] = [check]
        with self.assertRaisesRegex(ValueError, 'ambiguous same-name'):
            validate_candidate(self.plan, self.store, proposed)
        self.original['observation']['events'][1]['parent_span_id'] = 's-new-schema'
        self.rebuild()
        check['bindings']['schema'] = copy.deepcopy(declared)
        check['source_refs'] = [{'evidence_id': 'span:s-new-schema:tool:0', 'quote': 'read_batch'}]
        check['observation_ids'][0] = 'span:s-new-schema:tool:0'
        validate_candidate(self.plan, self.store, proposed)

    def test_future_conflicting_schema_does_not_override_available_schema(self):
        self.original['observation']['events'][0]['start_time_unix_nano'] = 50
        self.original['observation']['events'][1]['start_time_unix_nano'] = 100
        later = copy.deepcopy(self.original['observation']['events'][0])
        later.update(span_id='later-schema', start_time_unix_nano=200)
        later['payloads']['input']['value']['tools'][0]['function']['parameters']['properties']['batch']['type'] = 'integer'
        self.original['observation']['events'].append(later)
        self.rebuild()
        proposed = candidate()
        entry(proposed, 'TU-2')['checks'] = [tool_check()]
        validate_candidate(self.plan, self.store, proposed)

    def test_tool_output_is_never_treated_as_the_call_envelope(self):
        self.original['observation']['events'][1]['payloads']['output']['value'] = {'tool': 'read_batch', 'result': 42}
        self.rebuild()
        proposed, check = candidate(), tool_check()
        check['bindings']['call_path'] = '/observation/events/1/payloads/output/value'
        check['observation_ids'].append('span:s-tool:output')
        entry(proposed, 'TU-2')['checks'] = [check]
        with self.assertRaisesRegex(ValueError, 'output is not a call'):
            validate_candidate(self.plan, self.store, proposed)

    def test_missing_envelope_field_reaches_program_validation(self):
        self.original['observation']['events'][1]['payloads']['input']['value'].pop('id')
        self.rebuild()
        proposed = candidate()
        entry(proposed, 'TU-2')['checks'] = [tool_check()]
        validate_candidate(self.plan, self.store, proposed)
        self.assertTrue(any('missing_or_invalid_call_id' in a['protocol_errors']
                            for a in self.original['observation']['tool_attempts']))

    def test_tool_schema_cannot_be_rewritten_or_belong_to_another_tool(self):
        for change in ('schema', 'name', 'proposal'):
            proposed = candidate()
            check = tool_check()
            if change == 'schema':
                check['bindings']['schema']['properties']['batch']['type'] = 'integer'
            elif change == 'name':
                self.original['observation']['events'][1]['payloads']['input']['value']['tool'] = 'other_tool'
                self.rebuild()
            else:
                check['bindings']['call_path'] = '/observation/events/0/payloads/output/value'
                check['observation_ids'].append('span:s-model:output')
            entry(proposed, 'TU-2')['checks'] = [check]
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_candidate(self.plan, self.store, proposed)

    def test_tool_schema_source_must_be_cited(self):
        proposed = candidate()
        check = tool_check()
        check['source_refs'] = [{'evidence_id': 'rubric:TU-2', 'quote': 'Fixed definition for TU-2'}]
        entry(proposed, 'TU-2')['checks'] = [check]
        with self.assertRaisesRegex(ValueError, 'cited contract'):
            validate_candidate(self.plan, self.store, proposed)

    def test_when_does_not_automatically_trust_observed_status(self):
        proposed = candidate()
        check = entry(proposed)['checks'][0]
        check['when'] = {'path': '/observation/execution_status', 'op': 'eq', 'expected': 'done'}
        with self.assertRaisesRegex(ValueError, 'explicitly trusted'):
            validate_candidate(self.plan, self.store, proposed)
        self.original['reference']['observation_contract']['trusted_observation_paths'] = ['/observation/execution_status']
        self.rebuild()
        validate_candidate(self.plan, self.store, proposed)

    def test_different_observation_invalidates_frozen_plan(self):
        frozen = freeze_plan(self.plan, self.store, candidate())
        self.original['observation']['run_id'] = 'run-2'
        self.rebuild()
        with self.assertRaisesRegex(ValueError, 'Run/observation changed'):
            verify_instantiated(frozen, self.store)

    def test_frozen_checks_and_validation_record_cannot_diverge(self):
        for change in ('check', 'validation'):
            frozen = freeze_plan(self.plan, self.store, candidate())
            if change == 'check':
                frozen['task_checks'][0]['criterion'] = 'Always pass'
            else:
                frozen['instantiation']['validation']['method'] = 'accepted without validation'
            frozen['plan_hash'] = digest({k: v for k, v in frozen.items() if k != 'plan_hash'})
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_instantiated(frozen, self.store)

    def test_dependency_validation_rejects_missing_self_and_cycles(self):
        for target in ('missing', 'deliver-summary', 'second-check'):
            proposed = candidate()
            first = task_check(method='P')
            first['bindings']['items'][0]['actual_path'] = '/evaluation/checks/' + target + '/score'
            checks = [first]
            if target == 'second-check':
                second = task_check('second-check', 'P')
                second['bindings']['items'][0]['actual_path'] = '/evaluation/checks/deliver-summary/status'
                checks.append(second)
            entry(proposed)['checks'] = checks
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_candidate(self.plan, self.store, proposed)

    def test_p_h_j_and_kind_contracts_remain_strict(self):
        for changed in (dict(method='P', prompt=None, bindings={}), dict(method='H', bindings={}),
                        dict(bindings={'unexpected': True}), dict(prompt=''),
                        dict(prompt='Generic template judging rule'), dict(evidence_kinds=['not-in-profile']),
                        dict(required_evidence=[['not-in-selection']]), dict(observation_ids=['span:s-tool:input'])):
            proposed = candidate()
            entry(proposed)['checks'][0].update(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_candidate(self.plan, self.store, proposed)


if __name__ == '__main__':
    unittest.main()
