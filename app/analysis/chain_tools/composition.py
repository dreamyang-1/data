"""占比/构成链路的提问信号解析与共用工具。

帕累托（累计贡献）类问题要求完整分组结果：阈值集合必须包含首次
跨过阈值的边界实体，不能用 TopN 预览冒充。本模块只做纯文本信号
解析，不做数值计算；计算在确定性算子层完成。
"""
from __future__ import annotations

import re

# “贡献了80%”“累计占比达到60%”两类帕累托问法；不带百分比的普通
# 占比问题不命中，走普通份额路径。
_CUMULATIVE_THRESHOLD = re.compile(
    r"贡献了?(?:大约|约|超过)?\s*(\d+(?:\.\d+)?)\s*%"
    r"|累计(?:占比|份额|贡献)?(?:达到|超过|约)?\s*(\d+(?:\.\d+)?)\s*%"
)


def cumulative_contribution_threshold(question: str) -> float | None:
    """解析累计贡献阈值；非帕累托类问题返回 None。

    只认显式百分比，不设默认阈值：“主要贡献者”这类无阈值的问法
    属于普通份额或排名问题，不能擅自按 80% 口径计算。
    """
    compact = str(question or "").replace(" ", "")
    match = _CUMULATIVE_THRESHOLD.search(compact)
    if match is None:
        return None
    text = match.group(1) or match.group(2)
    value = float(text) / 100
    if not 0 < value < 1:
        return None
    return value
