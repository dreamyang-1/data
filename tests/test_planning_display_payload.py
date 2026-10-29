from app.services.orchestrator import _planning_display_payload


def test_planning_display_payload_hides_internal_keys_and_window_alignment():
    structured = {
        "意图": "TREND_ANALYSIS",
        "业务域": [81],
        "是否查询数据库": "是",
        "指标": ["含税销售总额"],
        "时间粒度": {
            "unit": "月",
            "time_range": "2025-10-23至2026-10-23",
            "anchor": None,
            "window_alignment": {
                "derived_from": "最近12个月",
                "reasons": ["保留目标日期；同比基期单独查询"],
            },
        },
    }

    payload = _planning_display_payload(structured)

    assert "意图" not in payload
    assert "业务域" not in payload
    assert "是否查询数据库" not in payload
    assert payload["指标"] == ["含税销售总额"]
    assert payload["时间粒度"] == {
        "unit": "月",
        "time_range": "2025-10-23至2026-10-23",
        "anchor": None,
    }
    # 展示裁剪不能改动内部提取结构，下游去重和绑定仍读全量键
    assert structured["时间粒度"]["window_alignment"]["derived_from"] == "最近12个月"
