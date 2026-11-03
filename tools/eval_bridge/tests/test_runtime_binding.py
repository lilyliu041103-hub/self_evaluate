"""Verify run-specific evidence boundaries survive grouping and missing actions."""
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
from eval_bridge.planning import prepare, bind_observation
from eval_bridge.scoring import evaluate_prepared
from test_evidence_pipeline import fixture


@unittest.skipUnless(DEFAULT_ENGINE.exists(), 'Requires supplied v4.1 engine')
class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / 'source.zip'
        source.write_bytes(b'synthetic source')
        self.original = fixture()
        self.original['observation']['events'] = [
            {'span_id': name, 'name': 'agentos.tool.call',
             'attributes': {'app.tool.name': 'read_table'}, 'payloads': {'output': {
                 'source': f'{name}/output.json', 'sha256': name,
                 'value': {'rows': [{'batch': 'B-TEST-17', 'value': value}]}}}}
            for name, value in [('first', 10), ('second', 11), ('unrelated', 999)]]
        with patch('agentos_metric_adapter.convert', return_value=(self.original, {})):
            self.base = prepare(source, self.root / 'bundle', DEFAULT_ENGINE)
        self.store = EvidenceStore(self.root / 'bundle')
        self.candidate = {'rubrics': [
            {'rubric_id': p['id'], 'checks': [], 'unresolved': ['Synthetic fixture prerequisite missing.']}
            for p in self.base['profiles']]}

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, rid):
        return next(r for r in self.candidate['rubrics'] if r['rubric_id'] == rid)

    def test_each_object_keeps_its_own_ids_when_grouped(self):
        entry = self.entry('TU-3')
        entry['unresolved'] = []
        for span in ('first', 'second'):
            entry['checks'].append({
                'id': 'TU-3.' + span, 'criterion': f'Interpret the read_table return from {span} for B-TEST-17 accurately.',
                'method': 'J', 'prompt': f'Check the observable interpretation of the {span} tool return.',
                'bindings': {}, 'evidence_kinds': ['task_request', 'trace_payload', 'trace_event'],
                'reference_ids': ['task:request'], 'observation_ids': [f'span:{span}:output'],
                'required_evidence': [['trace_payload']], 'when': None,
                'source_refs': [{'evidence_id': 'task:request', 'quote': 'Check batch B-TEST-17'}]})
        plan = freeze_plan(self.base, self.store, self.candidate)
        checks, selection = bind_observation(plan, self.store, DEFAULT_ENGINE)
        self.assertEqual(selection['mode'], 'run_instance')
        self.assertNotIn('span:second:output', checks[0]['evidence_ids'])
        self.assertNotIn('span:first:output', checks[1]['evidence_ids'])
        self.assertIn('span:first', checks[0]['evidence_ids'])
        requests = []
        evaluate_prepared(plan, checks, self.store.records, request_sink=requests)
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0]['user'])
        self.assertNotIn('span:unrelated:output', payload['evidence'])
        self.assertEqual([c['observation_ids'] for c in payload['checks']],
                         [['span:first:output'], ['span:second:output']])

    def test_unavailable_delivery_requirement_survives_as_unknown(self):
        entry = self.entry('TC-1')
        entry['unresolved'] = []
        entry['checks'] = [{
            'id': 'TC-1.delivery', 'criterion': 'Deliver the requested B-TEST-17 assessment.',
            'method': 'J', 'prompt': 'Check the delivered assessment of B-TEST-17 using the task requirement.',
            'bindings': {}, 'evidence_kinds': ['task_request', 'final_answer', 'run_status'],
            'reference_ids': ['task:request'], 'observation_ids': [],
            'required_evidence': [['final_answer']], 'when': None,
            'source_refs': [{'evidence_id': 'task:request', 'quote': 'Check batch B-TEST-17'}]}]
        plan = freeze_plan(self.base, self.store, self.candidate)
        checks, _ = bind_observation(plan, self.store, DEFAULT_ENGINE)
        report = evaluate_prepared(plan, checks, self.store.records,
            judge_fn=lambda _: self.fail('Unavailable delivery must not invoke scoring'),
            original_input=self.original)
        self.assertIn('TC-1.delivery', report['checks'])
        self.assertEqual(report['rubrics']['TC-1']['status'], 'unverifiable')

    def test_complete_answer_can_fail_an_omitted_requirement(self):
        self.original['task']['request'] = 'Give a conclusion and list exceptions for B-TEST-17.'
        self.original['observation']['final_answer'] = 'B-TEST-17 passes.'
        self.original['observation']['final_answer_status'] = 'recorded'
        source = self.root / 'source.zip'
        bundle = self.root / 'with-answer'
        with patch('agentos_metric_adapter.convert', return_value=(self.original, {})):
            base = prepare(source, bundle, DEFAULT_ENGINE)
        store = EvidenceStore(bundle)
        target = self.entry('TC-1')
        target['unresolved'] = []
        for name, requirement in (('conclusion', 'Give a conclusion'), ('exceptions', 'list exceptions')):
            target['checks'].append({
                'id': 'TC-1.' + name, 'criterion': requirement + ' for B-TEST-17.',
                'method': 'J', 'prompt': 'Check whether the complete answer satisfies: ' + requirement,
                'bindings': {}, 'evidence_kinds': ['task_request', 'final_answer'],
                'reference_ids': ['task:request'], 'observation_ids': ['run:final_answer'],
                'required_evidence': [['task_request'], ['final_answer']], 'when': None,
                'source_refs': [{'evidence_id': 'task:request', 'quote': requirement}]})
        plan = freeze_plan(base, store, self.candidate)
        checks, _ = bind_observation(plan, store, DEFAULT_ENGINE)
        requests = []

        def judge(request):
            payload = json.loads(request['user'])
            for check in payload['checks']:
                self.assertNotIn('target_type', check)
                self.assertNotIn('target_type', check['selection'])
            return {'results': [{'check_id': check['check_id'], 'status': 'scored',
                                 'verdict': check['check_id'] == 'TC-1.conclusion'}
                                for check in payload['checks']]}

        report = evaluate_prepared(plan, checks, store.records, judge_fn=judge,
                                   request_sink=requests, original_input=self.original)
        self.assertTrue(requests)
        self.assertTrue(report['checks']['TC-1.conclusion']['verdict'])
        self.assertFalse(report['checks']['TC-1.exceptions']['verdict'])
        self.assertFalse(report['rubrics']['TC-1']['verdict'])


if __name__ == '__main__':
    unittest.main()
