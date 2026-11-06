"""Required editable resources. Missing/malformed files fail explicitly."""
from pathlib import Path
import json
PROJECT = Path(__file__).resolve().parents[2]
def decode(v):
    if not isinstance(v,dict) or set(v)!={'type','value'}: raise ValueError('Invalid resource value')
    t,x=v['type'],v['value']
    if t=='dict': return {decode(k):decode(y) for k,y in x}
    if t=='tuple': return tuple(map(decode,x))
    if t=='set': return set(map(decode,x))
    if t=='list': return list(map(decode,x))
    if t=='scalar': return x
    raise ValueError('Invalid resource type: '+str(t))
def literal(relative,key):
    data=json.loads((PROJECT/relative).read_text(encoding='utf-8'))
    if data.get('schema')!='aims-literals/1': raise ValueError('Invalid resource schema: '+relative)
    return decode(data['values'][key])
def text(relative): return (PROJECT/relative).read_text(encoding='utf-8')
