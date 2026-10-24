"""Chinese display messages for publication failures; internal codes stay unchanged."""
from __future__ import annotations

import re


_STAGES = {
    "physical_prepare": "物理目录准备",
    "semantic_rebuild": "语义向量重建",
    "physical_write_verify": "物理目录写入与校验",
    "semantic_embedding": "语义向量生成",
    "semantic_write": "语义向量写入",
    "attribute_column_read": "属性字段读取",
    "attribute_value_read": "属性值读取",
    "attribute_value_embedding": "属性值向量生成",
    "attribute_value_write": "属性值向量写入",
}

_ERRORS = {
    "CATALOG_SOURCE_ID_COLLISION": "语义目录源记录标识冲突",
    "CATALOG_DUPLICATE_RECORD_ID": "语义目录记录标识重复",
    "CATALOG_STABLE_ID_REQUIRED": "语义目录记录缺少稳定标识",
    "CATALOG_GOVERNED_ID_REQUIRED": "语义目录记录缺少配置标识",
    "CATALOG_SOURCE_IDENTITY_MISSING": "语义目录缺少数据来源标识",
    "CATALOG_RELEASE_IDENTITY_MISSING": "语义目录缺少发布标识",
    "CATALOG_TARGET_IDENTITY_REQUIRED": "语义目录缺少目标索引标识",
    "CATALOG_DOCUMENT_INVALID": "语义目录文档格式不正确",
    "CATALOG_RECORD_INVALID": "语义目录记录格式不正确",
    "CATALOG_MODEL_MISMATCH": "语义目录与当前语义模型不一致",
    "CATALOG_DOMAIN_MISMATCH": "语义目录与当前业务域不一致",
    "CATALOG_PHYSICAL_MODEL_MISMATCH": "物理目录与当前语义模型不一致",
    "CATALOG_SCOPE_MISMATCH": "语义目录与当前请求范围不一致",
    "CATALOG_ORPHAN_METRIC_DOMAIN": "指标所属业务域不存在",
    "CATALOG_ENTITY_OWNERSHIP_INVALID": "实体归属配置不正确",
    "CATALOG_METRIC_OWNERSHIP_INVALID": "指标归属配置不正确",
    "CATALOG_FIELD_OWNERSHIP_INVALID": "字段归属配置不正确",
    "CATALOG_SHARED_DEFINITION_CONFLICT": "共享语义定义存在冲突",
    "CATALOG_SOURCE_RECORD_COVERAGE_MISMATCH": "语义目录记录与源数据不完整对应",
    "CATALOG_CONSISTENT_SNAPSHOT_UNSUPPORTED": "数据源不支持一致性快照读取",
    "CATALOG_SNAPSHOT_NESTING_NOT_SUPPORTED": "不支持嵌套读取语义目录快照",
    "CATALOG_SNAPSHOT_DIGEST_MISMATCH": "语义目录快照校验不一致",
    "CATALOG_MANIFEST_DIGEST_MISMATCH": "语义目录发布清单校验不一致",
    "CATALOG_RECORD_ALREADY_STAMPED": "语义目录记录已带有发布标记",
    "CATALOG_EMPTY_PUBLICATION": "没有可发布的语义目录记录",
    "CATALOG_AUTHORITY_DRIFT": "发布期间语义目录源数据发生变化",
    "CATALOG_OPERATION_SOURCE_CHANGED": "操作期间语义目录源数据发生变化",
    "CATALOG_INDEX_RECORD_DRIFT": "索引记录与语义目录不一致",
    "CATALOG_INDEX_INVENTORY_DRIFT": "索引记录清单与语义目录不一致",
    "CATALOG_INDEX_VECTOR_DRIFT": "索引向量与待发布向量不一致",
    "CATALOG_TARGET_MISMATCH": "语义目录目标索引不一致",
    "CATALOG_PUBLICATION_NOT_PROVEN": "语义目录发布状态尚未确认",
    "CATALOG_PUBLICATION_CHANGED_DURING_READ": "读取期间语义目录发布版本发生变化",
    "CATALOG_ENTITY_VALUES_CHANGED_DURING_READ": "读取期间实体属性值发生变化",
    "CATALOG_EMBEDDING_INVALID": "语义目录向量数据不正确",
    "CATALOG_EMBEDDING_DIMENSION_MISMATCH": "语义目录向量维数不一致",
    "PUBLICATION_EMBEDDING_INVALID": "待发布向量的维数或数值不正确",
    "PHYSICAL_CATALOG_SOURCE_INVALID": "物理目录数据或归属配置不正确",
    "PHYSICAL_CATALOG_EMBEDDING_INVALID": "物理目录向量数据不正确",
    "PHYSICAL_CATALOG_READBACK_FAILED": "物理目录写入后读取校验失败",
    "REQUEST_SCOPE_INVALID": "请求的语义模型或业务域范围不正确",
    "EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED": "暂不支持一次指定多个业务域",
}


def publication_failure_message(exc: BaseException, stage: str | None = None) -> str:
    """Translate display text only, without modifying exceptions or validation."""
    text = str(exc).strip()
    code = text.partition(":")[0].strip()
    detail = _ERRORS.get(code)
    if detail is None:
        # Keep useful Chinese reasons; translate common component names too.
        translated = re.sub(r"\b(?:milvus|mysql|redis|embedding)\b", lambda match: {
            "milvus": "向量数据库", "mysql": "关系数据库",
            "redis": "缓存服务", "embedding": "向量生成",
        }[match[0].lower()], text, flags=re.IGNORECASE)
        if re.search(r"[\u4e00-\u9fff]", translated) and not re.search(r"[A-Za-z]", translated):
            detail = translated
        elif isinstance(exc, TimeoutError) or any(word in text.lower() for word in ("timed out", "timeout")):
            detail = "服务请求超时"
        elif isinstance(exc, ConnectionError) or any(word in text.lower() for word in (
            "connection refused", "connection reset", "can't connect", "failed to connect",
        )):
            detail = "服务连接失败"
        elif code.startswith("CATALOG_"):
            detail = "语义目录校验失败"
        elif isinstance(exc, PermissionError):
            detail = "服务访问权限不足"
        elif isinstance(exc, ValueError):
            detail = "数据源或参数配置不正确"
        else:
            detail = "服务处理异常，请查看服务日志"
    if stage:
        return f"（阶段：{_STAGES.get(stage, '发布处理')}）{detail}"
    return detail
