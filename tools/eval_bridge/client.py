"""Explicit opt-in model client with bounded, read-only evidence tools."""
from __future__ import annotations

import copy
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .evidence import canonical, digest, write_json
from .roles import MODEL_ROLES


def _decode_json(content):
    """Read JSON or one unambiguous fenced JSON block; retain raw API responses."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        fenced = re.search(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', content)
        if fenced is None or content.count('```') != 2:
            raise
        return json.loads(fenced.group(1))


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'query_evidence', 'description': 'Query original evidence by kind, table or exact field value. Returns IDs and pagination; not a conclusion about source completeness.',
        'parameters': {'type': 'object', 'properties': {
            'scope': {'type': 'string', 'enum': ['task', 'reference', 'observation']},
            'kinds': {'type': 'array', 'items': {'type': 'string'}},
            'table_id': {'type': 'string'}, 'field': {'type': 'string'},
            'equals': {'type': 'string'}, 'contains': {'type': 'string'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
            'offset': {'type': 'integer', 'minimum': 0}}, 'additionalProperties': False}}},
    {'type': 'function', 'function': {
        'name': 'read_evidence', 'description': 'Read exact evidence IDs or business record IDs. Follow nested evidence_ref pointers. For read_budget_exceeded, use next_read_arguments: offset pages the JSON text of one exact record. Concatenate pages in offset order; next_offset=null ends paging. Missing/omitted content is not negative evidence.',
        'parameters': {'type': 'object', 'properties': {
            'ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 200},
            'offset': {'type': 'integer', 'minimum': 0},
            'max_chars': {'type': 'integer', 'minimum': 100, 'maximum': 40000}},
            'required': ['ids'], 'additionalProperties': False}}},
]


class ModelClient:
    def __init__(self, config_path, store, log_dir, max_tool_rounds=2, transport=None, *, role='score'):
        if isinstance(max_tool_rounds, bool) or not isinstance(max_tool_rounds, int) or not 0 <= max_tool_rounds <= 8:
            raise ValueError('max_tool_rounds must be an integer in 0..8')
        if role not in MODEL_ROLES:
            raise ValueError('Unknown model role: ' + str(role))
        self.role = role
        self.config = (copy.deepcopy(config_path) if isinstance(config_path, dict)
                       else json.loads(Path(config_path).read_text(encoding='utf-8-sig')))
        if not isinstance(self.config, dict) or any(
                not isinstance(self.config.get(field), str) or not self.config[field].strip()
                for field in ('api_key', 'model', 'base_url')):
            raise ValueError('API config needs base_url, model and api_key')
        if 'max_tokens' in self.config and (
                isinstance(self.config['max_tokens'], bool) or not isinstance(self.config['max_tokens'], int)
                or self.config['max_tokens'] <= 0):
            raise ValueError('max_tokens must be a positive integer')
        if 'enable_thinking' in self.config and type(self.config['enable_thinking']) is not bool:
            raise ValueError('enable_thinking must be a boolean')
        if 'thinking_budget' in self.config and (
                type(self.config['thinking_budget']) is not int or self.config['thinking_budget'] <= 0):
            raise ValueError('thinking_budget must be a positive integer')
        # Both roles read the same immutable run bundle; observations identify
        # runtime check instances while the fixed rubrics remain authoritative.
        self.store = store
        self.tools = copy.deepcopy(TOOLS)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_tool_rounds = max_tool_rounds
        self.transport = transport
        self.calls = 0
        self.attempts = 0
        self.cache_hits = 0
        self.tool_calls = 0
        self.sequence = 0

    def _audit_call(self, entry):
        path = self.log_dir / 'calls.jsonl'
        # Roles may share a directory, and a new process may reuse an audit log.
        # Calls are sequential; count the current log when appending, not only
        # when constructing the client.
        existing = 0
        if path.exists():
            with path.open(encoding='utf-8') as log:
                existing = sum(1 for line in log if line.strip())
        self.sequence = max(self.sequence, existing) + 1
        entry = {'sequence': self.sequence, 'role': self.role, **entry}
        with path.open('a', encoding='utf-8') as log:
            log.write(canonical(entry) + '\n')

    def _send(self, body, *, stage=None):
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        identity = {'role': self.role, 'endpoint': self.config['base_url'].rstrip('/'), 'body': body}
        if stage is not None:
            identity['stage'] = stage
        key = digest(identity)
        prefix = self.log_dir / key
        result_path = prefix.with_suffix('.response.json')
        request_path = prefix.with_suffix('.request.json')
        api_attempt = False
        try:
            write_json(request_path, identity)
            if result_path.exists():
                response = json.loads(result_path.read_text(encoding='utf-8'))
                self.cache_hits += 1
                status = 'cache'
            else:
                api_attempt = True
                self.attempts += 1
                if self.transport:
                    response = self.transport(body)
                else:
                    req = urllib.request.Request(self.config['base_url'].rstrip('/') + '/chat/completions',
                                                 data=canonical(body).encode('utf-8'), headers={
                                                     'Content-Type': 'application/json',
                                                     'Authorization': 'Bearer ' + self.config['api_key']})
                    with urllib.request.urlopen(req, timeout=self.config.get('timeout_seconds', 180)) as res:
                        response = json.load(res)
                self.calls += 1
                write_json(result_path, response)
                status = 'success'
        except Exception as exc:
            error = {'type': type(exc).__name__}
            if isinstance(exc, urllib.error.HTTPError):
                error['http_status'] = exc.code
            self._audit_call({'stage': stage, 'started_at': started_at,
                              'duration_seconds': round(time.perf_counter() - started, 6),
                              'request_file': request_path.name, 'response_file': None,
                              'status': 'error', 'api_attempt': api_attempt, 'error': error})
            raise
        entry = {'stage': stage, 'started_at': started_at,
                 'duration_seconds': round(time.perf_counter() - started, 6),
                 'request_file': request_path.name, 'response_file': result_path.name,
                 'status': status, 'api_attempt': api_attempt}
        if isinstance(response, dict) and 'usage' in response:
            entry['usage'] = response['usage']
        self._audit_call(entry)
        return response

    def __call__(self, request):
        messages = [{'role': 'system', 'content': request['system']},
                    {'role': 'user', 'content': request['user']}]
        tool_count = 0
        returned_chars = 0
        for round_no in range(self.max_tool_rounds + 1):
            final_round = round_no == self.max_tool_rounds
            if final_round:
                messages.append({'role': 'user', 'content': (
                    'Evidence retrieval has reached its configured round limit. '
                    'Return the final JSON for the original request now using the evidence already received. '
                    'Do not request further tools. Preserve unresolved prerequisites or unverifiable judgments '
                    'where evidence is insufficient; do not invent facts or infer absence from unread data.')})
            body = {'model': self.config['model'], 'messages': messages,
                    'response_format': {'type': 'json_object'}, 'stream': False,
                    'tools': self.tools, 'tool_choice': 'none' if final_round else 'auto'}
            if 'max_tokens' in self.config:
                body['max_tokens'] = self.config['max_tokens']
            for field in ('enable_thinking', 'thinking_budget'):
                if field in self.config:
                    body[field] = self.config[field]
            response = self._send(body, stage=request.get('stage'))
            choice = response['choices'][0]
            message = choice['message']
            if not message.get('tool_calls'):
                if choice.get('finish_reason') not in ('stop', None):
                    raise ValueError('Model output incomplete: ' + str(choice.get('finish_reason')))
                return _decode_json(message['content'])
            if round_no == self.max_tool_rounds:
                raise ValueError('Evidence tool-round budget exhausted; verdict unavailable')
            if len(message['tool_calls']) > 8 or tool_count + len(message['tool_calls']) > 16:
                raise ValueError('Evidence tool-call budget exhausted; verdict unavailable')
            messages.append(message)
            tool_ids = set()
            for call in message['tool_calls']:
                if call['id'] in tool_ids:
                    raise ValueError('Duplicate evidence tool call ID')
                tool_ids.add(call['id'])
                self.tool_calls += 1
                tool_count += 1
                try:
                    args = json.loads(call['function']['arguments'])
                    name = call['function']['name']
                    if name == 'query_evidence':
                        result = self.store.select(**args)
                    elif name == 'read_evidence':
                        args['max_chars'] = min(args.get('max_chars', 20000), 40000)
                        result = self.store.read(**args)
                    else:
                        result = {'error': 'Unknown evidence tool'}
                except (ValueError, KeyError, TypeError):
                    result = {'error': 'Invalid evidence tool arguments'}
                content = canonical(result)
                returned_chars += len(content)
                if returned_chars > 60000:
                    raise ValueError('Evidence return-text budget exhausted; verdict unavailable')
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': content})
        raise AssertionError('Unreachable')

    def statistics(self):
        return {'role': self.role, 'model': self.config['model'], 'base_url': self.config['base_url'],
                'api_calls': self.calls, 'api_attempts': self.attempts,
                'response_cache_hits': self.cache_hits, 'evidence_tool_calls': self.tool_calls}
