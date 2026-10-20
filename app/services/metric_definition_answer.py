"""Present retrieved metric definitions without running the data pipeline."""
from __future__ import annotations

import re

from app.domain.models import AgentResponse, AnalysisProcessStep, EvidenceItem, ReliabilityReport


def definition_response(request, results):
    sections, evidence, warnings = [], [], []
    for item in results:
        name = item['input']
        if item['status'] != 'RESOLVED':
            message = (f'「{name}」在当前范围有多个定义，暂不能唯一确定口径。'
                       if item['status'] == 'AMBIGUOUS' else
                       f'当前授权范围的向量库中未查到「{name}」的口径说明。')
            sections.append(message)
            warnings.append(message)
            continue
        data = item['definition']
        business = data.get('business_definition') or {}
        business = business if isinstance(business, dict) else {}
        rule = data.get('calculation_rule') or {}
        rule = rule if isinstance(rule, dict) else {}
        description = str(business.get('description') or '').strip()
        formula = str(rule.get('calc_formula') or '').strip()
        lines = [f"指标名称：{data['metric_name']}"]
        if description:
            lines.append('统计口径：' + description)
        if formula and (not description or re.search(r'公式|怎么算|怎么计算|如何计算', request.original_question)):
            lines.append('登记公式：' + formula)
        if not description and not formula:
            message = '已找到该指标，但向量库尚未登记统计口径或计算公式。'
            lines.append(message)
            warnings.append(message)
        if data.get('unit'):
            lines.append('单位：' + str(data['unit']))
        filters = rule.get('global_filters') or []
        if isinstance(filters, list):
            explanations = list(dict.fromkeys(str(value.get('filterExplanation') or value.get('description') or value.get('desc') or '').strip()
                for value in filters if isinstance(value, dict)))
            if any(explanations):
                lines.append('数据范围：' + '；'.join(value for value in explanations if value))
        scenes = business.get('application_scenes') or []
        if isinstance(scenes, list) and scenes:
            lines.append('适用场景：' + '、'.join(str(value) for value in scenes))
        sections.append('\n'.join(lines))
        evidence.append(EvidenceItem(evidence_id='definition:' + str(data['record_id']),
            kind='METRIC_DEFINITION', source_ref='vector-metric-catalog', payload=data))
    if not results:
        warnings.append('请说明需要解释哪个指标的统计口径。')
        sections.extend(warnings)
    status = 'COMPLETED' if evidence and not warnings else 'PARTIAL_SUCCESS' if evidence else 'SAFE_FALLBACK'
    return AgentResponse(request_id=request.request_id, conversation_id=request.conversation_id,
        status=status, intent=request.primary_intent, intent_source=request.intent_source,
        intent_confidence=request.intent_confidence, answer='\n\n'.join(sections), evidence=evidence,
        analysis_process=[AnalysisProcessStep(stage='KNOWLEDGE_RETRIEVAL',
            title='检索指标口径', status='COMPLETED' if evidence else 'DEGRADED',
            summary='依据当前授权范围内的向量库说明作答。', evidence_ids=[item.evidence_id for item in evidence])],
        reliability=ReliabilityReport(level='HIGH' if not warnings else 'LIMITED',
            score=1 if not warnings else 0.5, gates={'metadata_found': bool(evidence)}, warnings=warnings))
