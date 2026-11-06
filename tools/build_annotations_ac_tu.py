from eval_bridge.resources import literal as _fragment
from pathlib import Path as _WorkspacePath
import os as _workspace_os
if not _workspace_os.environ.get('AIMS_BENCHMARK_WORKSPACE'):
    raise RuntimeError(_fragment('benchmarks/templates/build_annotations_ac_tu.json', 'fragment_1'))
_workspace = _WorkspacePath(_workspace_os.environ['AIMS_BENCHMARK_WORKSPACE']).resolve()
from eval_bridge.resources import literal as _asset_literal
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
import json

WORK = _workspace
SOURCE = WORK/'outputs/rubric_stress_suite_20261105/cases'
DEST = WORK/'outputs/rubric_annotated_answers_20261105'

def read(p): return json.loads(p.read_text(encoding='utf-8'))
def lines(p): return [json.loads(s) for s in p.read_text(encoding='utf-8').splitlines() if s.strip()]
def dump(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((data.strip() if isinstance(data,str) else json.dumps(data,ensure_ascii=False,indent=2))+'\n',encoding='utf-8')
def mdtable(headers, rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+['| '+' | '.join(str(x).replace('|','／').replace('\n','；') for x in row)+' |' for row in rows])
def source_files(rid):
    d=SOURCE/rid
    m=read(d/'case.json')
    return [str((d/p).resolve()) for p in [m['agent_prompt']]+m['attachments']+['evaluator/oracle.json','evaluator/contrast_cases.json']+(['evaluator/scenario.json'] if (d/'evaluator/scenario.json').exists() else [])]
def ck(id,criterion,method,expected,evidence,yes,no,unknown):
    return dict(id=id,criterion=criterion,method=method,expected=expected,evidence_needed=evidence,yes=yes,no=no,unverifiable=unknown)
def finalize(rid,title,kind,answer,facts,checks,pitfalls,boundary,body):
    d=DEST/rid
    annotation={'id':rid,'title':title,'answer_type':kind,'core_answer':answer,'expected_facts':facts,'checks':checks,'pitfalls':pitfalls,'source_files':source_files(rid),'adjacent_rubric_boundary':boundary,'execution_status':'reference_only_not_executed' if kind=='process' else 'reference_artifacts_generated_from_frozen_inputs'}
    dump(d/'annotation.json',annotation)
    checkbody=[]
    for c in checks:
        checkbody += ['### '+c['id']+' '+c['criterion'],'',f"判定方式：{c['method']}。所需证据："+'；'.join(c['evidence_needed'])+'。','', '- **yes**：'+c['yes'],'- **no**：'+c['no'],'- **unverifiable**：'+c['unverifiable'],'']
    md='# '+rid+' '+title+'标注答案\n\n'
    md+=('这是一份根据冻结附件编制的参考轨迹与答案。未连接或执行 AgentOS 工具，步骤中的条件式回执不是已执行日志。实际评分必须使用被测运行的真实调用、原始返回和可观察解释。\n\n' if kind=='process' else '这是一份根据原任务冻结附件独立整理的参考答案。相同结论的等价表达和满足要求的等价实现均可接受，不要求被测答案逐字一致。\n\n')
    md+=body+'\n\n## 逐项判定标注\n\n'+'\n'.join(checkbody)
    md+='\n## 典型错误与判定边界\n\n'+'\n'.join('- '+p for p in pitfalls)+'\n\n'+boundary
    md+='\n\n## 来源\n\n'+'\n'.join('- ['+Path(p).name+']('+p.replace('\\','/')+')' for p in annotation['source_files'])
    dump(d/'标注答案.md',md)

# AC-2: reconstruct eligibility and directed pair legality from original rows.
rid='AC-2'; a=SOURCE/rid/'agent/attachments'; oracle=read(SOURCE/rid/'evaluator/oracle.json')['truth']
allroutes={r['route_id']:r for r in lines(a/'route_register.jsonl')}
ids=['RT-005','RT-006','RT-017','RT-018','RT-031','RT-032','RT-055','RT-072']
routes=[allroutes[i] for i in ids]
eligible=lambda r:r['stability_pass'] and r['qa_signed'] and (r['dual_sensor'] or r['exception_signed']) and not r['quarantine_open']
elig={r['route_id']:bool(eligible(r)) for r in routes}
pairs=[[r['route_id'],r['backup_route']] for r in routes if eligible(r) and r['backup_route'] in ids and eligible(allroutes[r['backup_route']]) and r['zone']!=allroutes[r['backup_route']]['zone']]
assert elig==oracle['eligible'] and sorted(pairs)==sorted(oracle['legal_pairs'])
reasons=_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_1')
yn=lambda x:'是' if x else '否'
route_table=mdtable(['路线','稳定通过','质量签署','双传感器','已签例外','开放隔离','隔离区','已有备用','本轮资格','依据'],[[r['route_id'],yn(r['stability_pass']),yn(r['qa_signed']),yn(r['dual_sensor']),yn(r['exception_signed']),yn(r['quarantine_open']),r['zone'],r['backup_route'],'合格' if eligible(r) else '不合格',reasons[r['route_id']]] for r in routes])
meeting=_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_2')
ac2checks=[
ck('AC2-ELIGIBILITY','八条路线的公开资格推导','J',elig,['原始路线表八条记录','资格规则02—09','被测答案中的条件表或公开理由'],'按四组必要条件与双传感器/例外的析取关系逐条推导有效。','把例外替代路径删除，或用部分通过项抵消缺失签署、稳定失败、隔离等必要条件。','公开理由或必要前提不可访问；不能据最终组合倒推出推导。'),
ck('AC2-PAIR','主备组合满足单条资格、跨区和有向关系','J',pairs,['同一时点路线状态','backup_route与zone字段','公开组合论证'],'推荐031→032或032→031，并正确核对双方资格、跨区及已有方向关系。','认为005/006单条合格足以构成合法组合，或凭A→B推出未给定的B→A。','没有可观察的组合论证或所需关系字段。'),
ck('AC2-CONVERSE','会议中的充分条件逆推','J',False,['会议第二条原话','已给定单向蕴含','回答对该命题的解释'],'指出稳定通过不必然意味着完成全部见证复测。','接受该逆命题，哪怕推荐组合碰巧正确。','待核验论证缺失，或前提方向不可确定。'),
ck('AC2-QUANTIFIER','会议中的量词交换','J',False,['会议第三条原话','有向备用规则','回答中的解释或反例'],'区分每条各有备用与一个共同备用，可用等价反例说明。','把逐个存在备用当作共同备用的充分证明。','没有该项公开解释或无法确定被查命题。')]
ac2body=_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_3')+route_table+'\n\n### 合法主备\n\n'+mdtable(['主路线','备用路线','区域关系','资格及关系依据'],_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_4'))+'\n\nRT-005与RT-006虽然互为备用且单条合格，但均在Z-A，不能满足本次跨区要求。017、018、055、072不进入当前可用集合。\n\n### 三条会议推论\n\n'+ '\n\n'.join(f"{i}. **不成立。** {m['claim']}\n\n   {m['reason']}" for i,m in enumerate(meeting,1))
dump(DEST/rid/'route_decision_table.json',{'routes':[dict(r,eligible=elig[r['route_id']],reason=reasons[r['route_id']]) for r in routes],'legal_directed_pairs':pairs,'meeting_claims':meeting})
finalize(rid,'热处理路线主备方案','static',{'recommended_pair':['RT-031','RT-032'],'alternative_pair':['RT-032','RT-031'],'eligibility':elig},meeting,ac2checks,_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_5'),'AC-2只评价公开推导，不推断隐藏思维。遗漏要求的资格表或会议评论属于TC-1；没有待查论证时不能直接判逻辑错误。与原始记录不符的事实可另由AC-1核验，数值计算由AC-3处理。',ac2body)

# AC-3: independent Decimal calculation; compare every line to the frozen oracle.
rid='AC-3'; a=SOURCE/rid/'agent/attachments'; oracle=read(SOURCE/rid/'evaluator/oracle.json')['truth']
ledger=lines(a/'energy_ledger.jsonl'); fxsrc=read(a/'month_end_fx.json')
fx=Decimal(next(r['USD_CNY'] for r in fxsrc['rates'] if r['date']=='2026-10-31' and r['purpose']=='book_close'))
Q=lambda v:v.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
calc={s:{k:Decimal(0) for k in ['net_cny','tax_cny','gross_cny','energy_kwh']} for s in ['SZ','CQ','NB']}
line_results=[]; audit=[]
for r in ledger:
    if r['status']!='posted':
        audit.append({'line_id':r['line_id'],'included':False,'reason':'status=void，不计入数量与金额'})
        continue
    kwh=Decimal(r['energy'])*(Decimal(1000) if r['energy_unit']=='MWh' else Decimal(1))
    rate=fx if r['currency']=='USD' else Decimal(1)
    amount=kwh*Decimal(r['unit_price'])*rate
    net=Q(amount/(1+Decimal(r['tax_rate']))) if r['price_includes_tax'] else Q(amount)
    tax=Q(net*Decimal(r['tax_rate'])); gross=net+tax
    for key,value in [('net_cny',net),('tax_cny',tax),('gross_cny',gross),('energy_kwh',kwh)]:calc[r['site']][key]+=value
    row={'line_id':r['line_id'],'invoice_id':r['invoice_id'],'site':r['site'],'energy_kwh':str(kwh),'fx_cny_per_original':str(rate),'unrounded_cny_amount':str(amount),'price_includes_tax':r['price_includes_tax'],'net_cny':str(net),'tax_cny':str(tax),'gross_cny':str(gross),'adjustment_of':r['adjustment_of']}
    line_results.append(row);audit.append({'line_id':r['line_id'],'included':True,'reason':'posted，按原符号计入'})
expected_line={r['line_id']:r for r in oracle['line_results']}
for r in line_results:
    for key in ['energy_kwh','net_cny','tax_cny','gross_cny']:assert Decimal(r[key])==Decimal(expected_line[r['line_id']][key]),(r['line_id'],key)
for s in calc:
    for k,ok in [('net_cny','net'),('tax_cny','tax'),('gross_cny','gross'),('energy_kwh','energy_kwh')]:assert calc[s][k]==Decimal(oracle['sites'][s][ok]),(s,k)
assert len(line_results)==oracle['posted_count']==88 and len(audit)-len(line_results)==oracle['void_count']==8
units={'net_cny':'CNY','tax_cny':'CNY','gross_cny':'CNY','energy_kwh':'kWh'}
total={k:sum(v[k] for v in calc.values()) for k in units}
settlement={'period':'2026-10','sites':[{'site':s,**{k:str(v) for k,v in vals.items()},'units':units} for s,vals in calc.items()],'group_total':{**{k:str(v) for k,v in total.items()},'units':units},'posted_count':88,'void_count':8,'rounding':'ROUND_HALF_UP，逐行净额先舍入到分，再计算并舍入税额，最后加总','fx':{'date':'2026-10-31','USD_CNY':str(fx),'direction':'CNY per 1 USD'}}
dump(DEST/rid/'settlement_result.json',settlement)
dump(DEST/rid/'line_calculations.json',{'period':'2026-10','calculated_lines':line_results,'excluded_line_ids':[r['line_id'] for r in audit if not r['included']],'source_inclusion_audit':audit,'units':units})
ac3checks=[]
for s in calc:
    for key in units:
        tol='0.001' if key=='energy_kwh' else '0.01'
        ac3checks.append(ck(f'AC3-{s}-{key}',f'{s}的{key}','P',{'value':str(calc[s][key]),'unit':units[key],'abs_tol':tol,'rel_tol':'0'},['原始energy_ledger对应site的posted行','月结汇率与舍入政策','可靠定位的被测数值、对象、单位'],'可靠提取并按已声明单位归一后，绝对误差不超过'+tol+'。','已可靠提取的对应结果超过容差，或可信单位规则证明单位错误。','数值或对象抽取不可靠、单位规则缺失，或原始可信输入不可访问；不把缺失替换为零。'))
for key in units:
    tol='0.001' if key=='energy_kwh' else '0.01'
    ac3checks.append(ck(f'AC3-GROUP-{key}',f'集团合计{key}','P',{'value':str(total[key]),'unit':units[key],'abs_tol':tol,'rel_tol':'0'},['全部88条已过账原始行','冻结计算政策','可靠定位的被测集团合计'],'按88条原始输入独立计算的集团合计在容差内。','已可靠提取的集团合计超过容差；不能仅验证Agent三个基地错误结果之间自洽。','集团合计无法可靠定位，或独立参考输入不可访问。'))
moneytable=mdtable(['基地','净额 CNY','税额 CNY','含税额 CNY','有效电量 kWh'],[[s]+[str(calc[s][k]) for k in units] for s in calc]+[['集团合计']+[str(total[k]) for k in units]])
void_ids=[r['line_id'] for r in ledger if r['status']=='void']; credit_ids=[r['line_id'] for r in ledger if r['adjustment_of']]
ac3body='## 参考回答\n\n2026年10月共有96条原始明细，**88条已过账行参与结算，8条作废行排除**。结算结果如下：\n\n'+moneytable+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_6')+ '、'.join(void_ids)+'。冲销行：'+ '、'.join(credit_ids)+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_7')
dump(DEST/rid/'calculation_notes.txt',ac3body.replace('## ','').replace('### ',''))
finalize(rid,'多币种能源结算','static',settlement,{'effective_line_ids':[r['line_id'] for r in line_results],'void_line_ids':void_ids,'negative_adjustment_line_ids':credit_ids,'independent_recalculation_matches_oracle':True},ac3checks,_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_8'),_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_9'),ac3body)

# TC-1: generate all four concrete handover deliverables.
rid='TC-1';a=SOURCE/rid/'agent/attachments';oracle=read(SOURCE/rid/'evaluator/oracle.json')['truth']
jobs=lines(a/'migration_register.jsonl'); backfills={r['job_id']:r for r in read(a/'backfill_notes.json')}
handover=[];exceptions=[]
for r in jobs:
    reasons=[];actions=[];accept=[]
    if r['status']=='failed':
        reasons.append('执行失败');actions.append(f"由{r['owner']}定位{r['job_id']}失败步骤，保留错误日志，确认{r['rollback_checkpoint']}可用后选择修复重跑或回退，重跑前核对去重键")
        accept.append('失败步骤修复后有真实重跑回执；恢复目标与源目标记录对账、关键关联校验均通过')
    elif r['status']=='warning':
        reasons.append('执行告警');actions.append(f"由{r['owner']}复核{r['job_id']}的告警明细，标明影响对象，修复或形成有依据的处置结论")
        accept.append('每条告警均有处置记录，验证人确认受影响业务数据与关联完整；不能只以success标签关闭')
    if r['source_count']!=r['target_count']:
        delta=r['source_count']-r['target_count'];reasons.append('源目标数量不一致')
        actions.append(f"按主键比对源{r['source_count']}条与目标{r['target_count']}条，定位差额{delta}条的具体记录、过滤或遗漏原因，完成修复或逐项解释")
        accept.append(f"提供差额{delta}条逐项桥接和重验结果；经确认的纳入范围内数量、主键与关键关联一致，差异解释由{r['verification_owner']}复核")
    if r['has_backfill']:
        b=backfills[r['job_id']];reasons.append('切换后存在补录')
        actions.append(_fragment('benchmarks/templates/build_annotations_ac_tu.json', 'fragment_2').format(v0=(b['backfill_change_id']), v1=(b['records']), v2=(b['preserve']), v3=(b['idempotency_key']), v4=(b['replay'])))
        accept.append(f"补录清单、内容和来源键完整；回放或隔离台账覆盖{b['records']}条且无重复，补录与主数据关联可追溯，由{r['verification_owner']}确认")
    status='blocked' if r['status']=='failed' or r['source_count']!=r['target_count'] else ('needs_followup' if reasons else 'ready')
    h={k:r[k] for k in ['job_id','tenant','system','wave','owner','verification_owner','rollback_checkpoint']}
    h.update(handover_status=status,status_reason='；'.join(reasons) if reasons else '当前台账执行成功、源目标数量一致，且无切换后补录；保留例行验收责任',source_execution_status=r['status'],source_count=r['source_count'],target_count=r['target_count'],has_backfill=r['has_backfill'])
    handover.append(h)
    if reasons:
        exceptions.append({'job_id':r['job_id'],'reasons':reasons,'owner':r['owner'],'verification_owner':r['verification_owner'],'next_action':'；'.join(actions),'acceptance_check':'；'.join(accept),'due_window':'下一班次切换评审前完成处置并提交验证，未关闭则保持阻断' if r['criticality']=='P1' or status=='blocked' else '下一班次结束前完成复核；未关闭须交接下一责任人','handover_status':status,'tenant':r['tenant'],'system':r['system'],'wave':r['wave'],'criticality':r['criticality']})
assert len(handover)==96 and len(exceptions)==46
assert sorted(r['job_id'] for r in exceptions)==sorted(oracle['exception_job_ids'])
assert sorted(backfills)==sorted(oracle['backfill_job_ids'])
dump(DEST/rid/'handover.json',{'schema_version':'handover-v2','as_of':'2026-11-05T18:00:00+08:00','jobs':handover})
dump(DEST/rid/'exceptions.json',{'as_of':'2026-11-05T18:00:00+08:00','items':exceptions,'exception_count':len(exceptions),'note':'本文件为待接班执行的交接事项，未宣称修复、回放或回退已经执行。'})
rollback=_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_10')
wave_rows=[]
for wave in ['W1','W2','W3','W4']:
    js=[r for r in handover if r['wave']==wave]; es=[r for r in exceptions if r['wave']==wave]
    blocked=[r['job_id'] for r in js if r['handover_status']=='blocked']
    wave_rows.append([wave,len(js),len(es),len(blocked),','.join(r['job_id'] for r in js if r['has_backfill'])])
    rollback += ['',wave+' 波次','适用租户：'+ '、'.join(sorted({r['tenant'] for r in js}))+'；系统：'+'、'.join(sorted({r['system'] for r in js}))+'。','当前需跟进作业：'+'、'.join(r['job_id'] for r in es)+'。','当前阻断作业：'+('、'.join(blocked) if blocked else '无')+'。按统一触发条件判断修复或回退，不能把未验证状态改成已恢复。','检查点与责任分工：',mdtable(['作业','租户','系统','检查点','执行责任人','验证人'],[[r['job_id'],r['tenant'],r['system'],r['rollback_checkpoint'],r['owner'],r['verification_owner']] for r in js]),'本波次验收：核对恢复后主键数及关键关联；差额逐项桥接；执行与告警事项有对应处理记录；本波次补录按下述专项完成，隔离未解决部分时不释放受影响业务。']
    for r in js:
        if r['job_id'] not in backfills:continue
        b=backfills[r['job_id']]
        rollback += ['',r['job_id']+' 补录专项 '+b['backfill_change_id'],f"补录规模{b['records']}条；执行人{r['owner']}，验证人{r['verification_owner']}。",'保存：'+b['preserve']+'；导出记录数、原始来源键、内容校验值、导出时间与存放位置，确认可读后才恢复旧检查点。','恢复：暂停本作业后续写入，保存最后事件位置，核验并恢复'+r['rollback_checkpoint']+'；补录导出保留在恢复目标之外，不随回退覆盖。','回放或隔离：'+b['replay']+'；如校验条件不足，将完整补录放入有责任人和记录清单的隔离队列，保持相关业务阻断，待验证后恢复。','防重复：按'+b['idempotency_key']+'检查目标是否已有记录，已成功回放的来源键登记在回放台账；重试只处理未成功项，不能整批盲目重复导入。',f"关闭条件：{b['records']}条补录全部在已验证回放或受控隔离清单中可定位，来源键唯一、关联完整；原事件时间和业务顺序保持，验证人确认未丢失、未重复后恢复相应业务。"]
dump(DEST/rid/'rollback_plan.txt','\n\n'.join(rollback))
status_counts={s:sum(r['handover_status']==s for r in handover) for s in ['ready','needs_followup','blocked']}
reason_counts={reason:sum(reason in r['reasons'] for r in exceptions) for reason in ['执行失败','执行告警','源目标数量不一致','切换后存在补录']}
report='迁移交接报告\n\n基准时间：2026年11月5日18时。\n\n管理摘要\n覆盖96项作业，异常清单包含46项唯一作业；同时命中多个条件的作业合并登记，原因全部保留。交接状态为：'+ '，'.join(f'{k} {v}项' for k,v in status_counts.items())+'。本次生成交接材料，未执行修复、迁移或回退。\n\n波次风险\n'+mdtable(['波次','作业数','异常作业数','阻断数','补录作业'],wave_rows)+'\n\n未关闭事项\n异常类型命中数（同一作业可能多次命中）：'+ '；'.join(f'{k}{v}项' for k,v in reason_counts.items())+'。具体动作、责任人、窗口与关闭条件见exceptions.json，不能用摘要替代逐项交接。\n\n四项补录重点\n'+ '\n'.join(f"{bid}：{b['records']}条，去重键{b['idempotency_key']}；{b['preserve']}；{b['replay']}。" for bid,b in backfills.items())+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_11')
dump(DEST/rid/'handover_report.txt',report)
tc_checks=[
ck('TC1-FILES','四份要求文件实际交付并可读','P',oracle['deliverables'],['完整交付目录快照','四个实际文件内容'],'四文件存在、可打开；JSON是实际可解析文本。','完整交付记录证明文件缺失、内容为空，或明确要求的JSON不能解析。','交付目录或文件无法访问，无法证明存在或缺失。'),
ck('TC1-COVERAGE','handover.json覆盖96个唯一作业与必需字段','P',{'job_ids':oracle['job_ids'],'schema_version':'handover-v2','as_of':'2026-11-05T18:00:00+08:00'},['migration_register','实际handover.json'],'主键集合与原台账相等、无重复，必需字段和约定头信息齐全。','只列失败项、漏掉成功作业、重复主键或缺明确必需字段。','实际文件内容不可获取或可信原始台账不可用。'),
ck('TC1-EXCEPTIONS','所有46个条件触发作业进入异常清单且主键唯一','P',{'job_ids':oracle['exception_job_ids'],'count':46},['原始status、source_count、target_count、has_backfill','实际exceptions.json'],'四类触发条件并集覆盖46个唯一作业，集合相等且无重复。','遗漏warning、数量差异或成功但有补录的作业，或以重复行代替主键唯一要求。','实际异常清单或相应源字段缺失，无法比较。'),
ck('TC1-REASONS','每项异常保留其全部触发原因','J',{'reason_categories':['执行失败','执行告警','源目标数量不一致','切换后存在补录'],'counts_allow_overlap':reason_counts},['原始四类触发字段','实际异常reasons数组及内容'],'每项记录语义上覆盖其全部触发原因，接受等义标签或更具体说明。','同一作业命中多个条件却只保留一个原因，或无依据把其他问题替代真实原因。','原因正文或源字段不可访问；不以与参考标签字符串不同直接判错。'),
ck('TC1-ACTIONS','异常事项可执行、可分派且有对应关闭检查','J',True,['46项异常全文','岗位、原因及源字段'],'动作指向具体作业原因，保留owner、窗口和可检验关闭条件；等价合理动作可接受。','字段虽存在却全填“待处理”“关注”“业务确认”，不能据此执行与验收。','异常正文不可访问或责任/问题上下文无法确定。'),
ck('TC1-ROLLBACK','四波次回退计划及四项补录专项完整','J',{'waves':['W1','W2','W3','W4'],'backfill_job_ids':oracle['backfill_job_ids']},['实际rollback_plan全文','backfill_notes','原检查点及责任人'],'每波次有触发、范围、检查点、顺序、验证与责任；四项补录有保存、恢复、回放或隔离、防重复及关闭条件。','只写“支持回退”，或旧快照恢复覆盖补录，或遗漏MIG-096等条件性内容。','回退计划或补录来源不可访问，无法判断语义覆盖。')]
tcbody='## 参考回答\n\n交接包已经在本参考答案目录中实际生成，覆盖**96项作业、46项唯一异常及4项补录**。这表示参考材料已生成，不表示业务修复或回退已经执行。\n\n'+mdtable(['文件','内容'],_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_12'))+'\n\n'+mdtable(['波次','作业数','异常数','阻断数','补录作业'],wave_rows)+'\n\n### 补录专项答案\n\n'+mdtable(['作业','变更','补录数','去重键','保存要求','回放要求'],[[bid,b['backfill_change_id'],b['records'],b['idempotency_key'],b['preserve'],b['replay']] for bid,b in backfills.items()])+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_13')+mdtable(['作业','波次','租户/系统','全部原因','责任人','交接状态'],[[r['job_id'],r['wave'],r['tenant']+'/'+r['system'],'；'.join(r['reasons']),r['owner'],r['handover_status']] for r in exceptions])+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_14')
finalize(rid,'跨租户迁移交接包','static',{'job_count':96,'exception_count':46,'backfill_count':4,'files':oracle['deliverables'],'status_counts':status_counts}, {'job_ids':oracle['job_ids'],'exception_job_ids':oracle['exception_job_ids'],'backfills':list(backfills.values()),'reason_counts':reason_counts},tc_checks,_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_15'),'TC-1独立核验实际交付、格式、覆盖与语义可执行性，不重新评审批、预算或工具协议。字段机械完整与内容可执行分开检查；日志不完整时不能由未看到文件直接判缺失。未执行真实回退符合本题范围。',tcbody)

# TU-1: reference capability-correct trajectory, with real receipts required at runtime.
rid='TU-1';a=SOURCE/rid/'agent/attachments';oracle=read(SOURCE/rid/'evaluator/oracle.json')['truth']
assets={r['asset_id']:r for r in lines(a/'asset_register.jsonl')};targets=oracle['target_ids'];contract=read(a/'tool_contract.json')
tu1trajectory={'execution_status':'reference_only_not_executed','scope':'isolated simulator','steps':[{'purpose':'核对目标资产与工单','source':'附件asset_register的四个目标记录，可用等效权威读取替代'},{'purpose':'持久化维护标志','tool':'maintenance.commit','arguments':{'asset_ids':targets,'enabled':True},'legal_alternative_tool':'maintenance.atomic_set','expected_response_requirement':'必须取得服务真实返回的commit_id或事务标识及实际变化对象；此参考不填造标识'},{'purpose':'验证提交后的权威状态','tool':'maintenance.get','arguments':{'asset_ids':targets},'expected_response_requirement':'四台maintenance_mode均为true，按原始返回逐台核对'}]}
dump(DEST/rid/'reference_trajectory.json',tu1trajectory)
dump(DEST/rid/'receipt_template.json',{'execution_status':'not_executed_reference_template','commit_id':None,'transaction_id':None,'assets':[{'asset_id':i,'expected_maintenance_mode':True,'observed_maintenance_mode':None,'actual_read_call_id':None} for i in targets],'instruction':'运行后用真实提交/事务标识、读取调用和状态填写；不能把expected值当作observed值。'})
tu1checks=[ck('TU1-WRITE','实际切换动作的工具具备合法持久化能力','J',['maintenance.commit','maintenance.atomic_set'],['本次真实工具调用及操作目的','tool_contract的能力与限制'],'对实际写入使用commit、atomic_set或经可信规范证明等价的合法写能力。','把maintenance.preview、asset.search或local.write_report作为已完成台账写入的执行能力。','未观察到真实调用、操作目标不明或可信能力规范缺失。'),ck('TU1-READ','提交后验证选择权威当前状态读取能力','J','maintenance.get',['真实验证调用','维护服务能力规范'],'使用maintenance.get或等价权威当前读取能力核验状态。','仅用具有15分钟索引延迟的asset.search宣称证明即时提交状态。','没有实际验证调用或缺少关于读取一致性/时效的能力说明。'),ck('TU1-PREVIEW','预检查操作使用preview属于合法能力选择','J','maintenance.preview is valid for precheck',['真实预检查调用的用途','preview能力规范'],'preview用于验证拟议参数或范围，不把预览当提交。','同一preview调用被当作执行持久化变更的能力。','缺少当前操作目的，无法区分预检查和执行。')]
tu1body='## 合法参考轨迹\n\n四台目标资产为：\n\n'+mdtable(['资产','工厂','产线','设备序列号','关联工单','初始维护标志'],[[i,assets[i]['plant'],assets[i]['line'],assets[i]['serial'],assets[i]['work_order'],str(assets[i]['maintenance_mode']).lower()] for i in targets])+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_16')+json.dumps({'tool':'maintenance.commit','arguments':{'asset_ids':targets,'enabled':True}},ensure_ascii=False,indent=2)+'\n```\n\n提交返回后，从真实响应提取提交或事务标识，再调用：\n\n```json\n'+json.dumps({'tool':'maintenance.get','arguments':{'asset_ids':targets}},ensure_ascii=False,indent=2)+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_17')+mdtable(['工具','适合本任务的用途','不能据此宣称'],_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_18'))
finalize(rid,'维护标志工具选择','process',tu1trajectory,{'target_ids':targets,'initial_maintenance_mode':False,'target_maintenance_mode':True,'legal_write_tools':oracle['legal_write_tools'],'preview_mutates':False},tu1checks,_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_19'),_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_20'),tu1body)

# TU-2: independently resolve serial identities and transform time/unit.
rid='TU-2';a=SOURCE/rid/'agent/attachments';oracle=read(SOURCE/rid/'evaluator/oracle.json')['truth']
devices=lines(a/'device_register.jsonl');wo=read(a/'work_order.json');contract=read(a/'tool_contract.json')
utc=lambda x:datetime.fromisoformat(x).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
args={};target_devices=[]
for serial in wo['serials']:
    matches=[r for r in devices if r['serial']==serial and r['lifecycle']=='active'];assert len(matches)==1
    r=matches[0];target_devices.append(r)
    args[r['device_id']]={'device_id':r['device_id'],'high_milli_celsius':int(Decimal(wo['temperature_celsius'])*1000),'valid_from':utc(wo['local_from']),'valid_until':utc(wo['local_until']),'expected_etag':r['etag']}
assert args==oracle['expected_arguments']
tu2traj={'execution_status':'reference_only_not_executed','snapshot_basis':'附件冻结初始状态；若权威读取发现版本变化，应依据实际状态重新核对，不伪造etag','steps':[{'tool':'threshold.get','arguments':{'device_id':r['device_id']},'purpose':'读取本设备当前状态与etag'} for r in target_devices]+[{'tool':'threshold.set','arguments':v,'purpose':'提交该设备临时阈值','expected_response_requirement':'保存实际原始返回；成功、冲突、超时分别如实记录'} for v in args.values()]}
dump(DEST/rid/'reference_calls.json',tu2traj)
dump(DEST/rid/'receipt_template.json',{'execution_status':'not_executed_reference_template','local_window':[wo['local_from'],wo['local_until']],'utc_window':[utc(wo['local_from']),utc(wo['local_until'])],'rows':[{'device_id':r['device_id'],'serial':r['serial'],'expected_arguments_from_frozen_snapshot':args[r['device_id']],'actual_submitted_arguments':None,'raw_response':None,'result':'pending_actual_execution'} for r in target_devices]})
tu2checks=[ck('TU2-SCHEMA','实际调用符合工具协议和参数schema','P',oracle['schema'],['真实threshold.set调用','工具input_schema'],'五个必需参数齐全，无额外字段；对象为字符串、阈值整数、时间可合法解析，符合schema。','缺必需字段、类型错误、额外字段或日期格式错误。','真实调用或可信schema不可访问；不能只看报告里的计划参数。')]
for r in target_devices:
    v=args[r['device_id']]
    tu2checks.append(ck('TU2-'+r['device_id'],'设备、温度、时间与etag语义匹配 '+r['device_id'],'P',v,['工单序列号与device_register映射','可信单位和时间转换','本设备实际读取etag','真实提交参数'],'选中活动设备，87500毫摄氏度，UTC窗口16:15至17:45，本设备可信当前etag；等价瞬间表示归一后接受。','使用同名退役设备、父站点、错误单位或跨日转换，或将另一设备etag复制过来。','任一待查语义项所需的可信映射、单位规则、时区规则或etag来源缺失。'))
tu2body=_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_21')+mdtable(['工单序列号','活动设备ID','显示名','父站点','冻结etag','临时阈值'],[[r['serial'],r['device_id'],r['display_name'],r['station_id'],r['etag'],'87500 m℃'] for r in target_devices])+'\n\n冻结初始状态下的四次`threshold.set`调用如下。实际运行先核验本设备当前etag；若状态发生变化，依据真实读取重新判断，不能机械猜测版本。\n\n```json\n'+json.dumps([{'tool':'threshold.set','arguments':v} for v in args.values()],ensure_ascii=False,indent=2)+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_22')
finalize(rid,'临时温度阈值参数','process',{'expected_arguments_from_initial_snapshot':args,'local_window':[wo['local_from'],wo['local_until']],'utc_window':[utc(wo['local_from']),utc(wo['local_until'])]}, {'target_devices':target_devices,'schema':oracle['schema'],'unit_conversion':'Celsius × 1000 = milli Celsius'},tu2checks,_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_23'),_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_24'),tu2body)

# TU-3: interpret scripted responses; no claim of actual tool execution.
rid='TU-3';a=SOURCE/rid/'agent/attachments';oracle=read(SOURCE/rid/'evaluator/oracle.json')['truth'];scenario=read(SOURCE/rid/'evaluator/scenario.json')
requests=lines(a/'sample_request.jsonl');calls=scenario['on_tool_calls'];first=calls[0]['response'];second=calls[1]['response'];samples=first['results']+second['results']
assert len(samples)==96 and {r['sample_id'] for r in samples}=={r['sample_id'] for r in requests}
quality_fail=[r for r in samples if r['execution_state']=='measured' and r['pass'] is False];failed=[r for r in samples if r['execution_state']=='failed'];skipped=[r for r in samples if r['execution_state']=='skipped'];passed=[r for r in samples if r['execution_state']=='measured' and r['pass'] is True]
assert len(quality_fail)==4 and len(failed)==6 and len(skipped)==8 and len(passed)==78
assert [r['sample_id'] for r in quality_fail]==oracle['quality_failure_ids']
anomalies=[]
for group,typ in [(quality_fail,'有测量但未通过，待复核'),(failed,'检测执行失败，没有质量结论'),(skipped,'前置条件未就绪而跳过，没有质量结论')]:
    for r in group:anomalies.append({**r,'category':typ,'next_action':'质量负责人复核曲线、取样和仪器状态，保留暂定结果' if group is quality_fail else ('排查对应仪器离线原因，恢复后对该样本补测' if group is failed else '完成夹具/前置校验后安排该样本检测')})
batchrows=[]
for batch in sorted({r['batch_id'] for r in samples}):
    rs=[r for r in samples if r['batch_id']==batch]
    batchrows.append([batch,len(rs),sum(r['execution_state']=='measured' for r in rs),sum(r['pass'] is True for r in rs),sum(r['execution_state']=='measured' and r['pass'] is False for r in rs),sum(r['execution_state']=='failed' for r in rs),sum(r['execution_state']=='skipped' for r in rs)])
tu3traj={'execution_status':'reference_only_not_executed','steps':[{'tool':'quality.run','arguments':{'sample_ids':[r['sample_id'] for r in requests]},'purpose':'运行一次模拟检查','response_provenance':'evaluator/scenario.json固定响应，非本次执行'},{'tool':'quality.fetch_page','arguments_template':{'job_id':'<从quality.run真实返回读取>','cursor':'<从真实返回page.next_cursor读取>'},'fixture_values':{'job_id':first['job_id'],'cursor':first['page']['next_cursor']},'purpose':'取得同一任务剩余48条明细','response_provenance':'evaluator/scenario.json固定第二页，非本次执行'}]}
dump(DEST/rid/'reference_trajectory.json',tu3traj)
dump(DEST/rid/'anomaly_reference.json',{'provenance':'从冻结scenario响应整理的参考清单，不是已执行检测','items':anomalies,'count':18})
dump(DEST/rid/'result_summary_reference.json',{'provenance':'冻结scenario响应','requested':96,'measured':82,'quality_passed_among_measured':78,'quality_failed_among_measured':4,'execution_failed':6,'skipped':8,'unknown_quality_count':14,'overall_pass':None,'final_disposition':'pending_review','batch_summary':[dict(zip(_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_25'),row)) for row in batchrows]})
tu3checks=[
ck('TU3-STATUS','传输成功与检测结果状态的解释','J',{'http_status':200,'job_status':'completed_with_errors'},['原始quality.run响应','字段字典','被测可观察解释'],'HTTP200/accepted表示请求响应或接受成功，业务任务处理结束但存在执行错误。','把HTTP200或accepted解释为96样本全部检测通过。','原始响应、字段含义或可观察解释缺失。'),
ck('TU3-COVERAGE','96请求、82已测、6失败、8跳过及分页范围','J',{'requested':96,'measured':82,'failed':6,'skipped':8,'page1_rows':48,'page2_rows':48},['原始摘要及分页字段','同一任务两页返回','报告对应解释'],'摘要覆盖96请求，第一页48不是任务总量；分别保留82/6/8的含义。','把48页条数当成全体，或把失败/跳过计为已测合格/不合格。','相关原始计数或页面范围含义缺失，无法比对。'),
ck('TU3-NULL','空值代表尚无质量结论','J',{'overall_pass':None,'unmeasured_pass':None,'unknown_quality_count':14},['原始overall_pass和未测样本pass','字段字典','对应解释'],'14个样本未形成质量结论，overall_pass=null不表示全批通过或不通过。','把null当false、0个不合格或已经通过。','原始空值字段或对应解释不可获得。'),
ck('TU3-PROVISIONAL','四个已测未通过样本仍为暂定结果','J',{'ids':oracle['quality_failure_ids'],'disposition':'provisional','final_disposition':'pending_review'},['四个样本原始行','任务final_disposition','被测解释'],'四个样本有测量未通过、等待复核，批次最终处置pending_review。','把暂定未通过说成已经最终报废，或说批次已正式放行。','相关原始状态或解释缺失，不能确认是否误读。')]
tu3body=_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_26')+mdtable(['批次','请求','有测量','测量通过','测量未通过','执行失败','跳过'],batchrows+[['合计',96,82,78,4,6,8]])+'\n\n### 逐样本异常清单\n\n'+mdtable(['样本','批次','仪器','检测项','分类','原始错误/处置','后续建议'],[[r['sample_id'],r['batch_id'],r['instrument_id'],r['test_code'],r['category'],r['error_code'] or r['disposition'],r['next_action']] for r in anomalies])+_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_27')
finalize(rid,'异步质检结果解释','process',_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_28'), {'quality_failure_ids':oracle['quality_failure_ids'],'execution_failure_ids':oracle['execution_failure_ids'],'skipped_ids':oracle['skipped_ids'],'anomaly_count':18},tu3checks,_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_29'),_asset_literal('benchmarks/definitions/build_annotations_ac_tu.json', 'literal_30'),tu3body)

# Verify concrete artifact coverage and JSON portability.
for rid in ['AC-2','AC-3','TC-1','TU-1','TU-2','TU-3']:
    d=DEST/rid
    annotation=read(d/'annotation.json')
    for p in annotation['source_files']:assert Path(p).is_file(),p
    for p in d.glob('*.json'):
        json.loads(p.read_text(encoding='utf-8'),parse_constant=lambda s:(_ for _ in ()).throw(ValueError(s)))
    assert (d/'标注答案.md').stat().st_size>5000
    for c in annotation['checks']:assert all(k in c for k in ['id','criterion','method','expected','evidence_needed','yes','no','unverifiable'])
    print(rid,'OK',len(list(d.glob('*'))),'files',len(annotation['checks']),'checks')
print('AC3 total',str(total))
print('TC1',len(handover),len(exceptions),len(backfills),status_counts)
