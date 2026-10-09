"""DSL 语义建模 YML 转 MD 独立服务。

POST /dsl/convert  上传 YML，调模型抽取语义生成 MD，返回下载地址
GET  /files/{filename}  下载生成的 MD

配置加载优先级：DSL_MD_ENV_PATH > 服务同目录 .env > /root/yyy/DataAnalysis_Agent/.env。
必需配置：DATA_AGENT_INTENT_MODEL_API_KEY / BASE_URL / NAME（抽取模型）。
可选配置：DATA_AGENT_MYSQL_*（按模型 id 反查平台模型名称）、
DSL_MD_MODEL_TABLE（反查表名，默认 semantic_model）、DSL_MD_PORT（默认 8012）。
"""

import os
import re
import time
from pathlib import Path

import httpx
import pymysql
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from prompt import EXTRACT_PROMPT

BASE_DIR = Path(__file__).resolve().parent
FILES_DIR = BASE_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)

_env_path = os.getenv("DSL_MD_ENV_PATH") or str(BASE_DIR / ".env")
if not Path(_env_path).is_file():
    _env_path = "/root/yyy/DataAnalysis_Agent/.env"
load_dotenv(_env_path)

API_KEY = os.environ["DATA_AGENT_INTENT_MODEL_API_KEY"]
BASE_URL = os.getenv(
    "DATA_AGENT_INTENT_MODEL_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
MODEL = os.getenv("DATA_AGENT_INTENT_MODEL_NAME", "qwen3.7-max")

app = FastAPI(title="DSL语义建模YML转MD服务")


def _semantic_model_id(yml_text: str) -> str | None:
    """从实体上取 semantic_model_id，用于输出文件命名。"""
    try:
        data = yaml.safe_load(yml_text) or {}
    except yaml.YAMLError:
        return None
    for entity in data.get("semantic_model_entity_type") or []:
        mid = entity.get("semantic_model_id")
        if mid:
            return str(mid)
    return None


def _semantic_model_name(yml_text: str) -> str | None:
    """从 YML 提取语义模型名称，没有则返回 None。"""
    try:
        data = yaml.safe_load(yml_text) or {}
    except yaml.YAMLError:
        return None
    for key in ("semantic_model_name", "model_name", "name"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    node = data.get("semantic_model")
    if isinstance(node, dict):
        v = node.get("name") or node.get("semantic_model_name")
        if isinstance(v, str) and v.strip():
            return v.strip()
    for entity in data.get("semantic_model_entity_type") or []:
        v = entity.get("semantic_model_name")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _safe_filename(name: str) -> str:
    """清洗为安全文件名：保留中英文、数字、_ . -，其余字符替换为 _。"""
    return re.sub(r"[^\w.-]+", "_", name).strip("_.")[:80]


def _model_name_from_db(model_id: str) -> str | None:
    """按 semantic_model_id 反查平台库的模型名称，未配置库或查询失败返回 None。"""
    host = os.getenv("DATA_AGENT_MYSQL_HOST")
    user = os.getenv("DATA_AGENT_MYSQL_USER")
    password = os.getenv("DATA_AGENT_MYSQL_PASSWORD")
    if not (host and user and password):
        return None
    table = os.getenv("DSL_MD_MODEL_TABLE", "semantic_model")
    if not re.fullmatch(r"\w+", table):
        table = "semantic_model"
    try:
        conn = pymysql.connect(
            host=host,
            port=int(os.getenv("DATA_AGENT_MYSQL_PORT", "3306")),
            user=user,
            password=password,
            database=os.getenv("DATA_AGENT_MYSQL_DATABASE", "info_sec_kb_v3"),
            connect_timeout=3,
            read_timeout=3,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT name FROM `{table}` WHERE id=%s AND is_deleted=0 LIMIT 1",
                    (model_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        print(f"[convert] 反查模型名称失败 model_id={model_id}: {e}", flush=True)
        return None
    return row[0].strip() if row and row[0] else None


def _build_domain_values(yml_text: str):
    """从 YML 计算维度表、指标表中各业务域列的取值。

    返回 (dim_values, ind_values)：
    - dim_values: {维度名: (所属业务域, 可关联指标所属业务域)}
    - ind_values: {指标名: (所属业务域, 绑定维度所属业务域)}
    业务域列不允许出现"待确认"，无关联指标时兜底取维度自身所属业务域。
    """
    try:
        data = yaml.safe_load(yml_text) or {}
    except yaml.YAMLError:
        return {}, {}

    domain_names = {}
    for d in data.get("semantic_model_business_domain") or []:
        if d.get("id") is not None and d.get("name"):
            domain_names[str(d["id"])] = d["name"]

    def dname(did):
        return domain_names.get(str(did)) if did is not None else None

    def uniq(names):
        return [n for n in dict.fromkeys(names) if n]

    indicators = data.get("semantic_model_indicator") or []
    ind_domain_by_code = {ind.get("indicator_code"): dname(ind.get("business_domain_id")) for ind in indicators}

    dims = data.get("semantic_model_dimension") or []
    dim_own_by_code = {}
    for dim in dims:
        own = uniq(dname(a.get("businessDomain")) for a in dim.get("entity_attribute") or [])
        if not own and len(domain_names) == 1:
            own = list(domain_names.values())
        dim_own_by_code[dim.get("dim_code")] = own

    dim_values = {}
    for dim in dims:
        name = dim.get("dim_name")
        if not name:
            continue
        own = dim_own_by_code.get(dim.get("dim_code"), [])
        linked = [ind_domain_by_code.get(c) for c in dim.get("indicator") or []]
        for ind in indicators:
            bound = {bd.get("dim_code") for bd in ind.get("bind_dimensions") or []}
            if dim.get("dim_code") in bound:
                linked.append(ind_domain_by_code.get(ind.get("indicator_code")))
        linked = uniq(linked) or own
        dim_values[name] = (", ".join(own), ", ".join(linked))

    ind_values = {}
    for ind in indicators:
        name = ind.get("indicator_name")
        if not name:
            continue
        own = dname(ind.get("business_domain_id"))
        bound_domains = uniq(
            d for bd in ind.get("bind_dimensions") or [] for d in dim_own_by_code.get(bd.get("dim_code"), [])
        )
        ind_values[name] = (own or "", ", ".join(bound_domains))

    return dim_values, ind_values


def _rewrite_table_rows(lines, start, values):
    """重写从 start 行（表头）开始的表格，按首列名称回填最后两列业务域取值。"""
    i = start + 1
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        if cells and set(cells[0]) - {"-", ":", " "}:
            if cells[0] in values and len(cells) >= 3:
                cells[-2], cells[-1] = values[cells[0]]
                lines[i] = "| " + " | ".join(cells) + " |"
        i += 1
    return i


def _fill_business_domain_columns(md: str, yml_text: str) -> str:
    """用 YML 中的确定性数据回填维度表、指标表的业务域列，消除"待确认"。"""
    dim_values, ind_values = _build_domain_values(yml_text)
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            if header[:1] == ["维度"] and "可关联指标所属业务域" in header:
                i = _rewrite_table_rows(lines, i, dim_values)
                continue
            if header[:1] == ["指标"] and "绑定维度所属业务域" in header:
                i = _rewrite_table_rows(lines, i, ind_values)
                continue
        i += 1
    return "\n".join(lines)


async def _call_model(yml_text: str) -> str:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": EXTRACT_PROMPT + yml_text}],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(
            f"{BASE_URL}/chat/completions", json=body, headers=headers
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"模型调用失败: {resp.status_code} {resp.text[:500]}")
    return resp.json()["choices"][0]["message"]["content"]


@app.post("/dsl/convert")
async def convert(request: Request, file: UploadFile):
    raw = await file.read()
    try:
        yml_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件不是有效的 UTF-8 文本")

    form = await request.form()
    print(f"[convert] 上传文件名={file.filename!r} 表单字段={list(form.keys())}", flush=True)

    md = await _call_model(yml_text)
    # 模型偶尔会用 ```markdown 围栏包整篇，落盘前剥掉
    md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", md.strip())
    md = _fill_business_domain_columns(md, yml_text)

    # 命名优先级：YML 名称字段 > 平台库反查 > 上传文件名 > 模型 id
    model_id = _semantic_model_id(yml_text)
    model_name = _semantic_model_name(yml_text)
    if not model_name and model_id:
        model_name = _model_name_from_db(model_id)
    filename = ""
    if model_name:
        filename = f"{_safe_filename(model_name)}.md"
    if filename == ".md":
        filename = ""
    if not filename:
        stem = Path(file.filename or "").stem.strip()
        if stem and re.search(r"[一-鿿]", stem):
            filename = f"{_safe_filename(stem)}.md"
    if not filename or filename == ".md":
        filename = f"semantic_model_{model_id or f'unknown_{int(time.time())}'}.md"
    print(f"[convert] model_id={model_id} 名称={model_name!r} 输出文件={filename!r}", flush=True)
    (FILES_DIR / filename).write_text(md, encoding="utf-8")

    return {
        "filename": filename,
        "download_url": f"{str(request.base_url).rstrip('/')}/files/{filename}",
    }


@app.get("/files/{filename}")
def download(filename: str):
    if not re.fullmatch(r"[\w.-]+", filename):
        raise HTTPException(400, "非法文件名")
    path = FILES_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=filename, media_type="text/markdown")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("DSL_MD_PORT", "8012")))
