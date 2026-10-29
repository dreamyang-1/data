"""Finite, deterministic calculator operations shared by planning and execution.

No expressions or executable code are accepted. Constants are numeric literals;
field references continue to be resolved by the existing dataset contract.
"""
from __future__ import annotations

from decimal import Decimal, DecimalException, localcontext, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
import copy
import math
import re


UNARY = frozenset({'square','cube','sqrt','cbrt','abs','negate','reciprocal','sign',
    'exp','exp2','expm1','ln','log10','log2','log1p','floor','ceil','trunc','round','factorial',
    'sin','cos','tan','asin','acos','atan','sinh','cosh','tanh','asinh','acosh','atanh','degrees','radians','bit_not'})
BINARY = frozenset({'add','subtract','multiply','divide','power','root','modulo','floor_divide',
    'log','hypot','atan2','minimum','maximum','gcd','lcm','combination','permutation',
    'bit_and','bit_or','bit_xor','shift_left','shift_right'})
AGGREGATES = frozenset({'count','sum','avg','min','max','count_distinct','median','quantile','percentile',
    'variance','variance_population','variance_sample','stddev','stddev_population','stddev_sample',
    'range','product','sum_squares','rms','geometric_mean','harmonic_mean','covariance_population',
    'covariance_sample','correlation'})
PAIRED = frozenset({'covariance_population','covariance_sample','correlation'})
MAX_SAFE_INTEGER = 2**53-1


class NumericOperationError(ValueError):
    """A safe, literal explanation of an undefined numeric operation."""


def parse_literal_binding(question):
    """Recognize one unambiguous calculator instruction; never eval input.

    Business words, multiple operations and unknown function names do not
    match. They retain the existing task-planning/admission path.
    """
    if not isinstance(question,str) or len(question)>512:return None
    text=question.strip().strip('？?。！! ')
    text=re.sub(r'^(?:请|帮我|计算一下|计算|算一下|求出|求|算)+\s*','',text).strip()
    text=re.sub(r'\s*(?:等于多少|是多少|的结果)$','',text).strip()
    atom=r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
    def value(surface):
        parsed=number(surface)
        return int(parsed) if parsed==parsed.to_integral_value() else finite(parsed)
    binding=None
    try:
        match=re.fullmatch(r'('+atom+r')\s*的\s*('+atom+r')\s*次方',text)
        if match:binding={'operation':'power','left':{'value':value(match[1])},'right':{'value':value(match[2])}}
        if binding is None:
            match=re.fullmatch(r'('+atom+r')\s*(?:的)?(平方根|立方根|平方|立方|绝对值|阶乘|倒数)',text)
            if match:
                op={'平方根':'sqrt','立方根':'cbrt','平方':'square','立方':'cube','绝对值':'abs','阶乘':'factorial','倒数':'reciprocal'}[match[2]]
                binding={'operation':op,'input':{'value':value(match[1])}}
        if binding is None:
            match=re.fullmatch(r'([A-Za-z_]+)\s*\(\s*('+atom+r')(?:\s*,\s*('+atom+r'))?\s*\)',text)
            if match:
                op={'pow':'power','mod':'modulo','square_root':'sqrt'}.get(match[1],match[1])
                if op in UNARY and match[3] is None:binding={'operation':op,'input':{'value':value(match[2])}}
                elif op=='round' and match[3] is not None:
                    binding={'operation':op,'input':{'value':value(match[2])},'digits':integer(value(match[3]),minimum=-15,maximum=15)}
                elif op in BINARY and match[3] is not None:
                    binding={'operation':op,'left':{'value':value(match[2])},'right':{'value':value(match[3])}}
        if binding is None:
            match=re.fullmatch(r'('+atom+r')\s*(\*\*|//|[+*/^%×÷-]|加|减|乘以|除以)\s*('+atom+r')',text)
            if match:
                op={'**':'power','^':'power','//':'floor_divide','%':'modulo','+':'add','加':'add','-':'subtract','减':'subtract','*':'multiply','×':'multiply','乘以':'multiply','/':'divide','÷':'divide','除以':'divide'}[match[2]]
                # -2**2 has a precedence rule; it is not the same as (-2)**2.
                if op=='power' and match[1].startswith('-'):return None
                binding={'operation':op,'left':{'value':value(match[1])},'right':{'value':value(match[3])}}
        return {**binding,'result_field':'结果','format':'number'} if binding else None
    except (ValueError,ArithmeticError):return None


def number(value):
    if value is None or isinstance(value, bool):
        raise NumericOperationError('计算输入必须是有限数值')
    text = str(value).replace(',', '')
    percent = text.endswith('%')
    if percent:
        text = text[:-1]
    if '%' in text:
        raise NumericOperationError('计算输入必须是有限数值')
    try:
        result = Decimal(text)
    except (DecimalException, ValueError, TypeError) as exc:
        raise NumericOperationError('计算输入必须是有限数值') from exc
    if not result.is_finite():
        raise NumericOperationError('计算输入必须是有限数值')
    return result / 100 if percent else result


def literal(value):
    # Strings can be dataset numeric representations, but are not constants.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise NumericOperationError('计算常量必须是数值字面量')
    return number(value)


def finite(value):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise NumericOperationError('计算结果超过有限数值范围') from exc
    if not math.isfinite(result):
        raise NumericOperationError('计算结果超过有限数值范围')
    return result


def integer(value, *, minimum=-MAX_SAFE_INTEGER, maximum=MAX_SAFE_INTEGER):
    value = number(value)
    if value != value.to_integral_value() or not minimum <= value <= maximum:
        raise NumericOperationError('该运算需要范围内的整数')
    return int(value)


def operator_roles(operation):
    if not isinstance(operation, str) or operation not in UNARY | BINARY:
        raise NumericOperationError('计算绑定必须声明受控运算')
    return ('input',) if operation in UNARY else ('left','right')


def validate_derive(step):
    op = step.get('operator')
    roles = operator_roles(op)
    for role in roles:
        field_key = 'field' if role == 'input' else role + '_field'
        value_key = 'value' if role == 'input' else role + '_value'
        has_field, has_value = field_key in step, value_key in step
        if has_field == has_value:
            raise NumericOperationError('操作数必须明确选择结果字段或数值常量')
        if has_field and (not isinstance(step[field_key], str) or not step[field_key].strip()):
            raise NumericOperationError('逐行计算缺少明确输入字段')
        if has_value:
            literal(step[value_key])
    # Do not ignore accidentally supplied extra operands.
    expected = {'field','value'} if op in UNARY else {'left_field','left_value','right_field','right_value'}
    if any(key in step and key not in expected for key in
           ('field','value','left_field','left_value','right_field','right_value')):
        raise NumericOperationError('运算操作数个数不匹配')
    label = step.get('result_field')
    if not isinstance(label,str) or not label.strip() or label.startswith('_') or len(label)>128:
        raise NumericOperationError('缺少有效的计算结果字段名')
    if op == 'round' and (type(step.get('digits',0)) is not int or not -15 <= step.get('digits',0) <= 15):
        raise NumericOperationError('四舍五入位数必须为-15至15的整数')
    return roles


def derive_operands(step, row):
    values=[]
    for role in operator_roles(step.get('operator')):
        field_key = 'field' if role=='input' else role+'_field'
        value_key = 'value' if role=='input' else role+'_value'
        values.append(number(row.get(step[field_key])) if field_key in step else literal(step[value_key]))
    return values


def evaluate(operator, first, second=None, *, digits=0):
    roles=operator_roles(operator);a=number(first)
    b=number(second) if len(roles)==2 else None
    try:
        with localcontext() as ctx:
            ctx.prec=50
            if operator in {'add','subtract','multiply','divide','modulo','floor_divide'}:
                if operator in {'divide','modulo','floor_divide'} and b==0:
                    raise NumericOperationError('计算除数不能为零')
                result={'add':lambda:a+b,'subtract':lambda:a-b,'multiply':lambda:a*b,
                    'divide':lambda:a/b,'floor_divide':lambda:(a/b).to_integral_value(rounding=ROUND_FLOOR),
                    'modulo':lambda:a-(a/b).to_integral_value(rounding=ROUND_FLOOR)*b}[operator]()
            elif operator in {'square','cube','abs','negate','sign','reciprocal'}:
                if operator=='reciprocal' and a==0:raise NumericOperationError('零没有倒数')
                result={'square':lambda:a*a,'cube':lambda:a*a*a,'abs':lambda:abs(a),'negate':lambda:-a,
                    'sign':lambda:Decimal(int(a>0)-int(a<0)),'reciprocal':lambda:1/a}[operator]()
            elif operator in {'sqrt','cbrt','root'}:
                degree=Decimal(2 if operator=='sqrt' else 3) if operator!='root' else b
                if degree==0:raise NumericOperationError('根次数不能为零')
                if a==0 and degree<0:raise NumericOperationError('零不能取负次根')
                if a<0 and (degree!=degree.to_integral_value() or int(degree)%2==0):
                    raise NumericOperationError('负数仅支持奇数次实数根')
                if operator=='sqrt':result=a.sqrt()
                else:
                    result=ctx.power(abs(a),1/degree)
                    if a<0:result=-result
            elif operator=='power':
                if abs(b)>10000:raise NumericOperationError('乘方指数超出支持范围')
                if a<0 and b!=b.to_integral_value():raise NumericOperationError('负底数不支持非整数实数幂')
                if a==0 and b<=0:raise NumericOperationError('零的零次幂或负次幂未定义')
                result=ctx.power(a,b)
            elif operator in {'ln','log10','log2','log','exp','exp2','expm1','log1p'}:
                if operator not in {'exp','exp2','expm1','log1p'} and a<=0:raise NumericOperationError('对数真数必须大于零')
                if operator=='log1p' and a<=-1:raise NumericOperationError('log1p输入必须大于-1')
                if operator=='log' and (b<=0 or b==1):raise NumericOperationError('对数底数必须大于零且不等于1')
                result={'ln':lambda:a.ln(),'log10':lambda:a.log10(),'log2':lambda:a.ln()/Decimal(2).ln(),
                    'log':lambda:a.ln()/b.ln(),'exp':lambda:a.exp(),'exp2':lambda:ctx.power(Decimal(2),a),
                    'expm1':lambda:math.expm1(finite(a)),'log1p':lambda:math.log1p(finite(a))}[operator]()
            elif operator in {'floor','ceil','trunc','round'}:
                if operator=='round':
                    if type(digits) is not int or not -15<=digits<=15:raise NumericOperationError('无效的四舍五入位数')
                    result=a.quantize(Decimal(1).scaleb(-digits),rounding=ROUND_HALF_UP)
                else:result=a.to_integral_value(rounding={'floor':ROUND_FLOOR,'ceil':ROUND_CEILING,'trunc':ROUND_DOWN}[operator])
            elif operator in {'factorial','gcd','lcm','combination','permutation'}:
                if operator=='factorial':result=math.factorial(integer(a,minimum=0,maximum=170))
                elif operator in {'combination','permutation'}:
                    n,k=integer(a,minimum=0,maximum=10000),integer(b,minimum=0,maximum=10000)
                    if k>n:raise NumericOperationError('选择数量不能大于总数')
                    result=(math.comb if operator=='combination' else math.perm)(n,k)
                else:result=(math.gcd if operator=='gcd' else math.lcm)(integer(a),integer(b))
            elif operator.startswith('bit_') or operator.startswith('shift_'):
                x=integer(a);y=integer(b) if b is not None else None
                if operator.startswith('shift_'):y=integer(b,minimum=0,maximum=52)
                result={'bit_not':lambda:~x,'bit_and':lambda:x&y,'bit_or':lambda:x|y,'bit_xor':lambda:x^y,
                    'shift_left':lambda:x<<y,'shift_right':lambda:x>>y}[operator]()
                return integer(result)
            elif operator in {'minimum','maximum'}:result=(min if operator=='minimum' else max)(a,b)
            else:
                x=finite(a)
                if operator in {'asin','acos'} and not -1<=a<=1:raise NumericOperationError('反三角函数输入必须在[-1,1]内')
                if operator=='acosh' and a<1:raise NumericOperationError('acosh输入必须不小于1')
                if operator=='atanh' and abs(a)>=1:raise NumericOperationError('atanh输入必须在(-1,1)内')
                if operator=='tan' and abs(math.cos(x))<1e-15:raise NumericOperationError('该角度的正切未定义')
                if operator in {'hypot','atan2'}:result=getattr(math,operator)(x,finite(b))
                else:result=getattr(math,operator)(x)
            return finite(result)
    except (DecimalException,OverflowError,ZeroDivisionError) as exc:
        raise NumericOperationError('运算超出实数定义域或有限数值范围') from exc


def normalize_aggregation(spec):
    """Normalize known statistical parameter spellings without dropping options."""
    if not isinstance(spec,dict):raise NumericOperationError('统计参数必须是对象')
    spec=copy.deepcopy(spec);function=spec.get('function')
    def assign(key,value):
        if key in spec and spec[key]!=value:raise NumericOperationError('统计参数的两种写法存在冲突')
        spec[key]=value
    if function in PAIRED and 'right_field' in spec:
        assign('other_field',spec.pop('right_field'))
    params=spec.pop('params',{})
    if not isinstance(params,dict):raise NumericOperationError('统计params必须是参数对象')
    for key,value in params.items():
        if key not in {'q','percentile','p','ddof'}:raise NumericOperationError('存在不支持的统计参数')
        assign(key,value)
    if 'p' in spec:
        p=literal(spec.pop('p'))
        if function not in {'quantile','percentile'} or not 0<=p<=1:
            raise NumericOperationError('分位概率p必须在0到1之间')
        assign('q' if function=='quantile' else 'percentile',float(p if function=='quantile' else p*100))
    if ('q' in spec and function!='quantile') or ('percentile' in spec and function!='percentile'):
        raise NumericOperationError('quantile使用q参数，percentile使用percentile参数')
    return spec


def validate_aggregation(spec):
    spec=normalize_aggregation(spec)
    function=spec.get('function')
    if not isinstance(function,str) or function not in AGGREGATES:
        raise NumericOperationError('聚合计算缺少明确函数或字段')
    for key in ('field',*(['other_field'] if function in PAIRED else [])):
        if not isinstance(spec.get(key),str) or not spec[key].strip():raise NumericOperationError('聚合计算缺少明确输入字段')
    if function in {'quantile','percentile'}:
        key='q' if function=='quantile' else 'percentile'
        q=literal(spec.get(key,0.5 if function=='quantile' else 50))
        if not 0<=q<=(1 if function=='quantile' else 100):raise NumericOperationError('分位点超出支持范围')
    if 'ddof' in spec:
        ddof=spec['ddof']
        if type(ddof) is not int or ddof not in {0,1}:raise NumericOperationError('ddof仅支持0或1')
        if function not in {'variance','stddev','variance_population','stddev_population','variance_sample','stddev_sample'}:
            raise NumericOperationError('该函数不使用ddof参数')
        if function.endswith('_sample') and ddof!=1 or function.endswith('_population') and ddof!=0:
            raise NumericOperationError('方差或标准差的总体/样本定义与ddof冲突')


def aggregate_numeric(rows,spec):
    spec=normalize_aggregation(spec)
    validate_aggregation(spec);function=spec['function'];field=spec['field']
    if function=='count_distinct':
        values=[row.get(field) for row in rows if row.get(field) is not None]
        if any(not isinstance(value,(str,int,float,bool,Decimal)) for value in values):raise NumericOperationError('去重计数不支持复合类型')
        if any(isinstance(value,(float,Decimal)) and not math.isfinite(value) for value in values):raise NumericOperationError('去重计数输入不能包含非有限数值')
        return len(set(values))
    data=[number(row[field]) for row in rows if row.get(field) is not None
          and (function not in PAIRED or row.get(spec['other_field']) is not None)]
    if not data:raise NumericOperationError('没有可用于统计的非空数值样本')
    with localcontext() as ctx:
        precision=max(len(value.as_tuple().digits) for value in data)+20
        if precision>512:raise NumericOperationError('统计数值有效位超过支持范围')
        ctx.prec=max(50,precision);n=len(data);mean=sum(data,Decimal(0))/n
        def quantile(q):
            values=sorted(data);rank=(n-1)*q;lo=int(rank);hi=min(lo+1,n-1)
            return values[lo]+(values[hi]-values[lo])*(rank-lo)
        if function in PAIRED:
            other=[number(row[spec['other_field']]) for row in rows if row.get(field) is not None and row.get(spec['other_field']) is not None]
            if n<2 and function!='covariance_population':raise NumericOperationError('样本协方差与相关系数至少需要两个成对样本')
            center=sum(other,Decimal(0))/n
            products=sum(((a-mean)*(b-center) for a,b in zip(data,other)),Decimal(0))
            if function=='correlation':
                scale=(sum(((a-mean)**2 for a in data),Decimal(0))*sum(((b-center)**2 for b in other),Decimal(0))).sqrt()
                if scale==0:raise NumericOperationError('常量列的相关系数未定义')
                result=max(Decimal(-1),min(Decimal(1),products/scale))
            else:result=products/(n-1 if function=='covariance_sample' else n)
        elif function in {'variance','variance_population','variance_sample','stddev','stddev_population','stddev_sample'}:
            sample=function.endswith('_sample') or spec.get('ddof')==1
            if sample and n<2:raise NumericOperationError('样本方差或标准差至少需要两个样本')
            result=sum(((value-mean)**2 for value in data),Decimal(0))/(n-1 if sample else n)
            if function.startswith('stddev'):result=result.sqrt()
        elif function in {'median','quantile','percentile'}:
            q=Decimal('0.5') if function=='median' else literal(spec.get('q',0.5)) if function=='quantile' else literal(spec.get('percentile',50))/100
            result=quantile(q)
        elif function=='range':result=max(data)-min(data)
        elif function=='product':
            result=Decimal(1)
            for value in data:result*=value
        elif function in {'sum_squares','rms'}:
            result=sum((value*value for value in data),Decimal(0))
            if function=='rms':result=(result/n).sqrt()
        elif function in {'geometric_mean','harmonic_mean'}:
            if any(value<=0 for value in data):raise NumericOperationError('几何或调和平均值要求正数样本')
            result=(sum((value.ln() for value in data),Decimal(0))/n).exp() if function=='geometric_mean' else n/sum((1/value for value in data),Decimal(0))
        else:raise NumericOperationError('该聚合函数应沿用原有执行路径')
        return finite(result)
