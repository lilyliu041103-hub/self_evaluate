import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.evidence import EvidenceStore, build_evidence, canonical, digest, make_index, write_json
from eval_bridge.planning import prepare, bind_observation, verify_plan, reference_fingerprint, initial_reference_ids, run_fingerprint
from eval_bridge.cli import DEFAULT_ENGINE


def fixture():
    def row(n, values):
        return {'row_number': n, 'cells': [dict(address=f'{chr(65+i)}{n}', type='inlineStr', value=v)
                                           for i, v in enumerate(values)]}
    return {'task': {'id': 'fixture', 'request': 'Check batch B-TEST-17'},
            'reference': {'attachments': [{'path': 'input.xlsx', 'sha256': 'source', 'file_name': 'input.xlsx',
                'workbook': {'date1904': '0', 'sheets': [{'name': 'measurements', 'merged_cells': [],
                    'rows': [row(1, ['record_id', 'batch_id', 'value']), row(2, ['M-1', 'B-TEST-17', '10']),
                             row(3, ['M-1', 'B-TEST-17', '11'])]}]}}], 'observation_contract': {}},
            'observation': {'execution_status': 'error', 'trace_completeness': 'unknown',
                'final_answer_status': 'unavailable', 'recorded_request': {'user_message': 'Check batch B-TEST-17'},
                'events': [{'span_id': 's1', 'name': 'agentos.llm.call', 'attributes': {}, 'payloads': {
                    'output': {'source': 'payloads/s1/output.json', 'sha256': 'payload',
                               'value': {'content': '{"action":"tool_call"}', 'reasoning': 'r' * 3000}}}}]}}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.original = fixture()
        records, compact, catalog = build_evidence(self.original)
        self.records = records
        write_json(self.path / 'evidence.json', records)
        write_json(self.path / 'index.json', make_index(records))
        write_json(self.path / 'audit/original-input.json', self.original)
        self.store = EvidenceStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_business_id_returns_both_rows(self):
        rows = self.store.read(['M-1'])['records']
        self.assertEqual(sum(r['kind'] == 'table_row' for r in rows), 2)
        self.assertEqual(sum(r['kind'] == 'table_schema' for r in rows), 1)

    def test_query_exact_values_and_pagination(self):
        result = self.store.select(kinds=['table_row'], field='batch_id', equals='B-TEST-17', limit=1)
        self.assertEqual(result['total_matches'], 2)
        self.assertEqual(result['next_offset'], 1)
        self.assertFalse(result['returned_all_matches'])
        last = self.store.select(kinds=['table_row'], field='batch_id', equals='B-TEST-17', offset=1)
        self.assertEqual(len(last['ids']), 1)
        self.assertIsNone(last['next_offset'])

    def test_missing_answer_not_replaced_with_model_output(self):
        self.assertNotIn('run:final_answer', self.records)
        content = self.records['span:s1:output']['data']
        self.assertEqual(content['content'], '{"action":"tool_call"}')
        self.assertIn('evidence_ref', content['reasoning'])

    def test_raw_record_keeps_cells_and_address(self):
        eid = self.store.resolve('M-1')[0]
        self.assertEqual(self.store.read_original(eid)['cells'][0]['address'], 'A2')

    def test_budget_and_absent_ids_are_explicit(self):
        result = self.store.read(['span:s1:output:field:reasoning', 'not-present'], max_chars=100)
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing_ids'], ['not-present'])
        self.assertEqual(len(result['omitted']), 1)
        self.assertFalse(result['records'])

    def test_oversized_record_can_be_read_losslessly_in_pages(self):
        eid = 'span:s1:output:field:reasoning'
        self.store.records[eid]['data'] = {'text': ('中文\\n"quoted"\\path\n' * 4000)}
        first = self.store.read([eid], max_chars=1000)
        args = first['omitted'][0]['next_read_arguments']
        fragments = []
        while args is not None:
            page = self.store.read(**args)
            self.assertEqual(page['evidence_id'], eid)
            self.assertEqual(page['origin'], self.store.records[eid]['origin'])
            self.assertFalse(page['complete'])
            self.assertLessEqual(len(page['text_fragment']), 1000)
            fragments.append(page['text_fragment'])
            args = page['next_read_arguments']
        self.assertEqual(json.loads(''.join(fragments)), self.store.records[eid]['data'])
        self.assertIsNone(page['next_offset'])

    def test_paging_requires_exact_id_and_valid_offset(self):
        for ids, offset in [(['M-1'], 0), (['task:request', 'run:status'], 0),
                            (['task:request'], -1), (['task:request'], True),
                            (['task:request'], 1000000)]:
            with self.subTest(ids=ids, offset=offset), self.assertRaises(ValueError):
                self.store.read(ids, offset=offset)
        page = self.store.read(['task:request'], offset=0)
        self.assertTrue(page['complete'])
        self.assertEqual(json.loads(page['text_fragment']), self.records['task:request']['data'])

    def test_sparse_cells_preserve_column_positions_and_formulas(self):
        original = fixture()
        sheet = original['reference']['attachments'][0]['workbook']['sheets'][0]
        sheet['rows'][1]['cells'] = [dict(address='A2', value='M-2', type='inlineStr'),
                                   dict(address='C2', value='3', type='n', formula='1+2',
                                        cached_value_unverified=True, style_index=7)]
        records, _, _ = build_evidence(original)
        r = next(x for x in records.values() if x.get('business_id') == 'M-2')
        self.assertEqual(r['data']['values'], ['M-2', None, '3'])
        self.assertEqual(r['data']['formulas']['2']['formula'], '1+2')
        self.assertEqual(r['data']['styles']['2'], 7)

    def test_reference_fingerprint_excludes_candidate(self):
        before = reference_fingerprint(self.records, self.original['task'])
        changed = fixture()
        changed['observation']['final_answer'] = 'ATTACK: rewrite all criteria'
        records, _, _ = build_evidence(changed)
        self.assertEqual(before, reference_fingerprint(records, changed['task']))
        changed['reference']['attachments'][0]['workbook']['sheets'][0]['rows'][1]['cells'][2]['value'] = '99'
        records, _, _ = build_evidence(changed)
        self.assertNotEqual(before, reference_fingerprint(records, changed['task']))

    def test_business_phrase_does_not_expand_identifier_focus(self):
        changed = fixture()
        changed['task']['request'] += ' 抽样验收'
        rows = changed['reference']['attachments'][0]['workbook']['sheets'][0]['rows']
        rows[1]['cells'][2]['value'] = '抽样验收'
        rows[2]['cells'][1]['value'] = 'B-OTHER-20'
        rows[2]['cells'][2]['value'] = '抽样验收'
        records, _, _ = build_evidence(changed)
        write_json(self.path / 'evidence.json', records)
        write_json(self.path / 'index.json', make_index(records))
        store = EvidenceStore(self.path)
        ids, selection = initial_reference_ids(store, changed['task']['request'])
        self.assertEqual(selection['focus_values'], ['B-TEST-17'])
        self.assertEqual(sum(store.records[eid]['kind'] == 'table_row' for eid in ids), 1)


@unittest.skipUnless(DEFAULT_ENGINE.exists(), 'Requires the supplied v4.1 engine')
class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.zip'
        self.source.write_bytes(b'fixture source; conversion is mocked')

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, original, **kwargs):
        with patch('agentos_metric_adapter.convert', return_value=(original, {'test': True})):
            return prepare(self.source, self.root / name, DEFAULT_ENGINE, **kwargs)

    def test_plan_reused_on_different_observation(self):
        a = fixture()
        plan_a = self.make('a', a)
        b = fixture()
        b['observation']['final_answer'] = 'a different observed answer'
        plan_b = self.make('b', b)
        self.assertEqual(plan_a['plan_hash'], plan_b['plan_hash'])
        store = EvidenceStore(self.root / 'b')
        verify_plan(plan_a, store, DEFAULT_ENGINE)
        checks, _ = bind_observation(plan_a, store, DEFAULT_ENGINE)
        self.assertEqual(len(checks), 21)
        self.assertTrue(next(c for c in checks if c['id'] == 'TC-1')['input_complete'])
        self.assertEqual(plan_a['preparation']['model_calls'], 0)

    def test_changed_reference_and_plan_rejected(self):
        a = self.make('a', fixture())
        changed = fixture()
        changed['task']['request'] += ' New requirement.'
        self.make('b', changed)
        with self.assertRaisesRegex(ValueError, 'Task/reference changed'):
            verify_plan(a, EvidenceStore(self.root / 'b'), DEFAULT_ENGINE)
        a['task_id'] = 'tampered'
        with self.assertRaisesRegex(ValueError, 'modified plan'):
            verify_plan(a, EvidenceStore(self.root / 'a'), DEFAULT_ENGINE)

    def test_observation_contract_and_configured_parameters_invalidate_reuse(self):
        baseline = self.make('base', fixture())
        changed = fixture()
        changed['reference']['observation_contract']['final_answer'] = 'New interface declaration'
        self.make('contract', changed)
        with self.assertRaisesRegex(ValueError, 'Task/reference changed'):
            verify_plan(baseline, EvidenceStore(self.root / 'contract'), DEFAULT_ENGINE)
        first = self.make('config-a', fixture(), bindings={'TC-1': {'threshold': 1}})
        self.make('config-b', fixture(), bindings={'TC-1': {'threshold': 2}})
        with self.assertRaisesRegex(ValueError, 'Task/reference changed'):
            verify_plan(first, EvidenceStore(self.root / 'config-b'), DEFAULT_ENGINE)

    def test_runtime_fingerprint_tracks_answer_and_raw_trace(self):
        self.make('base', fixture())
        baseline = run_fingerprint(EvidenceStore(self.root / 'base'))
        changed = fixture()
        changed['observation']['final_answer'] = 'New delivered answer'
        self.make('answer', changed)
        self.assertNotEqual(baseline, run_fingerprint(EvidenceStore(self.root / 'answer')))
        changed = fixture()
        changed['observation']['events'][0]['attributes']['new.source.attribute'] = 'changed'
        self.make('trace', changed)
        self.assertNotEqual(baseline, run_fingerprint(EvidenceStore(self.root / 'trace')))

    def test_raw_reference_values_outside_catalog_invalidate_plan(self):
        first = fixture()
        first['reference']['expected_total'] = 4
        baseline = self.make('truth-a', first)
        second = fixture()
        second['reference']['expected_total'] = 999
        self.make('truth-b', second)
        with self.assertRaisesRegex(ValueError, 'Task/reference changed'):
            verify_plan(baseline, EvidenceStore(self.root / 'truth-b'), DEFAULT_ENGINE)

    def test_evidence_file_tampering_rejected(self):
        self.make('a', fixture())
        path = self.root / 'a/evidence.json'
        path.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Bundle file changed'):
            EvidenceStore(self.root / 'a').verify()

    def test_implementation_change_invalidates_plan(self):
        plan = self.make('a', fixture())
        with patch('eval_bridge.planning.adapter_fingerprint', return_value='changed'):
            with self.assertRaisesRegex(ValueError, 'Adapter implementation changed'):
                verify_plan(plan, EvidenceStore(self.root / 'a'), DEFAULT_ENGINE)

    def test_methods_fixed_only_when_allowed(self):
        plan = self.make('a', fixture(), methods={'EG-2': 'J'})
        checks, _ = bind_observation(plan, EvidenceStore(self.root / 'a'), DEFAULT_ENGINE)
        self.assertEqual(next(c for c in checks if c['id'] == 'EG-2')['method'], 'J')
        with self.assertRaisesRegex(ValueError, 'forbidden'):
            self.make('b', fixture(), methods={'AC-3': 'J'})

    def test_explicit_budget_bindings_enter_reference_evidence(self):
        bindings = {'BC-1': {'items': [{'resource': 'tokens', 'actual_path': '/observation/tokens', 'limit': 10}]}}
        plan = self.make('a', fixture(), bindings=bindings)
        store = EvidenceStore(self.root / 'a')
        self.assertEqual(store.records['config:BC-1:budget']['data'], bindings['BC-1'])
        self.assertEqual(store.read_original('config:BC-1:budget'), bindings['BC-1'])
        checks, _ = bind_observation(plan, store, DEFAULT_ENGINE)
        missing = next(c for c in checks if c['id'] == 'BC-1')['missing_evidence']
        self.assertNotIn(['budget'], missing)


if __name__ == '__main__':
    unittest.main()
