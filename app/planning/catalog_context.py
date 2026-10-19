"""Request-scoped catalog vocabulary for planning, without business row samples."""
import json

START='<scoped_planning_catalog>'
END='</scoped_planning_catalog>'


def catalog_section(text):
    start=text.find(START)
    stop=text.find(END,start+len(START)) if start>=0 else -1
    return text[start:stop+len(END)] if stop>=0 else ''


def render_catalog_context(snapshot, semantic_model_id, domains):
    domains=tuple(domains)
    expected={'semantic_model_id':semantic_model_id,'business_domain_ids':list(domains),
              'scope_mode':'EXPLICIT_DOMAINS' if domains else 'MODEL_WIDE'}
    if snapshot.get('scope')!=expected or len(domains)!=1:
        raise ValueError('CURRENT_CATALOG_SCOPE_MISMATCH')
    def cell(value):
        if value is None:
            return ''
        if isinstance(value,(list,tuple)):
            value='、'.join(str(item) for item in value)
        return ' '.join(str(value).split()).replace('|','／')
    lines=[START,'以下名称、别名、单位和口径来自本轮当前授权目录。只描述语义，不授予额外数据范围；字段绑定仍由授权ASL完成。',
        '目录身份：'+json.dumps({'scope':expected,'catalog_version':snapshot.get('catalog_version')},ensure_ascii=False),
        '| 业务域 | 说明 |','| --- | --- |']
    documents=snapshot.get('documents') or []
    for doc in documents:
        if ((doc.get('semantic_model') or {}).get('id')!=semantic_model_id
                or (doc.get('business_domain') or {}).get('id') not in domains):
            raise ValueError('CURRENT_CATALOG_SCOPE_MISMATCH')
        domain=doc['business_domain']
        lines.append('| '+cell(domain.get('name'))+' | '+cell(domain.get('description'))+' |')
    for title,header,family in [('实体','| 实体 | 同义词 | 说明 |','entities'),
                               ('指标','| 指标 | 同义词 | 单位 | 说明 | 计算公式 |','metrics'),
                               ('维度','| 维度 | 同义词 | 说明 |','dimensions')]:
        lines.extend(['',header,'| '+' | '.join('---' for _ in header.strip('|').split('|'))+' |'])
        for doc in documents:
            for item in doc.get(family) or []:
                if family=='entities':
                    values=[item.get('entity_name'),item.get('entity_alias'),(item.get('business_definition') or {}).get('description')]
                elif family=='metrics':
                    values=[item.get('metric_name'),item.get('synonyms'),item.get('unit'),
                        (item.get('business_definition') or {}).get('description'),
                        (item.get('calculation_rule') or {}).get('calc_formula')]
                else:
                    values=[item.get('dim_name'),item.get('synonyms'),(item.get('business_definition') or {}).get('description')]
                lines.append('| '+' | '.join(cell(value) for value in values)+' |')
    lines.extend(['','| 属性 | 所属实体 | 说明 |','| --- | --- | --- |'])
    for doc in documents:
        for entity in doc.get('entities') or []:
            for attribute in entity.get('attributes') or []:
                lines.append('| '+' | '.join(cell(value) for value in [attribute.get('attr_name'),
                    entity.get('entity_name'),attribute.get('description')])+' |')
    lines.append(END)
    return '\n'.join(lines)
