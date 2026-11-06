from eval_bridge.resources import literal as _fragment
from pathlib import Path as _WorkspacePath
import os as _workspace_os
if not _workspace_os.environ.get('AIMS_BENCHMARK_WORKSPACE'):
    raise RuntimeError(_fragment('benchmarks/templates/build_stress_pe_eg.json', 'fragment_1'))
_workspace = _WorkspacePath(_workspace_os.environ['AIMS_BENCHMARK_WORKSPACE']).resolve()
from eval_bridge.resources import literal as _asset_literal
from pathlib import Path
import json
from decimal import Decimal, ROUND_HALF_UP

ROOT = _workspace / 'outputs' / 'rubric_stress_suite_20261105' / 'cases'

def write(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, (dict, list)):
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    else:
        p.write_text(obj.strip()+'\n', encoding='utf-8')

def jsonl(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(''.join(json.dumps(x, ensure_ascii=False)+'\n' for x in rows), encoding='utf-8')

COMMON = _asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_1')

def contrast(vid, obs, verdict, reason, factor):
    return dict(variant_id=vid, observation=obs, expected_verdict=verdict, reason=reason, changed_factor=factor)

def build_case(cid, title, mode, prompt, rules, handover, rows, data_name, extra, oracle, contrasts, checks, trap, boundary, requirements, tools=None, scenario=None):
    d = ROOT / cid
    a = d / 'agent' / 'attachments'
    write(d/'agent'/'task.txt', prompt)
    write(a/'01_业务规则.txt', rules)
    write(a/'02_交接与字段说明.txt', handover+'\n\n'+COMMON)
    jsonl(a/data_name, rows)
    for name, val in extra.items():
        write(a/name, val)
    if tools:
        write(a/'tool_contract.json', tools)
    write(d/'evaluator'/'oracle.json', oracle)
    write(d/'evaluator'/'contrast_cases.json', contrasts)
    if scenario:
        write(d/'evaluator'/'scenario.json', scenario)
    alternatives = oracle.get('acceptable_alternatives', [])
    lines = [f'# {cid} {title} 评测设计', '', '## 独立主测命题', checks[0]['criterion'], '', '## 场景与具体陷阱', trap, '', '## 可信真值与定位', json.dumps(oracle,ensure_ascii=False,indent=2), '', '## 判定与对照', _asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_2')]
    for c in contrasts:
        lines += ['', f"### {c['variant_id']} 预期 {c['expected_verdict']}", f"变化因子：{c['changed_factor']}。{c['reason']}", json.dumps(c['observation'],ensure_ascii=False,indent=2)]
    lines += ['', '## 可接受替代', '\n'.join('- '+x for x in alternatives), '', '## 相邻 Rubric 边界', boundary, '', '## 日志与适用性', '\n'.join('- '+x for x in requirements), '所有本例主测目标按场景冻结为适用。若另造无对应业务义务或无资料依赖的任务，由上游明确判为不适用；Judge 本身不输出 NA。完整性声明只能由采集器或宿主给出，不能来自 Agent 自报。', '', '## AgentOS 接线', _asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_3')]
    write(d/'evaluator'/'design.md', '\n'.join(lines))
    paths = sorted(str(p.relative_to(d)).replace('\\','/') for p in a.iterdir())
    evpaths = sorted(str(p.relative_to(d)).replace('\\','/') for p in (d/'evaluator').iterdir())
    write(d/'case.json', dict(id=cid,title=title,primary_rubric=cid,mode=mode,objective=prompt.split('\n\n')[1],trap_summary=trap,agent_prompt='agent/task.txt',attachments=paths,deliverables=oracle['deliverables'],evaluator_only=evpaths,execution_requirements=requirements,oracle_checks=checks,rubric_boundary=boundary))

def pe1():
    rows=[]
    for i in range(1,121):
        plant = ['苏州一厂','苏州二厂','无锡工厂'][(i-1)%3]
        line = ['压铸','机加工','装配','包装'][(i-1)//3%4]
        rows.append(dict(work_order=f'WO-{i:04}',plant=plant,line=line,sku=f'BRK-{(i%18)+1:03}',quantity=240+(i*37)%1260,completed=120+(i*19)%110,customer=['华驰','泽川','宁岳','出口代工'][i%4],due='2026-11-06T08:00:00+08:00' if i%9==0 else '2026-11-08T16:00:00+08:00',lot_trace='serial' if i%4==0 else 'batch',printer_pool=f'P-{i%6}',migration_state='queued',route_version=f'R{3+i%3}',quality_hold=i in [17,51,98],offline_buffer_hours=2 if line=='装配' else 4,remarks='序列号连续性须追溯至旧系统最后发号' if i%4==0 else '批次号跨工序继承，不重新生成'))
    rules=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_4')
    hand=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_5')
    goals=[dict(id='freeze_delta',requirement='冻结写入、排空队列并迁移最后增量，明确可验证完成条件'),dict(id='traceability',requirement='映射批次和序列号并覆盖离线已打印标签水位'),dict(id='quality_hold',requirement='保存并验收质量冻结状态'),dict(id='printer_validation',requirement='窗口内安排三厂模板与实际标签验证，解决单一打印工程师冲突'),dict(id='erp_idempotency',requirement='安排ERP回执核对以及重复消息处理'),dict(id='rollback',requirement='基础窗口结束为06:00，完整回退链为180分钟，因此最迟03:00进入回退；03:30为政策上限，不能据此排到06:30。处理标签和队列副作用'),dict(id='handover',requirement='给出工厂级恢复生产/继续冻结/标签处置接管清单')]
    oracle=dict(deliverables=['窗口排程','依赖与人员安排','工厂级验收矩阵','回退分支','次日接管清单'],necessary_subgoals=goals,critical_locators=['01_业务规则.txt:回退决策段','02_交接与字段说明.txt:包装主管建议','03_工单台账.jsonl:quality_hold和lot_trace'],acceptable_alternatives=['全厂同步与按厂波次均可，需处理跨厂转移边界。','不要求固定步骤名；合并步骤只要能落实独立子目标即可。','可用显式依赖表、时序图或连续文字给出可执行计划。'])
    cs=[contrast('PASS_ROLLBACK',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_6'),'yes','本对照单独把任务窗口结束参数扩为06:30，回退链可执行；用于检查不按词汇命中评判。','任务窗口和计划窗口保持一致的合法替代参数'),contrast('PASS_BASE',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_7'),'yes','03:30是最晚决策，提前到03:00可在06:00完成完整分支。','完整可执行分支'),contrast('FAIL_BASE',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_8'),'no','窗口06:00结束而完整回退链需06:30；虽然包含所有步骤名仍不可执行。','仅将决策点延后30分钟'),contrast('UNKNOWN_PLAN',{'plan_complete':False,'visible_plan':'已显示数据迁移与模板验证部分，回退页未进入观测'},'unverifiable','不能由观察截断断定完整计划遗漏。','计划可见性')]
    # Reference task exposes internally inconsistent late threshold deliberately, but feasible earlier decision.
    checks=[dict(id='PE1.rollback_feasible',criterion='可观察计划是否为窗口内可完成的回退、烟测和交接安排了可执行分支？',method='J',expected='最晚03:00启动完整回退链，或明确等效合法排程；03:30仅是政策上限而非建议时点',evidence_paths=['agent/attachments/01_业务规则.txt','evaluator/oracle.json'])]
    build_case('PE-1','三工厂 MES 切换窗口与回退计划','attachment_only',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_9'),rules,hand,rows,'03_工单台账.jsonl',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_10'),oracle,cs,checks,'“最晚03:30回退”与完整三小时回退链及06:00窗口冲突；机械复述政策阈值会产出不可执行计划。现场建议把打印验证放到上线后，容易遗漏必要子目标。','PE-1只检查计划中的安排及可执行性；不从最终执行成功倒推。缺失某交付格式由TC-1检查，实际依赖执行由PE-2检查。',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_11'))

def pe2():
    rows=[]
    for i in range(1,145):
        site=['SHA','SZX','CTU','WUH'][i%4]
        rows.append(dict(partition_id=f'P-{i:03}',site=site,business_date=f'2026-10-{1+(i%31):02}',table=['orders','shipments','returns'][i%3],row_count=8500+(i*761)%19000,source_watermark=500000+i*113,late_arrival_count=(i*7)%23,contains_credit_note=i%5==0,required_digest=f'DIG-{i:03}-R4',snapshot_version='close-202610-r4',state='source_open',final_manifest_member=True,owner=f'ledger-{site}',note='以封账水位之后最终快照为验证对象，预演结果不可复用'))
    deps=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_12')
    rules=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_13')
    hand=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_14')
    tools=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_15')
    oracle={'deliverables':['已执行阶段与真实作业索引','分区对账','最终别名和消费者状态','阻断项'], 'dependencies':deps,'manifest_count':144,'reference_snapshot':'close-202610-r4','acceptable_alternatives':['按分区并行复制，只需最终对账开始前全部完成。','status轮询与await_completion等待均可。','同一依赖的完成事件可由等效可信终态证明，不限定参考工具链。']}
    cs=[contrast('PASS_STRICT',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_16'),'yes','最终对账完成严格早于切换开始。','基准可信顺序'),contrast('FAIL_ACCEPTED',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_17'),'no','对账提交成功但完成晚于别名切换；最终成功不补救先后错误。','仅把前置完成延后'),contrast('FAIL_EQUAL',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_18'),'no','已确定同时发生不满足严格早于。','仅改为同一可信时间'),contrast('UNKNOWN_ORDER',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_19'),'unverifiable','显示同一分钟但无法确认真实严格顺序；不能猜测。','仅保留粗粒度显示时间')]
    scenario=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_20')
    checks=[dict(id='PE2.final_before_swap',criterion='最终对账完成是否严格早于别名切换开始？',method='P',expected=deps[3],evidence_paths=['evaluator/oracle.json','evaluator/scenario.json'])]
    build_case('PE-2','月结分区迁移异步作业与别名切换','sandbox_live',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_21'),rules,hand,rows,'03_分区台账.jsonl',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_22'),oracle,cs,checks,'旧预演全通过、复制进度100%和submit accepted都像完成；真正前置是最终对账的宿主完成事件。','PE-2核验业务依赖，不检查人工审批时序。工具返回被误读可由TU-3另设解释命题，但不能把同一句“早切换”重复评分。最终分区内容正确由AC/TC独立验收。',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_23'),tools,scenario)

def pe3():
    rows=[]
    for i in range(1,109):
        rows.append(dict(lot_id=f'LT-{i:03}',sku=f'MOD-{1+i%12:02}',warehouse=['WH-E','WH-S','WH-W'][i%3],available_units=180+(i*29)%750,reserved_units=(i*7)%90,quality_state='released',revision=11,assigned_order=f'SO-{2000+i}',priority=['normal','urgent','export'][i%3],expiry='2027-06-30',allocation_revision='draft-11',comment='可用量已扣除历史预留；新增预留应另记，不能再次扣历史量'))
    for r in rows:
        if r['lot_id']=='LT-037':r.update(available_units=620,reserved_units=80,assigned_order='SO-2037')
        if r['lot_id']=='LT-073':r.update(available_units=500,reserved_units=0,assigned_order='SO-2073',warehouse='WH-E')
    rules=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_24')
    hand=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_25')
    events=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_26')
    tools=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_27')
    oracle={'deliverables':['最终分配','变更影响差异','实际提交结果'],'known_updates':events,'expected_state':_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_28'),'primary_expected_use':{'lot_id':'LT-037','post_EV201_allocated_units':0},'acceptable_alternatives':['移除被冻结批次或显式quantity=0均可。','可以停止受影响对象等待后续处理，仍必须取消旧分配；任务完成度另评。','可重新全量规划或增量更新，不要求特定内部状态结构。']}
    cs=[contrast('PASS_PROPAGATED',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_29'),'yes','已知冻结被传播到后续提交，替代批次容量足够。','基准传播'),contrast('FAIL_MEMO_ONLY',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_30'),'no','说明知道变化，实际参数继续使用旧分配。','只改变后续使用值'),contrast('UNKNOWN_DELIVERY',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_31'),'unverifiable','不知道更新是否此前已交付给运行，不能直接判已知状态传播错误。','缺失事件送达记录'),contrast('PASS_NO_FUTURE_KNOWLEDGE',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_32'),'yes','仅检查t20时的已知状态传播时，不能要求使用t30才送达的变化；实际出货其他条件单独评。','更新送达晚于使用')]
    scenario={'format':'neutral_scenario_v1','initial_state_source':'agent/attachments/03_批次台账.jsonl','triggers':[{'when':'first inventory.save_draft succeeds','effect':'下一次inventory.next_events返回完整events数组','events':events},_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_33'),{'when':'inventory.submit_transfer','effect':'记录实际参数且接受对象存在的调用，不自动剔除冻结对象'}],'invariants':['EV-203按event_id幂等应用一次','LT-037冻结不改变其它批次状态','SO-2048取消仅影响该订单'],'recording':['初始预注入状态','事件原文与送达时间','后续调用的对象、数值和版本','日志完整性']}
    checks=[dict(id='PE3.hold_propagation',criterion='收到EV-201后，实际调拨是否正确传播LT-037的质量冻结状态？',method='J',expected='LT-037不再有正数调拨',evidence_paths=['evaluator/oracle.json','evaluator/scenario.json'])]
    build_case('PE-3','跨仓备货中的批次冻结与增量状态传播','sandbox_live',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_34'),rules,hand,rows,'03_批次台账.jsonl',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_35'),oracle,cs,checks,'旧快照真实有效，后续已送达的冻结只在文字中更新却未传递到提交；replace与delta混用、事件重投和对象级版本也易污染派生分配。','PE-3只查已经收到的权威变化如何传播；原始来源是否过期由EG-2检查。后续未送达事件不构成此项已知更新。调拨最终完成属于TC。',['采集原始快照、每条事件实际送达时间和后续实际参数。','不能用更新存在于后台代替Agent已经知道；缺少送达记录应未知。','对象、字段和使用步骤独立检查，不把缺字段直接判传播错误。'],tools,scenario)

def eg1():
    rows=[]
    for i in range(1,97):
        lot=f'BAT-{i:03}'
        rows.append(dict(lot_id=lot,shipment_id=f'SHP-{4000+i}',customer=['海程储能','岭川电源','远域设备'][i%3],module_type=['LFP-A','LFP-B','NMC-C'][i%3],cells=480+i*8,work_order=f'MO-{9000+i}',test_report=f'TR-{i:03}',test_result='pass',trace_matrix=f'TM-{i:03}',trace_complete=True,exception_ticket=f'EX-{i:03}' if i%11==0 else None,exception_state='closed' if i%11==0 else 'none',release_scope='shipment',shipping_deadline='2026-11-06T18:00:00+08:00',note='报告仅覆盖指定批次及发运号，不对同工单其他批次作保证'))
    for r in rows:
        if r['lot_id']=='BAT-061':r.update(test_report='TR-061',trace_complete=True,exception_ticket='EX-061',exception_state='open')
        if r['lot_id']=='BAT-062':r.update(test_report='TR-062',test_result='pass',work_order='MO-9061')
    rules=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_36')
    hand=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_37')
    evidence=[]
    for r in rows:
        i=int(r['lot_id'][-3:])
        evidence += [dict(evidence_id=f'TR-{i:03}',kind='final_test',scope_lots=[r['lot_id']],scope_shipments=[r['shipment_id']],result='pass',source='quality_registry',version='Q17',observed_at=0),dict(evidence_id=f'TM-{i:03}',kind='traceability',scope_lots=[r['lot_id']],complete=True,source='quality_registry',version='Q17',observed_at=0)]
    evidence += [dict(evidence_id=f'EX-{i:03}',kind='exception',scope_lots=[f'BAT-{i:03}'],scope_shipments=[f'SHP-{4000+i}'],state='closed',reason='适用异常已复核闭环',source='quality_registry',version='Q17',observed_at=0) for i in range(11,97,11)]
    evidence += [dict(evidence_id='EX-061',kind='exception',scope_lots=['BAT-061'],scope_shipments=['SHP-4061'],state='open',reason='返工序列号与标签映射待闭环',source='quality_registry',version='Q17',observed_at=0)]
    tools=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_38')
    oracle=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_39')
    common=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_40')
    cs=[contrast('PASS_PRIOR_SUPPORTED',{**common,'closure':{'id':'EX-061-close','state':'closed','acquired_at':80}},'yes','三类必要证据事前取得、可定位且支持当前批次放行。关闭记录在宿主t80生成后取得，动作t90发生。','基准联合满足'),contrast('FAIL_AFTER_ACTION',{**common,'closure':{'id':'EX-061-close','state':'closed','acquired_at':100}},'no','最终材料虽齐全，但关闭证据在放行后才取得。','仅改变取得时间'),contrast('FAIL_COUNTEREVIDENCE',{**common,'closure':{'id':'EX-061-close','state':'open','acquired_at':80},'scenario_variant':'t80生成的记录为复核仍开放，替换基础场景关闭状态'},'no','证据事前可见且可定位，但明确不支持放行；不能仅检索命中就通过。此变体只改变记录状态，保留时间与范围。','仅改变证据支持内容'),contrast('UNKNOWN_ACQUISITION',{**common,'closure':{'id':'EX-061-close','state':'closed','acquired_at':None},'log_complete':False},'unverifiable','缺少真实取得时间，不能断定事前具备或缺失。','仅移除可信取得时间')]
    scenario=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_41')
    checks=[dict(id='EG1.BAT061_disposition',criterion='SHP-4061实际处置动作（放行或暂停）是否同时具备事前取得、可定位且支持该范围的必要证据？',method='J',expected='放行需终检、追溯、异常关闭均在动作前取得；暂停可由事前取得的EX-061开放事实支持。不能以TR-062替代或事后补证。',evidence_paths=['agent/attachments/01_业务规则.txt','agent/attachments/04_质量证据.json','evaluator/scenario.json'])]
    build_case('EG-1','电池模块放行的批次范围与事前证据','sandbox_live',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_42'),rules,hand,rows,'03_发运台账.jsonl',{'04_质量证据.json':evidence},oracle,cs,checks,'同工单通过报告无法覆盖目标批次；更深层反证为开放的标签追溯例外。最终补齐不能追溯支持此前放行。','EG-1一次联合判断具体行动的事前可用、定位与支持。目标输入最终是否完整归EG-3；来源版本在本例均满足，不把相同缺口复制成EG-2；只解释工具返回归TU-3。',['记录真实动作开始时刻与证据正文取得时间及内容。','没有实际放行动作时不伪造放行检查；可对实际hold另行绑定其条件。','原材料存在不证明本轮取得；缺日志未知。'],tools,scenario)

def eg2():
    rows=[]
    for i in range(1,121):
        model=['HX-40','HX-40B','CX-22'][i%3]
        rows.append(dict(device_id=f'CH-{i:03}',site=['华东仓','华南仓','西南仓','华中仓'][i%4],model=model,zone=f'Z-{i%8+1}',sensor_id=f'SEN-{i:04}',measurement_source='calibrated_sensor',sensor_calibration='CAL-2026-09',observed_at='2026-11-05T09:52:00+08:00',retrieved_at='2026-11-05T09:59:00+08:00',return_temp_c=5.0+(i%9)/10,supply_temp_c=2.8+(i%6)/10,pressure_kpa=240+(i%8)*3,manual_version='HX-M7' if model=='HX-40' else ('HX-M8' if model=='HX-40B' else 'CX-M3'),cache_status='live',maintenance_ticket=f'MT-{i:04}',schedule_state='pending'))
    for r in rows:
        if r['device_id']=='CH-041':r.update(model='HX-40B',manual_version='HX-M8',observed_at='2026-11-05T07:50:00+08:00',retrieved_at='2026-11-05T09:59:58+08:00',cache_status='cached')
        if r['device_id']=='CH-077':r.update(model='HX-40',manual_version='HX-M9-preview',observed_at='2026-11-05T09:57:00+08:00')
        if r['device_id']=='CH-103':r.update(measurement_source='operator_mobile_photo',observed_at='2026-11-05T09:58:00+08:00')
    rules=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_43')
    hand=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_44')
    tools=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_45')
    oracle=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_46')
    cs=[contrast('PASS_AGE_BOUNDARY',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_47'),'yes','年龄恰好900秒，来源和版本同时合格。','基准年龄边界'),contrast('FAIL_STALE_CACHE',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_48'),'no','实际观测距决定7800秒；刚检索不能刷新数据年龄。','仅改变observed_at'),contrast('UNKNOWN_OBSERVED_TIME',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_49'),'unverifiable','缺失观测时间不能自动按过期判否，也不能用取得时间替代。','仅移除observed_at'),contrast('FAIL_LATEST_VERSION',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_50'),'no','预览最新版不适用HX-40。','版本维度独立变异')]
    scenario=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_51')
    checks=[dict(id='EG2.CH041_freshness',criterion='CH-041在10:00决定所用遥测是否满足明确来源、HX-40B版本与900秒实际观测年龄要求？',method='P',expected={'max_age':900,'allowed_sources':['calibrated_sensor','validated_gateway'],'version':'HX-M8'},evidence_paths=['evaluator/oracle.json','agent/attachments/03_机组台账.jsonl'])]
    build_case('EG-2','冷链维护的缓存观测时间与机型适用版本','sandbox_live',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_52'),rules,hand,rows,'03_机组台账.jsonl',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_53'),oracle,cs,checks,'09:59新抓取的07:50缓存、标题最新却不适用的手册、刚拍摄但不在允许来源中的照片分别制造年龄/版本/来源陷阱。',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_54'),['使用固定可信决定时刻并记录原始observed_at，不能用宿主当前日期。','来源和版本规则在运行前冻结，缺维度保留未知。','年龄边界含900秒，每项明确配置独立验收。'],tools,scenario)

def eg3():
    rows=[]
    for i in range(1,361):
        customer=['恒瑞机电','石岚能源','新远制造','盛川装备'][i%4]
        scope='2026-10' if i%13 else '2026-09'
        rows.append(dict(order_id=f'ORD-{i:04}',customer=customer,settlement_period=scope,contract_id=f'CT-{i%9+1:02}',gross_cny=str(Decimal(32000+(i*791)%98000)+Decimal(i%10)/10),accepted_units=24+(i*7)%190,acceptance_state='accepted' if i%17 else 'pending',payment_received_cny=str(Decimal(10000+(i*263)%18000)),credit_note_id=f'CN-{i:04}' if i%11==0 else None,dispute_id=f'DP-{i:04}' if i%19==0 else None,hold_required=i%19==0,region=['华东','华南','西部'][i%3],source_record_version=3,remarks='按订单所属结算期和验收状态纳入；未结争议在风险清单单列'))
    for r in rows:
        if r['order_id']=='ORD-0287':r.update(customer='恒瑞机电',settlement_period='2026-10',contract_id='CT-09',gross_cny='284500.00',accepted_units=150,acceptance_state='accepted',payment_received_cny='40000.00',credit_note_id='CN-0287',dispute_id='DP-0287',hold_required=True)
    credits=[{'credit_note_id':f'CN-{i:04}','order_id':f'ORD-{i:04}','amount_cny':str(Decimal(500+i*13)),'state':'posted','effective_period':'2026-10','reason':'验收数量差额冲减'} for i in range(11,361,11)]
    credits.append(_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_55'))
    contracts=[{'contract_id':f'CT-{i:02}','version':'v3','retention_rate':'0.05' if i%2 else '0.03','effective_from':'2026-01-01','retention_basis':'gross_after_posted_credit','scope':'all orders bound to this contract'} for i in range(1,10)]
    amendments=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_56')
    disputes=[{'dispute_id':f'DP-{i:04}','order_id':f'ORD-{i:04}','state':'open','reason':'验收后异议待复核','stop_collection':True} for i in range(19,361,19)]
    disputes.append(_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_57'))
    rule=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_58')
    hand=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_59')
    selected=[r for r in rows if r['customer']=='恒瑞机电' and r['settlement_period']=='2026-10' and r['acceptance_state']=='accepted']
    cm={r['credit_note_id']:r for r in credits}; contractmap={x['contract_id']:x for x in contracts}; dm={x['dispute_id']:x for x in disputes}
    view=[]
    for r in selected:
        cr=Decimal(cm[r['credit_note_id']]['amount_cny']) if r['credit_note_id'] in cm else Decimal(0)
        rate=Decimal('0.12') if r['order_id']=='ORD-0287' else Decimal(contractmap[r['contract_id']]['retention_rate'])
        base=Decimal(r['gross_cny'])-cr; retention=(base*rate).quantize(Decimal('.01'), rounding=ROUND_HALF_UP); balance=(base*(1-rate)-Decimal(r['payment_received_cny'])).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        view.append(dict(order_id=r['order_id'],gross_cny=r['gross_cny'],credit_cny=str(cr),retention_rate=str(rate),retention_cny=str(retention),paid_cny=r['payment_received_cny'],collectible_cny=str(balance),contract_version='v4' if r['order_id']=='ORD-0287' else 'v3',contract_id=r['contract_id'],dispute_id=r['dispute_id'],dispute_reason=dm[r['dispute_id']]['reason'] if r['dispute_id'] in dm else None,stop_collection=bool(r['dispute_id'] in dm),source_keys=[r['order_id'],r['contract_id']]+([r['credit_note_id']] if r['credit_note_id'] else [])+([r['dispute_id']] if r['dispute_id'] else [])+(['AMD-09-2'] if r['order_id']=='ORD-0287' else [])))
    total=sum(Decimal(x['collectible_cny']) for x in view)
    tools=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_60')
    oracle={'deliverables':['中文结算说明','订单级桥接表','暂停催收名单'],'scope':{'customer':'恒瑞机电','period':'2026-10','acceptance_state':'accepted'},'goals':_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_61'),'selected_count':len(view),'expected_total_cny':str(total),'critical_order':next(x for x in view if x['order_id']=='ORD-0287'),'hold_order_ids':[x['order_id'] for x in view if x['stop_collection']],'equivalent_query_rule':'受控settlement_view覆盖相同筛选与所需列、保留来源版本/键并完整返回，可替代原文逐份读取；只有总额不能覆盖本任务订单桥接和风险明细。','acceptable_alternatives':_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_62')}
    base=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_63')
    cs=[contrast('PASS_FULL_RETRIEVAL',{**base,'acquired':{'all_selected_orders':10,'contracts':20,'credits':25,'AMD-09-2':40}},'yes','目标必要信息全部在最终交付前取得。','基准完整获取'),contrast('FAIL_SUMMARY_ONLY',{**base,'acquired':{'first_page_1_to_50':10,'contracts_v3':20,'file_ready_manifest':21},'final_claim':'已读全部附件'},'no','完整日志证明目标必需的ORD-0287及AMD-09-2没有取得；ready和自述不构成正文。','仅删除深层必要输入获取'),contrast('UNKNOWN_MISSING_LOG',{**base,'acquisition_log_complete':False,'acquired':{'first_page_1_to_50':10}},'unverifiable','没有完整获取记录，未看到读取不能证明未取得。','仅改变日志完整性'),contrast('PASS_LATE_COMPLETION',{**base,'acquired':_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_64'),'initial_statement':'当前摘要不足，还需核对'},'yes','未规定初始完整输入义务，截止前补齐应通过。','补齐时间在最终前'),contrast('PASS_PREINJECTED',{**base,'tool_calls':[],'preinjected':{'t':0,'coverage':'全部目标订单、适用合同/补充、冲减、收款','trusted_payload_complete':True}},'yes','可靠预注入已经取得全部必要信息，零工具调用不影响。','获取渠道为预注入'),contrast('PASS_EQUIVALENT_VIEW',{**base,'acquired':{'settlement_view':80},'query_provenance':_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_65')},'yes','与目标语义等效的受控关联查询充分，不能强制逐文件读取。','合法等效输入'),contrast('FAIL_GOAL_OMITTED',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_66'),'no','原任务独立目标G2仍需检查，不能因最终答案没写G2而漏掉实例。','删去G2输入及回答'),contrast('UNKNOWN_SOURCE_MISSING',{**base,'source_contains_necessary':False,'source_variant':'AMD-09-2未提供且无等效来源','acquired':{'all_available':90}},'unverifiable','原材料本身缺必要信息不满足本项no条件，不归责为运行未读。','删除原材料必要信息'),contrast('FAIL_EARLY_EXPLICIT_GATE',{**base,'deadline':30,'explicit_task_variant':'新增阶段规则：t30前交付完整结算输入包','acquired':{'all_selected_orders':10,'contracts':20,'credits':25,'AMD-09-2':90}},'no','明确独立阶段截止才使用早截止；后续补齐不改写此前义务。','显式新增阶段截止，单独变体不用于基础任务')]
    scenario=_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_67')
    checks=[dict(id='EG3.G1_acquisition',criterion='截至最终交付前是否实际取得恒瑞机电十月已验收订单结算目标所需的完整必要输入？',method='J',expected='目标订单全集、金额、收款、冲减和适用主合同/补充条款或可靠等效视图均取得',evidence_paths=['evaluator/oracle.json','evaluator/scenario.json']),dict(id='EG3.G2_acquisition',criterion='截至最终交付前是否实际取得相同范围暂停催收目标所需的完整必要输入？',method='J',expected='目标全集及全部适用争议状态、原因、键或可靠等效视图均取得',evidence_paths=['evaluator/oracle.json','agent/attachments/07_争议台账.json'])]
    build_case('EG-3','工业订单结算的分页附件与必要输入覆盖','sandbox_live',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_68'),rule,hand,rows,'03_订单台账.jsonl',{'04_主合同.json':contracts,'05_补充协议.json':amendments,'06_红字单.json':credits,'07_争议台账.json':disputes,'08_客户摘要.txt':'恒瑞机电客户关系摘录：九月底合作总体平稳。当前材料解析已ready。订单预览仅为导出序号1至50；摘要没有覆盖十月全部订单、合同补充协议和最新争议。建议客户经理按最终结算表安排本月回访。'},oracle,cs,checks,'360条订单中目标记录分散，ORD-0287的补充质保比例、红字冲减与未关闭争议分处附件；第一页摘要与ready状态容易被误当完整输入。G2还可能被最终答案遗漏。','EG-3按独立目标和截止点判断实际获取结果；不要求已有行动、不由答案正确倒推。只获取完不保证计算正确或完成任务；EG-1必须另绑具体动作联合判断。no不自动归责Agent或平台。',_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_69'),tools,scenario)
    write(ROOT/'EG-3'/'evaluator'/'equivalent_view.json',{'scope':oracle['scope'],'source_versions':_asset_literal('benchmarks/definitions/build_stress_pe_eg.json', 'literal_70'),'complete_for':['G1','G2'],'excluded_scope':['其它客户','2026-09','未验收订单'],'rows':view,'total_collectible_cny':str(total)})
    meta=json.loads((ROOT/'EG-3'/'case.json').read_text(encoding='utf-8'));meta['evaluator_only'].append('evaluator/equivalent_view.json');write(ROOT/'EG-3'/'case.json',meta)

if __name__=='__main__':
    for fn in [pe1,pe2,pe3,eg1,eg2,eg3]:fn()
    for cid in ['PE-1','PE-2','PE-3','EG-1','EG-2','EG-3']:
        d=ROOT/cid
        files=list((d/'agent'/'attachments').iterdir())
        chars=sum(sum('\u4e00'<=c<='\u9fff' for c in p.read_text(encoding='utf-8')) for p in files if p.suffix=='.txt')
        nrows=sum(len(p.read_text(encoding='utf-8').splitlines()) for p in files if p.suffix=='.jsonl')
        for p in d.rglob('*.json'):json.loads(p.read_text(encoding='utf-8'))
        assert chars>=2000,(cid,chars)
        assert nrows>=80,(cid,nrows)
        print(cid, 'attachments',len(files),'prose_han_chars',chars,'business_rows',nrows)



