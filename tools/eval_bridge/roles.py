"""Independent model-role configuration for runtime instantiation and scoring."""
from __future__ import annotations

import copy
import json
from pathlib import Path


MODEL_ROLES = frozenset({'instantiate', 'score'})


def load_role_config(path, role):
    """Resolve exactly one role; linked config paths are relative to this file."""
    if role not in MODEL_ROLES:
        raise ValueError('Unknown model role: ' + str(role))
    path = Path(path).resolve()
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(document, dict):
        raise ValueError('Model-role config must be an object')
    roles = document.get('roles', document)
    if not isinstance(roles, dict) or role not in roles:
        raise ValueError('Model-role config is missing role: ' + role)
    override = roles[role]
    if not isinstance(override, dict):
        raise ValueError('Model role must be an object: ' + role)
    resolved = {}
    if 'config_path' in override:
        linked = override['config_path']
        if not isinstance(linked, str) or not linked.strip():
            raise ValueError('config_path must be a nonempty string')
        linked = Path(linked)
        if not linked.is_absolute():
            linked = path.parent / linked
        resolved = json.loads(linked.read_text(encoding='utf-8-sig'))
        if not isinstance(resolved, dict):
            raise ValueError('Linked API config must be an object')
    resolved.update({key: copy.deepcopy(value) for key, value in override.items() if key != 'config_path'})
    for field in ('base_url', 'api_key', 'model'):
        if not isinstance(resolved.get(field), str) or not resolved[field].strip():
            raise ValueError('Model role ' + role + ' needs ' + field)
    return resolved

