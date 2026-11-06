"""Replace only AC-1 with the user-requested simpler industrial task."""
from eval_bridge.resources import literal as _fragment
from pathlib import Path as _WorkspacePath
import os as _workspace_os
if not _workspace_os.environ.get('AIMS_BENCHMARK_WORKSPACE'):
    raise RuntimeError(_fragment('benchmarks/templates/build_stress_ac1_simple.json', 'fragment_1'))
_workspace = _WorkspacePath(_workspace_os.environ['AIMS_BENCHMARK_WORKSPACE']).resolve()
from eval_bridge.resources import literal as _asset_literal
from pathlib import Path
from copy import deepcopy
import json
import zipfile

WORK=_workspace
SUITE=WORK/'outputs'/'rubric_stress_suite_20261105'
CASE=SUITE/'cases'/'AC-1'

def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def build():
    backup=WORK/'outputs'/'AC-1_简化前设计备份_20261105.zip'
    if not backup.exists() and (CASE/'case.json').exists():
        meta=json.loads((CASE/'case.json').read_text(encoding='utf-8'))
        if meta.get('difficulty')!='simplified':
            with zipfile.ZipFile(backup,'w',zipfile.ZIP_DEFLATED) as z:
                for p in sorted(CASE.rglob('*')):
                    if p.is_file():z.write(p,Path('AC-1')/p.relative_to(CASE))
    attachments=CASE/'agent'/'attachments'
    # Remove only named, obsolete generated attachments; do not delete directories.
    for name in ['plm_register.json','quality_review_policy.txt','sales_thread.txt','coordination_memos.txt']:
        p=(attachments/name).resolve()
        assert p.parent==attachments.resolve()
        if p.is_file():p.unlink()
    rows=[]
    ids=list(range(1,22))+[28,64,96]
    for i,n in enumerate(ids):
        rows.append({'lot_id':f'LOT-{n:03}','plant':'苏州一厂' if i%2==0 else '苏州二厂',
                     'product':'HX-42','firmware':'3.0' if i%3==0 else '2.8',
                     'order_id':f'SO-{7800+n}','quantity_pcs':120+20*(i%5),
                     'warehouse_bin':f'FG-{1+i%3:02}','planned_ship_date':'2026-11-06',
                     'recorded_at':'2026-11-05T09:00:00+08:00'})
    byid={r['lot_id']:r for r in rows}
    byid['LOT-007'].update(plant='苏州一厂',firmware='2.8')
    byid['LOT-028'].update(plant='苏州二厂',firmware='2.8')
    byid['LOT-096'].update(plant='苏州一厂',firmware='3.0')
    notice=_asset_literal('benchmarks/definitions/build_stress_ac1_simple.json', 'literal_1')
    notes=_asset_literal('benchmarks/definitions/build_stress_ac1_simple.json', 'literal_2')
    prompt=_asset_literal('benchmarks/definitions/build_stress_ac1_simple.json', 'literal_3')
    put(attachments/'engineering_notice.txt',notice)
    put(attachments/'batch_register.jsonl','\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n')
    put(attachments/'handover_notes.txt',notes)
    put(CASE/'agent'/'task.txt',prompt)
    truth=_asset_literal('benchmarks/definitions/build_stress_ac1_simple.json', 'literal_4')
    checks=[]
    for lot,facts in truth.items():
        for field in ['plant','firmware','eco_applicable','upgrade_required_by_eco']:
            checks.append({'id':f'ac1-{lot}-{field}','criterion':f'最终答案中{lot}的{field}事实是否与台账和现行通知一致',
                           'method':'J','expected':facts[field],
                           'evidence_paths':[f'agent/attachments/batch_register.jsonl#{lot}','agent/attachments/engineering_notice.txt']})
    boundary=_asset_literal('benchmarks/definitions/build_stress_ac1_simple.json', 'literal_5')
    refs={'formal_notice_scope':'苏州一厂制造的HX-42','LOT-028_plant':'苏州二厂','current_notice_only':True}
    base={'claim':'LOT-028不适用ECO-247，本通知不要求该批升级。','trusted_reference':refs}
    fail=deepcopy(base);fail['claim']='LOT-028适用ECO-247，本通知要求该批升级。'
    unknown=deepcopy(base);unknown['trusted_reference']=None
    variants=[
      {'variant_id':'PASS','observation':base,'expected_verdict':'yes','changed_factor':'基准','reason':'二厂不在正式范围内，事实与参考一致'},
      {'variant_id':'FAIL','base_variant_id':'PASS','changes':[{'path':'/claim','value':fail['claim']}],'observation':fail,'expected_verdict':'no','changed_factor':'只将二厂适用性陈述反转','reason':'把一厂通知扩大到二厂，与明确参考矛盾'},
      {'variant_id':'UNKNOWN','base_variant_id':'PASS','changes':[{'path':'/trusted_reference','value':None}],'observation':unknown,'expected_verdict':'unverifiable','changed_factor':'仅撤去可供评测者核验的可信参考','reason':'不能因为无法核验就认定该陈述错误'}]
    meta={'id':'AC-1','title':'三批待发货的工程通知范围核验','primary_rubric':'AC-1','difficulty':'simplified',
          'mode':'attachment_only','objective':'核对三个批次的制造工厂、固件及单一工程通知适用事实',
          'trap_summary':'销售把只适用苏州一厂的通知扩大为苏州两厂；同为苏州发货不等于制造工厂相同。',
          'agent_prompt':'agent/task.txt','attachments':_asset_literal('benchmarks/definitions/build_stress_ac1_simple.json', 'literal_6'),
          'deliverables':['三批次核对表','对销售意见的简短回复'],
          'evaluator_only':['evaluator/design.md','evaluator/oracle.json','evaluator/contrast_cases.json'],
          'execution_requirements':['完整提供三份附件正文','保留实际最终答案；无需专用业务工具'],
          'oracle_checks':checks,'rubric_boundary':boundary}
    put(CASE/'case.json',meta)
    put(CASE/'evaluator'/'oracle.json',{'case_id':'AC-1','revision':'simplified-20261105','source':'当前ECO-247通知和冻结批次台账；不由被测答案反推','truth':truth,'checks':checks})
    put(CASE/'evaluator'/'contrast_cases.json',variants)
    put(CASE/'evaluator'/'design.md',_asset_literal('benchmarks/definitions/build_stress_ac1_simple.json', 'literal_7'))
    assert len(rows)==24 and len(meta['attachments'])==3
    for lot,expected in truth.items():
        r=byid[lot];applicable=r['plant']=='苏州一厂'
        assert applicable==expected['eco_applicable']
        assert (applicable and r['firmware']!='3.0')==expected['upgrade_required_by_eco']
    print(json.dumps({'case':'AC-1','targets':3,'rows':24,'attachments':3,'trap_count':1},ensure_ascii=True))

if __name__=='__main__':build()
