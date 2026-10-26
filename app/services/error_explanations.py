"""Explain observed failures; never infer a business cause from missing evidence."""
from __future__ import annotations

import json
import re

import httpx


def failure_kind(exc: BaseException | None) -> str:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return 'timeout'
    if isinstance(exc, (httpx.ConnectError, ConnectionError)):
        return 'connection'
    if isinstance(exc, httpx.RemoteProtocolError):
        return 'connection_interrupted'
    if isinstance(exc, ValueError):
        return 'invalid_response'
    return 'unknown'


def service_label(path: str) -> str:
    # Paths are used only for classification, never echoed to the user.
    path = str(path).lower()
    if 'translate' in path:
        return '生成数据库查询时'
    if 'execute' in path:
        return '读取数据库数据时'
    if '/agent/query' in path or 'surface_asl' in path:
        return '把问题转换成查询条件时'
    if 'semantic' in path or 'catalog' in path:
        return '读取业务定义时'
    return '调用数据处理服务时'


def explain_dependency_failure(exc) -> str:
    details = exc.details if isinstance(exc.details, dict) else {}
    if isinstance(details.get('upstream_details'), dict):
        details = {**details['upstream_details'], **details}
    stage = service_label(details.get('path', ''))
    kind = details.get('failure_kind') or failure_kind(exc.__cause__)
    reasons = {
        'timeout': '等待服务回复超时，没有收到完整处理结果。',
        'connection': '未能建立服务连接。现有记录不能确定是网络还是服务自身的问题。',
        'connection_interrupted': '服务连接中途断开，没有收到完整处理结果。',
        'invalid_response': '服务返回的内容无法读取，未得到可用结果。',
    }
    if kind in reasons:
        return stage + '，' + reasons[kind] + '请联系维护人员检查本次调用。'
    specific = reported_reason(details)
    if specific:
        return stage + '，服务报告：' + specific.rstrip('。') + '。'
    reasons_by_status = {
        401: '服务身份验证未通过，需要维护人员检查连接配置。',
        403: '服务拒绝了访问，需要维护人员核对本次数据访问权限。',
        404: '没有找到请求的接口或资源，需要维护人员核对服务配置。',
        409: '当前数据或处理状态发生冲突，需要维护人员核对相关状态。',
        422: '服务未接受生成的查询条件，但没有返回具体哪项不符合要求。',
        429: '调用次数达到服务限制，可以稍后重试。',
        502: '中间服务没有取得有效回复，需要维护人员检查连接的服务。',
        503: '服务当前无法处理请求，可以稍后重试。',
        504: '中间服务等待回复超时，没有收到完整处理结果。',
    }
    reason = reasons_by_status.get(exc.status_code)
    if reason:
        return stage + '，' + reason
    return stage + '发生错误，但服务未提供足够的具体原因。需要维护人员结合本次请求记录定位，暂不能确定根因。'


def reported_reason(details: dict) -> str:
    raw = str(details.get('reason') or details.get('message') or '')
    lower = raw.lower()
    for marker, explanation in [
        ('unknown column', '查询使用了数据库中不存在的字段，需要核对字段配置'),
        ('sql syntax', '生成的数据库查询语句格式错误，需要检查查询生成步骤'),
        ('access denied', '数据库拒绝了连接身份，需要核对连接权限'),
        ('too many connections', '数据库可用连接数已满，暂时无法接收新查询'),
        ('deadlock', '数据库并发操作发生锁冲突，本次操作未能完成'),
    ]:
        if marker in lower:
            return explanation
    if 'table' in lower and "doesn't exist" in lower:
        return '查询引用的数据表不存在，需要核对数据表配置'
    specific = business_text(raw)
    if specific and re.search(r'[\u4e00-\u9fff]', specific) and not re.search(r'[A-Za-z_]{3,}', specific):
        return specific
    return ''


def explain_unexpected_failure(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return '等待处理服务回复超时，本次没有取得完整结果。'
    if isinstance(exc, (ConnectionError, httpx.ConnectError)):
        return '未能连接处理服务，本次没有取得结果。请维护人员检查服务连接。'
    if isinstance(exc, KeyError):
        return '处理过程中缺少程序需要的信息，未能完成回答。请维护人员检查本次数据传递记录。'
    if isinstance(exc, (ValueError, TypeError)):
        return '处理过程中遇到不符合要求的数据格式或类型，未能完成回答。请维护人员检查本次处理记录。'
    return '处理过程中发生未分类异常，现有错误信息不足以确定原因。请维护人员结合本次请求记录排查。'


def business_text(value: object) -> str:
    """Only bounded business labels/reasons, without endpoint or secret material."""
    text = ' '.join(str(value or '').split())[:600]
    text = re.sub(r'https?://\S+|(?:[A-Za-z]:\\|/root/|/etc/)\S+', '[已隐藏]', text)
    text = re.sub(r'(?i)(?:authorization|api[_-]?key|token|password|secret)\s*[:=]\s*\S+', '[已隐藏]', text)
    text = re.sub(r'\b[A-Za-z_][\w]*\.[A-Za-z_][\w.]*\b', '对应字段', text)
    return text.replace('<', '＜').replace('>', '＞')


def explain_binding_reason(raw: str, *, subject: str = '', kind: str = '') -> str:
    match = re.search(r'结构化参数【(?P<slot>[^】]*)】(?P<label>.*?)：(?P<reason>.*)', raw, re.S)
    reason = raw
    slot = ''
    value = ''
    if match:
        slot, reason = match['slot'], match['reason']
        kind = {'过滤条件':'filter', '展示字段':'dimension', '维度':'dimension',
                '指标':'metric', '时间粒度':'time_anchor'}.get(slot.split('[')[0], kind)
        try:
            param = json.loads(match['label'])
        except ValueError:
            param = None
        if isinstance(param, dict):
            subject = str(param.get('field') or param.get('name') or subject)
            raw_value = param.get('value')
            value = '、'.join(map(str, raw_value)) if isinstance(raw_value, list) else str(raw_value or '')
    subject = business_text(subject)
    if subject.lower() in {'null', 'none', 'unknown'} or re.fullmatch(r'[A-Za-z0-9_.:\-\s]+', subject):
        subject = ''
    role = {'filter':'筛选条件', 'filter_slot':'筛选条件', 'dimension':'分组或展示字段',
            'metric':'指标', 'time_anchor':'时间范围'}.get(kind, '查询条件')
    target = f'{role}“{subject}”' if subject else role
    if 'omitted a metadata-resolved required filter' in reason:
        return f'生成的查询遗漏了已经确认的{target}，所以没有继续执行。需要维护人员检查条件传递，您无需重复填写。'
    if 'omitted a metadata-resolved required grouping' in reason:
        return f'生成的查询遗漏了要求的{target}，所以没有继续执行。需要维护人员检查分组条件传递。'
    if 'requires at least one governed metric' in reason:
        return f'生成的查询缺少已确认的{target}定义，无法完成计算，需要维护人员核对指标传递。'
    if 'ASL_TIME_ANCHOR_MISSING' in reason:
        return '时间范围缺少对应的业务日期字段，无法确定按哪种日期筛选。需要核对日期字段配置。'
    if '上游未提供结构化参数' in reason:
        return '前一步没有传来整理好的查询条件，后续无法生成查询。需要维护人员检查步骤之间的数据传递，您无需重复说明问题。'
    if '向量标准值' in reason or '值目录' in reason:
        target_value = f'“{business_text(value)}”' if value else '指定的取值'
        return f'{target}已找到对应字段，但{target_value}尚未在该字段的可选值中匹配成功。请核对取值及所属分类；目前不能据此认定数据不存在。'
    if '无法确认该筛选作用于哪个业务实体' in reason:
        location = f'“{business_text(value)}”' if value else target
        return f'还不能确定{location}限定的是哪个业务对象，例如医院所在地还是经销商所在地。请明确对象。'
    if 'sort field must be a selected dimension' in reason:
        return '排序字段没有出现在查询的分组字段中，查询步骤衔接不一致，需要维护人员检查生成的查询条件。'
    if '展示属性与分组对象' in reason and '唯一关联' in reason:
        name = f'“{subject}”' if subject else '要显示的名称'
        return f'系统无法确定每条查询结果应该对应哪个{name}，直接显示可能配错，因此本次没有继续查询。需要维护人员核对名称与查询对象的对应关系。'
    if '仅支持一个明确升序/降序' in reason:
        return f'{target}的排序方式未被正确识别，需要明确按从高到低还是从低到高排列。'
    if '多个' in reason or '不唯一' in reason or '歧义' in reason:
        return f'{target}存在多个可能的对应项，当前无法唯一确定要用哪一个。需要核对它所属的业务对象和字段定义。'
    reason = re.split(r'请补充或确认该项', reason)[0].strip(' 。；')
    reason = business_text(reason)
    for old, new in [('ASL', '查询条件'), ('SQL', '数据库查询'), ('向量目录', '数据目录'), ('未匹配到可执行的标准字段', '没有找到可用于查询的对应字段'), ('绑定', '对应')]:
        reason = reason.replace(old, new)
    # Raw English diagnostics, stack traces and internal codes are not business explanations.
    if not reason or re.search(r'[A-Za-z_]{3,}', reason) or not re.search(r'[\u4e00-\u9fff]', reason):
        return f'{target}处理失败，但服务未提供可说明具体原因的信息，需要维护人员查看本次错误记录。'
    return f'{target}处理失败：{reason}。'
