"""EG-3 evidence transport and verdict integration; no semantic model simulation."""
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
from eval_bridge.instantiation import freeze_plan, validate_candidate
from eval_bridge.planning import prepare, bind_observation
from eval_bridge.scoring import evaluate_prepared
from test_evidence_pipeline import fixture


class InputAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = fixture()
        self.original['observation'].update(
            execution_status='done', trace_completeness='full',
            final_answer_status='recorded', final_answer='I read every supplied record.')
        self.original['observation']['events'] = [{
            'span_id': 'initial', 'name': 'agentos.llm.call', 'attributes': {},
            'payloads': {'input': {'source': 'initial/input.json', 'sha256': 'initial',
                'value': {'messages': [{'role': 'user', 'content': 'Excerpt: B-TEST-17'}],
                          'context_is_bounded_excerpt': True}}}}]
        source = self.root / 'source.zip'
        source.write_bytes(b'synthetic fixture')
        with patch('agentos_metric_adapter.convert', return_value=(self.original, {})):
            self.base = prepare(source, self.root / 'bundle', DEFAULT_ENGINE)
        self.store = EvidenceStore(self.root / 'bundle')
        self.row_ids = [eid for eid, r in self.store.records.items() if r['kind'] == 'table_row']
        self.candidate = {'rubrics': [
            {'rubric_id': p['id'], 'checks': [], 'unresolved': ['No target in this fixture.']}
            for p in self.base['profiles']]}

    def tearDown(self):
        self.temp.cleanup()

    def check(self, observed=None):
        check = {
            'id': 'EG-3.batch-input',
            'criterion': 'Were the supplied B-TEST-17 measurement inputs acquired before final delivery?',
            'method': 'J', 'bindings': {},
            'prompt': 'Compare the supplied B-TEST-17 rows with actual inputs and returns before final delivery. '
                      'A bounded excerpt is not automatically a failure; require a complete acquisition window.',
            'evidence_kinds': ['task_request', 'table_row', 'table_schema', 'trace_payload',
                               'final_answer', 'run_status'],
            'reference_ids': ['task:request'] + self.row_ids,
            'observation_ids': ['span:initial:input'] if observed is None else observed,
            'required_evidence': [], 'when': None,
            'source_refs': [{'evidence_id': 'task:request', 'quote': 'Check batch B-TEST-17'}]}
        target = next(r for r in self.candidate['rubrics'] if r['rubric_id'] == 'EG-3')
        target.update(checks=[check], unresolved=[])
        return check

    def bound(self):
        plan = freeze_plan(self.base, self.store, self.candidate)
        checks, _ = bind_observation(plan, self.store, DEFAULT_ENGINE)
        return plan, checks

    def test_source_and_excerpt_remain_distinct_and_failure_reaches_eg(self):
        self.check()
        plan, checks = self.bound()
        requests = []

        def judge(request):
            payload = json.loads(request['user'])
            # The fixture judge verifies transport only; the supplied verdict is
            # deliberately fixed, not a substitute for semantic coverage judging.
            for eid in self.row_ids + ['span:initial:input']:
                self.assertIn(eid, payload['evidence'])
            self.assertEqual(payload['checks'][0]['observation_ids'], ['span:initial:input'])
            self.assertNotIn('run:final_answer', payload['checks'][0]['observation_ids'])
            return {'results': [{'check_id': 'EG-3.batch-input', 'status': 'scored', 'verdict': False}]}

        report = evaluate_prepared(plan, checks, self.store.records, judge_fn=judge,
                                   request_sink=requests, original_input=self.original)
        self.assertEqual(len(requests), 1)
        self.assertFalse(report['checks']['EG-3.batch-input']['verdict'])
        self.assertFalse(report['rubrics']['EG-3']['verdict'])
        self.assertFalse(report['metrics']['EG']['verdict'])
        self.assertEqual(report['metrics']['TC']['status'], 'unverifiable')

    def test_final_self_report_and_original_attachment_cannot_supply_acquisition_evidence(self):
        self.check(observed=['run:final_answer'])
        plan, checks = self.bound()
        self.assertFalse(checks[0]['input_complete'])
        report = evaluate_prepared(plan, checks, self.store.records,
            judge_fn=lambda _: self.fail('Missing acquisition evidence must stay unknown'),
            original_input=self.original)
        self.assertEqual(report['checks']['EG-3.batch-input'], {'status': 'unverifiable', 'verdict': None})

    def test_later_read_is_bound_alongside_initial_excerpt(self):
        self.original['observation']['events'].append({
            'span_id': 'later', 'name': 'agentos.tool.call', 'attributes': {'app.tool.name': 'read_table'},
            'payloads': {'output': {'source': 'later/output.json', 'sha256': 'later',
                'value': {'scope': 'B-TEST-17', 'complete': True,
                          'rows': [{'value': 10}, {'value': 11}]}}}})
        with patch('agentos_metric_adapter.convert', return_value=(self.original, {})):
            self.base = prepare(self.root / 'source.zip', self.root / 'later-bundle', DEFAULT_ENGINE)
        self.store = EvidenceStore(self.root / 'later-bundle')
        self.check(observed=['span:initial:input', 'span:later:output'])
        plan, checks = self.bound()

        def judge(request):
            payload = json.loads(request['user'])
            self.assertEqual(payload['checks'][0]['observation_ids'],
                             ['span:initial:input', 'span:later:output'])
            self.assertIn('span:later:output', payload['evidence'])
            return {'results': [{'check_id': 'EG-3.batch-input', 'status': 'scored', 'verdict': True}]}

        report = evaluate_prepared(plan, checks, self.store.records, judge_fn=judge,
                                   original_input=self.original)
        self.assertTrue(report['checks']['EG-3.batch-input']['verdict'])

    def test_unknown_judgment_is_not_replaced_by_failure(self):
        self.check()
        plan, checks = self.bound()
        report = evaluate_prepared(plan, checks, self.store.records,
            judge_fn=lambda _: {'results': [{'check_id': 'EG-3.batch-input',
                                            'status': 'unverifiable', 'verdict': None}]},
            original_input=self.original)
        self.assertEqual(report['rubrics']['EG-3'], {'status': 'unverifiable', 'verdict': None,
                                                   'check_ids': ['EG-3.batch-input'], 'failed_check_ids': []})

    def test_data_supplied_inline_does_not_require_an_artificial_attachment(self):
        self.original['reference']['attachments'] = []
        self.original['task']['request'] = 'Check batch B-TEST-17: the supplied measurements are 10 and 11.'
        self.original['observation']['events'][0]['payloads']['input']['value'] = {
            'messages': [{'role': 'user', 'content': self.original['task']['request']}]}
        with patch('agentos_metric_adapter.convert', return_value=(self.original, {})):
            self.base = prepare(self.root / 'source.zip', self.root / 'inline-bundle', DEFAULT_ENGINE)
        self.store = EvidenceStore(self.root / 'inline-bundle')
        self.row_ids = []
        self.check()
        _, checks = self.bound()
        self.assertTrue(checks[0]['input_complete'])
        self.assertIn('task:request', checks[0]['evidence_ids'])
        self.assertIn('span:initial:input', checks[0]['evidence_ids'])

    def test_generic_rubric_cannot_replace_task_grounding(self):
        check = self.check()
        profile = next(p for p in self.base['profiles'] if p['id'] == 'EG-3')
        check['source_refs'] = [{'evidence_id': 'rubric:EG-3', 'quote': profile['what']}]
        with self.assertRaisesRegex(ValueError, 'EG-3 requires a task/reference rule source'):
            validate_candidate(self.base, self.store, self.candidate)

    def test_eg3_rejects_program_and_hybrid_scoring(self):
        self.check()
        for method in ('P', 'H'):
            candidate = copy.deepcopy(self.candidate)
            target = next(r for r in candidate['rubrics'] if r['rubric_id'] == 'EG-3')
            target['checks'][0]['method'] = method
            with self.assertRaisesRegex(ValueError, 'Method forbidden by rubric EG-3'):
                validate_candidate(self.base, self.store, candidate)


if __name__ == '__main__':
    unittest.main()
