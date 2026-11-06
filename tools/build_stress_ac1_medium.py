"""Author the calibrated, medium-difficulty AC-1 fixture only."""
from eval_bridge.resources import literal as _fragment
from pathlib import Path as _WorkspacePath
import os as _workspace_os
if not _workspace_os.environ.get('AIMS_BENCHMARK_WORKSPACE'):
    raise RuntimeError(_fragment('benchmarks/templates/build_stress_ac1_medium.json', 'fragment_1'))
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
    backup=WORK/'outputs'/'AC-1_简化版设计备份_20261105.zip'
    if not backup.exists() and (CASE/'case.json').exists():
        old=json.loads((CASE/'case.json').read_text(encoding='utf-8'))
        if old.get('difficulty')=='simplified':
            with zipfile.ZipFile(backup,'w',zipfile.ZIP_DEFLATED) as z:
                for p in sorted(CASE.rglob('*')):
                    if p.is_file():z.write(p,Path('AC-1')/p.relative_to(CASE))
    a=CASE/'agent'/'attachments'
    for name in _asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_1'):
        p=(a/name).resolve()
        assert p.parent==a.resolve()
        if p.is_file():p.unlink()

    ids=list(range(1,45))+[49,64,83,96]
    rows=[]
    for i,n in enumerate(ids):
        plant='SZ1' if i%3!=1 else 'SZ2'
        day='2026-10-31' if i%7==0 else f'2026-11-{1+i%4:02}'
        rows.append({'lot_id':f'LOT-{n:03}','order_id':f'SO-{7800+n}','manufacturing_plant':plant,
                     'product':'HX-42','production_completed_at':day+' 16:20',
                     'shipping_warehouse':'SZ1-FG' if i%4 else 'SZ2-FG',
                     'firmware':'3.0' if i%4==0 else '2.8','quantity_pcs':120+20*(i%6),
                     'planned_ship_at':'2026-11-06 10:00'})
    byid={r['lot_id']:r for r in rows}
    specials=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_2')
    for lot,vals in specials.items():
        byid[lot].update(dict(zip(_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_3'),vals)))

    notice=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_4')
    columns=list(rows[0])
    ledger=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_5')
    ledger+='|'.join(columns)+'\n'+'\n'.join('|'.join(str(r[k]) for k in columns) for r in rows)+'\n'
    deviations=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_6')
    pcols=list(deviations[0])
    permissions=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_7')
    permissions+='|'.join(pcols)+'\n'+'\n'.join('|'.join(str(r[k]) for k in pcols) for r in deviations)+'\n'
    notes=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_8')
    task=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_9')
    for name,content in [('engineering_notice.txt',notice),('batch_register.txt',ledger),('deviation_register.txt',permissions),('handover_notes.txt',notes)]:put(a/name,content)
    put(CASE/'agent'/'task.txt',task)
    truth=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_10')
    for lot in truth:
        truth[lot]['source_facts']=byid[lot]
    checks=[]
    for lot,result in truth.items():
        for field in ['eco_applicable','upgrade_required_this_shipment','permit_id']:
            checks.append({'id':f'ac1-{lot}-{field}','criterion':f'最终答案中{lot}的{field}事实是否与该批次及本次订单范围一致','method':'J','expected':result[field],
                           'evidence_paths':[f'agent/attachments/batch_register.txt#{lot}','agent/attachments/engineering_notice.txt','agent/attachments/deviation_register.txt']})
    boundary=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_11')
    refs={lot:{'source_facts':r['source_facts'],'eco_applicable':r['eco_applicable'],'disposition':r['disposition']} for lot,r in truth.items()}
    claims={lot:r['disposition'] for lot,r in truth.items()}
    base={'claims':claims,'trusted_reference':refs}
    variants=[{'variant_id':'PASS','observation':base,'expected_verdict':'yes','changed_factor':'正确且有参考的五批事实','reason':'对象、日期与许可限定均一致'}]
    def variant(vid,path,value,verdict,reason):
        obs=deepcopy(base);parts=path.strip('/').split('/');target=obs
        for key in parts[:-1]:target=target[key]
        target[parts[-1]]=value
        variants.append({'variant_id':vid,'base_variant_id':'PASS','changes':[{'path':path,'value':value}],'observation':obs,'expected_verdict':verdict,'changed_factor':'仅改变'+path,'reason':reason})
    variant('FAIL_WAREHOUSE','/claims/LOT-028','LOT-028从一厂仓发运，因此适用ECO并需升级。','no','把发运仓改写为制造工厂')
    variant('FAIL_DATE','/claims/LOT-049','LOT-049在11月发运，属于ECO强制升级范围。','no','把发运日期改写为通知采用的生产完成时点')
    variant('FAIL_EXCEPTION','/claims/LOT-064','LOT-064有偏离许可，所以不适用ECO。','no','处理建议可能相同，但适用性事实错误')
    variant('UNKNOWN','/trusted_reference',None,'unverifiable','只撤去评测者可信参考，不能把无法核验判为事实错误')
    meta={'id':'AC-1','title':'跨厂发运中的工程变更日期与批次例外核验','primary_rubric':'AC-1','difficulty':'medium','mode':'attachment_only',
          'objective':'对五个待发批次核对制造归属、生产时点与单批许可限定下的最终事实',
          'trap_summary':'发运仓替代制造工厂；11月发运被当作11月生产；有效偏离许可被误写成不适用ECO。',
          'agent_prompt':'agent/task.txt','attachments':[f'agent/attachments/{n}' for n in ['engineering_notice.txt','batch_register.txt','deviation_register.txt','handover_notes.txt']],
          'deliverables':['五批次工程处置核对表','对销售拟回复的更正意见'],
          'evaluator_only':_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_12'),
          'execution_requirements':['完整提供四份附件正文','保存最终答案；无需专用业务执行工具'],
          'oracle_checks':checks,'rubric_boundary':boundary}
    put(CASE/'case.json',meta)
    put(CASE/'evaluator'/'oracle.json',{'case_id':'AC-1','revision':'medium-20261105','source':'已发布ECO、制造台账及明确对象的偏离登记','truth':truth,'checks':checks,'target_count':5,'source_row_count':48})
    put(CASE/'evaluator'/'contrast_cases.json',variants)
    answer=_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_13')
    put(CASE/'evaluator'/'参考答案.md',answer)
    put(CASE/'evaluator'/'design.md',_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_14'))
    # Re-derive expectations from raw rows and scoped permits, independent of the labelled outcomes.
    for lot,label in truth.items():
        r=byid[lot]
        applicable=(r['manufacturing_plant']=='SZ1' and r['product']=='HX-42' and r['production_completed_at']>='2026-11-01 00:00')
        permits=[p for p in deviations if p['status']=='released' and p['lot_id']==lot and p['order_id']==r['order_id'] and p['quantity_pcs']==r['quantity_pcs'] and p['allowed_firmware']==r['firmware'] and p['valid_from']<=r['planned_ship_at']<=p['valid_until']]
        assert applicable==label['eco_applicable']
        assert (applicable and r['firmware']!='3.0' and not permits)==label['upgrade_required_this_shipment']
        assert (permits[0]['permit_id'] if permits else None)==label['permit_id']
    assert len(rows)==48 and len({r['lot_id'] for r in rows})==48
    for rel in meta['attachments']:assert len((CASE/rel).read_text(encoding='utf-8'))<=6000,rel
    print(json.dumps(_asset_literal('benchmarks/definitions/build_stress_ac1_medium.json', 'literal_15'),ensure_ascii=True))

if __name__=='__main__':build()
