from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.observability.langfuse_client import trace_generation
from app.domain.models import AtomicTask, TaskPlan


class TaskPlanningError(RuntimeError):
    pass


class _ModelTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=1000)
    depends_on: list[int] = Field(default_factory=list)


class _ModelPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_multi_question: bool
    tasks: list[_ModelTask] = Field(default_factory=list, max_length=5)


_SYSTEM_PROMPT = """你是企业数据分析任务拆分器，只输出JSON，不回答问题。
判断用户输入是否包含多个可以分别交付结果的数据任务。
规则：
1. 单个目标的连续步骤（例如“查询销售额并分析趋势”）通常是一个任务。
2. 不同指标、不同实体、不同时间目标或不同交付物且可分别回答时，拆成多个任务。
3. 只有一个查询动作，但明确列出同一语义属性下需要分别返回的多个分类、关系类型、状态或时间切片，也要拆分。例如“查询TDC-3产品的主要适用科室、次要适用科室”必须拆成两个独立任务。
4. 普通返回字段列表或共同分组维度不得拆分。例如“查询产品名称、规格型号”和“按城市和品牌统计销售额”都仍是一个任务；“所有适用科室”也不是多个任务。
5. 每个任务必须补全原句中共享的指标、时间、对象和筛选值，使其脱离其他任务也能理解；不得创造原文没有的信息。
6. depends_on使用从0开始的任务下标。只有“基于上一步结果、再从其中、对上述结果”等确需复用前序结果时才建立依赖；并列分类分支之间没有依赖。
7. 最多5个任务，保持用户原始顺序，不输出推理过程。
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
        if not self._candidate(question):
            return None
        plan: TaskPlan | None = None
        if self.settings.multi_question_model_enabled and self.settings.intent_model_api_key:
            try:
                plan = await self._model_plan(question)
                if plan is not None:
                    self.validate(plan, source_question=question)
            except (httpx.HTTPError, KeyError, ValueError, RuntimeError, json.JSONDecodeError):
                plan = None
        if plan is None:
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
        if not result.is_multi_question or len(result.tasks) < 2:
            return None
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
        if not re.search(r"[;；?？\n]", normalized):
            dependent_plan = self._inline_dependent_plan(normalized)
            if dependent_plan is not None:
                return dependent_plan
        pieces = re.split(
            r"[;；?？\n]+|(?:，|,)?(?:另外|同时|此外|然后|再帮我|还要|以及还要)",
            normalized,
        )
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
        dependent_filter = re.fullmatch(
            r"(.+?)(?:，|,)?并根据(.{1,80}?)(筛选出|过滤出)(.+)", question
        )
        if dependent_filter:
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
        shared_identifiers = set(re.findall(
            r"(?<![0-9A-Za-z])(?=[0-9A-Za-z-]{3,64}(?![0-9A-Za-z-]))"
            r"(?=[0-9A-Za-z-]*[A-Za-z])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+",
            source_question.translate(str.maketrans({
                "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
                "\u2014": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-",
                "\uff0d": "-",
            })),
        ))
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
            normalized_task = task.question.translate(str.maketrans({
                "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
                "\u2014": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-",
                "\uff0d": "-",
            }))
            if not shared_identifiers.issubset(set(re.findall(
                r"(?<![0-9A-Za-z])(?=[0-9A-Za-z-]{3,64}(?![0-9A-Za-z-]))"
                r"(?=[0-9A-Za-z-]*[A-Za-z])[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+",
                normalized_task,
            ))):
                raise TaskPlanningError("子任务遗漏共享的业务型号或编码")

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
        action_count = len(re.findall(
            r"查询|查一下|统计|分析|比较|对比|占比|预测|解释|口径|血缘|"
            r"生成|导出|下载|检查|找出|列出|筛选|匹配|拆分|计算|算(?:出|一下)?|(?:再)?加(?:上)?|是多少",
            question,
        ))
        separators = bool(re.search(
            r"[;；?？\n、]|(?:另外|同时|此外|然后|再帮我|还要|以及还要|以及|和|与|及)", question
        ))
        numbered = len(re.findall(r"(?:^|\s)[一二三四五12345][、.)．]", question)) >= 2
        inline_dependency = bool(re.search(
            r"(?:"
            r"(?:并(?:再)?|然后|再)(?:计算|算(?:出|一下)?|找出|筛选|过滤|排序)"
            r".{0,80}(?:差|客单价|占比|最高|最低|最大|最小|排序|筛选|过滤)"
            r"|匹配.{1,80}(?:所属|适用).{1,40}(?:并|然后|再).{0,20}(?:筛选|过滤)"
            r"|(?:再)?加(?:上)?.{0,40}[，,](?:计算|算).{0,40}客单价"
            r")",
            question,
        ))
        qualified_facets = MultiQuestionPlanner._qualified_facet_parts(question) is not None
        return (
            action_count >= 2 and (separators or numbered or inline_dependency)
        ) or qualified_facets

    @staticmethod
    def _has_action(text: str) -> bool:
        return bool(re.search(
            r"查询|查一下|统计|分析|比较|对比|占比|预测|解释|口径|血缘|"
            r"生成|导出|下载|检查|找出|列出|筛选|匹配|拆分|计算|算(?:出|一下)?|(?:再)?加(?:上)?|是多少",
            text,
        ))

    @staticmethod
    def _depends_on_previous(text: str) -> bool:
        return bool(re.search(
            r"基于(?:上一步|前面|上述|这个结果)|从中|其中|这些结果|"
            r"再对(?:它|其)|(?:再|并)?(?:找出|计算|算).*(?:最高|最低|差|结果)|"
            r"最高比最低", text
        ))
