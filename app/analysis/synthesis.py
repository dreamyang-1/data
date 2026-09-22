from __future__ import annotations

import asyncio
import json
import math
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis.engine import AnalysisOutput
from app.config import Settings
from app.observability.langfuse_client import trace_generation
from app.domain.models import CanonicalAnalysisRequest, EvidenceItem

if TYPE_CHECKING:
    from app.services.context_builder import ContextEnvelope


class ClaimCertainty(StrEnum):
    VERIFIED_FACT = "VERIFIED_FACT"
    SUPPORTED_HYPOTHESIS = "SUPPORTED_HYPOTHESIS"
    LIMITATION = "LIMITATION"


class SynthesisClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=500)
    certainty: ClaimCertainty
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class SynthesisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[SynthesisClaim] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def no_duplicate_statements(self) -> "SynthesisOutput":
        normalized = [re.sub(r"\s+", "", item.statement) for item in self.claims]
        if len(normalized) != len(set(normalized)):
            raise ValueError("synthesis claims must not be duplicated")
        return self


SYSTEM_PROMPT = """你是专门解释企业数据的资深数据分析专家，负责“数据洞察分析”节点。你的任务是写出面向用户、可以复核的分析说明：使用了哪些数据、如何比较、哪些证据支持判断、哪些问题尚不能判断。最终输出节点另行提供结论，因此这里不要结论先行，也不要重复一份完整总结。你不执行新计算，不创造业务原因，不输出模型内部思维链。

强制规则：
1. 只能使用输入 evidence 中已经出现的信息。禁止补充常识、行业猜测、外部知识或新原因。
2. 不得修改、重新计算或创造任何数字。allowed_numbers_for_output 是唯一允许出现在 claim 中的数字清单；不在清单中的年份、月份、日期、比例、数量和序号一律不得输出。
3. 每条 claim 必须引用输入中存在的 evidence_id；VERIFIED_FACT 的 evidence_ids 至少包含一个 kind=ANALYSIS_RESULT 的证据，不能只引用 QUERY_RESULT。
4. VERIFIED_FACT 只能描述查询或算法已经验证的现象、变化、贡献、残差和覆盖度，不能使用“导致、造成、因为、根本原因”等因果词。
5. SUPPORTED_HYPOTHESIS 只能描述知识库候选解释，必须使用“可能、候选、待核实、尚未验证、需验证”之一，不能声称已经证实。
6. LIMITATION 必须明确说明数据或方法边界，不得弱化输入 warnings。
7. 如果输入明确 causality_established=false，禁止声称任何因素是严格因果原因。
8. 不输出 Markdown、标题、代码块或额外字段，只输出符合Schema的JSON。
9. 按分析顺序组织：比较口径、关键观察、证据之间的比较或抵消关系、判断边界。不要以“结论”开头，结束时至多一句简短判断，不再重复前文数字。
10. 必须围绕 untrusted_user_question，结合 deterministic_answer 和 facts 写成连续、充分展开的中文分析报告，而非几条简短结论。证据充分时正文以800至1500个汉字为目标，组织成6至9个自然段，每条claim对应一个自然段，通常120至200字。长度目标属于写作要求，不是业务数字，不得在正文复述。证据不足时按实际内容缩短，禁止为凑字数创造事实或重复表述。
11. 对趋势分析必须解释“哪一段变化对总体涨跌贡献最大、后续变化是否抵消”；这属于数值贡献解释，不得写成业务因果。若证据没有产品、地区、渠道等拆分，必须明确无法据此判断具体业务原因，并建议进一步拆分核验。
12. 如果 facts 中存在 answer_plan，它约束允许表达的业务判断。可以展开 facts 中支撑这些判断的已验证业务数据与比较依据，但不得创造新的判断；不得把 omitted_internal_fields 或 internal_diagnostics 中的算法诊断字段重新输出给用户。
13. 不得向用户机械罗列 slope、robust_slope、direction_consistency、coefficient_of_variation、max、min、volatility 等内部算法字段；这些字段只能用于支撑 answer_plan 已选择的业务判断。
14. 对单一汇总值或少量明细，也应以完整段落解释已有查询范围、结果含义及比较限制，而不是只报数字。没有对照期、基准或明细拆分时，不得推断增长、优劣或原因；不要反复描述字段数、行数、空值或截断状态。用户问题中的年份必须原样复述，不得自行展开成起止日期。
"""


SENIOR_ANALYSIS_EXPERT_PROMPT = """
高级分析专家工作方法：
你要像一名面向管理者和业务负责人的高级数据分析专家一样解释数据。专业性来自把证据与判断之间的关系讲清楚，而不是堆砌术语、增加篇幅或猜测业务原因。

一、先交代比较依据
- 第一条说明本次观察的指标、范围、时间粒度及比较基准，直接进入数据分析，不预先宣布总结性结论。
- 说明分析对象和比较口径；只有输入已经提供时间、范围或对照对象时才写出这些信息。
- 不要用“数据如下”“经过分析”等空泛开场，不要先复述用户问题。

二、按证据类型展开分析
- 趋势类：解释总体方向、最显著变化区间、拐点或反转，以及后续变化是延续还是抵消；只能使用 facts 或 deterministic_answer 已明确给出的阶段判断。
- 排名与结构类：说明领先项、落后项、主要贡献项、反向抵消项、集中度或覆盖度；突出头部与其余部分的差异，不要机械逐行复述。
- 对比类：明确谁高谁低、差异方向及比较基准；证据没有差值或变化率时，不得自行计算。
- 分布与异常类：说明范围、典型水平、离群或异常现象及其影响范围；只有算法已标记异常时才能称为异常。
- 归因类：严格区分“数值贡献”“相关候选解释”和“已验证因果”。贡献最大不等于业务根因；知识库内容只能作为待验证假设。
- 明细与单值查询：直接报告结果及必要的数据边界，不套用复杂分析模板。

三、把可复核的分析依据展开
- 每条围绕一个问题组织“观察到的数据 + 比较方法 + 该证据能支持的含义”。变化率应说明以哪个时期为基准；恢复比例应说明相对哪段下降。只解释输入已经验证的计算结果，缺少计算结果时只作有依据的定性比较。
- 趋势分析先比较期初与期末，再分段观察下降和回升，解释回升是否抵消前期下降；回升不等于恢复到起点，单次转向也不等于持续反转。只有输入数据支持时才采用这些说法。
- 多维、趋势、排名或归因任务采用分析报告的篇幅：证据充分时六至九段，总计约八百至一千五百字。每段围绕一个明确的分析问题，把证据、比较关系和适用边界讲完整，不写成一句话要点集。
- 把支持判断的事实、反向证据或例外连贯说明，最后说明哪些结论还需要更多时期或更细维度的数据才能验证。不要罗列完整结论、执行建议或重复总结。
- 不重复同一个数字，不把字段数、行数、空值率等技术信息当作业务洞察，除非它直接影响结论可靠性。

四、给出有边界的业务解释
- 只有 answer_plan 已选择 interpretations 或 priorities 时，才可以表达相应业务判断或关注优先级。
- 如果证据只能说明现象，就明确停留在现象层；如果缺少产品、区域、渠道、客户或时间拆分，可提出“进一步按已有维度拆分核验”，但不能指定未经证据支持的原因。
- limitations 和 warnings 必须转化成用户能理解的影响说明，例如样本不完整会限制哪些判断，而不是只说“数据有限”。

五、表达要求
- 按“小型分析报告”写作，使用连贯自然段和必要的承接语，不用项目符号、序号或“结论—事实—建议”的简短模板。前后段逐步推进，不反复换说法复述同一现象。
- 可以依次展开口径与基准、全期变化、主要变化区间、后续抵消或延续、反向证据、判断边界和后续核验所需数据；这些是可选分析角度，输入缺乏证据的部分必须省略。
- 使用专业、清晰、克制的中文，按分析依据展开，避免学术腔、模板腔和内部算法术语。不输出隐藏思维过程、自我对话或探索草稿，只交付面向用户的证据说明。
- 保留业务对象、指标、单位和口径原名；不要把相关性写成因果，不要把候选解释写成事实。
- 最终仍只输出符合 Schema 的 JSON claims；上述结构是 claims 的内容组织方法，不得输出标题或额外字段。
"""


SYNTHESIS_PROMPT_ADDENDUM = """
Additional presentation rules:
- When the input identifies a task number or task question, keep that task
  boundary in every claim; never blend evidence from different tasks.
- Preserve verified business object names and field labels exactly as supplied.
- Do not create chart or image URLs. Image Markdown is rendered only from
  trusted MCP/chart artifacts outside this synthesis model.
"""


class SynthesisValidationError(ValueError):
    pass


class QwenAnalysisSynthesizer:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport

    async def synthesize(
        self,
        request: CanonicalAnalysisRequest,
        analysis: AnalysisOutput,
        evidence: list[EvidenceItem],
        *,
        context: "ContextEnvelope | None" = None,
        agent_prompt: str = "",
    ) -> tuple[str, SynthesisOutput]:
        """Render verified facts with an optional bounded, row-free context."""

        if analysis.facts.get("decision_source") != "DETERMINISTIC_ALGORITHM":
            raise SynthesisValidationError(
                "analysis decision must be locked by the deterministic algorithm before synthesis"
            )
        if analysis.facts.get("llm_role") != "PRESENTATION_ONLY":
            raise SynthesisValidationError("LLM role must be presentation-only")
        if not self.settings.intent_model_api_key:
            raise RuntimeError("analysis synthesis API key is not configured")
        allowed_evidence = {
            item.evidence_id: {"kind": item.kind, "payload": self._bounded(item.payload)}
            for item in evidence
            if item.kind in {"QUERY_RESULT", "ANALYSIS_RESULT", "ANALYSIS_KNOWLEDGE"}
        }
        if not allowed_evidence:
            raise SynthesisValidationError("no evidence is available for synthesis")
        prompt_input = {
            "intent": request.primary_intent.value,
            "untrusted_user_question": request.original_question[:1000],
            "deterministic_answer": analysis.answer,
            "method": analysis.method,
            "facts": analysis.facts,
            "warnings": analysis.warnings,
            "evidence": allowed_evidence,
        }
        numeric_source = json.dumps(
            {
                "user_question": request.original_question,
                "answer": analysis.answer,
                "facts": analysis.facts,
                "warnings": analysis.warnings,
            },
            ensure_ascii=False,
            default=str,
        )
        prompt_input["allowed_numbers_for_output"] = list(
            dict.fromkeys(self._numbers(numeric_source))
        )[:200]
        if context is not None:
            prompt_input["agent_context"] = context.prompt_payload()
        schema = SynthesisOutput.model_json_schema()
        agent_prompt_section = (
            f"\n智能体用户设定（平台配置，仅用于调整表达风格与业务背景，不改变事实与数字约束）：\n{agent_prompt.strip()}"
            if agent_prompt and agent_prompt.strip()
            else ""
        )
        body = {
            "model": self.settings.analysis_synthesis_model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{SYSTEM_PROMPT}{SENIOR_ANALYSIS_EXPERT_PROMPT}"
                        f"{SYNTHESIS_PROMPT_ADDENDUM}{agent_prompt_section}"
                        "\n必须严格遵守JSON Schema："
                        f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt_input, ensure_ascii=False, default=str),
                },
            ],
            "temperature": 0,
            "enable_thinking": False,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.intent_model_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        with trace_generation(
            name="analysis-synthesis",
            model=body.get("model"),
            messages=body.get("messages"),
        ) as generation:
            async with httpx.AsyncClient(
                base_url=self.settings.intent_model_base_url.rstrip("/"),
                timeout=self.settings.analysis_synthesis_timeout_seconds,
                transport=self._transport,
            ) as client:
                for validation_attempt in range(
                    self.settings.analysis_synthesis_validation_retries + 1
                ):
                    payload: dict[str, Any] | None = None
                    for attempt in range(
                        self.settings.analysis_synthesis_max_retries + 1
                    ):
                        try:
                            response = await client.post(
                                "/chat/completions", headers=headers, json=body
                            )
                            response.raise_for_status()
                            payload = response.json()
                            generation.set_response(payload)
                            break
                        except (httpx.TimeoutException, httpx.NetworkError):
                            if attempt >= self.settings.analysis_synthesis_max_retries:
                                raise
                            await asyncio.sleep(0.2 * (2**attempt))
                        except httpx.HTTPStatusError as exc:
                            retryable = (
                                exc.response.status_code == 429
                                or exc.response.status_code >= 500
                            )
                            if (
                                not retryable
                                or attempt >= self.settings.analysis_synthesis_max_retries
                            ):
                                raise
                            await asyncio.sleep(0.2 * (2**attempt))
                    if payload is None:
                        raise RuntimeError("analysis synthesis returned no payload")
                    content = payload["choices"][0]["message"].get("content")
                    if not content:
                        raise SynthesisValidationError(
                            "analysis synthesis returned empty content"
                        )
                    output = SynthesisOutput.model_validate(json.loads(content))
                    try:
                        self._validate_claims(
                            output,
                            allowed_evidence,
                            analysis,
                            user_question=request.original_question,
                        )
                    except SynthesisValidationError as exc:
                        if validation_attempt >= (
                            self.settings.analysis_synthesis_validation_retries
                        ):
                            raise
                        body["messages"].extend([
                            {"role": "assistant", "content": content},
                            {
                                "role": "user",
                                "content": (
                                    "上一个JSON未通过证据校验："
                                    f"{exc}。请删除无依据内容并重新输出完整JSON。"
                                    "数字只能来自allowed_numbers_for_output；"
                                    "表述应紧贴deterministic_answer和facts，"
                                    "不得展开日期、补算数字或新增业务判断；"
                                    "VERIFIED_FACT必须引用ANALYSIS_RESULT证据。"
                                ),
                            },
                        ])
                        continue
                    return self._render(output), output
        raise RuntimeError("analysis synthesis validation loop ended unexpectedly")

    @classmethod
    def _validate_claims(
        cls,
        output: SynthesisOutput,
        allowed_evidence: dict[str, dict[str, Any]],
        analysis: AnalysisOutput,
        *,
        user_question: str,
    ) -> None:
        source_text = json.dumps(
            {
                "user_question": user_question,
                "answer": analysis.answer,
                "facts": analysis.facts,
                "warnings": analysis.warnings,
            },
            ensure_ascii=False,
            default=str,
        )
        source_numbers = cls._numbers(source_text)
        uncertainty_markers = ("可能", "候选", "待核实", "尚未验证", "需验证")
        certainty_markers = ("已经证实", "确定是", "根本原因", "主要原因是", "证明了")
        causal_markers = ("导致", "造成", "因为", "源于")
        for claim in output.claims:
            unknown = set(claim.evidence_ids) - set(allowed_evidence)
            if unknown:
                raise SynthesisValidationError(f"claim references unknown evidence: {sorted(unknown)}")
            claim_numbers = cls._numbers(claim.statement)
            ungrounded_numbers = [
                value
                for value in claim_numbers
                if not cls._number_is_grounded(value, source_numbers)
            ]
            if ungrounded_numbers:
                raise SynthesisValidationError(
                    f"claim contains an ungrounded number: {ungrounded_numbers[0]:g}"
                )
            if claim.certainty == ClaimCertainty.VERIFIED_FACT and any(
                marker in claim.statement for marker in causal_markers
            ):
                raise SynthesisValidationError("verified fact must not assert causality")
            referenced_kinds = {
                allowed_evidence[evidence_id]["kind"] for evidence_id in claim.evidence_ids
            }
            if (
                claim.certainty == ClaimCertainty.VERIFIED_FACT
                and "ANALYSIS_RESULT" not in referenced_kinds
            ):
                raise SynthesisValidationError(
                    "verified fact must cite ANALYSIS_RESULT evidence"
                )
            if claim.certainty == ClaimCertainty.SUPPORTED_HYPOTHESIS:
                if "ANALYSIS_KNOWLEDGE" not in referenced_kinds:
                    raise SynthesisValidationError("hypothesis must cite analysis knowledge")
                if not any(marker in claim.statement for marker in uncertainty_markers):
                    raise SynthesisValidationError("hypothesis must state uncertainty")
                if any(marker in claim.statement for marker in certainty_markers):
                    raise SynthesisValidationError("hypothesis overstates causal certainty")
                matched_knowledge = analysis.facts.get("matched_knowledge", [])
                matched_text = json.dumps(
                    matched_knowledge, ensure_ascii=False, default=str
                )
                if not matched_knowledge or cls._lexical_grounding_ratio(
                    claim.statement, matched_text
                ) < 0.30:
                    raise SynthesisValidationError(
                        "hypothesis was not selected by the deterministic matching algorithm"
                    )
            if (
                claim.certainty == ClaimCertainty.VERIFIED_FACT
                and analysis.method == "validated_query_result_summary"
            ):
                cls._validate_query_summary_wording(
                    claim.statement,
                    analysis,
                    source_text,
                )
                continue
            minimum_grounding = {
                ClaimCertainty.VERIFIED_FACT: 0.75,
                # Candidate selection has already been locked by the
                # deterministic matched_knowledge gate above. This second gate
                # only prevents unrelated prose from being added.
                ClaimCertainty.SUPPORTED_HYPOTHESIS: 0.40,
                ClaimCertainty.LIMITATION: 0.30,
            }[claim.certainty]
            if cls._lexical_grounding_ratio(claim.statement, source_text) < minimum_grounding:
                raise SynthesisValidationError("claim contains insufficiently grounded wording")
        profile_columns = analysis.facts.get("profile_columns") or []
        if profile_columns:
            rendered_claims = "\n".join(item.statement for item in output.claims)
            missing_profiles = [
                str(column)
                for column in profile_columns
                if str(column) not in rendered_claims
            ]
            if missing_profiles:
                raise SynthesisValidationError(
                    "ranking synthesis omitted requested profile columns: "
                    + ", ".join(missing_profiles)
                )
        if analysis.warnings and not any(
            item.certainty == ClaimCertainty.LIMITATION for item in output.claims
        ):
            raise SynthesisValidationError("analysis warnings require a limitation claim")

    @staticmethod
    def _numbers(text: str) -> list[float]:
        values: list[float] = []
        for raw, percent in re.findall(r"(?<![A-Za-z0-9_])(-?\d+(?:\.\d+)?)(%)?", text):
            value = float(raw)
            if math.isfinite(value):
                values.append(value)
                if percent:
                    values.append(value / 100)
        return values

    @staticmethod
    def _number_is_grounded(value: float, sources: list[float]) -> bool:
        return any(
            math.isclose(value, source, rel_tol=0.002, abs_tol=0.005)
            for source in sources
        )

    @staticmethod
    def _validate_query_summary_wording(
        statement: str,
        analysis: AnalysisOutput,
        source_text: str,
    ) -> None:
        unsupported_inference = (
            "反映",
            "意味着",
            "驱动",
            "原因",
            "需求旺盛",
            "采购意愿",
            "经营改善",
            "经营恶化",
        )
        if any(
            marker in statement and marker not in source_text
            for marker in unsupported_inference
        ):
            raise SynthesisValidationError(
                "query summary must not add business interpretation"
            )
        columns = [
            str(column)
            for column in analysis.facts.get("columns", [])
            if str(column).strip()
        ]
        mentions_result_field = any(column in statement for column in columns)
        describes_result_contract = any(
            marker in statement
            for marker in (
                "命中",
                "返回",
                "记录",
                "结果",
                "完整",
                "截断",
                "空值",
                "缺失",
                "平均",
                "范围",
                "不同值",
            )
        )
        if not mentions_result_field and not describes_result_contract:
            raise SynthesisValidationError(
                "query summary claim does not describe a validated result fact"
            )

    @staticmethod
    def _lexical_grounding_ratio(statement: str, source_text: str) -> float:
        normalized_statement = re.sub(r"\s+", "", statement).casefold()
        normalized_source = re.sub(r"\s+", "", source_text).casefold()
        # These words express presentation/certainty and need not occur in the
        # deterministic source. Business nouns, entities and event descriptions
        # still have to be lexically grounded.
        for word in (
            "已验证结论", "已验证", "结论", "数据显示", "分析显示", "当前",
            "可能", "候选", "待核实", "尚未验证", "需验证", "分析限制",
            "需要复核", "需要验证", "表明", "说明",
        ):
            normalized_statement = normalized_statement.replace(word, "")
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized_statement))
        tokens = [chinese[index:index + 2] for index in range(max(0, len(chinese) - 1))]
        tokens.extend(re.findall(r"[a-z_]{2,}", normalized_statement))
        if not tokens:
            return 1.0
        grounded = sum(token in normalized_source for token in tokens)
        return grounded / len(tokens)

    @classmethod
    def _bounded(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 5:
            return "<depth-limited>"
        if isinstance(value, dict):
            return {
                str(key)[:100]: cls._bounded(item, depth + 1)
                for key, item in list(value.items())[:50]
            }
        if isinstance(value, list):
            return [cls._bounded(item, depth + 1) for item in value[:20]]
        if isinstance(value, str):
            return value[:500]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:500]

    @staticmethod
    def _render(output: SynthesisOutput) -> str:
        introductions = {
            ClaimCertainty.VERIFIED_FACT: "从本次查询结果来看，",
            ClaimCertainty.SUPPORTED_HYPOTHESIS: "结合已有业务资料，",
            ClaimCertainty.LIMITATION: "需要注意的是，",
        }
        paragraphs: list[str] = []
        for certainty in ClaimCertainty:
            statements = [
                item.statement.rstrip("。；; ")
                for item in output.claims
                if item.certainty == certainty
            ]
            if statements:
                paragraphs.append(
                    introductions[certainty] + "。\n\n".join(statements) + "。"
                )
        return "\n\n".join(paragraphs)
