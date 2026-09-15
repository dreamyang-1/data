from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.observability.langfuse_client import trace_generation
from app.domain.models import AtomicTask, TaskPlan


_ACTION_PATTERN = (
    r"查询|查一下|统计|分析|比较|对比|占比|预测|解释|口径|血缘|"
    r"生成|导出|下载|检查|找出|列出|筛选|匹配|拆分|计算|"
    r"算(?:出|一下)?|(?:再)?加(?:上)?|是多少"
)
_NEW_SENTENCE_ACTION_START = (
    r"(?:请|麻烦|帮我|请帮我|再)?\s*"
    r"(?:查询|查一下|统计|分析|比较|对比|预测|解释|生成|导出|下载|"
    r"检查|找出|列出|筛选|匹配|拆分|计算|算)"
)
_TASK_SPLIT_PATTERN = re.compile(
    rf"[;；?？\n]+|[。！!]\s*(?={_NEW_SENTENCE_ACTION_START})|"
    r"(?:，|,)?(?:另外|同时|此外|然后|再帮我|还要|以及还要)"
)
_DASH_TRANSLATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-",
    "\uff0d": "-",
})
_BUSINESS_IDENTIFIER_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(?=[0-9A-Za-z-]{3,64}(?![0-9A-Za-z-]))"
    r"(?=[0-9A-Za-z-]*[A-Za-z])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+"
)


class TaskPlanningError(RuntimeError):
    pass


class _ModelTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=1000)
    depends_on: list[int] = Field(
        description="必填；并行或首个任务填空数组，依赖任务填从0开始的前序任务下标",
    )


class _ModelPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_structure: Literal["SINGLE_TASK", "PARALLEL_TASKS", "DEPENDENT_TASKS"] = Field(
        description="必填；业务任务结构",
    )
    tasks: list[_ModelTask] = Field(
        max_length=5,
        description="必填；单任务填空数组，多任务填2到5个完整业务问题",
    )


_SYSTEM_PROMPT = """你是数据智能体的业务任务理解与拆分器。只输出符合JSON Schema的JSON，不回答用户问题，不调用工具。

你的输出描述业务任务边界，不描述底层技术步骤。每个任务都会由系统独立执行完整链路：智能语义查询器（ASL结构化提取）→ SQL翻译服务 → SQL执行服务 → 数据集输出 → 结果校验 → 洞察分析。因此，不要把ASL、SQL、取数、校验或洞察这些内部步骤拆成任务。

先在内部完成以下判断，但不要输出推理过程：
1. 提取每个业务目标的动作、指标、返回对象或展示字段、分组维度、筛选值、时间范围与粒度、排序数量、分析或交付要求。
2. 判断每项目标能否形成可单独验收的答案或数据集；再判断目标之间是否需要消费前序结果。
3. 按任务结构输出：
   - SINGLE_TASK：只有一个可验收业务目标，tasks必须为空。
   - PARALLEL_TASKS：存在两个及以上可分别交付、互不消费结果的目标；所有depends_on必须为空。
   - DEPENDENT_TASKS：后续目标必须使用前序任务返回的名单、范围、分类、排名或计算结果；depends_on填写实际依赖。

数据分析任务边界：
1. 同一业务对象、筛选范围和结果粒度下的多个指标、多个展示字段或多个分组维度，通常可由一份ASL和一个数据集承载，属于一个任务。例如“按月统计今年上海市销售额和订单笔数”“查询产品名称、规格型号和生产厂家”均为SINGLE_TASK。
2. 围绕同一份数据完成查询并做趋势、占比、同比环比、排序、异常检测或口语化总结，若最终共同形成一个分析结果，属于一个任务。例如“查询今年上海市销售额并分析月度趋势”为SINGLE_TASK。
3. 不同返回对象、不同关系类型、不同业务主题或用户明确要求分别交付时，应拆分。例如同一产品的“合作经销商名单”和“合作医院名单”是两个任务；两个句子分别查询不同产品和不同对象也是两个任务。
4. 同一语义属性下互斥且业务口径不同的分类分支，需要分别返回时应拆分。例如“主要适用科室”和“次要适用科室”是两个并行任务；“所有适用科室”仍是一个任务。
5. 多个筛选值本身不等于多个任务。例如“分析费森尤斯产品在上海市和江苏省的销售趋势”可在同一数据集中按地区展示，属于一个任务；只有用户明确要求分别形成独立结果时才拆分。
6. 两个完整查询即使只用句号、逗号或口语连接，也要按真实目标拆分，不能依赖“另外、同时、以及”等固定连接词。
7. 排名后再查询入选对象的关系或明细，后一步消费前一步名单，属于DEPENDENT_TASKS。例如“找出销售额下降最大的五个产品，并列出这些产品涉及的经销商和医院”。普通的“查询并分析”不自动建立依赖。
8. “对比今年和去年销售额”是一个比较目标，可作为SINGLE_TASK；“分别查询今年和去年销售额，并比较同比变化”明确要求两份查询结果和后续比较，应拆为两个并行查询加一个依赖二者的比较任务。

子任务生成规则：
1. 每个子任务必须是自然、完整、可独立理解和执行的业务问题。补全原句中对该任务生效的产品、地区、指标、时间、筛选、排序及排除条件，不保留只有“另一个、上述条件”等内容的空泛指代。依赖任务应写成“根据上一步返回的某对象名单/范围……”并同时写明业务对象和后续动作。
2. 只允许复用用户原文明确提供的事实和共享条件；不得创造指标、日期、数量、业务对象、筛选值、口径或数据库字段，不负责替用户消除业务歧义。
3. 并行任务不互相依赖。依赖只表示后续任务确实需要前序结果，不表示语句先后顺序。depends_on使用从0开始的前序任务下标，可引用多个前序任务。
4. 每个任务都必须显式输出depends_on；没有依赖时也必须输出空数组，禁止省略字段。
5. 保持用户目标的原始顺序，不重复、不遗漏，任务总数为2到5。若无需拆分，task_structure输出SINGLE_TASK且tasks输出空数组。

判定示例：
- “查询空心纤维血液透析器合作的经销商名单。查询外周插管中心静脉导管合作的医院名单。” → PARALLEL_TASKS，两个任务均补全各自产品和返回对象。
- “查询TDC-3产品的主要适用科室和次要适用科室。” → PARALLEL_TASKS，两个任务均保留TDC-3。
- “查询空心纤维血液透析器合作的经销商和医院名单。” → PARALLEL_TASKS，因为返回对象和关系结果不同。
- “找出销售额下降最大的五个产品，并列出这些产品涉及的经销商和医院。” → DEPENDENT_TASKS，第二个任务依赖第一个任务。
- “分别查询今年和去年上海市销售额，并比较同比变化。” → DEPENDENT_TASKS，前两个任务无依赖，比较任务同时依赖前两个任务。
- “统计上海市各经销商的区域医院覆盖率和销售额。” → SINGLE_TASK。
- “分析费森尤斯产品在上海市和江苏省最近一年的销售趋势。” → SINGLE_TASK。
"""


class MultiQuestionPlanner:
    """LLM-assisted atomic task planning with deterministic validation/fallback."""

    _REPORT_FACET = re.compile(
        r"(?:整体)?(?:销售|订单|收入|利润|成本|库存|业务|业绩)(?:趋势|走势|变化)"
        r"|(?:医院|客户|门店|地区|区域|渠道|品类|商品|经销商|供应商)"
        r"(?:覆盖(?:情况|数据)?|分布)"
        r"|(?:合作)?(?:经销商|供应商|客户|医院|门店|渠道)"
        r"(?:数据|情况|清单|名单|画像|表现)"
        r"|(?:销售|订单|收入|利润|成本|库存|业务|业绩)"
        r"(?:规模|汇总|概览|构成|结构|排名)"
    )

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport

    async def plan(self, question: str) -> TaskPlan | None:
        model_decided_single = False
        if (
            self.settings.intent_model_enabled
            and self.settings.multi_question_model_enabled
            and self.settings.intent_model_api_key
        ):
            try:
                plan = await self._model_plan(question)
                if plan is not None:
                    self.validate(plan, source_question=question)
                    return plan
                model_decided_single = True
            except (httpx.HTTPError, KeyError, ValueError, RuntimeError, json.JSONDecodeError):
                # A transport, schema or grounding failure falls back to the
                # bounded deterministic planner.
                pass
        ranked_relation_plan = self._ranked_relation_plan(question)
        if ranked_relation_plan is not None:
            self.validate(ranked_relation_plan, source_question=question)
            return ranked_relation_plan
        report_plan = self._report_plan(question)
        if report_plan is not None:
            self.validate(report_plan, source_question=question)
            return report_plan
        parallel_ranking_plan = self._parallel_ranking_plan(question)
        if parallel_ranking_plan is not None:
            self.validate(parallel_ranking_plan, source_question=question)
            return parallel_ranking_plan
        # A valid model single-task decision is authoritative for ordinary
        # language. Only the narrow, proven structural plans above may retain
        # a split when the model misses an explicit report, ranking or
        # dependency contract.
        if model_decided_single:
            return None
        if not self._candidate(question):
            return None
        plan = self._rule_plan(question)
        if plan is None or len(plan.tasks) < 2:
            return None
        # Structured-model plans are validated inside the guarded block above so
        # an ungrounded model split can safely fall back to deterministic rules.
        # Rule plans must still pass the same dependency and grounding checks.
        if plan.planner != "STRUCTURED_MODEL":
            self.validate(plan, source_question=question)
        return plan

    @staticmethod
    def _ranked_relation_plan(question: str) -> TaskPlan | None:
        match = re.fullmatch(
            r"(?P<ranking>(?:查询|找出|列出).+?(?:最高|最低|最大|最小)"
            r"(?:的)?(?:前)?[一二两三四五六七八九十\d]*个?(?:产品|商品))"
            r"(?:，|,)?(?:并|再|然后)(?P<relation>列出|查询|找出)"
            r"(?P<tail>(?:涉及|关联|对应).+)",
            question.strip(" 。"),
        )
        if match is None:
            return None
        return TaskPlan(
            tasks=[
                AtomicTask(task_id="task-1", question=match.group("ranking")),
                AtomicTask(
                    task_id="task-2",
                    question=f"{match.group('relation')}上述产品{match.group('tail')}",
                    depends_on=["task-1"],
                ),
            ],
            planner="DETERMINISTIC_RULE",
        )

    def _parallel_ranking_plan(self, question: str) -> TaskPlan | None:
        """Split two independently ranked targets even when the verb is shared."""
        normalized = question.strip(" ，,。")
        # “分别查询A和B” and “查询A，以及B” commonly omit the second verb.
        # Only accept a split when both operands contain an explicit ranking
        # shape, avoiding splits of ordinary metric/dimension lists.
        match = re.match(
            r"^(?P<prefix>分别)?(?P<verb>查询|统计|找出|列出)"
            r"(?P<left>.+?(?:前[一二两三四五六七八九十\d]+|最高|最多|最低|最少).+?)"
            r"(?:，|,)?(?:以及|同时(?:查询|统计)?|和|与)"
            r"(?P<right>.+(?:前[一二两三四五六七八九十\d]+|最高|最多|最低|最少).*)$",
            normalized,
        )
        if match is None:
            return None
        verb = match.group("verb")
        left = match.group("left").strip(" ，,")
        right = re.sub(r"^(?:查询|统计|找出|列出)", "", match.group("right")).strip(" ，,")
        if not left or not right:
            return None
        return TaskPlan(
            tasks=[
                AtomicTask(task_id="task-1", question=f"{verb}{left}"),
                AtomicTask(task_id="task-2", question=f"{verb}{right}"),
            ],
            planner="DETERMINISTIC_RULE",
        )

    def _report_plan(self, question: str) -> TaskPlan | None:
        """Split an explicitly multi-facet report into independently provable queries.

        A report is a final deliverable, not a query shape.  Collapsing several
        requested facets into one exploratory ASL can silently return only one
        aggregate.  This deterministic path keeps the shared subject/filter text
        and executes every named facet as its own query before report assembly.
        """
        if not re.search(r"(?:分析)?报告|报表", question, re.IGNORECASE):
            return None
        facet_report = re.fullmatch(
            r"(?:请|帮我|给我)?(?:生成|制作|输出)?"
            r"(?P<scope>.+?)(?:销售)?分析(?:报告|报表)[，,；;]?"
            r"(?:包含|包括)(?P<facets>.+)",
            question.strip(" 。"),
        )
        if facet_report is not None:
            scope = facet_report.group("scope").strip(" ，,")
            facets = facet_report.group("facets")
            tasks: list[AtomicTask] = []
            for label, task_text in (
                ("趋势", f"分析{scope}销售趋势"),
                ("排名", f"分析{scope}销售排名"),
                ("异常", f"分析{scope}销售异常"),
            ):
                if label in facets:
                    tasks.append(AtomicTask(
                        task_id=f"task-{len(tasks) + 1}", question=task_text,
                    ))
            if len(tasks) >= 2:
                return TaskPlan(
                    tasks=tasks,
                    planner="DETERMINISTIC_RULE",
                    final_deliverable="COMBINED_REPORT",
                )
        body = re.sub(
            r"[，,；;]?\s*(?:(?:并|再)?(?:输出|生成|形成|制作|导出)|给我)"
            r".{0,24}?(?:分析)?(?:报告|报表).*$",
            "",
            question.strip(),
            flags=re.IGNORECASE,
        ).strip(" ，,；;。")
        matches = list(self._REPORT_FACET.finditer(body))
        # Resolve defensive overlap if a future facet expression is extended.
        selected: list[re.Match[str]] = []
        for match in matches:
            if selected and match.start() < selected[-1].end():
                if match.end() - match.start() > selected[-1].end() - selected[-1].start():
                    selected[-1] = match
                continue
            selected.append(match)
        if len(selected) < 2:
            return None

        prefix = body[: selected[0].start()]
        prefix = re.sub(
            r"^\s*(?:请|麻烦|帮我|给我|请帮我)?\s*"
            r"(?:分析|查询|统计|查看|看看|梳理)?\s*(?:一下|下)?\s*(?:关于)?",
            "",
            prefix,
        ).strip(" ，,；;、")
        if not prefix:
            return None

        tasks: list[AtomicTask] = []
        uses_default_history = not re.search(
            r"(?:19|20)\d{2}年|最近|过去|近(?:一|二|三|四|五|六|七|八|九|十|\d+)"
            r"(?:天|日|周|个月|月|季度|年)|本(?:周|月|季度|年)|上(?:周|月|季度|年)",
            question,
        )
        selected = selected[: self.settings.multi_question_max_tasks]
        for index, match in enumerate(selected):
            end = selected[index + 1].start() if index + 1 < len(selected) else len(body)
            facet = body[match.start():end].strip(" ，,；;、和及与")
            if not facet:
                continue
            matched_facet = match.group(0)
            explicit_detail = bool(re.search(r"清单|名单|明细", matched_facet))
            report_object = re.search(
                r"医院|客户|门店|地区|区域|渠道|品类|商品|经销商|供应商",
                matched_facet,
            )
            bounded_count_facet = bool(
                report_object
                and not explicit_detail
                and re.search(r"覆盖|分布|数据|情况|表现", matched_facet)
            )
            if bounded_count_facet:
                object_name = report_object.group(0)
                if "覆盖" in matched_facet:
                    task_question = f"统计{prefix}已合作{object_name}数"
                elif matched_facet.startswith("合作"):
                    task_question = f"统计{prefix}已合作{object_name}数"
                else:
                    task_question = f"统计{prefix}{facet}中的唯一{object_name}总数"
            elif explicit_detail:
                task_question = f"列出{prefix}{facet}明细"
            else:
                task_question = f"分析{prefix}{facet}"
            if uses_default_history:
                task_question += "，使用数据源全部可用历史"
            tasks.append(AtomicTask(
                task_id=f"task-{len(tasks) + 1}",
                question=task_question,
            ))
        if len(tasks) < 2:
            return None
        return TaskPlan(
            tasks=tasks,
            planner="DETERMINISTIC_RULE",
            final_deliverable="COMBINED_REPORT",
        )

    async def _model_plan(self, question: str) -> TaskPlan | None:
        schema = _ModelPlan.model_json_schema()
        body = {
            "model": self.settings.intent_model_name,
            "messages": [
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT + "\nJSON Schema：" + json.dumps(
                        schema, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                {"role": "user", "content": question},
            ],
            "temperature": 0,
            "enable_thinking": False,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": (
                "Bearer " + self.settings.intent_model_api_key.get_secret_value()
            ),
            "Content-Type": "application/json",
        }
        with trace_generation(
            name="task-dag-planning",
            model=body.get("model"),
            messages=body.get("messages"),
        ) as generation:
            async with httpx.AsyncClient(
                base_url=self.settings.intent_model_base_url.rstrip("/"),
                timeout=self.settings.multi_question_model_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/chat/completions", headers=headers, json=body
                )
                response.raise_for_status()
                payload = response.json()
                generation.set_response(payload)
        content = payload["choices"][0]["message"].get("content")
        if not content:
            raise ValueError("task planner returned empty content")
        result = _ModelPlan.model_validate(json.loads(content))
        if result.task_structure == "SINGLE_TASK":
            if result.tasks:
                raise ValueError("single-task decision must not contain tasks")
            return None
        if len(result.tasks) < 2:
            raise ValueError("multi-task decision must contain at least two tasks")
        has_dependencies = any(item.depends_on for item in result.tasks)
        if result.task_structure == "PARALLEL_TASKS" and has_dependencies:
            raise ValueError("parallel tasks must not contain dependencies")
        if result.task_structure == "DEPENDENT_TASKS" and not has_dependencies:
            raise ValueError("dependent tasks must contain at least one dependency")
        tasks = [
            AtomicTask(
                task_id=f"task-{index + 1}",
                question=item.question.strip(),
                depends_on=[f"task-{value + 1}" for value in item.depends_on],
            )
            for index, item in enumerate(result.tasks)
        ]
        return TaskPlan(tasks=tasks, planner="STRUCTURED_MODEL")

    def _rule_plan(self, question: str) -> TaskPlan | None:
        qualified_facet_plan = self._parallel_qualified_facet_plan(question)
        if qualified_facet_plan is not None:
            return qualified_facet_plan
        normalized = re.sub(r"\r\n?", "\n", question).strip()
        normalized = re.sub(
            r"(?:^|[\s;；])[一二三四五12345][、.)．]\s*", "\n", normalized
        )
        if _TASK_SPLIT_PATTERN.search(normalized) is None:
            dependent_plan = self._inline_dependent_plan(normalized)
            if dependent_plan is not None:
                return dependent_plan
        pieces = _TASK_SPLIT_PATTERN.split(normalized)
        pieces = [piece.strip(" ，,。.") for piece in pieces if piece.strip(" ，,。.")]
        actionable = [piece for piece in pieces if self._has_action(piece)]
        if len(actionable) < 2:
            return None
        tasks: list[AtomicTask] = []
        shared_metrics = self._unique_mentions(
            normalized,
            (
                "销售额", "订单量", "订单", "销售量", "销量", "客户数", "客单价",
                "退款额", "退款率", "退款", "毛利额", "毛利率", "利润额", "利润率",
                "库存量", "库存", "转化率",
            ),
        )
        shared_periods = self._unique_mentions(
            normalized,
            (
                "今天", "昨天", "本周", "上周", "本月", "上月", "本季度", "上季度",
                "今年", "去年", "最近一周", "最近一个月", "最近三个月", "最近半年",
                "最近一年",
            ),
        )
        explicit_periods = re.findall(
            r"(?:19|20)\d{2}年(?:1[0-2]|0?[1-9])月份?"
            r"|(?:19|20)\d{2}年"
            r"|(?:19|20)\d{2}[-/.](?:1[0-2]|0?[1-9])",
            normalized,
        )
        if not shared_periods and explicit_periods:
            shared_periods = list(dict.fromkeys(explicit_periods))
        for index, piece in enumerate(actionable[: self.settings.multi_question_max_tasks]):
            dependency = (
                [f"task-{index}"]
                if index > 0 and self._depends_on_previous(piece)
                else []
            )
            enriched = piece
            if len(shared_metrics) == 1 and shared_metrics[0] not in enriched:
                enriched += f"，指标为{shared_metrics[0]}"
            if len(shared_periods) == 1 and shared_periods[0] not in enriched:
                enriched += f"，时间范围为{shared_periods[0]}"
            tasks.append(
                AtomicTask(
                    task_id=f"task-{index + 1}",
                    question=enriched,
                    depends_on=dependency,
                )
            )
        return TaskPlan(tasks=tasks, planner="DETERMINISTIC_RULE")

    @classmethod
    def _parallel_qualified_facet_plan(cls, question: str) -> TaskPlan | None:
        """Split one shared action into independently requested category facets.

        This is a deterministic safety fallback for the structured planner. It
        deliberately recognises only contrasting business qualifiers and never
        splits an ordinary projection list such as ``名称、规格型号``.
        """
        parts = cls._qualified_facet_parts(question)
        if parts is None:
            return None
        prefix, facets = parts
        tasks = [
            AtomicTask(
                task_id=f"task-{index + 1}",
                question=(prefix + facet).strip(),
            )
            for index, facet in enumerate(facets)
        ]
        return TaskPlan(tasks=tasks, planner="DETERMINISTIC_RULE")

    @staticmethod
    def _qualified_facet_parts(
        question: str,
    ) -> tuple[str, list[str]] | None:
        """Return shared prefix and two contrasting, grounded facet phrases."""
        normalized = question.strip(" ，,。！？?；;")
        qualifier_groups = (
            ("主要", "次要"),
            ("正常", "异常"),
            ("新增", "存量"),
            ("线上", "线下"),
            ("有效", "无效"),
            ("合作", "未合作"),
        )
        qualifier_to_group = {
            qualifier: frozenset(group)
            for group in qualifier_groups
            for qualifier in group
        }
        qualifier_pattern = re.compile("|".join(
            re.escape(value)
            for value in sorted(qualifier_to_group, key=len, reverse=True)
        ))
        split_match = re.search(r"(?:、|，|,|以及|和|与|及)", normalized)
        if split_match is None:
            return None
        left = normalized[:split_match.start()].strip()
        right = normalized[split_match.end():].strip()
        if not left or not right or not MultiQuestionPlanner._has_action(left):
            return None

        right_match = qualifier_pattern.match(right)
        right_qualifier = right_match.group(0) if right_match is not None else None
        if right_qualifier is None:
            return None
        left_matches = list(qualifier_pattern.finditer(left))
        if not left_matches:
            return None
        left_match = left_matches[-1]
        left_start, left_qualifier = left_match.start(), left_match.group(0)
        if (
            left_qualifier == right_qualifier
            or qualifier_to_group[left_qualifier]
            != qualifier_to_group[right_qualifier]
        ):
            return None

        prefix = left[:left_start]
        left_tail = left[left_start + len(left_qualifier):].strip()
        right_tail = right[len(right_qualifier):].strip()
        if left_tail and right_tail:
            if left_tail != right_tail:
                return None
            suffix = left_tail
        elif not left_tail and right_tail:
            suffix = right_tail
        else:
            return None
        if not suffix or len(suffix) > 40:
            return None
        return prefix, [left_qualifier + suffix, right_qualifier + suffix]

    def _inline_dependent_plan(self, question: str) -> TaskPlan | None:
        """Split explicit query→calculation chains even without punctuation."""
        referenced_result = re.fullmatch(
            r"(?P<first>.+?)(?:，|,)?(?:并)?再"
            r"(?P<relation>从|根据|基于)"
            r"(?P<second>(?:上述|上一步|前述|前面|这些|该|其).+"
            r"(?:筛选|过滤|查找|查询|列出|统计|分析).+)",
            question,
        )
        dependent_filter = re.fullmatch(
            r"(.+?)(?:，|,)?并根据(.{1,80}?)(筛选出|过滤出)(.+)", question
        )
        if referenced_result:
            pieces = [
                referenced_result.group("first"),
                referenced_result.group("relation") + referenced_result.group("second"),
            ]
        elif dependent_filter:
            pieces = [
                dependent_filter.group(1),
                "根据" + dependent_filter.group(2)
                + dependent_filter.group(3) + dependent_filter.group(4),
            ]
        else:
            pieces = re.split(
                r"(?:(?:，|,)?(?:并(?:再)?|然后|再)|(?:，|,))(?="
                r"(?:计算|算(?:出|一下)?|找出|筛选|过滤|排序).{0,80}"
                r"(?:差|客单价|占比|最高|最低|最大|最小|排序|筛选|过滤))",
                question,
            )
        pieces = [piece.strip(" ，,。.") for piece in pieces if piece.strip(" ，,。.")]
        if len(pieces) < 2 or not self._has_action(pieces[0]):
            return None
        if not all(self._has_action(piece) for piece in pieces[1:]):
            return None
        shared_identifiers = self._business_identifiers(question)
        for index in range(1, len(pieces)):
            for identifier in shared_identifiers:
                if identifier not in pieces[index]:
                    pieces[index] += f"，业务对象为{identifier}"
        if (
            any("差" in piece for piece in pieces[1:])
            and len(re.findall(r"(?:1[0-2]|0?[1-9])月", pieces[0])) >= 2
            and not re.search(r"按月|每月|分月|分别", pieces[0])
        ):
            pieces[0] += "，分别按月返回"
        tasks = [
            AtomicTask(
                task_id=f"task-{index + 1}",
                question=piece,
                depends_on=[] if index == 0 else [f"task-{index}"],
            )
            for index, piece in enumerate(pieces[: self.settings.multi_question_max_tasks])
        ]
        return TaskPlan(tasks=tasks, planner="DETERMINISTIC_RULE")

    @staticmethod
    def validate(plan: TaskPlan, *, source_question: str | None = None) -> None:
        if not 2 <= len(plan.tasks) <= 5:
            raise TaskPlanningError("任务计划必须包含2到5个原子任务")
        task_ids = [task.task_id for task in plan.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise TaskPlanningError("任务ID重复")
        known: set[str] = set()
        for task in plan.tasks:
            if not task.question.strip():
                raise TaskPlanningError("原子任务问题不能为空")
            if task.task_id in task.depends_on:
                raise TaskPlanningError("任务不能依赖自身")
            unknown = set(task.depends_on) - known
            if unknown:
                raise TaskPlanningError(
                    "任务依赖必须引用排在其前面的任务：" + ",".join(sorted(unknown))
                )
            known.add(task.task_id)
        if source_question is not None:
            MultiQuestionPlanner._validate_grounding(plan, source_question)

    @staticmethod
    def _unique_mentions(text: str, candidates: tuple[str, ...]) -> list[str]:
        # Prefer the longest term so aliases nested in a canonical metric (for
        # example ``退款`` inside ``退款率``) do not look like two independent
        # metrics and suppress safe shared-context propagation.
        selected: list[str] = []
        for value in sorted(candidates, key=len, reverse=True):
            if value in text and not any(value in existing for existing in selected):
                selected.append(value)
        return sorted(selected, key=lambda value: text.find(value))

    @staticmethod
    def _business_identifiers(text: str) -> list[str]:
        normalized = text.translate(_DASH_TRANSLATION)
        return list(dict.fromkeys(_BUSINESS_IDENTIFIER_PATTERN.findall(normalized)))

    @staticmethod
    def _validate_grounding(plan: TaskPlan, source_question: str) -> None:
        """Reject model-created dates, numbers, metrics and quoted filters."""
        source_for_grounding = re.sub(
            r"(?:^|[;；\n])\s*[一二三四五1-5][、.)．]\s*",
            " ",
            source_question,
        )
        source_compact = re.sub(r"\s+", "", source_for_grounding).casefold()
        planned_compact = "".join(
            re.sub(r"\s+", "", task.question).casefold() for task in plan.tasks
        )
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?", source_compact))
        metric_terms = (
            "销售额", "订单量", "销售量", "客户数", "客单价", "退款额", "退款率",
            "毛利额", "毛利率", "利润额", "利润率", "库存量", "转化率",
        )
        temporal_terms = (
            "今天", "昨天", "本周", "上周", "本月", "上月", "本季度", "上季度",
            "今年", "去年", "明年", "最近一周", "最近一个月", "最近三个月",
            "最近半年", "最近一年",
        )
        source_metrics = {value for value in metric_terms if value in source_compact}
        source_temporal = {value for value in temporal_terms if value in source_compact}
        source_quoted = set(re.findall(r"[‘’'\"“”]([^‘’'\"“”]{1,100})[‘’'\"“”]", source_question))
        source_identifiers = set(
            MultiQuestionPlanner._business_identifiers(source_question)
        )
        for task in plan.tasks:
            compact = re.sub(r"\s+", "", task.question).casefold()
            if not set(re.findall(r"\d+(?:\.\d+)?", compact)).issubset(source_numbers):
                raise TaskPlanningError("子任务包含原问题中不存在的数字或日期")
            if not {value for value in metric_terms if value in compact}.issubset(source_metrics):
                raise TaskPlanningError("子任务包含原问题中不存在的指标")
            if not {value for value in temporal_terms if value in compact}.issubset(source_temporal):
                raise TaskPlanningError("子任务包含原问题中不存在的时间条件")
            quoted = set(re.findall(r"[‘’'\"“”]([^‘’'\"“”]{1,100})[‘’'\"“”]", task.question))
            if not quoted.issubset(source_quoted):
                raise TaskPlanningError("子任务包含原问题中不存在的过滤值")
            task_identifiers = set(
                MultiQuestionPlanner._business_identifiers(task.question)
            )
            if not task_identifiers.issubset(source_identifiers):
                raise TaskPlanningError("子任务包含原问题中不存在的业务型号或编码")

        # Grounding must be bidirectional: preventing invented information is
        # insufficient if a plan silently drops one part of the user's request.
        if not source_numbers.issubset(set(re.findall(r"\d+(?:\.\d+)?", planned_compact))):
            raise TaskPlanningError("任务计划遗漏原问题中的数字或日期")
        if not source_metrics.issubset({value for value in metric_terms if value in planned_compact}):
            raise TaskPlanningError("任务计划遗漏原问题中的指标")
        if not source_temporal.issubset({value for value in temporal_terms if value in planned_compact}):
            raise TaskPlanningError("任务计划遗漏原问题中的时间条件")
        if not source_quoted.issubset(set(re.findall(
            r"[‘’'\"“”]([^‘’'\"“”]{1,100})[‘’'\"“”]", " ".join(task.question for task in plan.tasks)
        ))):
            raise TaskPlanningError("任务计划遗漏原问题中的过滤值")
        planned_identifiers = set(
            MultiQuestionPlanner._business_identifiers(
                " ".join(task.question for task in plan.tasks)
            )
        )
        if not source_identifiers.issubset(planned_identifiers):
            raise TaskPlanningError("任务计划遗漏原问题中的业务型号或编码")

        facet_parts = MultiQuestionPlanner._qualified_facet_parts(source_question)
        if facet_parts is not None:
            _, required_facets = facet_parts
            coverage = {
                facet: sum(facet in task.question for task in plan.tasks)
                for facet in required_facets
            }
            if any(count != 1 for count in coverage.values()):
                raise TaskPlanningError("任务计划遗漏或重复了并列业务分支")

        source_goals = MultiQuestionPlanner._goal_signals(source_compact)
        if plan.final_deliverable == "COMBINED_REPORT":
            # Delivery is performed once by the root DAG after every analytical
            # child succeeds; forcing “报告” into each child would incorrectly
            # trigger one export per dataset.
            source_goals.discard("DELIVERY")
        planned_goals = MultiQuestionPlanner._goal_signals(planned_compact)
        if not source_goals.issubset(planned_goals):
            raise TaskPlanningError("任务计划遗漏原问题中的分析目标")
        constraint_markers = {
            marker for marker in ("不要", "不看", "不查", "排除", "剔除", "只看", "仅看")
            if marker in source_compact
        }
        if not constraint_markers.issubset({marker for marker in constraint_markers if marker in planned_compact}):
            raise TaskPlanningError("任务计划遗漏原问题中的排除或限定条件")

    @staticmethod
    def _goal_signals(text: str) -> set[str]:
        patterns = {
            "TREND": r"趋势|走势|按(?:日|周|月|季|年)变化",
            "COMPARE": r"同比|环比|对比|比较|差多少|增长率",
            "COMPOSITION": r"占比|构成|份额|结构",
            "ANOMALY": r"异常|突增|突降|离群|不正常",
            "ROOT_CAUSE": r"归因|原因|为什么|影响因素",
            "FORECAST": r"预测|预估|预计|推算",
            "DETAIL": r"明细|名单|逐笔|每一条|列表",
            "RANK": r"前\d+|后\d+|最高|最低|排名|排序",
            "DEFINITION": r"口径|定义|公式|怎么算",
            "LINEAGE": r"血缘|来源表|来源字段|来自哪",
            "QUALITY": r"数据质量|缺失|重复|空值|对账|延迟",
            "DELIVERY": r"报告|报表|导出|下载|生成(?:excel|xlsx|pdf|word|docx)",
        }
        return {name for name, pattern in patterns.items() if re.search(pattern, text)}

    @staticmethod
    def execution_layers(plan: TaskPlan) -> list[list[AtomicTask]]:
        remaining = {task.task_id: task for task in plan.tasks}
        completed: set[str] = set()
        layers: list[list[AtomicTask]] = []
        while remaining:
            ready = [
                task for task in remaining.values()
                if set(task.depends_on).issubset(completed)
            ]
            if not ready:
                raise TaskPlanningError("任务DAG存在环路或无效依赖")
            layers.append(ready)
            completed.update(task.task_id for task in ready)
            for task in ready:
                remaining.pop(task.task_id)
        return layers

    @classmethod
    def deduplicate(cls, plan: TaskPlan) -> tuple[TaskPlan, dict[str, str]]:
        """Merge identical atomic queries while retaining aliases for final output."""
        canonical_by_key: dict[tuple[str, tuple[str, ...]], str] = {}
        aliases: dict[str, str] = {}
        unique: list[AtomicTask] = []
        resolved: dict[str, str] = {}
        for task in plan.tasks:
            dependencies = tuple(dict.fromkeys(resolved.get(dep, dep) for dep in task.depends_on))
            key = (cls.query_fingerprint(task.question), dependencies)
            existing = canonical_by_key.get(key)
            if existing is not None:
                aliases[task.task_id] = existing
                resolved[task.task_id] = existing
                continue
            canonical_by_key[key] = task.task_id
            resolved[task.task_id] = task.task_id
            unique.append(task.model_copy(update={"depends_on": list(dependencies)}))
        # TaskPlan normally requires at least two nodes. An all-duplicate plan is
        # executed as one task by the orchestrator without weakening that public contract.
        if len(unique) == len(plan.tasks):
            return plan, aliases
        return plan.model_copy(update={"tasks": unique}, deep=True), aliases

    @staticmethod
    def query_fingerprint(question: str) -> str:
        normalized = question.casefold().strip()
        normalized = re.sub(r"[\s，。！？；、,.!?;:：]+", "", normalized)
        normalized = re.sub(r"^(?:请|帮我|麻烦|请帮我)+", "", normalized)
        return normalized

    @staticmethod
    def _candidate(question: str) -> bool:
        action_count = len(re.findall(_ACTION_PATTERN, question))
        separators = bool(
            _TASK_SPLIT_PATTERN.search(question)
            or re.search(r"[、]|(?:以及|和|与|及)", question)
        )
        numbered = len(re.findall(r"(?:^|\s)[一二三四五12345][、.)．]", question)) >= 2
        inline_dependency = bool(re.search(
            r"(?:"
            r"(?:并(?:再)?|然后|再)(?:计算|算(?:出|一下)?|找出|筛选|过滤|排序)"
            r".{0,80}(?:差|客单价|占比|最高|最低|最大|最小|排序|筛选|过滤)"
            r"|匹配.{1,80}(?:所属|适用).{1,40}(?:并|然后|再).{0,20}(?:筛选|过滤)"
            r"|(?:再)?加(?:上)?.{0,40}[，,](?:计算|算).{0,40}客单价"
            r"|(?:并)?再(?:从|根据|基于)(?:上述|上一步|前述|前面|这些|该|其)"
            r".{1,80}(?:筛选|过滤|查找|查询|列出|统计|分析)"
            r")",
            question,
        ))
        qualified_facets = MultiQuestionPlanner._qualified_facet_parts(question) is not None
        return (
            action_count >= 2 and (separators or numbered or inline_dependency)
        ) or qualified_facets

    @staticmethod
    def _has_action(text: str) -> bool:
        return bool(re.search(_ACTION_PATTERN, text))

    @staticmethod
    def _depends_on_previous(text: str) -> bool:
        return bool(re.search(
            r"基于(?:上一步|前面|上述|这个结果)|从中|其中|这些结果|"
            r"再对(?:它|其)|(?:再|并)?(?:找出|计算|算).*(?:最高|最低|差|结果)|"
            r"最高比最低", text
        ))
