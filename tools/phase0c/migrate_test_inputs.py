"""Mechanical migration: add explicit model 81 to previously unscoped fixtures.

No production defaults, assertion changes, monkeypatch of validation, or model
calls. The manifest permits review of every inserted fixture scope.
"""
import ast
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def main():
    manifest=[]
    for path in sorted((ROOT/'tests').glob('test_*.py')):
        if 'phase0c' in path.name:
            continue
        text=path.read_text(encoding='utf-8')
        tree=ast.parse(text)
        lines=text.splitlines(keepends=True)
        def offset(node):
            # AST columns are UTF-8 byte offsets; preserve Chinese fixtures.
            prefix=lines[node.lineno-1].encode('utf-8')[:node.col_offset].decode('utf-8')
            return sum(map(len,lines[:node.lineno-1]))+len(prefix)
        edits=[]
        for n in ast.walk(tree):
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='ChatRequest':
                if not any(k.arg in {None,'semantic_model_id'} for k in n.keywords):
                    start=text.index('(',offset(n))+1
                    edits.append((start,'semantic_model_id=81, ',n.lineno,'ChatRequest'))
            elif isinstance(n,ast.Dict):
                keys={k.value for k in n.keys if isinstance(k,ast.Constant) and isinstance(k.value,str)}
                if {'application_id','conversation_id','message_id','question'} <= keys and 'semantic_model_id' not in keys:
                    edits.append((offset(n)+1,'"semantic_model_id": 81, ',n.lineno,'HTTP fixture'))
        for start,insert,_,_ in sorted(edits,reverse=True):
            text=text[:start]+insert+text[start:]
        if edits:
            path.write_text(text,encoding='utf-8',newline='\n')
            manifest.append({'path':path.relative_to(ROOT).as_posix(),'insertions':[{'line':line,'kind':kind,'semantic_model_id':81} for _,_,line,kind in edits]})
    output=ROOT/'docs/phase0c/test_input_migration.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print('FILES',len(manifest),'EXPLICIT_FIXTURES',sum(len(r['insertions']) for r in manifest))


if __name__=='__main__': main()
