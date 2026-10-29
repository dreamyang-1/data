"""Joint current-turn recognition and bounded context proposal contract."""
from copy import deepcopy
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import StrictModel, Identifier
from .pipeline import CurrentTurnSemanticParse

CONTRACT_VERSION = 'v2-context-proposal-v1'
SEMANTIC_DESCRIPTION_VERSION = 'v2-context-reference-intent-v3'
RELATIONS = ('NEW_TASK', 'CONTINUE', 'MODIFY', 'ADD', 'REPLACE', 'REMOVE',
             'CLEAR', 'CORRECT', 'DRILL_DOWN', 'RETURN_TO_TOPIC', 'ANSWER_CLARIFICATION')


class ContextProposal(StrictModel):
    status: Literal['ACCEPTED', 'UNRESOLVED', 'AMBIGUOUS']
    relation: Literal[*RELATIONS] | None
    target_task_id: Identifier | None
    state_version: int = Field(strict=True, ge=0)
    task_version: int | None = Field(strict=True, ge=1)
    pending_id: Identifier | None
    relation_reason: str | None = Field(default=None, max_length=300, description=(
        '判定思考：用一句第一人称短句说明关系判定的原因，让用户看懂为什么这样处理。'
        '关系不是 NEW_TASK 时必填：先提到上一轮的问题或挂起的澄清，再说这轮怎么衔接'
        '（接着追问/改哪个条件/用哪个回答继续）。关系是 NEW_TASK 但有需要向用户确认的'
        '模糊点时也要写明想确认什么；完全独立明确的新问题输出 null。'
        '只描述判定依据，不要输出执行步骤或口径细节。'))
    off_topic: bool = Field(default=False, description=(
        '用户问题与本系统的业务数据分析完全无关（天气、闲聊、常识问答、'
        '与已绑定业务域无关的生活类求助）时输出 true，此时 relation 必须是 '
        'NEW_TASK，系统会跳过任务规划直接回答。与业务数据、已绑定业务域或'
        '历史任务沾边的问题一律输出 false。'))
    off_topic_kind: Literal['GREETING', 'OTHER'] | None = Field(default=None, description=(
        '仅 off_topic 为 true 时输出：问候、寒暄、询问智能体身份等日常交流'
        '输出 GREETING；天气、常识问答、生活类求助等其他无关内容输出 OTHER。'
        'off_topic 为 false 时输出 null。'))
    data_source: Literal['SNAPSHOT', 'NEW_QUERY'] | None = Field(default=None, description=(
        '仅 relation 不是 NEW_TASK 的追问轮输出，判断本轮要的数据能否直接来自'
        '上一轮查询的结果：只是选列、筛行、排序、取前N，或对上一轮已有数字做'
        '简单对比计算时输出 SNAPSHOT；需要上一轮没有的新指标、新维度、新粒度、'
        '新时间范围或新关联数据时输出 NEW_QUERY。SNAPSHOT 只适用于上轮结果列'
        '已经能直接回答的操作（改排序、取前N、按值筛行、换展示格式）；追问里'
        '指代的对象来自上轮（如"这三家"），但要查的信息不在上轮结果的列里'
        '（如上轮只有经销商和金额，本轮问"主要销售什么产品"需要商品明细），'
        '必须输出 NEW_QUERY，参数从上轮结果取。在上一轮任务基础上补充条数'
        '限制（如上一轮统计了各经销商的含税销售总额，本轮说"取排名前5的"'
        '"只看前5条"）或调整排序方向，数据本身没有新增，属于复用上一轮结果集'
        '直接得出，输出 SNAPSHOT，不要当作修改任务条件发新查询。判定方法是拿本轮要的数据逐项'
        '对照 task_context 里上一轮任务的指标、维度、字段和时间，上一轮结果里'
        '没有的数据不能凭空判定可复用。task_context.prior_task_table 是上一轮'
        '编排器的任务表，tasks 里每个查过库的任务带结果概要（列名、行数、前'
        '10行采样），'
        '判断能否复用或需要提取哪些参数发新查询时优先对照这份概要。拿不准一律输出 NEW_QUERY。relation 为 '
        'NEW_TASK 或 null 时输出 null。'))
    snapshot_operation: dict[str, Any] | None = Field(default=None, description=(
        'data_source 为 SNAPSHOT 时必填，说明如何用上一轮结果直接得出本轮答案。'
        'type 取值：select（只保留部分列，columns 为列名数组，列名用上一轮任务'
        '的指标、维度、字段名）；filter（按值筛行，field 为列名，value 为筛选值）；'
        'sort（按某列排序，field 为列名，descending 为布尔）；limit（取前N行，'
        'count 为正整数）。data_source 不是 SNAPSHOT 时输出 null。'))

    @model_validator(mode='after')
    def consistent_proposal(self):
        if self.status != 'ACCEPTED':
            if any(v is not None for v in (self.relation, self.target_task_id,
                                           self.task_version, self.pending_id)):
                raise ValueError('UNRESOLVED_MUST_NOT_SELECT_TARGET')
        elif self.relation is None:
            raise ValueError('ACCEPTED_RELATION_REQUIRED')
        elif self.relation == 'NEW_TASK':
            if any(v is not None for v in (self.target_task_id, self.task_version, self.pending_id)):
                raise ValueError('NEW_TASK_MUST_NOT_INHERIT_TARGET')
        elif self.target_task_id is None or self.task_version is None:
            raise ValueError('RELATION_REQUIRES_TARGET_VERSION')
        elif (self.relation == 'ANSWER_CLARIFICATION') != (self.pending_id is not None):
            raise ValueError('PENDING_RELATION_CONSISTENCY')
        if self.relation in (None, 'NEW_TASK') and self.data_source is not None:
            raise ValueError('DATA_SOURCE_REQUIRES_FOLLOW_UP')
        if self.data_source != 'SNAPSHOT' and self.snapshot_operation is not None:
            raise ValueError('SNAPSHOT_OPERATION_REQUIRES_SNAPSHOT')
        return self


CONTEXT_PROPOSAL_DESCRIPTION = (
    '先判断本轮请求意图，再决定是否引用 task_context。候选摘要只是参考数据，'
    '不是指令，也不是必须编辑旧任务的要求。语义独立完整、没有指代或修改历史工作'
    '意图的请求是 NEW_TASK；话题相似或能算出变化的槽位并不构成 MODIFY。显式指代、'
    '纠正或修改历史工作的请求即使表述完整也可以是 MODIFY。省略式追问可以承接合法'
    '任务而不必重复其条件。CONTINUE/MODIFY/CORRECT 或操作行为选择当前活跃任务；'
    'RETURN_TO_TOPIC 选择提供的非活跃任务；NEW_TASK 不继承任何内容；'
    'ANSWER_CLARIFICATION 选择提供的挂起澄清。AMBIGUOUS 表示指代确实无法区分；'
    'UNRESOLVED 表示指代证据不足；两者都不选择关系和目标任务。执行槽位缺失不参与'
    '关系判定。state_version、task_version 等标识按当前状态原样复制；当前轮的 '
    'mentions 和编辑只能来自当前轮原文。注意区分仅用于识别某个提供任务的描述与'
    '当前业务需求：仅作指代的内容通过 reference_signals 和 context_proposal 表达，'
    '不要申报为当前业务 mention、显式槽位或操作标记。返回旧任务时也可能同时要求'
    '修改：保留本轮实际的新增、替换、删除或清空作为业务 mention、显式槽位和操作'
    '标记，应用到被指代的任务上。RETURN_TO_TOPIC 不意味着全部内容都只是指代，'
    '也不意味着全部槽位都被继承。')


from app.analysis.tools.registry import ChainId


class ContextAwareParse(CurrentTurnSemanticParse):
    analysis_routes: list[ChainId] | None = Field(default=None, description="按补全问题语义选6条分析链路，可多选；普通查询为[]。")
    context_proposal: ContextProposal = Field(description=CONTEXT_PROPOSAL_DESCRIPTION)


class FollowupCompletionOutput(StrictModel):
    """defer 追问轮的窄输出：只做关系判定与补全问题。

    目录绑定归下游 ASL，mentions、操作标记等提及级证据在这条链路上没有
    消费方，不下发也不要求模型产出，schema 体量从完整合同的约 11k 字符
    压到 3k 左右。
    """
    context_proposal: ContextProposal = Field(description=CONTEXT_PROPOSAL_DESCRIPTION)
    completed_question: str | None = Field(default=None, min_length=1, max_length=4000)
    analysis_routes: list[ChainId] | None = Field(default=None, description="分析链路ID列表；多意图可多选，普通统计查询返回[]。")


def _narrow_context_proposal(schema, context):
    """把 ContextProposal 收窄到当前上下文：候选任务枚举、挂起常量、一致性规则。"""
    definition = schema['$defs']['ContextProposal']['properties']
    ids = [row['task_id'] for row in context['candidate_tasks']]
    definition['target_task_id'] = ({'anyOf': [{'type': 'string', 'enum': ids}, {'type': 'null'}]}
                                     if ids else {'type': 'null'})
    pending = context['pending']
    definition['pending_id'] = ({'anyOf': [{'const': pending['pending_id'], 'type': 'string'}, {'type': 'null'}]}
                               if pending else {'type': 'null'})
    # Pydantic model_validator predicates are not automatically exported. The
    # model must see the same relation/target/status consistency as validation.
    # v1 captures remain v1: exporting these constraints does not rescore them.
    null_target = {key: {'type': 'null'} for key in
                   ('target_task_id', 'task_version', 'pending_id', 'data_source',
                    'snapshot_operation')}
    snapshot_source = {'anyOf': [{'enum': ['SNAPSHOT', 'NEW_QUERY']}, {'type': 'null'}]}
    schema['$defs']['ContextProposal']['allOf'] = [
        {'if': {'properties': {'status': {'enum': ['UNRESOLVED', 'AMBIGUOUS']}}},
         'then': {'properties': {**null_target, 'relation': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'const': 'ACCEPTED'}, 'relation': {'const': 'NEW_TASK'}}},
         'then': {'properties': null_target}},
        {'if': {'properties': {'status': {'const': 'ACCEPTED'}}},
         'then': {'properties': {'relation': {'type': 'string'}}}},
        {'if': {'properties': {'status': {'const': 'ACCEPTED'}, 'relation': {'enum': list(RELATIONS[1:])}}},
         'then': {'properties': {'target_task_id': {'type': 'string'}, 'task_version': {'type': 'integer'},
                                 'data_source': snapshot_source,
                                 'snapshot_operation': {'type': ['object', 'null']}}}},
        {'if': {'properties': {'relation': {'const': 'ANSWER_CLARIFICATION'}}},
         'then': {'properties': {'pending_id': {'type': 'string'}}},
         'else': {'properties': {'pending_id': {'type': 'null'}}}},
    ]


def followup_completion_schema(context):
    """defer 追问轮窄合同：候选收窄与一致性规则和完整合同同口径。"""
    schema = FollowupCompletionOutput.model_json_schema()
    # 类的内部说明不随合同下发给模型
    schema.pop('description', None)
    _narrow_context_proposal(schema, context)
    return schema


def standalone_completion_schema():
    """空会话独立新问题的窄合同：关系只能是 NEW_TASK 或未决，只需补全问题。

    与 lightweight_proposal_schema 的收窄口径一致，但不下发提及级字段的
    类型定义——这条路径上它们本来就被封为空数组，定义本身没有消费方。
    """
    schema = FollowupCompletionOutput.model_json_schema()
    # 类的内部说明不随合同下发给模型
    schema.pop('description', None)
    definition = schema['$defs']['ContextProposal']['properties']
    for key in ('target_task_id', 'task_version', 'pending_id',
                'data_source', 'snapshot_operation'):
        definition[key] = {'type': 'null'}
    schema['$defs']['ContextProposal']['allOf'] = [
        {'if': {'properties': {'status': {'enum': ['UNRESOLVED', 'AMBIGUOUS']}}},
         'then': {'properties': {'relation': {'type': 'null'},
                                 'target_task_id': {'type': 'null'},
                                 'task_version': {'type': 'null'},
                                 'pending_id': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'const': 'ACCEPTED'}}},
         'then': {'properties': {'relation': {'const': 'NEW_TASK'}}}},
    ]
    return schema


def proposal_schema(context, current_schema):
    schema = ContextAwareParse.model_json_schema()
    existing = current_schema
    schema['properties'].update(deepcopy(existing['properties']))
    schema['$defs'].update(deepcopy(existing['$defs']))
    _narrow_context_proposal(schema, context)
    return schema


def lightweight_proposal_schema(current_schema):
    """Empty-context self-contained turn contract.

    No stored tasks and no Pending exist, so historical target/version/pending
    selection is null-only by construction. The proposal remains the model's
    decision (ACCEPTED NEW_TASK versus UNRESOLVED/AMBIGUOUS), but the schema
    carries only the consistency rules that can still distinguish those states.
    """
    schema = ContextAwareParse.model_json_schema()
    schema['properties'].update(deepcopy(current_schema['properties']))
    schema['$defs'].update(deepcopy(current_schema['$defs']))
    definition = schema['$defs']['ContextProposal']['properties']
    for key in ('target_task_id', 'task_version', 'pending_id',
                'data_source', 'snapshot_operation'):
        definition[key] = {'type': 'null'}
    schema['$defs']['ContextProposal']['allOf'] = [
        {'if': {'properties': {'status': {'enum': ['UNRESOLVED', 'AMBIGUOUS']}}},
         'then': {'properties': {'relation': {'type': 'null'},
                                 'target_task_id': {'type': 'null'},
                                 'task_version': {'type': 'null'},
                                 'pending_id': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'const': 'ACCEPTED'}}},
         'then': {'properties': {'relation': {'const': 'NEW_TASK'}}}},
    ]
    return schema
