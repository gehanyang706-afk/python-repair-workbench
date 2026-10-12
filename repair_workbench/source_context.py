"""Bounded static source navigation; never imports repository modules."""
import ast
import hashlib
import difflib
from pathlib import Path

PROFILES = {'v2': (False, False), 'observe': (False, False),
            'context': (True, False), 'review': (False, True), 'full': (True, True)}


def units(text):
    tree = ast.parse(text)
    result = {}
    def walk(body, prefix=''):
        scaffold = []
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if prefix + node.name in result:
                    raise ValueError('ambiguous duplicate source symbol')
                result[prefix + node.name] = node
            elif isinstance(node, ast.ClassDef):
                if prefix + node.name + '.__scope__' in result:
                    raise ValueError('ambiguous duplicate class symbol')
                walk(node.body, prefix + node.name + '.')
                header = ast.ClassDef(name=node.name, bases=node.bases, keywords=node.keywords,
                    body=[], decorator_list=node.decorator_list, type_params=getattr(node,"type_params",[]))
                scaffold.append(header)
            else:
                scaffold.append(node)
        result[prefix + '__scope__'] = ast.Module(body=scaffold, type_ignores=[])
    walk(tree.body)
    return result


def outline(text):
    try:
        parsed = units(text)
    except (SyntaxError, ValueError, RecursionError):
        return {'symbols': [], 'parse_error': True}
    return {'symbols': [{'symbol': name, 'start_line': min([node.lineno]+[d.lineno for d in node.decorator_list]), 'end_line': node.end_lineno}
                        for name, node in parsed.items() if hasattr(node, 'lineno')][:100],
            'parse_error': False, 'truncated': sum(hasattr(n,'lineno') for n in parsed.values()) > 100}


def change_review(original, candidate, path):
    before, after = units(original), units(candidate)
    changes, risks = [], []
    for name in sorted(before.keys() | after.keys()):
        a, b = before.get(name), after.get(name)
        if a is not None and b is not None and ast.dump(a) == ast.dump(b):
            continue
        changes.append({'path':path, 'symbol':name})
        kinds = []
        if a is None or b is None:
            kinds.append('definition_added_or_removed')
        elif name.endswith('__scope__'):
            kinds.append('module_or_class_state_changed')
        else:
            if type(a) is not type(b) or ast.dump(a.returns or ast.Constant(None)) != ast.dump(b.returns or ast.Constant(None)) or ast.dump(a.args) != ast.dump(b.args) or ast.dump(ast.Module(body=a.decorator_list,type_ignores=[])) != ast.dump(ast.Module(body=b.decorator_list,type_ignores=[])):
                kinds.append('signature_changed')
            for node_type, kind in [(ast.Compare,'comparison_changed'), (ast.Raise,'exception_changed')]:
                old=[ast.dump(n)[:1200] for n in ast.walk(a) if isinstance(n,node_type)]
                new=[ast.dump(n)[:1200] for n in ast.walk(b) if isinstance(n,node_type)]
                if old != new:
                    risks.append({'kind':kind,'path':path,'symbol':name,
                                  'before':old[:12], 'after':new[:12]})
        risks.extend({'kind':kind,'path':path,'symbol':name} for kind in kinds)
    # AST alone ignores comments/formatting outside the edited function.
    # Review these textual hunks too, without executing untrusted code.
    owned = set()
    for tag,i,j,_,_ in difflib.SequenceMatcher(a=original.splitlines(),b=candidate.splitlines(),autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        for line in range(i+1,max(i+2,j+1)):
            owner = next((name for name,node in before.items() if hasattr(node,'lineno')
                          and min([node.lineno]+[d.lineno for d in node.decorator_list]) <= line <= node.end_lineno), '__scope__')
            owned.add(owner)
    existing={x['symbol'] for x in changes}
    for name in sorted(owned-existing):
        changes.append({'path':path,'symbol':name})
        risks.append({'kind':'text_only_change','path':path,'symbol':name})
    if original != candidate and not changes:
        risks.append({'kind':'text_only_change','path':path,'symbol':'__scope__'})
        changes.append({'path':path,'symbol':'__scope__'})
    return changes, risks


def receipt(text):
    return hashlib.sha256(text.encode()).hexdigest()
