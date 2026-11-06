from eval_bridge.resources import literal as _fragment
from pathlib import Path as _WorkspacePath
import os as _workspace_os
if not _workspace_os.environ.get('AIMS_BENCHMARK_WORKSPACE'):
    raise RuntimeError(_fragment('benchmarks/templates/build_annotations_pe_eg.json', 'fragment_1'))
_workspace = _WorkspacePath(_workspace_os.environ['AIMS_BENCHMARK_WORKSPACE']).resolve()
from eval_bridge.resources import literal as _asset_literal
from pathlib import Path
import json
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from collections import Counter

BASE=_workspace
SRC=BASE/'outputs'/'rubric_stress_suite_20261105'/'cases'
OUT=BASE/'outputs'/'rubric_annotated_answers_20261105'
IDS=['PE-1','PE-2','PE-3','EG-1','EG-2','EG-3']

def read(cid,rel):
    return json.loads((SRC/cid/rel).read_text(encoding='utf8'))

def rows(cid,name):
    return [json.loads(x) for x in (SRC/cid/'agent'/'attachments'/name).read_text(encoding='utf8').splitlines()]

def save(cid,name,value):
    p=OUT/cid/name
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n' if isinstance(value,(dict,list)) else value.strip()+'\n',encoding='utf8')

def mdtable(headers,items):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+['| '+' | '.join(str(v).replace('|','\\|').replace('\n','<br>') for v in x)+' |' for x in items])

def check(id,criterion,method,expected,evidence_needed,yes,no,unknown):
    return dict(id=id,criterion=criterion,method=method,expected=expected,evidence_needed=evidence_needed,yes=yes,no=no,unverifiable=unknown)

def finish(cid,core,facts,checks,pitfalls,body,extras=None):
    meta=read(cid,'case.json')
    source_files=[str(p.resolve()) for p in sorted((SRC/cid).rglob('*')) if p.is_file() and (p.name in ['case.json','task.txt','oracle.json','scenario.json','equivalent_view.json'] or 'attachments' in p.parts)]
    ann=dict(id=cid,title=meta['title'],answer_type='static' if cid=='PE-1' else 'process',core_answer=core,expected_facts=facts,checks=checks,pitfalls=pitfalls,source_files=source_files)
    save(cid,'annotation.json',ann)
    for n,v in (extras or {}).items():save(cid,n,v)
    intro='本文是评测者参考答案。排程是可接受的实施方案之一；对未给定工时和交通时间的计划假设明确标出，不能把这些假设设成唯一评分真值。' if cid=='PE-1' else _asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_1')
    parts=[f'# {cid} {meta["title"]} 标注答案','',intro,'',body,'','## 逐项标注与判定边界','']
    for c in checks:
        parts += [f'### {c["id"]}',c['criterion'],f'判定方式：{c["method"]}。期望：{c["expected"]}',f'所需证据：{c["evidence_needed"]}',f'- **yes**：{c["yes"]}',f'- **no**：{c["no"]}',f'- **unverifiable**：{c["unverifiable"]}','']
    parts += ['## 相邻 Rubric 与常见误判',meta['rubric_boundary'],'']+['- '+x for x in pitfalls]+['','## 来源文件']+['- ['+Path(x).name+']('+x.replace('\\','/')+')' for x in source_files]
    save(cid,'标注答案.md','\n'.join(parts))

def pe1():
    cid='PE-1';data=rows(cid,'03_工单台账.jsonl')
    schedule=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_2')
    factory=[]
    for plant in ['苏州一厂','苏州二厂','无锡工厂']:
      subset=[x for x in data if x['plant']==plant]
      factory.append({'plant':plant,'work_orders':len(subset),'serial_orders':sum(x['lot_trace']=='serial' for x in subset),'batch_orders':sum(x['lot_trace']=='batch' for x in subset),'hold_orders':[x['work_order'] for x in subset if x['quality_hold']],'line_counts':dict(Counter(x['line'] for x in subset))})
    checks=[
      check('PE1.rollback','完整回退链是否能在06:00前完成？','J','最迟03:00进入90+60+30分钟顺序回退链。','完整计划、窗口边界、回退工时与分支安排。','如03:00–04:30恢复、04:30–05:30烟测、05:30–06:00交接，或合法更早等效安排。','采用03:30决策并排到06:30；遗漏标签/队列恢复使分支不能落实必要子目标。','计划截断或回退部分未被可靠观察，无法判可执行性。'),
      check('PE1.data_preparation','冻结后最后增量与追溯映射是否获得可执行安排？','J','冻结/排空完成后取最终基线；工单路线、批次映射、离线标签水位一并迁移。','计划中的子目标、依赖和完成标准。','前置准备放到窗口前，最后增量放在冻结后，清楚界定排空证据。','使用仍写入时的试跑快照作为最终基线，或遗漏离线已打印标签水位。','必要子目标或能力限制缺失，无法判断安排能否落实。'),
      check('PE1.resources','三厂打印验证及人员约束是否获得可执行安排？','J','一名P不同时现场服务两厂；实际标签验证在生产恢复前。','完整岗位安排、现场服务时段与经验证的转场假设。','本方案按三厂串行验证；合法远程/分厂替代也接受。','将三厂现场验证同时分配给同一P，或把全部标签验证延后上线。','转场可行性未有足够信息确认且它决定排程能否完成；不能自行断言路程工时。'),
      check('PE1.quality_erp_handover','冻结、ERP幂等和接班子目标是否合理安排？','J','覆盖WO-0017、WO-0051、WO-0098冻结；ERP按业务键防重；交接含可恢复与继续冻结对象。','验收矩阵、回退副作用处理、交班字段。','明确检查条件和责任岗位，安排失败分支。','仅说“全部验证通过/通知有关人员”而缺少实现这些必要子目标的安排。','完整计划或必要规则不可访问。')]
    core={'strategy':'三厂同步冻结、数据迁移并行、打印工程师按厂串行验证、03:00最迟回退门禁','rollback_latest':'2026-11-08T03:00:00+08:00','window_end':'2026-11-08T06:00:00+08:00','rollback_minutes':180,'schedule':schedule,'factory_acceptance_scope':factory,'assumptions':_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_3')}
    body=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_4')+mdtable(['时间','工作','资源','完成门槛'],schedule)+'''

### 三厂验收矩阵

'''+mdtable(['工厂','台账工单','序列号型/批次型','已知冻结工单','验收安排'],[[x['plant'],x['work_orders'],f"{x['serial_orders']} / {x['batch_orders']}",', '.join(x['hold_orders']) or '起始快照无冻结工单','数量/完成状态、路线与批次映射、序列号水位、实际标签、ERP回执、质量状态全部验收'] for x in factory])+_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_5')
    finish(cid,core,_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_6'),checks,['允许不同合法排程与子目标拆分，不按步骤名称或数量打分。','计划通过不能证明后来实际按顺序执行；执行日志属于PE-2。','窗口资源假设需验证，不能把参考假设当作已存在的现场事实。'],body,{'参考排程.json':core})

def pe2():
    cid='PE-2';data=rows(cid,'03_分区台账.jsonl')
    trace=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_7')
    # All explicit job IDs are fixture labels, and real adapters must use each actual returned ID.
    for e in trace:
      if e.get('call')=='workflow.submit' and 'response' not in e:
        op=e['arguments']['operation'];e['response']={'accepted':True,'job_id':_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_8')[op]}
    expected=[{'partition_id':x['partition_id'],'site':x['site'],'table':x['table'],'expected_snapshot':'close-202610-r4','expected_row_count':x['row_count'],'expected_source_watermark':x['source_watermark'],'expected_digest':x['required_digest'],'observed':None,'comparison_status':'待真实最终对账返回'} for x in data]
    deps=read(cid,'evaluator/oracle.json')['dependencies'];times={x['event']:x['t'] for x in trace}
    for d in deps:assert times[d['before']]<times[d['after']]
    checks=[check('PE2.edge.'+str(i+1),f"{d['before']}是否严格早于{d['after']}？",'P',f"参考轨迹为{times[d['before']]} < {times[d['after']]}；任何可信严格先后均可。",'稳定事件ID、业务完成终态、可信高分辨率事件时间及完整轨迹。','前置已实际完成且严格早于后续开始；接受等效合法完成证明。','后续开始在前置完成之前或确定同时；或完整轨迹证明必要前置/必要目标缺失。','只有分钟级显示或丢失终态/顺序/完整性，无法确定实际严格先后。') for i,d in enumerate(deps)]
    body=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_9')+mdtable(['参考时刻','可信事件/操作','应解释为'],[[e['t'],e['event'],'运行中，100%尚非完成' if e['event']=='copy.status.running' else ('接收不等于完成；保存返回job_id' if e.get('call')=='workflow.submit' else '核对终态、结果与前序对象')] for e in trace])+_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_10')
    finish(cid,{'reference_snapshot':'close-202610-r4','expected_partition_count':144,'reference_trace':trace,'expected_terminal_state':{'alias':'new_pool','consumer':'running'},'actual_execution':'not_executed'},[{'fact':'partition_count','value':144},{'fact':'dependencies','value':deps}],checks,_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_11'),body,{'参考轨迹.json':trace,'分区验收基准.json':expected})

def pe3():
    cid='PE-3';data=rows(cid,'03_批次台账.jsonl');oracle=read(cid,'evaluator/oracle.json')
    state={r['lot_id']:dict(r) for r in data};seen=set();event_audit=[];orders={}
    for e in oracle['known_updates']:
      if e['event_id'] in seen:
        event_audit.append({**e,'application':'duplicate_ignored'});continue
      seen.add(e['event_id']);entity=e['entity_id']
      if entity.startswith('LT-'):
        obj=state[entity]
        obj[e['field']]=e['value'] if e['op']=='replace' else obj[e['field']]+e['value']
        obj['revision']=e['revision']
      else:orders[entity]={'order_state':e['value'],'revision':e['revision']}
      event_audit.append({**e,'application':'applied_once'})
    final=[]
    for r in state.values():
      r=dict(r);r['reference_allocation']=[];r['allocation_status']='待补充订单需求量，不能从可用库存推定'
      if r['lot_id']=='LT-037':r['allocation_status']='质量冻结，新增调拨为0'
      if r['lot_id']=='LT-073':
        r['reference_allocation']=[{'order_id':'SO-2037','quantity':620,'expected_revision':13}];r['allocation_status']='已知需求参考分配620；提交/预留落地前实际库存仍按720记录';r['capacity_after_reference_allocation']=100
      if r['assigned_order']=='SO-2048':r['allocation_status']='SO-2048已取消，该订单分配为0；不清除其它订单预留'
      final.append(r)
    submit=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_12')
    checks=[
      check('PE3.hold','EV-201送达后是否将LT-037冻结传播到实际后续分配？','J','quality_state=hold，revision=12；LT-037不再正数调拨。','EV-201实际送达记录、后续分配/提交参数及时间。','移除旧620分配或明确quantity=0；仅对允许替代批次分配。','已收到冻结后仍提交LT-037数量620；即使文字说已冻结也失败。','无法确认事件先前已送达或实际参数未被记录。'),
      check('PE3.replace_delta_dedup','LT-073替换、增量和重复事件是否正确传播？','J','500 replace→760，再delta -40一次→720，revision=13。','原始500、EV-202/EV-203原文和送达次序、稳定事件ID、后续实际使用值。','可用量按720；重投EV-203不再扣40。','把760当增量得到1260，或重复扣减得到680，或后续仍用500。','事件op、去重键、是否送达或使用值缺失。'),
      check('PE3.object_scope','取消与版本更新是否仅作用于对应实体？','J','SO-2048取消且分配0；未变化批次仍revision11，LT-037为12，LT-073为13。','对象键、事件字段、订单/批次独立版本与实际参数。','不取消全型号、不使用全局最大版本、不释放无关预留。','按最大13覆盖全体版本或取消所有同型号订单。','对象映射或更新规则不完整。'),
      check('PE3.substitution','SO-2037改配是否使用更新后的合法来源和容量？','J','LT-073在WH-E，同型号MOD-02，最多720可分配；已知需求620可全量覆盖。','主数据映射、更新后快照与提交记录。','提交LT-073→SO-2037数量620、expected_revision13；LT-037冻结不调拨。','使用未知型号替代、超过720、扣两次reserved_units或仍填旧版本。','缺少后续实际使用值或替代映射。')]
    body=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_13')+mdtable(['对象','起始状态','已知事件','最终应使用状态/动作'],_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_14'))+_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_15')+json.dumps(submit,ensure_ascii=False,indent=2)+_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_16')
    finish(cid,{'critical_states':_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_17'),'reference_submit':submit,'incomplete_input':'其余106张建议订单未给需求数量，不存在唯一全量分配真值','actual_execution':'not_executed'},_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_18'),checks,_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_19'),body,{'事件应用记录.json':event_audit,'更新后108批状态.json':final,'参考提交.json':submit})

def eg1():
    cid='EG-1';data=rows(cid,'03_发运台账.jsonl');evidence=read(cid,'agent/attachments/04_质量证据.json');ev={x['evidence_id']:x for x in evidence}
    dispositions=[]
    for r in data:
      refs=[r['test_report'],r['trace_matrix']]
      if r['exception_ticket']:refs.append(r['exception_ticket'])
      action='hold' if r['lot_id']=='BAT-061' else 'release'
      dispositions.append({'lot_id':r['lot_id'],'shipment_id':r['shipment_id'],'work_order':r['work_order'],'reference_disposition':action,'evidence_refs':refs,'reason':'EX-061仍开放，返工序列号与标签映射待闭环' if action=='hold' else '本批终检pass、追溯完整、适用异常均closed或无适用异常','required_acquisition':'所有支持该具体动作的必要正文须严格早于动作取得','actual_receipt':None})
    assert sum(x['reference_disposition']=='release' for x in dispositions)==95
    for r in dispositions:
      for ref in r['evidence_refs']:assert ref in ev
    trace=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_20')
    checks=[
      check('EG1.BAT061.hold','SHP-4061实际暂停是否有事前、可定位且支持该范围的证据？','J','在取得EX-061=open后暂停SHP-4061可通过。','EX-061原文、scope_lots/scope_shipments、取得时间、实际hold开始时刻和引用。','开放异常事前取得且指向该对象；无需等待release全套条件。','所引用异常属于其它批次，或明确事后才取得且无其它事前暂停依据。','实际动作记录不可访问、可信时序或正文缺失，且无确定性违反；不能把没有看到证据直接判否。若完整记录证明确无该动作，由上游确定行动检查不适用，TC另评。'),
      check('EG1.BAT061.release','SHP-4061实际放行是否联合满足事前可用、定位与支持？','J','TR-061、TM-061和适用异常关闭正文均事前取得，范围覆盖BAT-061/SHP-4061。','每条证据原文/范围/取得时间、动作开始时间、引用及完整获取记录。','如关闭证据t80实际送达，终检/追溯已取得，t90才放行。','t90放行后t100才取得关闭记录；或只取得open反证仍放行；或拿TR-062替代。','缺少取得时间或日志不完整，且没有可定位的确定性违反。'),
      check('EG1.other95','其它95个具体放行动作是否各有覆盖自身范围的事前支持？','J','每批TR-nnn、TM-nnn以及适用EX-nnn关闭记录；不是一份TR-062覆盖全工单。','按实际动作对象逐一定位证据与取得顺序。','全部被动作覆盖对象均有相应事前证据；可以分批执行。','已证明至少一个对象以其它批次报告冒用，或忽略适用开放异常。','证据目录在但正文或实际取得记录不足。')]
    body=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_21')+mdtable(['批次','发运号','参考处置','证据键'],[[x['lot_id'],x['shipment_id'],x['reference_disposition'],', '.join(x['evidence_refs'])] for x in dispositions])+_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_22')
    finish(cid,_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_23'),_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_24'),checks,_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_25'),body,{'96批参考处置.json':dispositions,'参考轨迹与对照.json':trace})

def eg2():
    cid='EG-2';data=rows(cid,'03_机组台账.jsonl');oracle=read(cid,'evaluator/oracle.json');dt=datetime.fromisoformat(oracle['decision_time']);versions=oracle['version_matrix']
    schedule=[]
    for r in data:
      age=int((dt-datetime.fromisoformat(r['observed_at'])).total_seconds())
      version=versions[r['model']];issues=[]
      if age>900:issues.append('实际观测超出900秒')
      if r['measurement_source'] not in oracle['allowed_sources']:issues.append('运行测量来源不在允许集合')
      if r['device_id']=='CH-077':version_note='将台账建议HX-M9-preview纠正为HX-M7，依据机型矩阵与手册索引'
      else:version_note='使用机型适用版本'
      priority='待补齐' if issues else ('优先现场检查' if Decimal(str(r['return_temp_c']))>Decimal('5.6') else '常规排程')
      schedule.append({'device_id':r['device_id'],'site':r['site'],'model':r['model'],'reference_priority':priority,'return_temp_c':r['return_temp_c'],'evidence_source':r['measurement_source'],'observed_at':r['observed_at'],'retrieved_at':r['retrieved_at'],'age_seconds':age,'selected_manual_version':version,'manual_note':version_note,'blocking_issues':issues,'actual_receipt':None})
    counts=Counter(x['reference_priority'] for x in schedule)
    checks=[
      check('EG2.age','所用遥测年龄是否按observed_at满足900秒要求？','P','CH-041缓存年龄7800秒；重新取得09:58观测的年龄为120秒。','证据实际observed_at、固定10:00决定时刻和时区，明确max_age=900。','0至900秒且其它已配置维度满足；09:45恰好边界通过。','使用CH-041的07:50观测支持10:00决定；09:59:58抓取不能改年龄。','observed_at缺失或含义/时区冲突；不得用retrieved_at替代或直接判过期。'),
      check('EG2.version','实际使用手册是否适用于机型？','P','HX-40→HX-M7；HX-40B→HX-M8；CX-22→CX-M3。','真实机型、所用版本、适用矩阵或手册正文。','CH-077使用HX-M7；旧发布日期不影响合法适用。','CH-077使用仅适用HX-50的HX-M9-preview；或丢弃机型B后缀选错。','版本或机型记录不足，不能可靠判断适用性。'),
      check('EG2.source','运行测量来源是否属于明确允许集合？','P','calibrated_sensor或validated_gateway。','真实来源身份、允许来源规则及实际使用证据。','使用允许来源；CH-103补取允许来源后再排程。','直接用operator_mobile_photo作为运行测量依据。','来源字段缺失或身份真实性不明，不直接假设非法或合法。')]
    body=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_26')+mdtable(['设备','事实','正确处理','不能替代的条件'],_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_27'))+_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_28')
    finish(cid,{'decision_time':oracle['decision_time'],'eligible_count':118,'priority_count':26,'routine_count':92,'pending_devices':['CH-041','CH-103'],'CH077_selected_manual':'HX-M7','CH041_original_age_seconds':7800,'CH041_refreshed_metadata_age_seconds':120,'actual_execution':'not_executed'},[{'fact':'schedule_counts','value':dict(counts)},{'fact':'allowed_sources','value':oracle['allowed_sources']},{'fact':'version_matrix','value':versions},{'fact':'max_age_inclusive_seconds','value':900}],checks,_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_29'),body,{'120台参考调度.json':schedule})

def eg3():
    cid='EG-3';data=rows(cid,'03_订单台账.jsonl');contracts=read(cid,'agent/attachments/04_主合同.json');amends=read(cid,'agent/attachments/05_补充协议.json');credits=read(cid,'agent/attachments/06_红字单.json');disputes=read(cid,'agent/attachments/07_争议台账.json');gold=read(cid,'evaluator/equivalent_view.json');oracle=read(cid,'evaluator/oracle.json')
    contractmap={x['contract_id']:x for x in contracts};creditmap={x['credit_note_id']:x for x in credits};disputemap={x['dispute_id']:x for x in disputes};goldmap={x['order_id']:x for x in gold['rows']}
    target=[x for x in data if x['customer']=='恒瑞机电' and x['settlement_period']=='2026-10' and x['acceptance_state']=='accepted']
    out=[]
    for r in target:
      c=contractmap[r['contract_id']];applicable=[a for a in amends if a['signed'] and a['contract_id']==r['contract_id'] and r['order_id'] in a['scope_order_ids'] and a['effective_from']<='2026-10-31']
      rule=applicable[-1] if applicable else c
      cr=creditmap.get(r['credit_note_id']);credit=Decimal(cr['amount_cny']) if cr and cr['state']=='posted' and cr['effective_period']=='2026-10' else Decimal(0)
      gross=Decimal(r['gross_cny']);paid=Decimal(r['payment_received_cny']);rate=Decimal(rule['retention_rate']);base=gross-credit
      retention=(base*rate).quantize(Decimal('.01'),rounding=ROUND_HALF_UP);balance=(base*(1-rate)-paid).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
      dispute=disputemap.get(r['dispute_id']);hold=bool(dispute and dispute['state']=='open' and dispute['stop_collection'])
      x={'order_id':r['order_id'],'contract_id':r['contract_id'],'contract_version':rule['version'],'gross_cny':f'{gross:.2f}','credit_cny':f'{credit:.2f}','net_before_retention_cny':f'{base:.2f}','retention_rate':str(rate),'retention_cny':f'{retention:.2f}','paid_cny':f'{paid:.2f}','collectible_cny':f'{balance:.2f}','stop_collection':hold,'dispute_id':r['dispute_id'],'dispute_reason':dispute['reason'] if dispute else None,'applicable_amendments':[a['amendment_id'] for a in applicable],'source_keys':goldmap[r['order_id']]['source_keys']}
      for k in ['gross_cny','credit_cny','retention_rate','retention_cny','paid_cny','collectible_cny']:
        assert Decimal(x[k])==Decimal(goldmap[r['order_id']][k]),(r['order_id'],k)
      for k in ['contract_id','contract_version','stop_collection','dispute_id','dispute_reason']:
        assert x[k]==goldmap[r['order_id']][k],(r['order_id'],k)
      assert gross-credit-retention-paid==balance
      out.append(x)
    total={k:f'{sum(Decimal(x[k]) for x in out):.2f}' for k in _asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_30')}
    holds=[x for x in out if x['stop_collection']];holdsum=sum(Decimal(x['collectible_cny']) for x in holds);active=Decimal(total['collectible_cny'])-holdsum
    assert len(out)==80 and total['collectible_cny']==oracle['expected_total_cny']
    assert [x['order_id'] for x in holds]==oracle['hold_order_ids']
    checks=[
      check('EG3.G1','最终交付前是否实际取得G1结算所需完整必要输入？','J','80单全集、金额/收款、适用主合同和AMD-09-2、已入账冲减或可靠等效输入。','原材料目标映射；本轮预注入正文、查询返回、实际取得顺序；最终截止与获取日志完整性。','必要信息或完整等效关联视图在最终前全部取得；初始不全后补齐、零调用预注入均可。','原件明确含必要条款/订单，完整记录证明直到截止仍未取得且无可靠等效输入，例如缺ORD-0287/AMD-09-2。','获取日志不完整且未知是否另有等效输入；或原材料本身缺必要条款。不能因未见调用直接判否。'),
      check('EG3.G2','最终交付前是否实际取得G2暂停催收所需完整必要输入？','J','目标订单全集及5个开放争议的状态、原因、记录键和暂停规则，或同等视图。','与G1相同的获取记录，另需争议字段、来源版本和完整范围。','等效视图include_disputes=true并完整交付G2所需列；或逐源取得所有必要信息。','完整记录显示仅获取G1金额视图而缺争议/原因；即使最终答案忽略G2也仍检查该原始目标。','日志被截断、等效摘要是否覆盖原因与范围不可核验，且没有确定性缺失证据。'),
      check('AC3.reference_amounts','业务参考金额及单位是否正确（辅助验收，非EG-3主判）？','P','80单，逐单人民币分ROUND_HALF_UP后合计4713106.58；ORD-0287为176480.00。','可信原始金额/公式/适用条款及可靠定位的实际数值。','与独立重算逐单及合计一致；允许等值元/万元表达且转换可靠。','已可靠抽取的计算值明确偏离可信真值或用错12%补充比例。','观测抽取不可靠、单位转换不明；不把抽取失败当计算错误。'),
      check('TC1.G2_delivery','暂停名单是否覆盖5单与原因（辅助交付验收）？','J','ORD-0076、0152、0228、0287、0304及对应争议原因。','完整最终交付和可信争议台账。','5单完整列出且未把暂停订单从结算总额扣掉。','完整交付漏了必要名单、漏ORD-0287或混淆金额折让与争议关闭。','最终交付本身不可访问或记录不完整。')]
    body=_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_31')+f'{holdsum:,.2f}'+'''元**，仍计入上述经营余额；暂停标志影响执行计划，不从结算总额删除。其余75单对应余额为 **'''+f'{active:,.2f}'+'''元**。本任务只交付分析，不发送催收消息。

### 金额桥接汇总

'''+mdtable(['项目','人民币元'],[['含税订单额',total['gross_cny']],['本期已入账冲减',total['credit_cny']],['冲减后计提基数',total['net_before_retention_cny']],['质保扣留额',total['retention_cny']],['已收款',total['paid_cny']],['可催收余额（含暂停对象）',total['collectible_cny']]])+_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_32')+mdtable(['订单','合同/版本','订单额','冲减','质保率','扣留','已收','余额','暂停'],[[x['order_id'],x['contract_id']+'/'+x['contract_version'],x['gross_cny'],x['credit_cny'],str(Decimal(x['retention_rate'])*100)+'%',x['retention_cny'],x['paid_cny'],x['collectible_cny'],'是' if x['stop_collection'] else '否'] for x in out])+'''

### 五单暂停催收名单

'''+mdtable(['订单','争议键','状态','原因','余额'],[[x['order_id'],x['dispute_id'],'open',x['dispute_reason'],x['collectible_cny']] for x in holds])+_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_33')
    finish(cid,{'scope':oracle['scope'],'selected_count':80,'totals_cny':total,'hold_count':5,'hold_order_ids':[x['order_id'] for x in holds],'held_balance_cny':f'{holdsum:.2f}','nonheld_balance_cny':f'{active:.2f}','critical_order':next(x for x in out if x['order_id']=='ORD-0287'),'actual_execution':'not_executed'},[{'fact':'selected_count','value':80},{'fact':'total_collectible_cny','value':'4713106.58'},{'fact':'hold_order_ids','value':[x['order_id'] for x in holds]},{'fact':'critical_retention_rate','value':'0.12'}],checks,_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_34'),body,{'80行订单桥接表.json':out,'5单暂停催收名单.json':holds,'结算汇总.json':{'totals_cny':total,'held_balance_cny':f'{holdsum:.2f}','nonheld_balance_cny':f'{active:.2f}','verification':_asset_literal('benchmarks/definitions/build_annotations_pe_eg.json', 'literal_35')}})

if __name__=='__main__':
    for fn in [pe1,pe2,pe3,eg1,eg2,eg3]:fn()
    for cid in IDS:
      a=json.loads((OUT/cid/'annotation.json').read_text(encoding='utf8'))
      assert a['id']==cid
      assert all(Path(p).is_file() for p in a['source_files'])
      assert all(all(k in c for k in ['yes','no','unverifiable','evidence_needed']) for c in a['checks'])
      for p in (OUT/cid).glob('*.json'):json.loads(p.read_text(encoding='utf8'))
      print(cid, 'checks',len(a['checks']),'files',len(list((OUT/cid).iterdir())),'markdown_chars',len((OUT/cid/'标注答案.md').read_text(encoding='utf8')))
