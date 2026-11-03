import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.client import ModelClient
from eval_bridge.evidence import write_json


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


if __name__ == '__main__':
    unittest.main()
