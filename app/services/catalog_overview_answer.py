"""Present the registered metric/dimension catalog without running the pipeline.

“XX域建了哪些指标和维度”类解释口径问题：意图识别完成后直接读语义模型
登记文件（与向量目录同源的 capture_catalog 快照）作答，不进入澄清、
任务规划、调度执行及以下节点。
"""
from __future__ import annotations

from app.domain.models import AgentResponse, AnalysisProcessStep, EvidenceItem, ReliabilityReport


def catalog_overview_response(request, overview: dict) -> AgentResponse:
    overview = overview if isinstance(overview, dict) else {}
    model_name = str(overview.get("model_name") or "").strip()
    domains = [item for item in overview.get("domains") or [] if isinstance(item, dict)]
    shared_metrics = [
        item for item in overview.get("shared_metrics") or [] if isinstance(item, dict)
    ]
    dimensions = [
        item for item in overview.get("dimensions") or [] if isinstance(item, dict)
    ]

    # 问题点名了某个业务域时只展示该域，其余域不展开。
    question = str(request.original_question or "")
    mentioned = [
        item for item in domains
        if item.get("name") and str(item["name"]) in question
    ]
    if len(mentioned) == 1:
        domains = mentioned

    sections: list[str] = []
    warnings: list[str] = []
    metric_names = [str(item.get("name") or "").strip() for item in shared_metrics]
    metric_names = [name for name in metric_names if name]

    for domain in domains:
        names = [
            str(item.get("name") or "").strip()
            for item in domain.get("metrics") or []
        ]
        names = [name for name in names if name]
        label = f"业务域「{domain['name']}」" if domain.get("name") else "当前语义模型"
        if names:
            sections.append(f"{label}已登记指标（{len(names)} 个）：{'、'.join(names)}")
        else:
            message = f"{label}尚未登记任何指标。"
            sections.append(message)
            warnings.append(message)
    if len(domains) > 1 and metric_names:
        sections.append(
            f"未绑定业务域的公共指标（{len(metric_names)} 个）：{'、'.join(metric_names)}"
        )
    elif not domains and metric_names:
        sections.append(
            f"已登记指标（{len(metric_names)} 个）：{'、'.join(metric_names)}"
        )

    dim_names = [str(item.get("name") or "").strip() for item in dimensions]
    dim_names = [name for name in dim_names if name]
    if dim_names:
        sections.append(f"已登记维度（{len(dim_names)} 个）：{'、'.join(dim_names)}")
    else:
        message = "当前语义模型尚未登记任何维度。"
        sections.append(message)
        warnings.append(message)
    if not sections:
        message = "当前语义模型尚未登记任何指标或维度。"
        sections.append(message)
        warnings.append(message)

    prefix = f"语义模型「{model_name}」" if model_name else "当前语义模型"
    answer = f"{prefix}：\n" + "\n\n".join(sections)

    evidence = [
        EvidenceItem(
            evidence_id="catalog:" + str(request.semantic_model_id),
            kind="SEMANTIC_CATALOG",
            source_ref="semantic-model-registry",
            payload={
                "semantic_model_id": request.semantic_model_id,
                "model_name": model_name,
                "domain_ids": [item.get("id") for item in domains],
                "metric_names": [
                    str(item.get("name") or "").strip()
                    for item in (shared_metrics + [
                        metric for domain in domains for metric in domain.get("metrics") or []
                    ])
                    if str(item.get("name") or "").strip()
                ],
                "dimension_names": dim_names,
            },
        )
    ]
    status = "COMPLETED" if not warnings else "PARTIAL_SUCCESS"
    return AgentResponse(
        request_id=request.request_id,
        conversation_id=request.conversation_id,
        status=status,
        intent=request.primary_intent,
        intent_source=request.intent_source,
        intent_confidence=request.intent_confidence,
        answer=answer,
        evidence=evidence,
        analysis_process=[AnalysisProcessStep(
            stage="KNOWLEDGE_RETRIEVAL",
            title="检索语义目录",
            status="COMPLETED",
            summary="依据语义模型登记文件作答。",
            evidence_ids=[evidence[0].evidence_id],
        )],
        reliability=ReliabilityReport(
            level="HIGH" if not warnings else "LIMITED",
            score=1.0 if not warnings else 0.5,
            gates={"metadata_found": True},
            warnings=warnings,
        ),
    )
