"""Package evaluator-only annotated references without changing the task suite."""
from eval_bridge.resources import literal as _fragment
from pathlib import Path as _WorkspacePath
import os as _workspace_os
if not _workspace_os.environ.get('AIMS_BENCHMARK_WORKSPACE'):
    raise RuntimeError(_fragment('benchmarks/templates/package_rubric_annotations.json', 'fragment_1'))
_workspace = _WorkspacePath(_workspace_os.environ['AIMS_BENCHMARK_WORKSPACE']).resolve()
from eval_bridge.resources import literal as _asset_literal
from pathlib import Path
import json, hashlib, shutil, zipfile, re

WORK=_workspace
SOURCE=WORK/'outputs'/'rubric_stress_suite_20261105'
ROOT=WORK/'outputs'/'rubric_annotated_answers_20261105'
IDS=_asset_literal('benchmarks/definitions/package_rubric_annotations.json', 'literal_1')
METHODS={cid:{'P','J','H'} for cid in IDS}
for cid in ['AC-2','TU-1','PE-1','EG-1','EG-3']:METHODS[cid]={'J'}
for cid in ['AC-3','BC-1']:METHODS[cid]={'P'}
for cid in ['PE-2','EG-2','ER-2']:METHODS[cid]={'P','J'}
SUMMARIES=_asset_literal('benchmarks/definitions/package_rubric_annotations.json', 'literal_2')

def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def require(condition,message):
    if not condition:raise AssertionError(message)

def main():
    required=_asset_literal('benchmarks/definitions/package_rubric_annotations.json', 'literal_3')
    checks_required={'id','criterion','method','expected','evidence_needed','yes','no','unverifiable'}
    entries=[];source_hashes={};parsed=0
    for cid in IDS:
        folder=ROOT/cid;metadata=read(folder/'annotation.json')
        require(required<=metadata.keys(),f'{cid} missing metadata fields: {required-metadata.keys()}')
        require(metadata['id']==cid,f'{cid} wrong ID')
        source_meta=read(SOURCE/'cases'/cid/'case.json')
        kind='static' if source_meta['mode']=='attachment_only' else 'process'
        require(metadata['answer_type']==kind,f'{cid} answer type')
        require(bool(metadata['core_answer']) and bool(metadata['checks']),f'{cid} empty answer')
        require(len((folder/'标注答案.md').read_text(encoding='utf-8'))>500,f'{cid} answer too short')
        for check in metadata['checks']:
            require(checks_required<=check.keys(),f'{cid} incomplete check: {check.get("id")}')
            allowed=METHODS[cid] | ({'P','J'} if '辅助' in check['criterion'] else set())
            require(check['method'] in allowed,f'{cid} invalid method')
            require(all(check[k] for k in ['criterion','evidence_needed','yes','no','unverifiable']),f'{cid} empty grading rule')
        require(metadata['source_files'],f'{cid} no sources')
        for name in metadata['source_files']:
            p=Path(name)
            require(p.is_absolute() and p.is_file(),f'{cid} missing source {name}')
            if SOURCE.resolve() in p.resolve().parents and 'cases' not in p.relative_to(SOURCE).parts:
                rel=p.relative_to(SOURCE)
                dest=ROOT/'依据快照'/'公共'/rel
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(p,dest)
                require(sha(dest)==sha(p),f'{cid} shared snapshot mismatch')
                source_hashes[f'公共/{rel.as_posix()}']=sha(p)
        # Freeze the exact task, attachments and original evaluator rules alongside the answer.
        case_source=SOURCE/'cases'/cid
        for p in sorted(case_source.rglob('*')):
            if not p.is_file() or '__pycache__' in p.parts:continue
            rel=p.relative_to(case_source);dest=ROOT/'依据快照'/cid/rel
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
            require(sha(dest)==sha(p),f'{cid} snapshot content mismatch')
            source_hashes[f'{cid}/{rel.as_posix()}']=sha(p)
        for p in folder.rglob('*'):
            if p.suffix=='.json':read(p);parsed+=1
            elif p.suffix=='.jsonl':
                for line in p.read_text(encoding='utf-8-sig').splitlines():
                    if line.strip():json.loads(line)
                parsed+=1
        files=[p.relative_to(ROOT).as_posix() for p in sorted(folder.rglob('*')) if p.is_file()]
        entries.append({'id':cid,'title':metadata['title'],'answer_type':kind,'answer':f'{cid}/标注答案.md',
                        'annotation':f'{cid}/annotation.json','core_answer':metadata['core_answer'],
                        'check_count':len(metadata['checks']),'files':files})
    require(len(entries)==21 and all(x['id']!='AC-1' for x in entries),'21 cases excluding AC-1')
    write(ROOT/'来源快照清单.json',{'source_suite':str(SOURCE),'algorithm':'SHA-256','files':source_hashes})
    write(ROOT/'标注索引.json',{'purpose':'评测者参考答案，不是被测Agent的实际运行评分','excluded':['AC-1'],'entries':entries})
    preface=_asset_literal('benchmarks/definitions/package_rubric_annotations.json', 'literal_4')
    overview='# 其余21项任务标注答案总览\n\n'+preface+'\n## 答案索引\n\n| Rubric | 任务 | 参考答案入口 | 核心结论 |\n| --- | --- | --- | --- |\n'
    for e in entries:
        summary=SUMMARIES[e['id']]
        summary=summary.replace('|','／').replace('\n',' ')
        overview+=f'| {e["id"]} | {e["title"]} | [标注答案]({e["answer"]}) | {summary} |\n'
    overview+=_asset_literal('benchmarks/definitions/package_rubric_annotations.json', 'literal_5')
    write(ROOT/'标注答案总览.md',overview)
    combined='# AC-1以外全部21项任务标注答案\n\n'+preface+'\n'
    for e in entries:
        text=(ROOT/e['answer']).read_text(encoding='utf-8')
        def relocate_link(match):
            target=match.group(1)
            if re.match(r'^(?:[A-Za-z]:|/|#|https?://)',target):return match.group(0)
            return ']('+e['id']+'/'+target.removeprefix('./')+')'
        text=re.sub(r'\]\(([^)]+)\)',relocate_link,text)
        # Keep each answer's hierarchy readable inside the combined volume.
        text=re.sub(r'^(#{1,5}) ',r'#\1 ',text,flags=re.M)
        combined+='\n'+text+'\n'
    write(ROOT/'全部21项标注答案.md',combined)
    write(ROOT/'README.md',_asset_literal('benchmarks/definitions/package_rubric_annotations.json', 'literal_6'))
    validation={'case_count':21,'excluded':['AC-1'],'actual_agentos_runs_scored':0,
                'annotation_fields_valid':True,'source_paths_exist':True,'source_snapshots_hash_match':True,
                'json_files_parsed':parsed,'check_count':sum(e['check_count'] for e in entries),
                'static_cases':[e['id'] for e in entries if e['answer_type']=='static'],
                'process_cases':[e['id'] for e in entries if e['answer_type']=='process']}
    write(ROOT/'validation_report.json',validation)
    hashes={p.relative_to(ROOT).as_posix():sha(p) for p in sorted(ROOT.rglob('*')) if p.is_file() and p.name!='checksums.json' and '__pycache__' not in p.parts}
    write(ROOT/'checksums.json',{'algorithm':'SHA-256','files':hashes})
    output=ROOT.parent/'除AC1外21项任务_标注答案与参考交付.zip'
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ROOT.rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts:z.write(p,Path(ROOT.name)/p.relative_to(ROOT))
    with zipfile.ZipFile(output) as z:
        require(z.testzip() is None,'ZIP CRC failure')
        require(not any('/AC-1/' in name for name in z.namelist()),'AC-1 unexpectedly included')
    print(json.dumps({'cases':21,'checks':validation['check_count'],'zip':str(output),'validation':'passed'},ensure_ascii=True))

if __name__=='__main__':main()
