import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_bridge.client import ModelClient
from eval_bridge.evidence import EvidenceStore, make_index, write_json
from eval_bridge.roles import load_role_config


def evidence_fixture():
    store = EvidenceStore.__new__(EvidenceStore)
    store.records = {
        'task:1': {'id': 'task:1', 'kind': 'task_request', 'scope': 'task', 'data': 'Process M1'},
        'schema:1': {'id': 'schema:1', 'kind': 'table_schema', 'scope': 'reference',
                     'data': {'columns': ['id', 'value']}},
        'row:1': {'id': 'row:1', 'kind': 'table_row', 'scope': 'reference',
                  'schema_id': 'schema:1', 'business_id': 'M1', 'data': {'values': ['M1', 17]}},
        'trace:1': {'id': 'trace:1', 'kind': 'trace_event', 'scope': 'observation',
                    'business_id': 'M1', 'data': 'PRIVATE_TRACE_DATA'},
        'answer:1': {'id': 'answer:1', 'kind': 'final_answer', 'scope': 'observation',
                    'data': 'PRIVATE_ANSWER_DATA'},
    }
    store.index = make_index(store.records)
    return store


def response(payload):
    return {'choices': [{'message': {'content': json.dumps(payload)}, 'finish_reason': 'stop'}]}


class ModelRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.secret = 'never-persist-this-test-key'
        self.config = {'base_url': 'https://instantiate.invalid', 'api_key': self.secret,
                       'model': 'planning-model', 'max_tokens': 4000}
        self.request = {'system': 'Apply fixed rubrics to runtime evidence', 'user': 'Instantiate this run'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_role_config_inherits_relative_file_and_has_independent_overrides(self):
        (self.root / 'config').mkdir()
        write_json(self.root / 'shared.json', self.config)
        roles_file = self.root / 'config' / 'roles.json'
        write_json(roles_file, {'instantiate': {'config_path': '../shared.json'},
                               'score': {'config_path': '../shared.json', 'model': 'scoring-model',
                                         'base_url': 'https://score.invalid', 'max_tokens': 1000}})
        inst = load_role_config(roles_file, 'instantiate')
        score = load_role_config(roles_file, 'score')
        self.assertEqual(inst['model'], 'planning-model')
        self.assertEqual(score['model'], 'scoring-model')
        self.assertEqual(score['base_url'], 'https://score.invalid')
        self.assertEqual(score['max_tokens'], 1000)
        self.assertEqual(score['api_key'], self.secret)
        self.assertNotIn('config_path', score)
        with self.assertRaisesRegex(ValueError, 'Unknown model role: review'):
            load_role_config(roles_file, 'review')

    def test_direct_role_credentials_and_wrapped_config_supported(self):
        file = self.root / 'roles.json'
        write_json(file, {'roles': {'instantiate': self.config}})
        self.assertEqual(load_role_config(file, 'instantiate'), self.config)
        with self.assertRaisesRegex(ValueError, 'Unknown model role'):
            load_role_config(file, 'other')

    def test_both_roles_can_read_runtime_evidence_in_current_bundle(self):
        for role in ('instantiate', 'score'):
            store = evidence_fixture()
            client = ModelClient(self.config, store, self.root / role, role=role)
            self.assertIs(client.store, store)
            self.assertEqual(client.store.select()['total_matches'], 5)
            self.assertEqual(client.store.select(scope='observation')['total_matches'], 2)
            self.assertEqual(client.store.select(limit=1)['next_offset'], 1)
            self.assertEqual(client.store.resolve('M1'), ['row:1', 'trace:1'])
            self.assertEqual(client.store.read(['answer:1'])['records'][0]['data'], 'PRIVATE_ANSWER_DATA')
            self.assertEqual(client.store.read(['foreign-run:answer'])['missing_ids'], ['foreign-run:answer'])

    def test_review_role_is_removed(self):
        with self.assertRaisesRegex(ValueError, 'Unknown model role: review'):
            ModelClient(self.config, evidence_fixture(), self.root / 'removed', role='review')
        file = self.root / 'removed-role.json'
        write_json(file, {'review': self.config, 'instantiate': self.config})
        with self.assertRaisesRegex(ValueError, 'Unknown model role: review'):
            load_role_config(file, 'review')
        with self.assertRaisesRegex(ValueError, 'missing role: score'):
            load_role_config(file, 'score')

    def test_model_tools_query_and_read_observations_for_both_roles(self):
        for role in ('instantiate', 'score'):
            sent = []
            queue = [
                {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'q1', 'type': 'function', 'function': {'name': 'query_evidence', 'arguments': '{}'}},
                    {'id': 'q2', 'type': 'function', 'function': {'name': 'query_evidence',
                         'arguments': '{"scope":"observation"}'}},
                    {'id': 'r1', 'type': 'function', 'function': {'name': 'read_evidence',
                         'arguments': '{"ids":["row:1","answer:1"]}'}},
                    {'id': 'r2', 'type': 'function', 'function': {'name': 'read_evidence',
                         'arguments': '{"ids":["trace:1"]}'}},
                ]}, 'finish_reason': 'tool_calls'}]}, response({'checks': []})]
            def transport(body):
                sent.append(copy.deepcopy(body))
                return queue.pop(0)
            client = ModelClient(self.config, evidence_fixture(), self.root / role,
                                 transport=transport, role=role)
            self.assertEqual(client(self.request), {'checks': []})
            contents = [json.loads(m['content']) for m in sent[1]['messages'] if m['role'] == 'tool']
            self.assertEqual(contents[0]['total_matches'], 5)
            self.assertEqual(contents[1]['total_matches'], 2)
            self.assertIn('PRIVATE_ANSWER_DATA', json.dumps(contents[2]))
            self.assertIn('PRIVATE_TRACE_DATA', json.dumps(contents[3]))
            enums = sent[0]['tools'][0]['function']['parameters']['properties']['scope']['enum']
            self.assertEqual(enums, ['task', 'reference', 'observation'])

    def test_roles_use_separate_models_endpoints_caches_and_context(self):
        sent = []
        def transport(body):
            sent.append(copy.deepcopy(body))
            return response({'accepted': True})
        generator = ModelClient(self.config, evidence_fixture(), self.root / 'logs',
                                transport=transport, role='instantiate')
        scorer_config = dict(self.config, base_url='https://score.invalid', model='score-model', max_tokens=2000)
        scorer = ModelClient(scorer_config, evidence_fixture(), self.root / 'logs',
                               transport=transport, role='score')
        generator(self.request)
        scorer(self.request)
        self.assertEqual([body['model'] for body in sent], ['planning-model', 'score-model'])
        self.assertEqual([body['max_tokens'] for body in sent], [4000, 2000])
        self.assertTrue(all(len(body['messages']) == 2 for body in sent))
        self.assertEqual(scorer.statistics()['api_attempts'], 1)
        self.assertEqual(scorer.statistics()['role'], 'score')
        identities = [json.loads(p.read_text(encoding='utf-8')) for p in (self.root / 'logs').glob('*.request.json')]
        self.assertEqual({i['endpoint'] for i in identities}, {'https://instantiate.invalid', 'https://score.invalid'})
        self.assertEqual({i['role'] for i in identities}, {'instantiate', 'score'})
        for path in (self.root / 'logs').glob('*'):
            self.assertNotIn(self.secret, path.read_text(encoding='utf-8'))
        generator(self.request)
        scorer(self.request)
        self.assertEqual(len(sent), 2)
        self.assertEqual(scorer.statistics()['response_cache_hits'], 1)

    def test_repeated_calls_have_fresh_context_and_score_can_read_observation(self):
        sent = []
        client = ModelClient(self.config, evidence_fixture(), self.root / 'score', role='score',
                             transport=lambda body: (sent.append(copy.deepcopy(body)) or response({'results': []})))
        client(self.request)
        client(dict(self.request, user='Another independent check'))
        self.assertTrue(all(len(body['messages']) == 2 for body in sent))
        self.assertEqual(client.store.read(['answer:1'])['records'][0]['data'], 'PRIVATE_ANSWER_DATA')

    def test_output_budget_validation_and_failed_attempt_count(self):
        for invalid in (True, 0, -1, '1000', 4.5):
            with self.assertRaisesRegex(ValueError, 'max_tokens'):
                ModelClient(dict(self.config, max_tokens=invalid), evidence_fixture(), self.root / 'invalid')
        def fail(body):
            raise RuntimeError('fixture failure')
        client = ModelClient(self.config, evidence_fixture(), self.root / 'failure', transport=fail)
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            client(self.request)
        self.assertEqual(client.statistics()['api_attempts'], 1)
        self.assertEqual(client.statistics()['api_calls'], 0)

    def test_ordered_call_audit_records_cache_failure_and_reopened_clients(self):
        log_dir = self.root / 'audit'
        payload = response({'accepted': True})
        payload['usage'] = {'prompt_tokens': 123, 'completion_tokens': 12, 'total_tokens': 135}
        request = dict(self.request, stage='instantiate')
        first = ModelClient(self.config, evidence_fixture(), log_dir,
                            role='instantiate', transport=lambda body: payload)
        # The second client exists before the first one writes its entries.
        second = ModelClient(self.config, evidence_fixture(), log_dir,
                             role='score', transport=lambda body: payload)
        first(request)
        first(request)
        second(dict(self.request, stage='score'))
        failure = urllib.error.HTTPError('https://fixture.invalid', 429, self.secret, {}, None)
        def fail(body):
            raise failure
        reopened = ModelClient(self.config, evidence_fixture(), log_dir, transport=fail)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            reopened(dict(self.request, stage='evaluate_group'))
        self.assertIs(caught.exception, failure)
        entries = [json.loads(line) for line in (log_dir / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([e['sequence'] for e in entries], [1, 2, 3, 4])
        self.assertEqual([e['status'] for e in entries], ['success', 'cache', 'success', 'error'])
        self.assertEqual([e['api_attempt'] for e in entries], [True, False, True, True])
        self.assertEqual([e['role'] for e in entries], ['instantiate', 'instantiate', 'score', 'score'])
        self.assertEqual([e['stage'] for e in entries], ['instantiate', 'instantiate', 'score', 'evaluate_group'])
        self.assertEqual(entries[0]['usage'], payload['usage'])
        self.assertEqual(entries[1]['usage'], payload['usage'])
        self.assertEqual(entries[-1]['error'], {'type': 'HTTPError', 'http_status': 429})
        self.assertIsNone(entries[-1]['response_file'])
        for item in entries:
            self.assertGreaterEqual(item['duration_seconds'], 0)
            self.assertTrue((log_dir / item['request_file']).exists())
            if item['response_file']:
                self.assertTrue((log_dir / item['response_file']).exists())
        for file in log_dir.glob('*'):
            self.assertNotIn(self.secret, file.read_text(encoding='utf-8'))

    def test_transport_failure_audit_does_not_include_exception_message(self):
        failure = RuntimeError('Do not persist credential: ' + self.secret)
        def fail(body):
            raise failure
        client = ModelClient(self.config, evidence_fixture(), self.root / 'error_audit', transport=fail)
        with self.assertRaises(RuntimeError) as caught:
            client(self.request)
        self.assertIs(caught.exception, failure)
        entry = json.loads((self.root / 'error_audit' / 'calls.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(entry['error'], {'type': 'RuntimeError'})
        self.assertNotIn(self.secret, json.dumps(entry))
        self.assertNotIn('Do not persist', json.dumps(entry))


if __name__ == '__main__':
    unittest.main()
