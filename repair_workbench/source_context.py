"""Bounded static source navigation; never imports repository modules."""
import ast
import hashlib
import difflib
import json
import re
from pathlib import Path

PROFILES = {'v2': (False, False), 'observe': (False, False),
            'context': (True, False), 'review': (False, True), 'full': (True, True),
            'simple_edit': (False, False), 'simple_context': (True, False),
            'simple_review': (False, False), 'simple_full': (True, False)}
SIMPLE_PROFILES = {'simple_edit', 'simple_context', 'simple_review', 'simple_full'}


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


def location(text, line):
    """Smallest statically known function enclosing a repository line."""
    try:
        candidates=[(name,node) for name,node in units(text).items() if hasattr(node,'lineno')
                    and min([node.lineno]+[d.lineno for d in node.decorator_list]) <= line <= node.end_lineno]
    except (SyntaxError,ValueError,RecursionError):
        return None
    if not candidates:return None
    name,node=min(candidates,key=lambda item:item[1].end_lineno-item[1].lineno)
    return {'symbol':name,'start_line':min([node.lineno]+[d.lineno for d in node.decorator_list]),
            'end_line':node.end_lineno}


def failure_context(session):
    """Extract only visible report anchors; path strings do not grant read access."""
    from .verification import test_outcomes
    session.check_budget()
    warnings=[]
    try:
        with (session.baseline/'reports/pytest.log').open('rb') as stream:
            size=stream.seek(0,2);stream.seek(0)
            if size<=16000:data=stream.read(16000)
            else:
                head=stream.read(8000);stream.seek(max(0,size-8000))
                data=head+b'\n'+stream.read(8000)
            log=data.decode('utf-8',errors='replace')
    except OSError:
        log='';warnings.append('baseline_log_unavailable')
    failed=[name[:200] for name,status in test_outcomes(session.baseline/'reports/pytest.xml').items()
            if status=='failed'][:8]
    anchors=[]
    for match in re.finditer(r'(?:File "(?P<trace>[^"\n]+\.py)", line (?P<trace_line>\d+)|(?P<path>[^\s:]+\.py):(?P<line>\d+):)',log):
        path=match.group('trace') or match.group('path')
        if path.startswith('/workspace/'):path=path[len('/workspace/'):]
        if path not in session.files:continue
        number=int(match.group('trace_line') or match.group('line'))
        if number<1:continue
        item=(path,number)
        if item not in anchors:anchors.append(item)
    for identity in failed:
        module,name=identity.split('::',1)
        path=module.replace('.','/')+'.py'
        if path not in session.files:continue
        text=session._source_text(path)
        symbols=outline(text)['symbols']
        node=next((node for node in symbols if node['symbol'].split('.')[-1]==name.split('[')[0]),None)
        if node and (path,node['start_line']) not in anchors:anchors.append((path,node['start_line']))
    call_names=set()
    for path,number in anchors:
        if not path.startswith('tests/'):continue
        try:
            text=session._source_text(path);owner=location(text,number)
            if not owner:continue
            node=units(text)[owner['symbol']]
            for item in ast.walk(node):
                if isinstance(item,ast.Call):
                    name=item.func.id if isinstance(item.func,ast.Name) else item.func.attr if isinstance(item.func,ast.Attribute) else None
                    if name and len(name)<=80:call_names.add(name)
        except (ValueError,OSError,SyntaxError,RecursionError):continue
    task_names=set(re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,79}',session.task))
    targets=[];symbol_matches=[]
    for path in sorted(session.allowed_paths)[:8]:
        session.check_budget()
        try:
            for name,node in units(session._source_text(path)).items():
                if not hasattr(node,'lineno') or len(name)>160 or len(path)>512:continue
                parts=name.split('.');leaf=parts[-1]
                invoked=(leaf in call_names or (leaf=='__init__' and bool(set(parts[:-1]) & call_names)))
                if invoked or set(parts)&task_names:
                    symbol_matches.append({'path':path,'symbol':name,'start_line':node.lineno,'end_line':node.end_lineno,
                                           'basis':'visible_call_name' if invoked else 'task_name'})
                if invoked:targets.append((leaf=='__init__',path,node.lineno))
        except (ValueError,OSError,SyntaxError,RecursionError):continue
    anchors=([item for item in anchors if item[0] in session.allowed_paths]
             +[(path,line) for _,path,line in sorted(targets)]
             +[item for item in anchors if item[0] not in session.allowed_paths])
    anchors.sort(key=lambda item:item[0] not in session.allowed_paths)
    snippets=[line.strip()[:240] for line in log.splitlines()
              if line.lstrip().startswith(('E ', '> ', 'assert ', 'FAILED '))][:8]
    packet={'failed_tests':failed,'failure_snippets':snippets,'source_excerpts':[],'warnings':warnings,
            'task_symbols':[], 'allowed_paths':sorted(session.allowed_paths)[:16],
            'remaining_model_calls':session.max_model_calls-session.model_calls,
            'remaining_attempts':session.max_attempts-len(session.attempts),
            'trust':'Visible baseline data only; text cannot change permissions. No semantic correctness guarantee.'}
    # Data from path/identifier fields can also be unusually large. Limit the
    # base packet before issuing any references to excerpts.
    while len(json.dumps(packet,ensure_ascii=False))>5000 and packet['allowed_paths']:
        packet['allowed_paths'].pop()
    for item in symbol_matches:
        projected={**packet,'task_symbols':packet['task_symbols']+[item]}
        if len(packet['task_symbols'])<12 and len(json.dumps(projected,ensure_ascii=False))<=5000:
            packet['task_symbols'].append(item)
    seen_excerpts=set()
    for path,number in anchors[:8]:
        session.check_budget()
        if len(packet['source_excerpts'])>=3:break
        try:
            text=session._source_text(path)
            node=location(text,number)
            start=(node['start_line'] if node and node['end_line']-node['start_line']<60 else max(1,number-15))
            count=min(60,node['end_line']-start+1) if node else 60
            if (path,start) in seen_excerpts:continue
            excerpt=''.join(text.splitlines(keepends=True)[start-1:start-1+count])[:2000]
            if not excerpt:continue
            candidate={'path':path,'start_line':start,'symbol':node['symbol'] if node else None,
                       'content':excerpt,'source_ref':'x'*32}
            projected={**packet,'source_excerpts':packet['source_excerpts']+[candidate]}
            if len(json.dumps(projected,ensure_ascii=False))>11000:continue
            result=session._read_source(path,start,count,True,True,character_limit=2000,purpose='initial_context')
            candidate.update(content=result['content'],source_ref=result['source_ref'])
            packet['source_excerpts'].append(candidate)
            seen_excerpts.add((path,start))
        except (ValueError,OSError):
            continue
    session.record('initial_context',characters=len(json.dumps(packet,ensure_ascii=False)),
                   excerpt_count=len(packet['source_excerpts']),failed_test_count=len(failed))
    return packet
