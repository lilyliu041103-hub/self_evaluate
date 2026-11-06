from eval_bridge.resources import literal as _fragment
from pathlib import Path as _WorkspacePath
import os as _workspace_os
if not _workspace_os.environ.get('AIMS_BENCHMARK_WORKSPACE'):
    raise RuntimeError(_fragment('benchmarks/templates/build_stress_ac_tu.json', 'fragment_1'))
_workspace = _WorkspacePath(_workspace_os.environ['AIMS_BENCHMARK_WORKSPACE']).resolve()
from eval_bridge.resources import literal as _asset_literal
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import json

ROOT = _workspace / 'outputs/rubric_stress_suite_20261105/cases'

def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str): path.write_text(value.strip()+'\n', encoding='utf-8')
    else: path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def jsonl(rows): return '\n'.join(json.dumps(x, ensure_ascii=False) for x in rows)

COMMON = _asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_1')

def policy(title, paragraphs):
    return title+'\n\n'+COMMON+'\n\n'+'\n\n'.join(f'{i:02d} {p}' for i,p in enumerate(paragraphs,1))

def finish(id, title, objective, trap, task, attachments, checks, truth, boundary, contrasts, design_extra='', scenario=None):
    d=ROOT/id
    for name,value in attachments.items(): dump(d/'agent/attachments'/name,value)
    dump(d/'agent/task.txt',task)
    paths=['agent/attachments/'+x for x in attachments]
    oracle={'case_id':id,'source':'冻结的场景规则及独立生成真值；不得由Agent答案反推','truth':truth,'checks':checks}
    dump(d/'evaluator/oracle.json',oracle)
    dump(d/'evaluator/contrast_cases.json',contrasts)
    if scenario: dump(d/'evaluator/scenario.json',scenario)
    extra=['evaluator/design.md','evaluator/oracle.json','evaluator/contrast_cases.json']+(['evaluator/scenario.json'] if scenario else [])
    dump(d/'case.json',{'id':id,'title':title,'primary_rubric':id,'mode':'sandbox_live' if scenario else 'attachment_only','objective':objective,'trap_summary':trap,'agent_prompt':'agent/task.txt','attachments':paths,'deliverables':truth.get('deliverables',['任务规定的最终报告']),'evaluator_only':extra,'execution_requirements':['使用真实AgentOS调用日志或受信执行器事件，不把待执行计划当作执行证据','沙箱能力契约需要由测试运行器注册；本包未接入AgentOS'] if scenario else ['附件正文必须实际对模型可见','保留最终答案与交付物的完整快照'],'oracle_checks':checks,'rubric_boundary':boundary})
    rows=['# '+id+' '+title,'','主测命题：'+objective,'','## 针对性陷阱','',trap,'','## 冻结真值与定位','', '独立真值见 `oracle.json`，每条检查绑定输入附件的主键或字段。真值由场景作者固定，执行时不可从 Agent 的答案生成。','']
    for c in checks: rows += ['- '+c['id']+'：'+c['criterion']+'；定位：'+', '.join(c['evidence_paths'])]
    verdict_rules=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_2')
    rows += ['','## 判断边界','',boundary,'',verdict_rules[id]+' 适用性由可信测试配置决定；Judge不自行把未知改成NA。','', '## 最小差异对照','']
    for c in contrasts: rows += ['- '+c['variant_id']+' → '+c['expected_verdict']+'：'+c['reason']+'；改变因素：'+c['changed_factor']]
    rows += ['','## 采集要求','', _asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_3'),'',design_extra]
    dump(d/'evaluator/design.md','\n'.join(rows))

def check(id,criterion,expected,paths,method='J'):
    return {'id':id,'criterion':criterion,'method':method,'expected':expected,'evidence_paths':paths}

def variants(yes,no,unknown,factor='仅改变指定待查命题的一个因素'):
    return [dict(variant_id='PASS',observation=yes,expected_verdict='yes',reason='指定主命题满足，邻接条件保持相同',changed_factor='基线'),dict(variant_id='FAIL',observation=no,expected_verdict='no',reason='指定主命题存在可定位反例',changed_factor=factor),dict(variant_id='UNKNOWN',observation=unknown,expected_verdict='unverifiable',reason='主命题所需的一个证据输入不可获得，不能按错误处理',changed_factor='仅删除指定可信证据；不改变业务事实')]

# AC-1: scope-aware factual correctness.
lots=[]
for i in range(1,97):
    plant=['苏州一厂','苏州二厂','重庆工厂','宁波工厂'][(i-1)%4]
    lots.append({'lot_id':f'LOT-{i:03d}','plant':plant,'product':['HX-42','HX-42R','HX-52'][(i//4)%3],'hardware_revision':['B','C','D'][i%3],'supplier_legal_id':['SUP-118','SUP-181','SUP-204'][i%3],'customer_region':['CN','EU','US'][i%3],'production_date':f'2026-10-{(i%28)+1:02d}','firmware':['2.7.8','2.8.0','2.8.1'][i%3],'order_id':f'SO-{7800+i}','serial_start':f'SN{610000+i*120}','serial_end':f'SN{610119+i*120}','warehouse':['A01','B03','C02'][i%3],'disposition':'待逐批确认','record_owner':['成品质量','供应商质量','制造工程'][i%3]})
specials=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_4')
for num,vals in specials.items():
    for k,v in zip(_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_5'),vals): lots[num-1][k]=v
docs=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_6')
ac1_paras=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_7')
finish('AC-1','跨工厂工程变更与同名供应商事实核验','最终结论对六个批次的ECO适用事实是否准确','草案修订号更高；两家供应商简称相同；HX-42R近似产品名；B版豁免与C版规则并存；仅升级项满足被扩大为全量放行。',
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_8'),
{'batch_register.jsonl':jsonl(lots),'plm_register.json':docs,'quality_review_policy.txt':policy('工程变更评审口径与交接规则',ac1_paras),'sales_thread.txt':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_9')},
[check('ac1-007','LOT-007因ECO-247-R2要求而冻结',True,['batch_register.jsonl#LOT-007','plm_register.json#ECO-247-R2']),check('ac1-028','LOT-028不在R2工厂范围',True,['batch_register.jsonl#LOT-028','plm_register.json#ECO-247-R2']),check('ac1-049','LOT-049不在R2产品范围',True,['batch_register.jsonl#LOT-049']),check('ac1-064','LOT-064不适用C版升级要求，执行B版原路线',True,['plm_register.json#WAIVER-064']),check('ac1-083','LOT-083不在R2供应商法人范围',True,['plm_register.json#SUP-MASTER-11']),check('ac1-096','LOT-096满足本ECO固件项，不推出总放行',True,['plm_register.json#QA-LOT-096'])],
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_10'),
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_11'),
variants({'claim':'LOT-028不在ECO-247-R2工厂范围','reference':'R2仅苏州一厂'},{'claim':'LOT-028属于ECO-247-R2的强制冻结范围','reference':'R2仅苏州一厂'},{'claim':'LOT-028不在R2范围','reference':None},'仅把LOT-028的适用性结论反转'))

# AC-2: quantified route-selection reasoning.
routes=[]
for i in range(1,97):
    routes.append({'route_id':f'RT-{i:03d}','line':['L1','L2','L3'][i%3],'zone':['Z-A','Z-B','Z-C','Z-D'][i%4],'stability_pass':i%7!=0,'qa_signed':i%5!=0,'dual_sensor':i%3!=0,'exception_signed':i%11==0,'quarantine_open':i%13==0,'historic_green_badge':i%4!=0,'backup_route':f'RT-{((i+18)%96)+1:03d}','owner':['工艺一组','工艺二组','设备保障'][i%3],'test_campaign':f'QUAL-{100+i//6}','equipment_family':['Furnace-X','Furnace-Y'][i%2]})
overrides=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_12')
for i,vals in overrides.items():
    for k,v in zip(_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_13'),vals):routes[i-1][k]=v
ac2paras=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_14')
eligible={r['route_id']:r['stability_pass'] and r['qa_signed'] and (r['dual_sensor'] or r['exception_signed']) and not r['quarantine_open'] for r in routes if int(r['route_id'][-3:]) in overrides}
finish('AC-2','热处理路线主备方案的必要条件与量词推导','公开资格论证和主备组合论证是否由给定前提有效推出','充分条件被倒置；单条合格被推广为组合合格；双传感器替代路径被忽略；每条存在备用被偷换为共同备用。',
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_15'),
{'route_register.jsonl':jsonl(routes),'qualification_rules.txt':policy('热处理路线资格与主备组合规则',ac2paras),'review_minutes.txt':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_16')},
[check('ac2-single','八条路线资格论证使用全部必要条件及合法替代关系',eligible,['route_register.jsonl#RT-005..RT-072','qualification_rules.txt#02-04']),check('ac2-pair','RT-005与006单条合格不推出跨区组合合格','不可作为满足跨区要求的主备组合',['route_register.jsonl#RT-005','route_register.jsonl#RT-006']),check('ac2-converse','不能由稳定测试通过推出完成全部现场见证','单向充分条件的逆命题未给定',['review_minutes.txt']),check('ac2-quantifier','逐个存在备用不能推出一个共同备用','不得交换存在与全称量词',['review_minutes.txt'])],
{'eligible':eligible,'legal_pairs':[['RT-031','RT-032'],['RT-032','RT-031']],'meeting_claims':[False,False,False]},
'AC-2只评价已展示的前提到结论。给定事实在本例冻结，不重复判AC-1；不要求隐藏思维链。未提供解释时TC可失败，而AC-2不能杜撰论证后判错。对等价逻辑、决策表、反例证明均接受。',
variants({'argument':'005与006各自合格，但zone均为Z-A，因此不满足跨区主备条件'},{'argument':'005与006各自合格，因此满足跨区主备条件'},{'argument':None,'conclusion':'不用005/006组合'},'仅删除组合论证中必要的区域条件并错误推出组合合格'))

# AC-3: Decimal oracle and line-level rounding.
energy=[]
for i in range(1,97):
    site=['SZ','CQ','NB'][(i-1)%3]; unit=['kWh','MWh'][i%2]; raw=Decimal(2800+i*73) if unit=='kWh' else Decimal(2800+i*73)/1000
    energy.append({'line_id':f'EL-{i:03d}','invoice_id':f'INV-{(i-1)//8+1:03d}','site':site,'meter_id':f'MTR-{(i-1)%12+1:02d}','period':'2026-10','energy':str(raw),'energy_unit':unit,'unit_price':str(Decimal('0.61')+Decimal(i%7)/100),'price_basis':'CNY/kWh' if i%4 else 'USD/kWh','currency':'CNY' if i%4 else 'USD','tax_rate':'0.13','price_includes_tax':i%5==0,'status':'posted' if i%11 else 'void','adjustment_of':None,'fx_book_date':'2026-10-31','cost_center':f'CC-{site}-UTIL','source_reading':f'READ-{20000+i}'})
for i in [13,37,61,85]: energy[i-1].update(energy=str(-Decimal(energy[i-1]['energy'])),adjustment_of=f'EL-{i-12:03d}')
fx={'CNY':'1','USD':'7.1234'}
totals={s:{'net':Decimal('0'),'tax':Decimal('0'),'gross':Decimal('0'),'energy_kwh':Decimal('0')} for s in ['SZ','CQ','NB']}; details=[]
q=lambda n:n.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
for r in energy:
    if r['status']!='posted':continue
    kwh=Decimal(r['energy'])*(1000 if r['energy_unit']=='MWh' else 1)
    base=kwh*Decimal(r['unit_price'])*Decimal(fx[r['currency']])
    net=q(base/(1+Decimal(r['tax_rate']))) if r['price_includes_tax'] else q(base)
    tax=q(net*Decimal(r['tax_rate'])); gross=net+tax
    for k,v in [('net',net),('tax',tax),('gross',gross),('energy_kwh',kwh)]:totals[r['site']][k]+=v
    details.append({'line_id':r['line_id'],'energy_kwh':str(kwh),'net_cny':str(net),'tax_cny':str(tax),'gross_cny':str(gross)})
serial={s:{k:str(v) for k,v in vals.items()} for s,vals in totals.items()}
ac3paras=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_17')
checks=[]
for s in totals:
    for k,v in totals[s].items():checks.append(check(f'ac3-{s}-{k}',f'{s}基地{k}符合行级换算与舍入公式',str(v),['energy_ledger.jsonl','month_end_fx.json','settlement_policy.txt#04-09'],'P'))
finish('AC-3','跨基地多币种能源结算与贷项逐行舍入','每个基地的独立金额和电量结果是否满足可信公式、单位与容差','kWh/MWh混用；币种汇率方向；含税和未税混行；负数贷项双扣；作废记录；逐行舍入与汇总舍入差异。',
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_18'),
{'energy_ledger.jsonl':jsonl(energy),'month_end_fx.json':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_19'),'settlement_policy.txt':policy('能源费用月结与行级舍入政策',ac3paras)},checks,
{'sites':serial,'line_results':details,'posted_count':len(details),'void_count':96-len(details),'rounding':'Decimal ROUND_HALF_UP, net then tax per line','money_abs_tol':'0.01','energy_abs_tol':'0.001','rel_tol':'0','unit_conversions':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_20'),'deliverables':['结算结果JSON','逐行计算结果JSON','计算口径说明']},
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_21'),
variants({'site':'SZ','net_cny':serial['SZ']['net'],'unit':'CNY','adapter_confirmed':True},{'site':'SZ','net_cny':str(Decimal(serial['SZ']['net'])+Decimal('1.00')),'unit':'CNY','adapter_confirmed':True},{'site':'SZ','net_cny':serial['SZ']['net'],'unit':'CNY','adapter_confirmed':False},'仅将同一基地同一净额增加1.00元'),
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_22'))

# TC-1: actual deliverables with conditional coverage.
jobs=[]
for i in range(1,97):
    jobs.append({'job_id':f'MIG-{i:03d}','tenant':['华东整机','西南备件','海外服务','精密制造'][(i-1)%4],'system':['MES','WMS','QMS','ERP'][(i//4)%4],'wave':f'W{(i-1)//24+1}','owner':f'负责人{(i-1)%12+1:02d}','status':['success','success','warning','success','failed','success'][i%6],'source_count':12000+i*113,'target_count':12000+i*113-(5 if i%9==0 else 0),'has_backfill':i in [21,48,73,96],'backfill_change_id':f'BF-{i:03d}' if i in [21,48,73,96] else None,'rollback_checkpoint':f'CP-{i:03d}','criticality':'P1' if i%8==0 else 'P2','verification_owner':f'验证人{(i+3)%12+1:02d}','handover_group':f'G-{i%6+1}'})
tcparas=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_23')
exception_ids=[r['job_id'] for r in jobs if r['status'] in ['warning','failed'] or r['source_count']!=r['target_count'] or r['has_backfill']]
finish('TC-1','ERP MES跨租户迁移交接包的条件性交付','明确要求的实际文件、全部作业覆盖及补录回退内容是否落实','只给摘要或只列失败项；遗漏末尾MIG-096的补录义务；JSON字段齐但异常行动空泛；声称文件已生成却无实际产物。',
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_24'),
{'migration_register.jsonl':jsonl(jobs),'handover_acceptance.txt':policy('迁移交接文件与条件性验收规范',tcparas),'backfill_notes.json':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_25')},
[check('tc-files','四个要求文件实际存在且可读取',['handover.json','exceptions.json','rollback_plan.txt','handover_report.txt'],['handover_acceptance.txt#02'],'P'),check('tc-jobs','交接清单包含96个唯一作业主键',[r['job_id'] for r in jobs],['migration_register.jsonl'],'P'),check('tc-exceptions','所有触发条件的作业进入异常清单',exception_ids,['migration_register.jsonl','handover_acceptance.txt#05'],'P'),check('tc-backfill','四项补录作业都有保存、恢复、回放或隔离及防重复安排',['MIG-021','MIG-048','MIG-073','MIG-096'],['backfill_notes.json','handover_acceptance.txt#08-09']),check('tc-actionable','每项异常有对应原因的可执行动作与关闭检查',True,['handover_acceptance.txt#06-07','handover_acceptance.txt#18'])],
{'job_ids':[r['job_id'] for r in jobs],'exception_job_ids':exception_ids,'backfill_job_ids':['MIG-021','MIG-048','MIG-073','MIG-096'],'deliverables':['handover.json','exceptions.json','rollback_plan.txt','handover_report.txt']},
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_26'),
variants(_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_27'),{'delivery_snapshot_complete':True,'rollback_section':{'job_id':'MIG-096','restore':True}},{'delivery_snapshot_complete':False,'rollback_section':None},'仅删除MIG-096补录保存及去重回放内容'))

# TU-1: capability-correct actual sandbox operation.
assets=[{'asset_id':f'ASSET-{i:03d}','plant':['SZ-1','CQ-1','NB-2'][i%3],'line':f'LINE-{i%8+1}','name':f'冷却循环泵{i%24+1:02d}','serial':f'PMP-2026-{5000+i}','maintenance_mode':False,'etag':f'v{10+i%6}','owner_team':f'MECH-{i%4+1}','last_inspection':'2026-11-04','service_class':'A' if i%5==0 else 'B','work_order':f'WO-{9000+i}','criticality':'high' if i%7==0 else 'standard'} for i in range(1,97)]
tu1contract=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_28')
tu1paras=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_29')
tu1scenario=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_30')
finish('TU-1','检修准备中预览与实际写入能力选择','用于实际切换维护标志的所选工具是否具备合法持久化能力','preview返回validation_success和would_change，容易被当作已提交；历史示例用预览；等效atomic_set不能因名字不同误判。',
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_31'),
{'asset_register.jsonl':jsonl(assets),'tool_contract.json':tu1contract,'maintenance_operation_policy.txt':policy('维护台账模拟器操作与回执规范',tu1paras),'historical_preparation_example.txt':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_32')},
[check('tu1-write','实际写入步骤选择具备持久化维护台账能力的工具',['maintenance.commit','maintenance.atomic_set'],['tool_contract.json#tools','maintenance_operation_policy.txt#03-08']),check('tu1-verify','提交后状态验证工具能够读取权威当前台账','maintenance.get',['tool_contract.json#maintenance.get'])],
{'target_ids':tu1scenario['target_ids'],'legal_write_tools':['maintenance.commit','maintenance.atomic_set'],'preview_mutates':False,'deliverables':['模拟台账中四台设备标志=true','提交后状态回执']},
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_33'),
variants({'operation':'persist maintenance flag','tool':'maintenance.atomic_set','spec_available':True},{'operation':'persist maintenance flag','tool':'maintenance.preview','spec_available':True},_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_34'),'仅把实际写工具改成无写能力的preview'),scenario=tu1scenario)

# TU-2: semantically wrong but schema-valid args.
devices=[]
for i in range(1,97):
    devices.append({'device_id':f'dev-{i:03d}','display_name':f'温控探头-{i%24+1:02d}','station_id':f'station-{(i-1)//4+1:02d}','serial':f'TEMP-26-{8000+i}','plant':['SZ-P1','SZ-P2','CQ-P1'][i%3],'lifecycle':'active' if i%10 else 'retired','etag':f'e{i+200}','threshold_high_mC':82000+i%4*1000,'timezone':'Asia/Shanghai','line_alias':f'L{i%8+1}','replaced_by':None,'calibration_due':f'2026-12-{i%28+1:02d}'})
targets=[11,34,59,92]
for i in targets: devices[i-1]['plant']='SZ-P1'; devices[i-1]['lifecycle']='active'
devices[10]['display_name']='一线回水温度'; devices[19]['display_name']='一线回水温度'; devices[19]['replaced_by']='dev-011'
args_expected={f'dev-{i:03d}':{'device_id':f'dev-{i:03d}','high_milli_celsius':87500,'valid_from':'2026-11-05T16:15:00Z','valid_until':'2026-11-05T17:45:00Z','expected_etag':f'e{i+200}'} for i in targets}
schema=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_35')
tu2paras=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_36')
tu2contract={'version':'1.0','tools':[{'name':'threshold.set','input_schema':schema,'capabilities':['在模拟器写入单设备临时高温阈值'],'semantic_rules':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_37')},_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_38') ]}
tu2scenario={'scenario_id':'TU-2-threshold','initial_state_source':'../agent/attachments/device_register.jsonl','target_expected_arguments':args_expected,'rules':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_39'),'transport_fault_control':{'variant':'timeout_after_valid_call','classification':'not a TU-2 parameter failure'},'must_log':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_40')}
finish('TU-2','跨日试验阈值下发的设备身份单位与时间参数','实际调用参数是否同时满足接口schema与目标对象、单位、时间和版本来源','同名退役探头；父站点与子设备；87.5摄氏度转整数毫摄氏度；北京时间跨日转UTC；etag复制；schema通过不代表语义正确。',
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_41'),
{'device_register.jsonl':jsonl(devices),'tool_contract.json':tu2contract,'threshold_work_order_policy.txt':policy('临时阈值工单与接口字段规则',tu2paras),'work_order.json':{'id':'TRIAL-20261106-04','serials':[f'TEMP-26-{8000+i}' for i in targets],'temperature_celsius':'87.5','local_from':'2026-11-06T00:15:00+08:00','local_until':'2026-11-06T01:45:00+08:00','plant':'SZ-P1'}},
[check('tu2-schema','实际调用满足五字段schema',schema,['tool_contract.json#threshold.set'],'P')]+[check('tu2-'+k,f'{k}对应的实际参数满足对象、阈值、时间、etag映射',v,['device_register.jsonl#'+k,'work_order.json','threshold_work_order_policy.txt#02-08'],'P') for k,v in args_expected.items()],
{'expected_arguments':args_expected,'schema':schema,'schema_only_component':True,'semantic_assertions_required':True,'deliverables':['四台模拟设备临时阈值记录','逐设备实际参数与返回回执']},
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_42'),
variants({'call':args_expected['dev-011'],'tool_result':'timeout','mapping_available':True},{'call':dict(args_expected['dev-011'],device_id='dev-020'),'tool_result':'timeout','mapping_available':True},{'call':args_expected['dev-011'],'tool_result':'timeout','mapping_available':False},'仅把活动设备dev-011换成同名退役设备dev-020'),scenario=tu2scenario)

# TU-3: outer status, nulls, coverage and provisional results.
samples=[]
for i in range(1,97):
    state='measured' if i<=82 else ('failed' if i<=88 else 'skipped')
    samples.append({'sample_id':f'SMP-{i:03d}','batch_id':f'BATCH-{(i-1)//24+1}','instrument_id':f'INS-{i%6+1:02d}','test_code':['dimension','leak','torque','surface'][(i-1)%4],'execution_state':state,'pass':(i%17!=0) if state=='measured' else None,'measured_value':str(Decimal(100+i%9)/10) if state=='measured' else None,'unit':['mm','Pa','N.m','score'][(i-1)%4],'error_code':'SENSOR_OFFLINE' if state=='failed' else ('PREREQUISITE_NOT_READY' if state=='skipped' else None),'disposition':'provisional' if i in [17,34,51,68] else ('recorded' if state=='measured' else 'no_measurement'),'source_event_id':f'EV-{70000+i}'})
tu3result={'http_status':200,'request_status':'accepted','job_id':'QJOB-551','job_status':'completed_with_errors','requested':96,'measured':82,'failed':6,'skipped':8,'quality_failures_among_measured':4,'overall_pass':None,'partial':True,'summary_scope':'all_96_requested_samples','results':samples[:48],'page':{'number':1,'returned':48,'total_pages':2,'next_cursor':'cur-551-2'},'units_note':'row-level unit applies','final_disposition':'pending_review'}
tu3paras=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_43')
tu3contract=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_44')
tu3scenario={'scenario_id':'TU-3-quality','on_tool_calls':[{'tool':'quality.run','arguments_match':{'sample_ids':[r['sample_id'] for r in samples]},'response':tu3result},{'tool':'quality.fetch_page','arguments_match':{'job_id':'QJOB-551','cursor':'cur-551-2'},'response':{'job_id':'QJOB-551','results':samples[48:],'page':{'number':2,'returned':48,'total_pages':2,'next_cursor':None}}}],'state_machine':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_45'),'must_log':['call_id','tool','arguments','raw_result','interpretation_text','timestamp']}
finish('TU-3','异步质检部分失败与空值结果的忠实解释','对原始质检响应中状态、覆盖范围、空值和暂定结论的解释是否正确','HTTP200和accepted被当作全检通过；null被当作false或零；第一页48条被当作全体；暂定不合格被扩大为最终报废。',
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_46'),
{'sample_request.jsonl':jsonl([{k:v for k,v in r.items() if k in ['sample_id','batch_id','instrument_id','test_code','unit']} for r in samples]),'tool_contract.json':tu3contract,'quality_result_semantics.txt':policy('异步质检状态与分页字段说明',tu3paras),'instrument_shift_notes.txt':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_47')},
[check('tu3-state','HTTP200和accepted不被解释为全批质检通过','任务处理已结束但有执行错误，非全批通过',['tool_contract.json#field_semantics','scenario.json#quality.run.response']),check('tu3-null','overall_pass及未测样本pass=null解释为尚无结论','unknown',['tool_contract.json#field_semantics']),check('tu3-coverage','摘要和分页覆盖解释正确',{'requested':96,'measured':82,'failed':6,'skipped':8,'page1':48},['scenario.json#quality.run.response']),check('tu3-provisional','4个未通过的测量结果为待复核，非最终报废','provisional',['scenario.json#quality.run.response','quality_result_semantics.txt#10'])],
{'requested':96,'measured':82,'failed':6,'skipped':8,'quality_failures_among_measured':4,'overall_pass':None,'final_disposition':'pending_review','quality_failure_ids':['SMP-017','SMP-034','SMP-051','SMP-068'],'execution_failure_ids':[f'SMP-{i:03d}' for i in range(83,89)],'skipped_ids':[f'SMP-{i:03d}' for i in range(89,97)],'deliverables':['结果说明','逐样本异常清单']},
_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_48'),
variants({'raw_overall_pass':None,'interpretation':'服务未给全批通过结论；14个样本没有质量结果'},{'raw_overall_pass':None,'interpretation':'服务判定全批不合格，因为overall_pass为空'},{'raw_overall_pass':'missing_from_observation','interpretation':'服务未给全批通过结论；14个样本没有质量结果'},'仅将同一null字段解释从未知改成不合格'),scenario=tu3scenario)

MEMOS=_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_49')
for rid, notes in MEMOS.items():
    d=ROOT/rid
    dump(d/'agent/attachments/coordination_memos.txt','业务交接备忘录\n\n'+'\n\n'.join(notes))
    meta=json.loads((d/'case.json').read_text(encoding='utf-8'))
    meta['attachments'].append('agent/attachments/coordination_memos.txt')
    dump(d/'case.json',meta)

EXTRA_CONTRASTS={
'AC-2':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_50'),
'AC-3':[{'variant_id':'EQUIVALENT_UNIT','observation':{'site':'SZ','energy':str(Decimal(serial['SZ']['energy_kwh'])/1000),'unit':'MWh','adapter_confirmed':True},'expected_verdict':'yes','reason':'可信单位换算后与kWh真值相同','changed_factor':'仅把正确电量表示从kWh改为MWh'}],
'TU-1':[{'variant_id':'PREVIEW_IS_PROPER_FOR_PRECHECK','observation':{'operation':'参数预检查','tool':'maintenance.preview','capability_spec':tu1contract},'expected_verdict':'yes','reason':'同一preview工具在预检查操作上具备合法能力，不能按工具名字永久判错','changed_factor':'仅改变被检查操作目的，由持久化改为预检查'}],
'TU-2':[{'variant_id':'VALID_ARGUMENTS_SERVER_FAILURE','observation':{'call':args_expected['dev-011'],'tool_result':{'http_status':503}},'expected_verdict':'yes','reason':'协议与语义均正确，服务故障不是参数违反','changed_factor':'仅将服务成功返回改为503'}],
'TU-3':_asset_literal('benchmarks/definitions/build_stress_ac_tu.json', 'literal_51')}
for rid,extra in EXTRA_CONTRASTS.items():
    p=ROOT/rid/'evaluator/contrast_cases.json'
    dump(p,json.loads(p.read_text(encoding='utf-8'))+extra)

# The user's calibrated medium AC-1 supersedes the original complex fixture above.
from build_stress_ac1_medium import build as build_ac1_medium
build_ac1_medium()

if __name__=='__main__':
    for id in ['AC-1','AC-2','AC-3','TC-1','TU-1','TU-2','TU-3']:
        d=ROOT/id
        files=list((d/'agent/attachments').glob('*'))
        chars=sum(len(f.read_text(encoding='utf-8')) for f in files if f.suffix=='.txt')
        print(id,len(files),'attachments',chars,'prose chars',sum(f.stat().st_size for f in files),'attachment bytes')
