"""业务语义描述/结构化配置生成 SQL 计算公式服务。

POST /formula/generate  入参兼容自然语言描述与结构化配置，返回公式文本
GET  /health            健康检查

模型配置复用 DataAnalysis_Agent 的 .env（DATA_AGENT_INTENT_MODEL_*）。
返回体为 {code, data, msg} 结构。
"""

import os
import re

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, Field

from prompt import build_prompt

load_dotenv("/root/yyy/DataAnalysis_Agent/.env")

API_KEY = os.environ["DATA_AGENT_INTENT_MODEL_API_KEY"]
BASE_URL = os.getenv(
    "DATA_AGENT_INTENT_MODEL_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
MODEL = os.getenv("DATA_AGENT_INTENT_MODEL_NAME", "qwen3.7-max")

app = FastAPI(title="SQL公式生成服务")


class FormulaGenerateRequest(BaseModel):
    # 自然语言描述与结构化配置至少给一个，可组合使用
    description: str = ""
    structured: dict = Field(default_factory=dict)
    # 实体属性列表，沿用前端 attribute/list 返回结构（name/attrName/mappingTable/mappingColumn）
    attributes: list[dict] = Field(default_factory=list)
    indicatorLevel: int = 1  # 1原子 2派生 3复合
    atomicOperators: list[dict] = Field(default_factory=list)
    indicatorName: str = ""
    businessDomain: str = ""


def _ok(formula: str) -> dict:
    return {"code": 200, "data": {"formula": formula}, "msg": "success"}


def _err(code: int, msg: str) -> dict:
    return {"code": code, "data": None, "msg": msg}


def _clean_formula(text: str) -> str:
    """模型输出清洗：去围栏、去前缀、挑公式行、去结尾分号。"""
    text = text.strip()
    text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    # 模型无法生成时按约定以 #UNKNOWN# 起头，整行原样返回
    m = re.search(r"^#UNKNOWN#.*$", text, flags=re.MULTILINE)
    if m:
        return m.group(0).strip()
    text = re.sub(r"^(计算)?公式\s*[:：]\s*", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    # 取首个 SQL 起始字符开头的行，中文起头的行为解释文字
    for ln in lines:
        if re.match(r"^[A-Za-z0-9('\"]", ln):
            return ln.rstrip(";").strip()
    return lines[0].rstrip(";").strip()


def _extract_field_refs(formula: str) -> set[str]:
    """抠出公式里所有 表.字段 引用，先剔除字符串常量防止 '1.2' 这类字面量误判。"""
    no_str = re.sub(r"'[^']*'", "", formula)
    return {f"{t}.{c}" for t, c in re.findall(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", no_str)}


async def _call_model(prompt: str) -> str:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{BASE_URL}/chat/completions", json=body, headers=headers
        )
    if resp.status_code != 200:
        raise RuntimeError(f"模型调用失败: {resp.status_code} {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


@app.post("/formula/generate")
async def generate(req: FormulaGenerateRequest):
    if not req.description.strip() and not req.structured:
        return _err(400, "description 和 structured 至少提供一个")
    if req.indicatorLevel in (2, 3):
        if not req.atomicOperators:
            return _err(400, "派生/复合指标需要提供 atomicOperators")
    elif not req.attributes:
        return _err(400, "原子指标需要提供 attributes")

    prompt = build_prompt(
        description=req.description.strip(),
        structured=req.structured,
        attributes=req.attributes,
        indicator_level=req.indicatorLevel,
        atomic_operators=req.atomicOperators,
        indicator_name=req.indicatorName.strip(),
        business_domain=req.businessDomain.strip(),
    )

    try:
        raw = await _call_model(prompt)
    except Exception as e:
        return _err(500, str(e))

    formula = _clean_formula(raw)
    if not formula:
        return _err(500, "模型未返回有效公式")
    if formula.startswith("#UNKNOWN#"):
        return _err(400, f"无法生成公式: {formula[len('#UNKNOWN#'):].strip()}")

    # 原子指标校验字段白名单，引用清单外字段视为幻觉直接打回
    if req.indicatorLevel not in (2, 3):
        valid = {
            f"{a.get('mappingTable', '')}.{a.get('mappingColumn', '')}"
            for a in req.attributes
        }
        valid.discard(".")
        unknown = _extract_field_refs(formula) - valid
        if unknown:
            return _err(400, f"公式引用了属性列表之外的字段: {', '.join(sorted(unknown))}")

    return _ok(formula)


@app.get("/health")
def health():
    return {"code": 200, "msg": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=48002)
