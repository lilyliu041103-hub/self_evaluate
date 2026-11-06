from pathlib import Path
PROJECT=Path(__file__).resolve().parents[2]
ENGINE=PROJECT/'runtime/metric_rubric_impl_v4_modular'
def engine_resource(engine,path):
    engine=Path(engine).resolve()
    if engine!=ENGINE: return engine/path
    if path=='configs/rubrics':return PROJECT/'rubrics/templates'
    if path=='configs/metrics.yaml':return PROJECT/'metrics/metrics.yaml'
    return engine/path
def asset_files():
    return sorted(p for folder in ('metrics','rubrics','prompts') for p in (PROJECT/folder).rglob('*') if p.is_file())
