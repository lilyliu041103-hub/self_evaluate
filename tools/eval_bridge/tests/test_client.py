import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.client import ModelClient, request_overhead_chars
from eval_bridge.evidence import digest, write_json


class FakeStore:
    def select(self, **kwargs):
        return {'ids': ['row:M1'], 'total_matches': 1, 'next_offset': None}

    def read(self, **kwargs):
        return {'records': [{'id': 'row:M1', 'data': [10]}], 'complete': True}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / 'api.local.json'
        self.secret = 'test-secret-never-record'
        write_json(self.config, {'api_key': self.secret, 'model': 'fixture-model', 'base_url': 'https://test.invalid'})
        self.request = {'system': 'Fixed test instructions', 'user': 'Return JSON'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_instantiation_json_repair_is_bounded_and_audited(self):
        bodies = []
        def respond(body):
            bodies.append(copy.deepcopy(body))
            content = '{"quote":"a"b"}' if len(bodies) == 1 else '{"quote":"a\\"b"}'
            return {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]}
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', role='instantiate', transport=respond)
        self.assertEqual(client(self.request), {'quote': 'a"b'})
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[1]['tool_choice'], 'none')
        feedback = json.loads(bodies[1]['messages'][-1]['content'])
        self.assertEqual(feedback['code'], 'invalid_json')
        self.assertGreater(feedback['column'], 0)
        self.assertIn('instantiate_json_repair', (self.root / 'logs/calls.jsonl').read_text())

    def test_invalid_json_repair_retries_once_for_plan_and_scoring(self):
        for role, expected in [('instantiate', 2), ('score', 2)]:
            seen = []
            def respond(body):
                seen.append(body)
                return {'choices': [{'message': {'content': '{broken'}, 'finish_reason': 'stop'}]}
            client = ModelClient(self.config, FakeStore(), self.root / role, role=role, transport=respond)
            with self.assertRaises(json.JSONDecodeError):
                client(self.request)
            self.assertEqual(len(seen), expected)

    def test_tools_and_secret_free_audit(self):
        queue = [
            {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'tool1', 'type': 'function', 'function': {'name': 'read_evidence',
                 'arguments': '{"ids":["row:M1"]}'}}]}, 'finish_reason': 'tool_calls'}]},
            {'choices': [{'message': {'content': '{"results":[]}'}, 'finish_reason': 'stop'}]},
        ]
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', transport=lambda body: queue.pop(0))
        self.assertEqual(client(self.request), {'results': []})
        self.assertEqual(client.tool_calls, 1)
        self.assertEqual(client.calls, 2)
        for file in (self.root / 'logs').glob('*'):
            self.assertNotIn(self.secret, file.read_text(encoding='utf-8'))
        self.assertEqual(client(self.request), {'results': []})
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.cache_hits, 2)

    def test_tool_round_budget_stops_loop(self):
        response = {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'tool1', 'type': 'function', 'function': {'name': 'query_evidence', 'arguments': '{}'}}]}}]}
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', max_tool_rounds=0,
                             transport=lambda body: response)
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            client(self.request)

    def test_read_tool_routes_pagination_arguments(self):
        calls = []

        class PagedStore(FakeStore):
            def read(self, **kwargs):
                calls.append(kwargs)
                return {'evidence_id': 'row:M1', 'text_fragment': '42', 'next_offset': None}

        queue = [
            {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'page1', 'type': 'function', 'function': {'name': 'read_evidence',
                 'arguments': '{"ids":["row:M1"],"offset":40000,"max_chars":1000}'}}]},
                'finish_reason': 'tool_calls'}]},
            {'choices': [{'message': {'content': '{"results":[]}'}, 'finish_reason': 'stop'}]},
        ]
        client = ModelClient(self.config, PagedStore(), self.root / 'logs',
                             transport=lambda body: queue.pop(0))
        self.assertEqual(client(self.request), {'results': []})
        self.assertEqual(calls, [{'ids': ['row:M1'], 'offset': 40000, 'max_chars': 1000}])
        read_tool = next(t['function'] for t in client.tools if t['function']['name'] == 'read_evidence')
        self.assertIn('offset', read_tool['parameters']['properties'])

    def test_incomplete_json_response_rejected(self):
        client = ModelClient(self.config, FakeStore(), self.root / 'logs',
                             transport=lambda body: {'choices': [{'message': {'content': '{"results":[]}'},
                                                                  'finish_reason': 'length'}]})
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            client(self.request)

    def test_tool_fanout_is_bounded(self):
        calls = [{'id': str(i), 'type': 'function', 'function': {'name': 'query_evidence', 'arguments': '{}'}}
                 for i in range(9)]
        client = ModelClient(self.config, FakeStore(), self.root / 'logs',
                             transport=lambda body: {'choices': [{'message': {'tool_calls': calls}}]})
        with self.assertRaisesRegex(ValueError, 'tool-call budget'):
            client(self.request)

    def test_negative_round_budget_rejected(self):
        with self.assertRaisesRegex(ValueError, 'max_tool_rounds'):
            ModelClient(self.config, FakeStore(), self.root / 'logs', max_tool_rounds=-1)

    def test_final_round_disables_tools_and_requests_conservative_json(self):
        bodies = []
        def respond(body):
            bodies.append(json.loads(json.dumps(body)))
            if len(bodies) == 1:
                return {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'q1', 'type': 'function', 'function': {
                        'name': 'query_evidence', 'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}]}
            return {'choices': [{'message': {'content': '{"results":[]}'}, 'finish_reason': 'stop'}]}
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', max_tool_rounds=1, transport=respond)
        self.assertEqual(client(self.request), {'results': []})
        self.assertEqual([b['tool_choice'] for b in bodies], ['auto', 'none'])
        self.assertIn('unverifiable', bodies[-1]['messages'][-1]['content'])
        self.assertEqual(client.calls, 2)

    def test_zero_retrieval_rounds_requests_final_json_immediately(self):
        def respond(body):
            self.assertEqual(body['tool_choice'], 'none')
            return {'choices': [{'message': {'content': '{"results":[]}'}, 'finish_reason': 'stop'}]}
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', max_tool_rounds=0, transport=respond)
        self.assertEqual(client(self.request), {'results': []})
        self.assertEqual(client.calls, 1)

    def test_explicit_thinking_options_are_sent_to_provider(self):
        config = json.loads(self.config.read_text(encoding='utf-8'))
        for enabled in (True, False):
            def respond(body):
                self.assertIs(body['enable_thinking'], enabled)
                self.assertEqual(body['thinking_budget'], 4096)
                return {'choices': [{'message': {'content': '{"results":[]}'}, 'finish_reason': 'stop'}]}
            client = ModelClient({**config, 'enable_thinking': enabled, 'thinking_budget': 4096},
                                 FakeStore(), self.root / str(enabled), max_tool_rounds=0, transport=respond)
            self.assertEqual(client(self.request), {'results': []})

    def test_unconfigured_thinking_options_are_omitted(self):
        def respond(body):
            self.assertNotIn('enable_thinking', body)
            self.assertNotIn('thinking_budget', body)
            return {'choices': [{'message': {'content': '{"results":[]}'}, 'finish_reason': 'stop'}]}
        client = ModelClient(self.config, FakeStore(), self.root / 'logs',
                             max_tool_rounds=0, transport=respond)
        self.assertEqual(client(self.request), {'results': []})

    def test_invalid_thinking_options_rejected(self):
        config = json.loads(self.config.read_text(encoding='utf-8'))
        for settings in ({'enable_thinking': 'false'}, {'enable_thinking': 1},
                         {'thinking_budget': True}, {'thinking_budget': 0}, {'thinking_budget': 1.5}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                ModelClient({**config, **settings}, FakeStore(), self.root / 'logs')

    def test_complete_fenced_json_response_is_parsed(self):
        for index, content in enumerate(['```json\n{"results":[]}\n```',
                                         '  ```\r\n{"results":[]}\r\n```\n',
                                         'Result follows.\n```json\n{"results":[]}\n```\nEnd.']):
            client = ModelClient(self.config, FakeStore(), self.root / str(index), max_tool_rounds=0,
                                 transport=lambda body: {'choices': [{'message': {'content': content},
                                                                      'finish_reason': 'stop'}]})
            self.assertEqual(client(self.request), {'results': []})

    def test_fence_parsing_rejects_ambiguous_or_invalid_json(self):
        for index, content in enumerate(['```json\n{}\n```\n```json\n[]\n```',
                                         '```json\n{bad}\n```', '```json\n{}',
                                         'Here is an object: {}']):
            client = ModelClient(self.config, FakeStore(), self.root / str(index), max_tool_rounds=0,
                                 transport=lambda body: {'choices': [{'message': {'content': content},
                                                                      'finish_reason': 'stop'}]})
            with self.subTest(content=content), self.assertRaises(json.JSONDecodeError):
                client(self.request)

    def test_server_usage_excludes_local_cache_and_tracks_metrics(self):
        usage = [
            {'prompt_tokens': 100, 'prompt_cache_hit_tokens': 70, 'completion_tokens': 10},
            {'prompt_tokens': 200, 'prompt_tokens_details': {'cached_tokens': 140},
             'completion_tokens': 20},
            {'prompt_tokens': 30, 'completion_tokens': 3},
        ]
        def respond(body):
            return {'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}],
                    'usage': usage.pop(0)}
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', transport=respond)
        first = dict(self.request, metric_id='AC')
        client(first)
        client(first)
        client(dict(self.request, metric_id='TC', user='TC independent criteria'))
        client(dict(self.request, metric_id='TC', user='TC second batch'))
        stats = client.statistics()
        self.assertEqual(stats['api_calls'], 3)
        self.assertEqual(stats['response_cache_hits'], 1)
        self.assertEqual(stats['server_usage']['input_tokens'], 330)
        self.assertEqual(stats['server_usage']['cached_input_tokens'], 210)
        self.assertEqual(stats['server_usage']['output_tokens'], 33)
        self.assertEqual(stats['server_usage']['input_usage_calls'], 3)
        self.assertEqual(stats['server_usage']['cache_usage_calls'], 2)
        self.assertEqual(stats['by_metric']['AC']['server_usage']['input_tokens'], 100)
        self.assertEqual(stats['by_metric']['AC']['response_cache_hits'], 1)
        self.assertEqual(stats['by_metric']['TC']['server_usage']['input_tokens'], 230)
        entries = [json.loads(line) for line in
                   (self.root / 'logs' / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertNotIn('normalized_usage', entries[1])
        self.assertIsNone(entries[-1]['normalized_usage']['cached_input_tokens'])
        self.assertEqual([entry['metric_id'] for entry in entries], ['AC', 'AC', 'TC', 'TC'])
        self.assertEqual(stats['server_usage']['duration_seconds'], round(sum(
            entry['duration_seconds'] for entry in entries if entry['api_attempt']), 6))

    def test_missing_and_malformed_usage_remain_unknown(self):
        for index, usage in enumerate((None, {}, {'prompt_tokens': True,
                'prompt_cache_hit_tokens': -1, 'completion_tokens': '20'})):
            client = ModelClient(self.config, FakeStore(), self.root / str(index),
                                 transport=lambda body: {'choices': [{'message': {'content': '{}'}}],
                                                         'usage': usage})
            client(self.request)
            totals = client.statistics()['server_usage']
            for field in ('input_tokens', 'cached_input_tokens', 'output_tokens'):
                self.assertIsNone(totals[field])
            self.assertEqual(totals['cache_usage_calls'], 0)
        zero = ModelClient(self.config, FakeStore(), self.root / 'zero',
                           transport=lambda body: {'choices': [{'message': {'content': '{}'}}],
                               'usage': {'prompt_tokens': 20, 'prompt_cache_hit_tokens': 0}})
        zero(self.request)
        self.assertEqual(zero.statistics()['server_usage']['cached_input_tokens'], 0)
        self.assertEqual(zero.statistics()['server_usage']['cache_usage_calls'], 1)

    def test_disabling_local_cache_preserves_each_actual_response(self):
        sent = []
        def respond(body):
            sent.append(copy.deepcopy(body))
            return {'choices': [{'message': {'content': json.dumps({'call': len(sent)})}}],
                    'usage': {'prompt_tokens': 10, 'prompt_cache_hit_tokens': len(sent),
                              'completion_tokens': 2}}
        log_dir = self.root / 'logs'
        cached = ModelClient(self.config, FakeStore(), log_dir, transport=respond)
        self.assertEqual(cached(self.request), {'call': 1})
        client = ModelClient(self.config, FakeStore(), log_dir,
                             transport=respond, response_cache=False)
        self.assertEqual(client(self.request), {'call': 2})
        self.assertEqual(client(self.request), {'call': 3})
        self.assertEqual(client.cache_hits, 0)
        self.assertEqual(client.statistics()['server_usage']['input_tokens'], 20)
        entries = [json.loads(line) for line in
                   (log_dir / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len({entry['response_file'] for entry in entries}), 3)
        observed = [json.loads((log_dir / entry['response_file']).read_text(encoding='utf-8'))
                    for entry in entries]
        self.assertEqual([item['usage']['prompt_cache_hit_tokens'] for item in observed], [1, 2, 3])
        self.assertEqual(cached(self.request), {'call': 1})

    def test_common_prefix_verified_and_tools_included_in_effective_hash(self):
        prefix = 'Shared task and evidence\n'
        prefix_hash = digest({'system': self.request['system'], 'user_prefix': prefix})
        requests = [dict(self.request, user=prefix + metric_id, metric_id=metric_id,
                         common_prefix_chars=len(prefix), common_prefix_hash=prefix_hash)
                    for metric_id in ('AC', 'TC')]
        client = ModelClient(self.config, FakeStore(), self.root / 'logs',
                             transport=lambda body: {'choices': [{'message': {'content': '{}'}}]})
        for request in requests:
            client(request)
        entries = [json.loads(line) for line in
                   (self.root / 'logs' / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual({entry['common_prefix_hash'] for entry in entries}, {prefix_hash})
        expected = digest({'system': self.request['system'], 'user_prefix': prefix,
                           'tools': client.tools})
        self.assertEqual({entry['effective_prefix_hash'] for entry in entries}, {expected})
        with self.assertRaisesRegex(ValueError, 'common_prefix_hash'):
            client(dict(requests[0], user='Changed shared task and evidence\nAC'))
        self.assertEqual(client.calls, 2)
        self.assertGreater(request_overhead_chars(), 100)

    def test_evidence_pack_prefix_audit_and_tamper_detection_before_api_call(self):
        from eval_bridge.scoring import build_groups, build_group_request
        evidence = {eid: {'id': eid, 'scope': 'observation', 'kind': 'trace_payload', 'data': eid}
                    for eid in ('a', 'b')}
        checks = [{'id': mid + '-1', 'rubric_id': mid + '-1', 'metric_id': mid,
                   'method': 'J', 'criterion': 'Fixed independent check', 'prompt': 'Judge it',
                   'group': 'answer', 'evidence_ids': [eid], 'input_complete': True}
                  for mid, eid in (('AC', 'a'), ('TC', 'a'), ('PE', 'b'))]
        requests = [build_group_request(g) for g in build_groups(checks, evidence,
                    scoring_mode='metric', common_evidence_mode='packs')]
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', response_cache=False,
                             transport=lambda body: {'choices': [{'message': {'content': '{}'}}]})
        for request in requests:
            client(request)
        entries = [json.loads(line) for line in (self.root / 'logs/calls.jsonl').read_text(encoding='utf-8').splitlines()]
        for request, entry in zip(requests, entries):
            self.assertEqual(entry['evidence_prefix_hash'], request['evidence_prefix_hash'])
            self.assertEqual(entry['evidence_pack_ids'], request['evidence_pack_ids'])
            expected = digest({'system': request['system'], 'tools': client.tools,
                               'user_prefix': request['user'][:request['evidence_prefix_chars']]})
            self.assertEqual(entry['effective_evidence_prefix_hash'], expected)
        self.assertEqual(entries[0]['effective_evidence_prefix_hash'], entries[1]['effective_evidence_prefix_hash'])
        self.assertNotEqual(entries[0]['effective_evidence_prefix_hash'], entries[2]['effective_evidence_prefix_hash'])
        corruptions = [dict(evidence_prefix_hash='wrong'), dict(evidence_prefix_chars=True),
                       dict(evidence_pack_ids=[]), dict(pack_prefixes=[{
                           **requests[0]['pack_prefixes'][0], 'prefix_hash': 'wrong'}])]
        for changes in corruptions:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                client(dict(requests[0], **changes))
        self.assertEqual(client.calls, 3)
        # The same pack text under different tool definitions is a different effective prefix.
        client.tools = list(reversed(client.tools))
        client(requests[0])
        last = json.loads((self.root / 'logs/calls.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertNotEqual(entries[0]['effective_evidence_prefix_hash'], last['effective_evidence_prefix_hash'])

    def test_tool_results_and_counters_are_branch_local(self):
        bodies = []
        def respond(body):
            bodies.append(copy.deepcopy(body))
            if len(bodies) == 1:
                return {'choices': [{'message': {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'ac-read', 'type': 'function', 'function': {
                        'name': 'read_evidence', 'arguments': '{"ids":["row:M1"]}'}}]},
                    'finish_reason': 'tool_calls'}]}
            return {'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]}
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', transport=respond)
        client(dict(self.request, metric_id='AC', tool_reserve_chars=4000))
        client(dict(self.request, metric_id='TC', user='Independent TC'))
        self.assertTrue(any(message['role'] == 'tool' for message in bodies[1]['messages']))
        self.assertEqual(len(bodies[2]['messages']), 2)
        self.assertNotIn('ac-read', json.dumps(bodies[2]))
        self.assertEqual(client.statistics()['by_metric']['AC']['evidence_tool_calls'], 1)
        self.assertEqual(client.statistics()['by_metric']['TC']['evidence_tool_calls'], 0)

    def test_explicit_tool_return_budget_is_enforced(self):
        def respond(body):
            return {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
                {'id': 't1', 'type': 'function', 'function': {
                    'name': 'query_evidence', 'arguments': '{}'}}]}}]}
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', transport=respond)
        with self.assertRaisesRegex(ValueError, 'budget'):
            client(dict(self.request, tool_reserve_chars=1))
        self.assertEqual(client.calls, 1)
        def final(body):
            self.assertEqual(body['tool_choice'], 'none')
            return {'choices': [{'message': {'content': '{}'}}]}
        client = ModelClient(self.config, FakeStore(), self.root / 'zero', transport=final)
        client(dict(self.request, tool_reserve_chars=0))

    def test_read_allowance_fits_remaining_tool_context_and_preserves_paging(self):
        from eval_bridge.evidence import EvidenceStore, make_index
        store = EvidenceStore.__new__(EvidenceStore)
        store.records = {'long:1': {'id': 'long:1', 'kind': 'trace_event', 'scope': 'observation',
                                   'data': 'x' * 12000, 'origin': {'pointer': '/trace/0'}}}
        store.index = make_index(store.records)
        bodies = []
        read_limits = []
        original_read = store.read
        def read(**args):
            read_limits.append(args['max_chars'])
            return original_read(**args)
        store.read = read
        def respond(body):
            bodies.append(copy.deepcopy(body))
            if len(bodies) <= 2:
                args = {'ids': ['long:1']}
                if len(bodies) == 2:
                    prior = json.loads(body['messages'][-1]['content'])
                    args = prior['omitted'][0]['next_read_arguments']
                return {'choices': [{'message': {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'read' + str(len(bodies)), 'type': 'function',
                                   'function': {'name': 'read_evidence',
                                                'arguments': json.dumps(args)}}]}}]}
            return {'choices': [{'message': {'content': '{}'}}]}
        client = ModelClient(self.config, store, self.root / 'logs', transport=respond)
        self.assertEqual(client(dict(self.request, tool_reserve_chars=8000)), {})
        self.assertEqual(client.calls, 3)
        self.assertTrue(all(limit < 4000 for limit in read_limits))
        page = json.loads(bodies[-1]['messages'][-2]['content'])
        self.assertTrue(page['text_fragment'])
        self.assertFalse(page['complete'])
        self.assertIsNotNone(page['next_read_arguments'])

    def test_tool_arguments_are_counted_against_context_reserve(self):
        client = ModelClient(self.config, FakeStore(), self.root / 'logs',
                             transport=lambda body: {'choices': [{'message': {
                                 'role': 'assistant', 'tool_calls': [{'id': 'long', 'type': 'function',
                                     'function': {'name': 'query_evidence', 'arguments': json.dumps({
                                         'contains': 'x' * 10000})}}]}}]})
        with self.assertRaisesRegex(ValueError, 'tool-context budget'):
            client(dict(self.request, tool_reserve_chars=8000))
        self.assertEqual(client.calls, 1)
        self.assertEqual(client.tool_calls, 0)

    def test_failed_call_keeps_metric_and_prefix_audit(self):
        prefix_hash = digest({'system': self.request['system'], 'user_prefix': ''})
        def fail(body):
            raise RuntimeError(self.secret)
        client = ModelClient(self.config, FakeStore(), self.root / 'logs', transport=fail)
        with self.assertRaises(RuntimeError):
            client(dict(self.request, metric_id='AC', common_prefix_chars=0,
                        common_prefix_hash=prefix_hash))
        entry = json.loads((self.root / 'logs' / 'calls.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(entry['metric_id'], 'AC')
        self.assertEqual(entry['common_prefix_hash'], prefix_hash)
        self.assertEqual(client.statistics()['by_metric']['AC']['api_attempts'], 1)
        self.assertIsNone(client.statistics()['server_usage']['input_tokens'])


if __name__ == '__main__':
    unittest.main()
