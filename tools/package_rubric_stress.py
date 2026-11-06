from eval_bridge.resources import literal as _fragment
from pathlib import Path as _WorkspacePath
import os as _workspace_os
if not _workspace_os.environ.get('AIMS_BENCHMARK_WORKSPACE'):
    raise RuntimeError(_fragment('benchmarks/templates/package_rubric_stress.json', 'fragment_1'))
_workspace = _WorkspacePath(_workspace_os.environ['AIMS_BENCHMARK_WORKSPACE']).resolve()
from eval_bridge.resources import literal as _asset_literal
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from copy import deepcopy
import json, hashlib, re, zipfile, shutil

WORK=_workspace
ROOT=WORK/'outputs'/'rubric_stress_suite_20261105'
ORDER=_asset_literal('benchmarks/definitions/package_rubric_stress.json', 'literal_1')
ALLOWED={x:['P','J','H'] for x in ORDER}
for x in ['AC-1','AC-2','TU-1','PE-1','EG-1','EG-3']:ALLOWED[x]=['J']
for x in ['AC-3','BC-1']:ALLOWED[x]=['P']
for x in ['PE-2','EG-2','ER-2']:ALLOWED[x]=['P','J']

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def put(p,v):
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(v if isinstance(v,str) else json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def require(test,message):
    if not test:raise AssertionError(message)

def recompute_ac3():
    d=ROOT/'cases'/'AC-3'; a=d/'agent'/'attachments'
    rates=read(a/'month_end_fx.json')
    fx=Decimal(next(x['USD_CNY'] for x in rates['rates'] if x['date']=='2026-10-31' and x['purpose']=='book_close'))
    src=[json.loads(s) for s in (a/'energy_ledger.jsonl').read_text(encoding='utf-8').splitlines() if s]
    totals=defaultdict(lambda:defaultdict(Decimal)); lines={}; posted=0
    for r in src:
        if r['status']!='posted':continue
        posted+=1
        energy=Decimal(r['energy'])*(1000 if r['energy_unit']=='MWh' else 1)
        raw=energy*Decimal(r['unit_price'])*(fx if r['currency']=='USD' else 1)
        rate=Decimal(r['tax_rate'])
        net=(raw/(1+rate) if r['price_includes_tax'] else raw).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        tax=(net*rate).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        line={'energy_kwh':energy,'net_cny':net,'tax_cny':tax,'gross_cny':net+tax}
        lines[r['line_id']]=line
        for k,v in {'energy_kwh':energy,'net':net,'tax':tax,'gross':net+tax}.items():totals[r['site']][k]+=v
    oracle=read(d/'evaluator'/'oracle.json')['truth']
    require(posted==oracle['posted_count'],'AC3 posted count')
    for site,vals in oracle['sites'].items():
        for k,v in vals.items():require(totals[site][k]==Decimal(v),f'AC3 {site} {k}')
    for expected in oracle['line_results']:
        for k,v in expected.items():
            if k!='line_id':require(lines[expected['line_id']][k]==Decimal(v),f'AC3 line {expected["line_id"]} {k}')
    return {'check':'AC-3独立逐行及分基地Decimal复算','passed':True,'source_rows':len(src),'posted_rows':posted,'totals':{s:{k:str(v) for k,v in t.items()} for s,t in totals.items()}}

def recompute_eg3():
    d=ROOT/'cases'/'EG-3'; a=d/'agent'/'attachments'
    rows=[json.loads(s) for s in (a/'03_订单台账.jsonl').read_text(encoding='utf-8').splitlines() if s]
    contracts={r['contract_id']:r for r in read(a/'04_主合同.json')}
    amendments=read(a/'05_补充协议.json');credits=read(a/'06_红字单.json');disputes=read(a/'07_争议台账.json')
    selected=[r for r in rows if r['customer']=='恒瑞机电' and r['settlement_period']=='2026-10' and r['acceptance_state']=='accepted']
    values={};hold=[]
    for r in selected:
        rate=Decimal(contracts[r['contract_id']]['retention_rate'])
        for am in amendments:
            if am['signed'] and r['order_id'] in am['scope_order_ids']:rate=Decimal(am['retention_rate'])
        credit=sum((Decimal(x['amount_cny']) for x in credits if x['order_id']==r['order_id'] and x['state']=='posted' and x['effective_period']=='2026-10'),Decimal(0))
        val=((Decimal(r['gross_cny'])-credit)*(1-rate)-Decimal(r['payment_received_cny'])).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        values[r['order_id']]=val
        if any(x['order_id']==r['order_id'] and x['state']=='open' and x['stop_collection'] for x in disputes):hold.append(r['order_id'])
    o=read(d/'evaluator'/'oracle.json');total=sum(values.values())
    require(len(selected)==o['selected_count'],'EG3 target count')
    require(total==Decimal(o['expected_total_cny']),'EG3 total')
    require(values['ORD-0287']==Decimal(o['critical_order']['collectible_cny']),'EG3 critical balance')
    require(len(hold)==5,'EG3 hold count')
    return {'check':'EG-3独立按合同补充、红字及争议关联复算','passed':True,'selected_rows':len(selected),'total_cny':str(total),'critical_order_cny':str(values['ORD-0287']),'hold_order_ids':hold}

def validate_contrast_patch(variants):
    index={v['variant_id']:v for v in variants}
    for v in variants:
        if not v.get('base_variant_id'):continue
        if 'changes' not in v:continue
        obs=deepcopy(index[v['base_variant_id']]['observation'])
        for change in v['changes']:
            parts=change['path'].strip('/').split('/');target=obs
            for key in parts[:-1]:target=target[int(key)] if isinstance(target,list) else target[key]
            if isinstance(target,list):target[int(parts[-1])]=change['value']
            else:target[parts[-1]]=change['value']
        require(obs==v['observation'],f'contrast patch {v["variant_id"]}')

def validate_bc_costs():
    data=read(ROOT/'cases'/'BC-1'/'evaluator'/'contrast_cases.json')
    for c in data:
        o=c['observation']; b=o['call_breakdown']
        require(sum(b.values())==o['task_tool_calls'],f'BC calls {c["variant_id"]}')
        cost=(40000+7000*(b['batch']-1) if b['batch'] else 0)+5000*b['diagnose']+2000*b['submit']
        require(cost==o['tool_elapsed_ms'],f'BC cost {c["variant_id"]}')
    return {'check':'BC-1各对照用时与调用账可达性','passed':True,'variants':len(data)}

def main():
    eg3_path=ROOT/'cases'/'EG-3'/'evaluator'/'contrast_cases.json'
    eg3_variants=read(eg3_path)
    eg3_fail=next(x for x in eg3_variants if x['variant_id']=='FAIL_SUMMARY_ONLY')
    eg3_unknown=next(x for x in eg3_variants if x['variant_id']=='UNKNOWN_MISSING_LOG')
    eg3_unknown['observation']=deepcopy(eg3_fail['observation'])
    eg3_unknown['observation']['acquisition_log_complete']=False
    eg3_unknown['base_variant_id']='FAIL_SUMMARY_ONLY'
    eg3_unknown['changes']=[{'path':'/acquisition_log_complete','value':False}]
    put(eg3_path,eg3_variants)
    cases=[];stats=[];parsed=0;total_contrasts=0
    require(set(p.name for p in (ROOT/'cases').iterdir() if p.is_dir())==set(ORDER),'22 IDs exact')
    for cid in ORDER:
        d=ROOT/'cases'/cid;c=read(d/'case.json');cases.append(c)
        require(c['id']==cid and c['primary_rubric']==cid,f'id {cid}')
        require(c['mode'] in ['attachment_only','sandbox_live'],f'mode {cid}')
        for path in [c['agent_prompt']]+c['attachments']+c['evaluator_only']:
            require((d/path).is_file(),f'missing {cid}/{path}')
        require(len(c['attachments'])>=3,f'attachments {cid}')
        for x in c['oracle_checks']:require(x['method'] in ALLOWED[cid],f'method mismatch {cid}/{x["id"]}')
        variants=read(d/'evaluator'/'contrast_cases.json')
        if isinstance(variants,dict):variants=variants.get('cases',variants.get('variants',[]))
        require({'yes','no','unverifiable'} <= {x['expected_verdict'] for x in variants},f'verdicts {cid}')
        validate_contrast_patch(variants);total_contrasts+=len(variants)
        main_counts=[];primary_jsonl_count=None;cjk=0;chars=0;byte_count=0
        for path in c['attachments']:
            p=d/path;txt=p.read_text(encoding='utf-8-sig');chars+=len(txt);byte_count+=p.stat().st_size
            if p.suffix=='.txt':cjk+=len(re.findall('[\u4e00-\u9fff]',txt))
            if p.suffix=='.jsonl':
                n=len([line for line in txt.splitlines() if line.strip()]);main_counts.append(n)
                if primary_jsonl_count is None:primary_jsonl_count=n
            if cid=='AC-1' and p.name=='batch_register.txt':
                primary_jsonl_count=len(re.findall(r'^LOT-\d{3}\|',txt,re.M))
            if p.suffix=='.json':
                data=read(p)
                if isinstance(data,list):main_counts.append(len(data))
        maxrows=primary_jsonl_count if primary_jsonl_count is not None else max(main_counts,default=0)
        floors={'simplified':(24,600),'medium':(48,1000)}
        row_floor,prose_floor=floors.get(c.get('difficulty'),(80,2000))
        require(maxrows>=row_floor,f'industrial record count {cid}')
        require(cjk>=prose_floor,f'substantive text floor {cid}: {cjk}')
        stats.append({'id':cid,'attachments':len(c['attachments']),'primary_table_records':maxrows,'attachment_chars':chars,'attachment_bytes':byte_count,'prose_cjk':cjk,'contrast_variants':len(variants),'mode':c['mode']})
        for p in d.rglob('*'):
            if p.suffix=='.json':read(p);parsed+=1
            elif p.suffix=='.jsonl':
                for line in p.read_text(encoding='utf-8-sig').splitlines():
                    if line.strip():json.loads(line)
                parsed+=1
    numeric_checks=[recompute_ac3(),recompute_eg3(),validate_bc_costs()]
    manifests=[]
    for c in cases:
        cid=c['id'];d=ROOT/'cases'/cid;dest=ROOT/'uploads'/cid;mapping=[]
        for rel in c['attachments']:
            source=d/rel;name=source.name if source.suffix in ['.txt','.md'] else source.name+'.txt'
            target=dest/'attachments'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
            require(sha(source)==sha(target),f'upload content hash {cid}/{name}')
            mapping.append({'original':rel,'upload_file':'attachments/'+name,'encoding':'UTF-8','format_inside':source.suffix.lstrip('.'),'sha256':sha(source),'bytes':source.stat().st_size})
        task=(d/c['agent_prompt']).read_text(encoding='utf-8-sig')
        for m in sorted(mapping,key=lambda x:len(Path(x['original']).name),reverse=True):
            old=Path(m['original']).name;new=Path(m['upload_file']).name
            if old!=new:task=task.replace(old,new)
        task+='\n\n附件文件清单：\n'+'\n'.join(f'- {Path(m["upload_file"]).name}' for m in mapping)+'\n'
        allowed_names={Path(m['upload_file']).name for m in mapping}
        upload_dir=(dest/'attachments').resolve()
        for old_file in upload_dir.iterdir():
            if old_file.is_file() and old_file.name not in allowed_names:
                require(old_file.resolve().parent==upload_dir,'stale upload path boundary')
                old_file.unlink()
        put(dest/'task.txt',task)
        man={'case_id':cid,'prompt':'task.txt','files_to_upload':[m['upload_file'] for m in mapping],'mapping':mapping,'mode':c['mode'],'note':'每次仅投喂当前目录task及attachments；manifest为操作者清单，不是AgentOS导入配置。'}
        put(dest/'upload_manifest.json',man);manifests.append(man)
    report={'validation_kind':'静态结构、附件哈希、真值复算和对照约束校验；非22例AgentOS执行结果','cases':len(cases),'attachment_files':sum(x['attachments'] for x in stats),'contrast_variants':total_contrasts,'json_and_jsonl_parsed':parsed,'all_case_paths_exist':True,'all_methods_allowed':True,'upload_copy_sha256_equal':True,'numeric_checks':numeric_checks,'case_statistics':stats}
    put(ROOT/'validation_report.json',report)
    mode_counts={m:sum(c['mode']==m for c in cases) for m in ['attachment_only','sandbox_live']}
    put(ROOT/'suite_manifest.json',{'format':'rubric_stress_suite_authoring_v1','not_native_agentos_import':True,'rubric_reference_version':'4.2.0','created_date':'2026-11-05','rubric_ids':ORDER,'case_count':22,'mode_counts':mode_counts,'cases':[{'id':c['id'],'metadata':f'cases/{c["id"]}/case.json','prompt':f'uploads/{c["id"]}/task.txt','mode':c['mode']} for c in cases]})
    put(ROOT/'rubric_audit_template.json',{'purpose':'填真实运行与评估输出，null表示尚未执行，不是失败','runs':[{'rubric':c['id'],'run_id':None,'variant_id':None,'applicability':None,'reference_verdict':None,'framework_status':None,'framework_verdict':None,'evidence_complete':None,'discrepancy_type':None,'independent_check_ids':[q['id'] for q in c['oracle_checks']],'notes':None} for c in cases]})
    create_docs(cases,stats,report,mode_counts)
    srcdir=ROOT/'rebuild_sources';srcdir.mkdir(exist_ok=True)
    for name in _asset_literal('benchmarks/definitions/package_rubric_stress.json', 'literal_2'):
        shutil.copyfile(WORK/'tools'/name,srcdir/name)
    # Archives are built from allowlisted trees; evaluator files never enter the upload archive.
    public_zip=ROOT.parent/'AgentOS_22项任务与附件_20261105.zip'
    with zipfile.ZipFile(public_zip,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted((ROOT/'uploads').rglob('*')):
            if p.is_file():z.write(p,p.relative_to(ROOT/'uploads'))
    with zipfile.ZipFile(public_zip) as z:
        require(all('evaluator' not in n and not n.endswith('case.json') for n in z.namelist()),'public isolation')
        require(sum(n.endswith('/task.txt') for n in z.namelist())==22,'public prompts')
        require(z.testzip() is None,'public ZIP CRC')
        for name in z.namelist():
            if name.endswith('.txt'):
                content=z.read(name).decode('utf-8-sig')
                require(not any(key in content for key in ['expected_verdict','trap_summary','oracle_checks']),f'evaluator leakage {name}')
    ac1_difficulty=next(c for c in cases if c['id']=='AC-1').get('difficulty')
    ac1_label='中等难度版' if ac1_difficulty=='medium' else '简化版'
    ac1_zip=ROOT.parent/f'AC-1_{ac1_label}任务与附件.zip'
    with zipfile.ZipFile(ac1_zip,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted((ROOT/'uploads'/'AC-1').rglob('*')):
            if p.is_file():z.write(p,p.relative_to(ROOT/'uploads'/'AC-1'))
    full_zip=ROOT.parent/'rubric_stress_suite_20261105_完整设计包.zip'
    hashes={str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for p in sorted(ROOT.rglob('*')) if p.is_file() and p.name!='checksums.json' and '__pycache__' not in p.parts}
    put(ROOT/'checksums.json',{'algorithm':'sha256','files':hashes})
    with zipfile.ZipFile(full_zip,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ROOT.rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts:z.write(p,Path(ROOT.name)/p.relative_to(ROOT))
    with zipfile.ZipFile(full_zip) as z:require(z.testzip() is None,'full ZIP CRC')
    print(json.dumps({'cases':22,'attachments':report['attachment_files'],'contrasts':total_contrasts,'mode_counts':mode_counts,'validation':'passed','public_zip':str(public_zip),'full_zip':str(full_zip)},ensure_ascii=True))

def create_docs(cases,stats,report,mode_counts):
    sby={x['id']:x for x in stats}
    intro=_fragment('benchmarks/templates/package_rubric_stress.json', 'fragment_2').format(v0=(report['attachment_files']), v1=(report['contrast_variants']))
    ac1=next(c for c in cases if c['id']=='AC-1')
    ac1_label='中等难度版' if ac1.get('difficulty')=='medium' else '简化版'
    intro=intro.replace('每例有96至360条主业务记录，3至9份附件',_fragment('benchmarks/templates/package_rubric_stress.json', 'fragment_3').format(v0=(ac1_label), v1=(sby['AC-1']['primary_table_records']), v2=(sby['AC-1']['attachments'])))
    for c in cases:
        s=sby[c['id']];mode='附件会话' if c['mode']=='attachment_only' else '接入沙箱后执行'
        intro+=_fragment('benchmarks/templates/package_rubric_stress.json', 'fragment_4').format(v0=(c['id']), v1=(c['title']), v2=(c['id']), v3=(c['trap_summary']), v4=(s['primary_table_records']), v5=(s['attachments']), v6=(mode))
    intro+=_asset_literal('benchmarks/definitions/package_rubric_stress.json', 'literal_3')
    for c in cases:
        cid=c['id'];s=sby[cid];prompt=(ROOT/'uploads'/cid/'task.txt').read_text(encoding='utf-8')
        intro+=_fragment('benchmarks/templates/package_rubric_stress.json', 'fragment_5').format(v0=(cid), v1=(c['title']), v2=(cid), v3=(cid), v4=(cid), v5=(cid), v6=(cid))
        intro+='```text\n'+prompt.rstrip()+'\n```\n\n'
        intro+=f'**评测者关注：** {c["trap_summary"]}\n\n**边界：** {c["rubric_boundary"]}\n\n'
    put(ROOT/'任务总览.md',intro)
    put(ROOT/'README.md',_fragment('benchmarks/templates/package_rubric_stress.json', 'fragment_6').format(v0=(report['attachment_files']), v1=(report['contrast_variants'])))

if __name__=='__main__':main()
