"""Non-blocking ASL selection disclosures, not a second binding validator."""


def binding_notices(repairs):
    result = []
    for item in repairs or []:
        if not isinstance(item, dict) or item.get("source") != "SURFACE_MENTION_RECALL":
            continue
        mention = str(item.get("mention") or "")
        if item.get("type") == "DROP_UNMATCHED_SURFACE_MENTION":
            message = (f"“{mention}”未匹配到可用目录值，本次未应用该项筛选。"
                       "结果仅代表实际保留条件下的数据；若该词表示您要求的筛选条件，查询范围可能扩大。"
                       "请数据部门核对目录配置。")
        elif item.get("type") == "SURFACE_MATCH_NOTICE":
            selected = f"{item.get('resolved_field')} = {item.get('canonical_value')}"
            if item.get("reason") == "TIED_CANDIDATES":
                candidates = "；".join(
                    f"{c.get('field')} = {c.get('value')}（字符串相似度 {c.get('score')}）"
                    for c in item.get("candidates", []) if isinstance(c, dict)
                )
                message = (f"“{mention}”存在同分候选，本次随机采用 {selected}。"
                           f"同分候选：{candidates}。结果基于本次选择生成，请数据部门核对；同分不代表数据一定错误。")
            else:
                message = (f"“{mention}”存在重复候选，已合并 {item.get('duplicate_count', 0)} 条重复项，"
                           f"采用 {selected}，本次查询继续执行。")
        else:
            continue
        if message not in result:
            result.append(message)
    return result


def attach_binding_notices(result, repairs):
    messages = binding_notices(repairs)
    if messages:
        result.execution_transforms.append({
            "type": "ASL_BINDING_NOTICES", "warnings": messages,
            "repairs": [item for item in repairs if isinstance(item, dict)
                        and item.get("type") in {"SURFACE_MATCH_NOTICE", "DROP_UNMATCHED_SURFACE_MENTION"}],
        })
    return result


def render_binding_notices(repairs):
    messages = binding_notices(repairs)
    return "\n\n查询条件提示：\n" + "\n".join(messages) if messages else ""
