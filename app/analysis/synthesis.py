from __future__ import annotations

import asyncio
import json
import math
import re
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis.engine import AnalysisOutput
from app.config import Settings
from app.observability.langfuse_client import trace_generation
from app.domain.models import CanonicalAnalysisRequest, EvidenceItem


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


SYSTEM_PROMPT = """你是企业数据分析结果解释器。你不执行计算，也不创造原因；你只把输入的确定性分析事实整理为用户容易理解的中文结论。

强制规则：
1. 只能使用输入 evidence 中已经出现的信息。禁止补充常识、行业猜测、外部知识或新原因。
2. 不得修改、重新计算或创造任何数字。每个数字必须能在 deterministic_answer 或 facts 中找到。
3. 每条 claim 必须引用输入中存在的 evidence_id。
4. VERIFIED_FACT 只能描述查询或算法已经验证的现象、变化、贡献、残差和覆盖度，不能使用“导致、造成、因为、根本原因”等因果词。
5. SUPPORTED_HYPOTHESIS 只能描述知识库候选解释，必须使用“可能、候选、待核实、尚未验证、需验证”之一，不能声称已经证实。
6. LIMITATION 必须明确说明数据或方法边界，不得弱化输入 warnings。
7. 如果输入明确 causality_established=false，禁止声称任何因素是严格因果原因。
8. 不输出 Markdown、标题、代码块或额外字段，只输出符合Schema的JSON。
9. 优先顺序：先讲发生了什么，再讲数据验证的驱动，再讲待验证候选，最后讲限制。
10. 必须结合 untrusted_user_question 回答用户真正问的问题，并对 deterministic_answer 做自然、简洁的中文润色，避免机械罗列字段名。
11. 对趋势分析必须解释“哪一段变化对总体涨跌贡献最大、后续变化是否抵消”；这属于数值贡献解释，不得写成业务因果。若证据没有产品、地区、渠道等拆分，必须明确无法据此判断具体业务原因，并建议进一步拆分核验。
12. 如果 facts 中存在 answer_plan，它是面向用户的信息取舍边界。只表达其中选中的结论、关键事实、判断、优先级和限制，不得把 omitted_internal_fields 或 internal_diagnostics 中的算法诊断字段重新输出给用户。
13. 不得向用户机械罗列 slope、robust_slope、direction_consistency、coefficient_of_variation、max、min、volatility 等内部算法字段；这些字段只能用于支撑 answer_plan 已选择的业务判断。
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
    ) -> tuple[str, SynthesisOutput]:
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
        schema = SynthesisOutput.model_json_schema()
        body = {
            "model": self.settings.analysis_synthesis_model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{SYSTEM_PROMPT}\n必须严格遵守JSON Schema："
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
                payload: dict[str, Any] | None = None
                for attempt in range(self.settings.analysis_synthesis_max_retries + 1):
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
                        if not retryable or attempt >= self.settings.analysis_synthesis_max_retries:
                            raise
                        await asyncio.sleep(0.2 * (2**attempt))
        if payload is None:
            raise RuntimeError("analysis synthesis returned no payload")
        content = payload["choices"][0]["message"].get("content")
        if not content:
            raise SynthesisValidationError("analysis synthesis returned empty content")
        output = SynthesisOutput.model_validate(json.loads(content))
        self._validate_claims(output, allowed_evidence, analysis)
        return self._render(output), output

    @classmethod
    def _validate_claims(
        cls,
        output: SynthesisOutput,
        allowed_evidence: dict[str, dict[str, Any]],
        analysis: AnalysisOutput,
    ) -> None:
        source_text = json.dumps(
            {"answer": analysis.answer, "facts": analysis.facts, "warnings": analysis.warnings},
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
            if any(not cls._number_is_grounded(value, source_numbers) for value in claim_numbers):
                raise SynthesisValidationError("claim contains an ungrounded number")
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
                raise SynthesisValidationError("verified fact must cite data or analysis evidence")
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
        labels = {
            ClaimCertainty.VERIFIED_FACT: "已验证结论",
            ClaimCertainty.SUPPORTED_HYPOTHESIS: "待验证原因",
            ClaimCertainty.LIMITATION: "分析限制",
        }
        grouped: list[str] = []
        for certainty in ClaimCertainty:
            statements = [item.statement for item in output.claims if item.certainty == certainty]
            if statements:
                grouped.append(f"{labels[certainty]}：" + "；".join(statements) + "。")
        return "\n".join(grouped)
