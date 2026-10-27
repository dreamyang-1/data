"""为异常解释读取当前模型的语义文件；文件只作参考，不参与执行绑定。"""
from __future__ import annotations

import asyncio
import re

from app.domain.semantic_description import load_semantic_description
from app.domain.semantic_scope import AuthorizedSemanticScope
from .diagnostics import redact


def _domain_name(settings, scope) -> str | None:
    """从平台目录核对域名称；不相信提问、历史或模型给出的域名。"""
    import pymysql
    password = settings.mysql_password
    connection = pymysql.connect(
        host=settings.mysql_host, port=settings.mysql_port, user=settings.mysql_user,
        password=password.get_secret_value() if hasattr(password, 'get_secret_value') else password,
        database=settings.mysql_database, charset='utf8mb4', autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=settings.mysql_connect_timeout_seconds, read_timeout=None, write_timeout=None,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT id, name FROM semantic_model_business_domain '
                           'WHERE semantic_model_id = %s AND is_deleted = 0', (scope.semantic_model_id,))
            domains = cursor.fetchall()
        selected = [d for d in domains if d['id'] in scope.business_domain_ids]
        if len(selected) != 1 or not isinstance(selected[0].get('name'), str):
            return None
        name = selected[0]['name'].strip()
        return name if name and sum(d.get('name', '').strip() == name for d in domains) == 1 else None
    finally:
        connection.close()


def domain_section(content: str, name: str) -> str:
    """只取生成文件中明确标注的当前业务域章节；格式不明确时不猜边界。"""
    headings = list(re.finditer(r'(?m)^(#{1,3})\s+([^\r\n]+)', content))
    sections = []
    for index, heading in enumerate(headings):
        if heading.group(1) != '###':
            continue
        match = re.fullmatch(r'业务域\s*[:：]\s*(.+?)\s*', heading.group(2))
        if match and match.group(1) == name:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
            sections.append(content[heading.start():end])
    return sections[0] if len(sections) == 1 else ''


async def load_reference(settings, raw_scope: dict | None) -> dict:
    unavailable = {'available': False, 'content': '',
                   'notice': '语义参考不可用不等于用户参数缺失或目录没有配置。'}
    try:
        scope = AuthorizedSemanticScope.model_validate(raw_scope)
        if len(scope.business_domain_ids) > 1:
            return dict(unavailable, reason='当前显式多域范围不支持')
        reference = await load_semantic_description(settings, scope.semantic_model_id)
        if not reference.get('available'):
            return dict(unavailable, reason='当前模型语义文件不可用')
        content = reference['content']
        if scope.business_domain_ids:
            name = await asyncio.to_thread(_domain_name, settings, scope)
            content = domain_section(content, name) if name else ''
            if not content:
                return dict(unavailable, reason='无法确认语义文件中当前业务域的唯一章节')
        maximum = 40000
        # 按行脱敏，避免一条 SQL 片段吞掉后面的全部业务定义。
        safe_content = '\n'.join(redact(line, maximum) for line in content[:maximum].splitlines())
        return {'available': True, 'source': reference.get('source'),
                'semantic_model_id': scope.semantic_model_id,
                'business_domain_ids': list(scope.business_domain_ids),
                'content': safe_content, 'truncated': len(content) > maximum,
                'notice': '仅用于理解实体、指标、维度、关系和口径；不证明绑定成功或参数已验证。'}
    except Exception as exc:
        return dict(unavailable, reason='语义参考读取失败', error_type=type(exc).__name__)
