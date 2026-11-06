"""Read AgentOS declarations without importing or running the application.

The JSON Schemas are projections of static declarations, not runtime exports.
Unsupported expressions fail closed; Python validators remain source evidence.
"""
from __future__ import annotations

import ast
import collections.abc
import copy
import datetime
import decimal
import hashlib
import pathlib
import typing

import pydantic
import pydantic.json_schema
import typing_extensions


class Unsupported(ValueError):
    pass


class SourceReader:
    def __init__(self, root: pathlib.Path):
        self.root = root.resolve()
        self.modules = {}
        self.cache = {}
        self.active = set()
        self.evidence = {}
        self.warnings = []

    def module(self, path):
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise Unsupported('source path escapes repository')
        if path not in self.modules:
            text = path.read_text(encoding='utf-8-sig')
            tree = ast.parse(text)
            names, imports = {}, {}
            for node in tree.body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    names[node.name] = node
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            names[target.id] = node.value
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names[node.target.id] = node.value
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        imports[alias.asname or alias.name] = (node.module, node.level, alias.name)
            self.modules[path] = (text, tree, names, imports)
        return self.modules[path]

    def record(self, path, node, kind):
        text, _, _, _ = self.module(path)
        key = (path.relative_to(self.root).as_posix(), node.lineno, kind)
        self.evidence[key] = {
            'source_path': key[0], 'line': node.lineno,
            'end_line': getattr(node, 'end_lineno', node.lineno), 'kind': kind,
            'source_file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'text': ast.get_source_segment(text, node),
        }

    def resolve(self, path, name):
        key = (path, name)
        if key in self.cache:
            return self.cache[key]
        if key in self.active:
            raise Unsupported(f'recursive declaration: {name}')
        builtin = {'str': str, 'int': int, 'float': float, 'bool': bool,
                   'dict': dict, 'list': list, 'tuple': tuple, 'set': set,
                   'bytes': bytes, 'object': object, 'None': None}
        if name in builtin:
            return builtin[name]
        _, _, names, imports = self.module(path)
        self.active.add(key)
        try:
            if name in names and names[name] is not None:
                node = names[name]
                if isinstance(node, ast.ClassDef):
                    value = self.model(path, node)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    raise Unsupported(f'function is not a declarative value: {name}')
                else:
                    value = self.value(path, node)
            elif name in imports:
                module, level, original = imports[name]
                allowed = {'typing': typing, 'typing_extensions': typing_extensions,
                           'pydantic': pydantic, 'pydantic.json_schema': pydantic.json_schema,
                           'collections.abc': collections.abc,
                           'datetime': datetime, 'decimal': decimal, 'pathlib': pathlib}
                if module in allowed and not level:
                    value = getattr(allowed[module], original)
                else:
                    base = path.parent if level else self.root
                    for _ in range(max(0, level - 1)):
                        base = base.parent
                    target = base.joinpath(*(module or '').split('.'))
                    target = target.with_suffix('.py') if target.with_suffix('.py').exists() else target / '__init__.py'
                    if not target.exists():
                        raise Unsupported(f'unavailable import {module}.{original}')
                    value = self.resolve(target.resolve(), original)
            else:
                raise Unsupported(f'unresolved name {name}')
            self.cache[key] = value
            return value
        finally:
            self.active.remove(key)

    def value(self, path, node):
        if node is None:
            raise Unsupported('missing declaration')
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.resolve(path, node.id)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self.value(path, v) for v in node.elts]
            return tuple(values) if isinstance(node, ast.Tuple) else set(values) if isinstance(node, ast.Set) else values
        if isinstance(node, ast.Dict):
            return {self.value(path, k): self.value(path, v) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = self.value(path, node.operand)
            return -v if isinstance(node.op, ast.USub) else +v
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self.value(path, node.left) | self.value(path, node.right)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(path, node.left), self.value(path, node.right)
            if isinstance(left, (str, int, float, list, tuple)) and type(left) is type(right):
                return left + right
            raise Unsupported('non-literal addition')
        if isinstance(node, ast.Subscript):
            return self.value(path, node.value)[self.value(path, node.slice)]
        if isinstance(node, ast.Lambda) and not node.args.args:
            v = self.value(path, node.body)
            return lambda: copy.deepcopy(v)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'model_json_schema' and not node.args and not node.keywords:
                model = self.value(path, node.func.value)
                if isinstance(model, type) and issubclass(model, pydantic.BaseModel):
                    return model.model_json_schema()
                raise Unsupported('schema call on non-model')
            fn = self.value(path, node.func)
            allowed = (pydantic.Field, pydantic.ConfigDict, pydantic.StringConstraints,
                       pydantic.AliasChoices, pydantic.AliasPath, typing_extensions.TypedDict,
                       list, dict, tuple, set, frozenset)
            if not any(fn is candidate for candidate in allowed):
                raise Unsupported('non-declarative function call')
            if any(k.arg is None for k in node.keywords):
                raise Unsupported('dynamic keyword arguments')
            return fn(*[self.value(path, a) for a in node.args],
                      **{k.arg: self.value(path, k.value) for k in node.keywords})
        raise Unsupported(f'unsupported expression: {ast.unparse(node)[:160]}')

    def model(self, path, node):
        self.record(path, node, 'model_declaration')
        bases = [self.value(path, b) for b in node.bases]
        is_typed_dict = any(b is typing_extensions.TypedDict or b is typing.TypedDict for b in bases)
        base = next((b for b in bases if isinstance(b, type) and issubclass(b, pydantic.BaseModel)), None)
        if base is None and not is_typed_dict:
            raise Unsupported(f'unsupported model base: {node.name}')
        fields, annotations, config = {}, {}, None
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                name = item.target.id
                if name.startswith('_') or name == 'model_config':
                    continue
                annotation = self.value(path, item.annotation)
                if typing.get_origin(annotation) is typing.ClassVar:
                    continue
                annotations[name] = annotation
                fields[name] = (annotation, self.value(path, item.value) if item.value else ...)
            elif isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'model_config' for t in item.targets):
                config = self.value(path, item.value)
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.warnings.append({'source_path': path.relative_to(self.root).as_posix(),
                                      'model': node.name, 'method': item.name,
                                      'reason': 'Python model methods/validators are preserved as evidence, not executed.'})
        if is_typed_dict:
            total = next((self.value(path, k.value) for k in node.keywords if k.arg == 'total'), True)
            return typing_extensions.TypedDict(node.name, annotations, total=total)
        kwargs = {'__base__': base}
        if config is not None:
            kwargs['__config__'] = config
        return pydantic.create_model(node.name, **kwargs, **fields)

    def schema(self, value):
        if isinstance(value, dict):
            return value
        return pydantic.TypeAdapter(value).json_schema()

    def function_schema(self, path, fn):
        fields = {}
        args = fn.args.posonlyargs + fn.args.args
        defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
        args += fn.args.kwonlyargs
        defaults += fn.args.kw_defaults
        injected = []
        for arg, default in zip(args, defaults):
            annotation_text = ast.unparse(arg.annotation) if arg.annotation else ''
            if arg.arg in ('self', 'cls') or any(t in annotation_text for t in ('RunnableConfig', 'InjectedToolCallId', 'InjectedState', 'ToolRuntime')):
                injected.append(arg.arg)
                continue
            annotation = self.value(path, arg.annotation)
            if typing.get_origin(annotation) is typing.Annotated:
                values = typing.get_args(annotation)
                annotation = typing.Annotated[values[0], *[
                    pydantic.Field(description=v) if isinstance(v, str) else v for v in values[1:]]]
            fields[arg.arg] = (annotation, self.value(path, default) if default is not None else ...)
        model = pydantic.create_model(fn.name + 'Input', **fields)
        return model.model_json_schema(), injected

    def export_tools(self):
        records = {}
        roots = ['plugins/tools', 'core/meta_agent', 'app/web/services/chat/native',
                 'app/web/services/agent_builder']
        files = sorted({p for root in roots for p in (self.root / root).rglob('*.py')})
        for path in files:
            try:
                text, tree, _, _ = self.module(path)
            except (SyntaxError, UnicodeError):
                continue
            plugin_metadata = []
            for call in ast.walk(tree):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'PluginMetadata':
                    data = {}
                    for k in call.keywords:
                        if k.arg in ('name', 'version', 'display_name', 'limitations', 'examples', 'meta_agent_access', 'side_effect', 'dependencies'):
                            try:
                                data[k.arg] = self.value(path, k.value)
                            except Exception:
                                pass
                    plugin_metadata.append(data)
            candidates = []
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in fn.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    if isinstance(target, ast.Name) and target.id == 'tool':
                        name = fn.name
                        if isinstance(dec, ast.Call) and dec.args:
                            try: name = self.value(path, dec.args[0])
                            except Exception: continue
                        kw = {k.arg: k.value for k in dec.keywords} if isinstance(dec, ast.Call) else {}
                        candidates.append((name, fn, kw, fn))
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute) or call.func.attr != 'from_function':
                    continue
                if not isinstance(call.func.value, ast.Name) or call.func.value.id != 'StructuredTool':
                    continue
                kw = {k.arg: k.value for k in call.keywords}
                if 'name' not in kw or 'args_schema' not in kw:
                    continue
                try: name = self.value(path, kw['name'])
                except Exception: continue
                candidates.append((name, None, kw, call))
            for name, fn, kw, location in candidates:
                if not isinstance(name, str):
                    continue
                self.record(path, location, 'tool_declaration')
                rec = {'runtime_tool_name': name, 'description': None,
                       'input_schema': None, 'output_schema': None,
                       'schema_origin': 'source_declaration_projection',
                       'runtime_export_verified': False, 'deployed': None,
                       'historical_version_match': 'unverified',
                       'source_path': path.relative_to(self.root).as_posix(), 'source_line': location.lineno,
                       'plugin_metadata_candidates': plugin_metadata, 'gaps': []}
                try:
                    rec['description'] = self.value(path, kw['description']) if 'description' in kw else ast.get_docstring(fn) if fn else None
                except Exception as exc:
                    rec['gaps'].append('description: ' + str(exc))
                try:
                    if 'args_schema' in kw:
                        expr = kw['args_schema']
                        if isinstance(expr, ast.IfExp):
                            rec['input_schema_variants'] = [
                                {'condition': ast.unparse(expr.test), 'branch': 'true', 'schema': self.schema(self.value(path, expr.body))},
                                {'condition': ast.unparse(expr.test), 'branch': 'false', 'schema': self.schema(self.value(path, expr.orelse))}]
                            rec['gaps'].append('Actual runtime branch is unknown; no default variant selected.')
                        else:
                            rec['input_schema'] = self.schema(self.value(path, expr))
                    elif fn:
                        rec['input_schema'], rec['injected_parameters'] = self.function_schema(path, fn)
                except Exception as exc:
                    rec['gaps'].append('input_schema: ' + str(exc))
                try:
                    if fn and fn.returns:
                        rec['output_schema'] = self.schema(self.value(path, fn.returns))
                    else:
                        rec['gaps'].append('Output declaration unavailable in the inspected tool binding.')
                except Exception as exc:
                    rec['gaps'].append('output_schema: ' + str(exc))
                rec['scope_note'] = '静态声明投影，不证明已部署、已授权或与历史运行一致；Python 校验与动态绑定需查原声明。'
                if name in records:
                    previous = records[name]
                    previous.setdefault('alternative_source_bindings', []).append(rec)
                else:
                    records[name] = rec
        return records
