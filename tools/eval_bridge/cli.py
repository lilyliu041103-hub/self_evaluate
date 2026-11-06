"""Separate preparation, indexed evidence access, and explicit scoring commands."""
from __future__ import annotations
from .resources import literal as _fragment

import argparse
import copy
import json
import sys
import time
from pathlib import Path

from .evidence import EvidenceStore, write_json
from .planning import bind_observation, prepare, verify_plan

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE = ROOT / 'runtime/metric_rubric_impl_v4_modular'


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest='command', required=True)
    prep = subs.add_parser('prepare', help='Local-only preparation; no API, no candidate-dependent rubric generation')
    prep.add_argument('--input', required=True)
    prep.add_argument('--out', required=True)
    prep.add_argument('--engine', default=str(DEFAULT_ENGINE))
    prep.add_argument('--observation')
    prep.add_argument('--task-id')
    prep.add_argument('--bindings', help='Optional trusted JSON mapping rubric IDs to program bindings')
    prep.add_argument('--methods', help='Optional JSON mapping rubric IDs to allowed P/J/H methods, fixed before scoring')
    inst = subs.add_parser('instantiate', help=_fragment('prompts/bridge/fragments/cli.json', 'fragment_1'))
    inst.add_argument('--bundle', required=True)
    inst.add_argument('--engine', default=str(DEFAULT_ENGINE))
    inst.add_argument('--out', required=True)
    inst.add_argument('--model-roles', help='Live opt-in using the instantiate role configuration')
    inst.add_argument('--max-tool-rounds', type=int, default=2)
    score = subs.add_parser('score', help='Execute locally validated checks for this exact run. Offline unless a model configuration is given.')
    score.add_argument('--bundle', required=True)
    score.add_argument('--plan', help='Frozen runtime plan for this exact observation and task/reference version')
    score.add_argument('--engine', default=str(DEFAULT_ENGINE))
    score.add_argument('--out', required=True)
    score_models = score.add_mutually_exclusive_group()
    score_models.add_argument('--model-roles', help='Live opt-in using the independently configured score role')
    score_models.add_argument('--api-config', help='Legacy score-role-only live configuration; never copied to the output bundle')
    score.add_argument('--template-preview', action='store_true', help='Explicit offline diagnostic of static templates; cannot make API calls')
    score.add_argument('--task-profile', help='Task-level capability profile JSON; decides rubric applicability before Judge requests are built')
    score.add_argument('--no-task-profile', action='store_true', help='Disable automatic configs/task-profiles/<task_id>.json discovery')
    score.add_argument('--max-chars', type=int, default=64000,
                       help='Character budget for the full initial request, tools, and output/retrieval reserves')
    score.add_argument('--max-checks', type=int, default=8)
    score.add_argument('--max-tool-rounds', type=int, default=2)
    score.add_argument('--scoring-mode', choices=['baseline', 'metric'], default='metric',
                       help='Group by rubric family (baseline) or isolate each metric (default)')
    score.add_argument('--common-evidence-mode', choices=['none', 'shared', 'packs'], default='packs',
                       help='Legacy layout, shared prefix, or evidence pack prefixes (default)')
    score.add_argument('--judge-layout', choices=['inline', 'hoisted'], default='inline',
                       help='inline repeats rubric rule text per check; hoisted shares it once per group')
    score.add_argument('--no-response-cache', action='store_true',
                       help='Bypass local response reuse for provider prefix-cache measurements')
    score.add_argument('--common-prefix-max-chars', type=int, default=12000,
                       help='Maximum serialized shared evidence prefix characters')
    score.add_argument('--output-reserve-chars', type=int, default=8000,
                       help='Character allowance reserved for model output (not a token limit)')
    score.add_argument('--tool-reserve-chars', type=int, default=8000,
                       help='Character allowance reserved for supplemental evidence')
    query = subs.add_parser('query', help='Search evidence; complete matches are paginated')
    query.add_argument('--bundle', required=True)
    query.add_argument('--scope', choices=['task', 'reference', 'observation'])
    query.add_argument('--kind', action='append', dest='kinds')
    query.add_argument('--table-id')
    query.add_argument('--field')
    query.add_argument('--equals')
    query.add_argument('--contains')
    query.add_argument('--offset', type=int, default=0)
    query.add_argument('--limit', type=int, default=50)
    read = subs.add_parser('read', help='Get records by evidence ID or business ID')
    read.add_argument('--bundle', required=True)
    read.add_argument('--id', dest='ids', action='append', required=True)
    read.add_argument('--raw', action='store_true', help='Return original audit values, not model view')
    read.add_argument('--max-chars', type=int, default=40000)
    return p


def empty_output(path):
    output = Path(path)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use an empty output directory; previous records are preserved.')
    return output


def instantiate(args, store):
    from .instantiation import build_instantiation_request, instantiate_plan
    output = empty_output(args.out)
    base = json.loads((Path(args.bundle) / 'plan.json').read_text(encoding='utf-8'))
    verify_plan(base, store, args.engine)
    if base.get('stage') != 'template':
        raise ValueError('Instantiation requires a prepared template plan.')
    first = build_instantiation_request(base, store)
    write_json(output / 'instantiate.request.json', first)
    if not args.model_roles:
        write_json(output / 'status.json', {
            'stage': 'request_exported', 'api_calls': 0, 'frozen_plan': None,
            'note': 'Runtime checks have not been generated or locally validated. This is not a scoring plan.'})
        return {'saved': str(output.resolve()), 'stage': 'request_exported', 'api_calls': 0}
    from .client import ModelClient
    from .roles import load_role_config
    client = ModelClient(load_role_config(args.model_roles, 'instantiate'), store,
                         output / 'api-calls' / 'instantiate', args.max_tool_rounds,
                         role='instantiate')
    requests = []
    phase = 'model_call'

    def recorded(request):
        nonlocal phase
        phase = 'model_call'
        attempt = len(requests)
        write_json(output / 'attempts' / f'{attempt:02d}.request.json', request)
        write_json(output / 'instantiate.request.json', request)
        response = client(request)
        write_json(output / 'attempts' / f'{attempt:02d}.response.json', response)
        write_json(output / 'instantiate.response.json', response)
        phase = 'local_validation'
        return response

    try:
        plan = instantiate_plan(base, store, recorded, request_sink=requests,
                                provenance={'roles': {'instantiate': {
                                    key: value for key, value in client.statistics().items()
                                    if key in ('role', 'model', 'base_url')}}})
        verify_plan(plan, store, args.engine)
        write_json(output / 'plan.json', plan)
    except Exception as exc:
        # Full provider records live in api-calls; avoid persisting arbitrary
        # exception text that could contain transport configuration secrets.
        failure = {
            'stage': 'not_frozen', 'error_type': type(exc).__name__,
            'phase': phase, 'roles': {'instantiate': client.statistics()}, 'frozen_plan': None}
        if phase == 'local_validation':
            # The API response is already recorded. These diagnostics explain
            # candidate/plan validation, not a transport configuration failure.
            failure['validation_error'] = str(exc)
        write_json(output / 'status.json', failure)
        raise
    finally:
        write_json(output / 'requests.json', requests)
    statistics = {'instantiate': client.statistics()}
    write_json(output / 'status.json', {
        'stage': 'frozen', 'phase': 'complete', 'plan_hash': plan['plan_hash'],
        'generation_attempts': len(requests),
        'run_fingerprint': plan['run_fingerprint'], 'validation': plan['instantiation']['validation'],
        'roles': statistics, 'checks': len(plan['task_checks']), 'unresolved': plan['unresolved']})
    return {'saved': str((output / 'plan.json').resolve()), 'stage': 'frozen',
            'checks': len(plan['task_checks']), 'unresolved_rubrics': len(plan['unresolved']),
            'roles': statistics}


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == 'prepare':
            bindings = json.loads(Path(args.bindings).read_text(encoding='utf-8-sig')) if args.bindings else None
            methods = json.loads(Path(args.methods).read_text(encoding='utf-8-sig')) if args.methods else None
            plan = prepare(args.input, args.out, args.engine, args.observation, args.task_id, bindings, methods)
            print(json.dumps({'prepared': str(Path(args.out).resolve()), 'rubrics': len(plan['profiles']),
                              'plan_hash': plan['plan_hash'], 'api_calls': 0}, ensure_ascii=False))
            return 0
        store = EvidenceStore(args.bundle)
        store.verify()
        if args.command == 'query':
            result = store.select(**{k: getattr(args, k) for k in
                                     ('scope', 'kinds', 'table_id', 'field', 'equals', 'contains', 'limit', 'offset')})
        elif args.command == 'read':
            result = [store.read_original(eid) for eid in args.ids] if args.raw else store.read(args.ids, args.max_chars)
        elif args.command == 'instantiate':
            result = instantiate(args, store)
        else:
            from .scoring import evaluate_prepared
            from .client import request_overhead_chars
            if args.tool_reserve_chars == 0 and args.max_tool_rounds > 0:
                raise ValueError('--tool-reserve-chars 0 requires --max-tool-rounds 0.')
            if not 0 <= args.max_tool_rounds <= 8:
                raise ValueError('--max-tool-rounds must be in 0..8.')
            output = empty_output(args.out)
            plan_path = Path(args.plan) if args.plan else Path(args.bundle) / 'plan.json'
            plan = json.loads(plan_path.read_text(encoding='utf-8'))
            if args.template_preview:
                if args.api_config or args.model_roles:
                    raise ValueError('--template-preview is offline only; remove live API configuration.')
                if plan.get('stage') != 'template':
                    raise ValueError('--template-preview requires a static template plan.')
            elif plan.get('schema_version') != 'agentos-rubric-plan/4' or plan.get('stage') != 'frozen':
                raise ValueError(_fragment('prompts/bridge/fragments/cli.json', 'fragment_2'))
            checks, selection = bind_observation(plan, store, args.engine)
            original = json.loads((Path(args.bundle) / 'audit/original-input.json').read_text(encoding='utf-8'))
            sys.path.insert(0, str(Path(args.engine).resolve()))
            from adaptive_eval.programmatic import HANDLERS as value_handlers
            from adaptive_eval.temporal import HANDLERS as temporal_handlers
            callback = None
            if args.api_config or args.model_roles:
                from .client import ModelClient
                from .roles import load_role_config
                config = load_role_config(args.model_roles, 'score') if args.model_roles else args.api_config
                callback = ModelClient(config, store, output / 'api-calls', args.max_tool_rounds,
                                       role='score', response_cache=not args.no_response_cache)
            applicability, profile_meta = None, None
            if not args.no_task_profile:
                from . import task_profile as task_profile_module
                if args.task_profile:
                    profile_path = Path(args.task_profile)
                else:
                    profile_path = (Path(__file__).resolve().parents[2] / "configs" /
                                    "task-profiles" / (str(plan.get("task_id", "")) + ".json"))
                if args.task_profile or profile_path.exists():
                    profile = task_profile_module.load(profile_path)
                    observed = task_profile_module.observed_gates_from_checks(checks)
                    applicability = task_profile_module.rubric_applicability(
                        profile, [p["id"] for p in plan["profiles"]], observed)
                    profile_meta = {"path": str(profile_path), "task_id": profile["task_id"],
                                    "sha256": task_profile_module.digest(profile_path),
                                    "observed": sorted(observed),
                                    "summary": task_profile_module.summarize(applicability)}
            requests = []
            started = time.perf_counter()
            report = evaluate_prepared(plan, checks, store.records, judge_fn=callback,
                                       programs={**value_handlers, **temporal_handlers}, original_input=original,
                                       request_sink=requests, max_chars=args.max_chars, max_checks=args.max_checks,
                                       scoring_mode=args.scoring_mode,
                                       common_evidence_mode=args.common_evidence_mode,
                                       common_prefix_max_chars=args.common_prefix_max_chars,
                                       output_reserve_chars=args.output_reserve_chars,
                                       tool_reserve_chars=args.tool_reserve_chars,
                                       request_overhead_chars=request_overhead_chars(),
                                       applicability=applicability,
                                       task_profile_meta=profile_meta,
                                       judge_layout=args.judge_layout)
            report.setdefault('execution', {})['mode'] = ('live' if callback else
                ('template_preview' if args.template_preview else 'offline_preview'))
            report['execution']['duration_seconds'] = round(time.perf_counter() - started, 6)
            report['execution']['response_cache_enabled'] = not args.no_response_cache
            report['execution']['max_tool_rounds'] = args.max_tool_rounds
            report['execution']['judge_layout'] = args.judge_layout
            if callback:
                report['execution']['client'] = callback.statistics()
            report['execution']['note'] = ('Model-backed group judgments; missing evidence and missing program bindings remain unverifiable.'
                                           if callback else 'No Judge API was called; this is an offline preview, not a measured model score.')
            write_json(output / 'report.json', report)
            write_json(output / 'bound-checks.json', checks)
            write_json(output / 'selection.json', selection)
            write_json(output / 'requests.json', requests)
            if args.common_evidence_mode == 'packs':
                manifest = copy.deepcopy(report['execution']['evidence_packs'])
                for pack in manifest['packs']:
                    pack['request_group_ids'] = [r['group_id'] for r in requests
                                                 if pack['pack_id'] in r['evidence_pack_ids']]
                    write_json(output / 'evidence-packs' / (pack['pack_id'] + '.json'), {
                        'schema_version': 'score-evidence-pack/1', 'pack_id': pack['pack_id'],
                        'evidence': {eid: store.records[eid] for eid in pack['evidence_ids']}})
                manifest['cache_semantics'] = (_fragment('prompts/bridge/fragments/cli.json', 'fragment_3'))
                write_json(output / 'evidence-packs' / 'manifest.json', manifest)
            write_json(output / 'run-binding.json', {'plan_hash': plan['plan_hash'],
                                                     'bundle': str(Path(args.bundle).resolve()),
                                                     'run_fingerprint': plan.get('run_fingerprint'),
                                                     'source_trace_sha256': original['observation'].get('trace_sha256'),
                                                     'scoring_mode': args.scoring_mode,
                                                     'common_evidence_mode': args.common_evidence_mode})
            result = {'saved': str((output / 'report.json').resolve()), 'mode': report['execution']['mode'],
                      'rubrics': len(report.get('rubrics', {})), 'requests_exported': len(requests),
                       'task_profile': (profile_meta or {}).get('summary')}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        p.error(str(exc))


if __name__ == '__main__':
    main()
