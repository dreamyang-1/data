"""Compile bounded numeric syntax to data operations, without eval or code execution."""
import ast
import re

from app.numeric_operations import UNARY, BINARY, literal, parse_literal_binding, validate_derive


def constant_pipeline(spec):
    """Prove every referenced column was created from literals in this pipeline."""
    if not isinstance(spec, dict) or set(spec) != {'operation'}:
        return None
    operation = spec['operation']
    if not isinstance(operation, dict) or operation.get('type') != 'pipeline':
        return None
    steps = operation.get('operations')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 5:
        return None
    columns = []
    try:
        for index, step in enumerate(steps):
            if step.get('type') == 'derive':
                roles = validate_derive(step)
                for role in roles:
                    key = 'field' if role == 'input' else role + '_field'
                    if key in step and step[key] not in columns:
                        return None
                if step['result_field'] in columns:
                    return None
                columns.append(step['result_field'])
            elif step.get('type') == 'select' and index == len(steps)-1:
                selected = step.get('columns')
                if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected):
                    return None
                if any(value not in columns for value in selected):
                    return None
                columns = selected
            else:
                return None
    except (ValueError, TypeError, AttributeError):
        return None
    return columns


def parse_literal_computation(question):
    """Keep simple bindings stable; recognize only fully numeric compound requests."""
    simple = parse_literal_binding(question)
    if simple:
        return {'是否查询数据库': '否', '计算绑定': simple}
    if not isinstance(question, str) or len(question) > 512:
        return None
    text = question.strip().strip('。？！!? ')
    text = re.sub(r'^(?:(?:请|帮我|计算一下|算一下|计算|求出|求|算|分别)[，,\s]*)+', '', text)
    text = re.sub(r'[，,]\s*(?:按先括号后乘除计算|并说明为什么)$', '', text)
    # Split independent goals only outside parentheses/function arguments.
    text = text.replace('（', '(').replace('）', ')')
    pieces = []; start = depth = 0
    for match in re.finditer(r'[()]|、|以及|和', text):
        if match[0] == '(':
            depth += 1
        elif match[0] == ')':
            depth -= 1
        elif depth == 0:
            pieces.append(text[start:match.start()]); start = match.end()
    pieces.append(text[start:])
    steps = []; outputs = []
    binary = {ast.Add:'add', ast.Sub:'subtract', ast.Mult:'multiply', ast.Div:'divide',
              ast.Pow:'power', ast.Mod:'modulo', ast.FloorDiv:'floor_divide'}
    atom = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
    def emit(op, operands, **options):
        if len(steps) >= 4:
            raise ValueError('numeric pipeline limit')
        step = {'type':'derive', 'operator':op, 'result_field':f'计算步骤{len(steps)+1}', **options}
        roles = [''] if op in UNARY else ['left_', 'right_']
        for role, operand in zip(roles, operands):
            step[role + ('field' if isinstance(operand, str) else 'value')] = operand
        validate_derive(step); steps.append(step)
        return step['result_field']
    def compile_node(node):
        if isinstance(node, ast.Constant):
            literal(node.value); return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = compile_node(node.operand)
            if not isinstance(value, str):
                return value if isinstance(node.op, ast.UAdd) else -value
            return value if isinstance(node.op, ast.UAdd) else emit('negate', [value])
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            return emit(binary[type(node.op)], [compile_node(node.left), compile_node(node.right)])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            op = {'pow':'power', 'mod':'modulo'}.get(node.func.id, node.func.id)
            if op == 'round' and len(node.args) == 2:
                digits = node.args[1]
                if not isinstance(digits, ast.Constant) or type(digits.value) is not int:
                    raise ValueError('round digits must be a literal')
                return emit(op, [compile_node(node.args[0])], digits=digits.value)
            if op in UNARY and len(node.args) == 1 or op in BINARY and len(node.args) == 2:
                return emit(op, [compile_node(arg) for arg in node.args])
        raise ValueError('unsupported numeric syntax')
    try:
        for piece in pieces:
            piece = piece.strip(); digits = None
            rounded = re.search(r'[，,]?\s*(?:结果)?(?:四舍五入)?保留([0-9]+|[零一二两三四五六七八九十])位小数$', piece)
            if rounded:
                word = rounded[1]; digits = int(word) if word.isdigit() else {'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[word]
                piece = piece[:rounded.start()]
            # “先加，再除” must execute sequentially rather than changing precedence.
            clauses = re.split(r'[，,]\s*(?:然后|接着|再)', piece)
            piece = clauses[0]
            for clause in clauses[1:]:
                piece = '(' + piece + ')' + clause
            for phrase, operator in [('加上','+'),('减去','-'),('乘以','*'),('除以','/'),('加','+'),('减','-')]:
                piece = piece.replace(phrase, operator)
            for phrase, function in [('平方根','sqrt'),('立方根','cbrt'),('平方','square'),('立方','cube'),('阶乘','factorial'),('绝对值','abs'),('倒数','reciprocal')]:
                piece = re.sub('('+atom+r')的?'+phrase, lambda m: function+'('+m[1]+')', piece)
            piece = re.sub('('+atom+r')的('+atom+r')次方', r'power(\1,\2)', piece)
            piece = piece.replace('×','*').replace('÷','/').replace('^','**')
            tree = ast.parse(piece.strip(), mode='eval')
            if len(list(ast.walk(tree))) > 64:
                return None
            result = compile_node(tree.body)
            if digits is not None:
                result = emit('round', [result], digits=digits)
            if not isinstance(result, str):
                return None
            label = '结果' if len(pieces) == 1 else f'结果{len(outputs)+1}'
            steps[-1]['result_field'] = label; outputs.append(label)
        operation = {'type':'pipeline', 'operations':[*steps, {'type':'select', 'columns':outputs}]}
        spec = {'operation':operation}
        return {'是否查询数据库':'否', '结果操作':spec} if constant_pipeline(spec) else None
    except (ValueError, TypeError, SyntaxError, RecursionError, OverflowError):
        return None
