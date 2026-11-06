"""Resource paths for the installed engine and explicitly supplied alternate libraries."""
from pathlib import Path
ENGINE=Path(__file__).resolve().parents[1]
PROJECT=ENGINE.parents[1]
def resource(path,engine=None):
    base=ENGINE if engine is None else Path(engine).resolve()
    if base!=ENGINE: return base/path
    s=Path(path).as_posix()
    if s=='configs/metrics.yaml': return PROJECT/'metrics/metrics.yaml'
    if s=='configs/rubrics': return PROJECT/'rubrics/templates'
    if s.startswith('legacy/prompts/'): return PROJECT/'prompts/legacy'/s[len('legacy/prompts/'):]
    if s=='legacy/prompts': return PROJECT/'prompts/legacy'
    if s.startswith('prompts/'): return PROJECT/'prompts/runtime'/s[len('prompts/'):]
    if s=='prompts': return PROJECT/'prompts/runtime'
    return base/path
def resource_files():
    return sorted([PROJECT/'metrics/metrics.yaml',*(PROJECT/'rubrics').rglob('*.*'),*(PROJECT/'prompts').rglob('*.*')])
