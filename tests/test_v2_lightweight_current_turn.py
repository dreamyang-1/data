"""Empty-context lightweight current-turn contract; full contract everywhere else.

Recorded model responses test the branch gate and contract shape, not model
accuracy. Pending resume, user option confirmation and historical follow-ups
must keep the complete frozen contract.
"""
import json

import pytest

from app.semantic_v2.recognition import (LIGHTWEIGHT_CURRENT_TURN_PROMPT,
    BUSINESS_SEMANTIC_EXTRACTION_RULES, PARSE_PROMPT,
    STANDALONE_COMPLETION_PROMPT, FOLLOWUP_COMPLETION_PROMPT)
from test_v2_raw_turn_recognition import (planner, metric_step, parse, request,
    IDENTITY, turns, NOW)
from test_v2_pending_recognition import ask, catalog


SCHEMA_MARKER = '\nJSON Schema:\n'


def split_instruction_and_schema(body):
    # defer 路径数据在前、合同在后；按内容定位合同消息，不依赖固定下标
    for message in body['messages']:
        content = message['content']
        if SCHEMA_MARKER in content:
            instruction, schema_json = content.split(SCHEMA_MARKER, 1)
            return instruction, json.loads(schema_json), message['role']
    raise AssertionError('contract message not found')


def data_message(body):
    return next(m for m in body['messages'] if SCHEMA_MARKER not in m['content'])


@pytest.mark.asyncio
async def test_empty_state_still_calls_model_with_lightweight_contract(catalog):
    steps = [metric_step('销售额')]
    engine, transport = planner(catalog, steps)
    results = await turns(engine, steps)

    # The model is still invoked for fine-grained extraction; the lightweight
    # branch is a contract change, not a regex bypass.
    assert len(transport.calls) >= 1
    instruction, schema, role = split_instruction_and_schema(transport.calls[0])
    assert role == 'system'
    assert instruction == LIGHTWEIGHT_CURRENT_TURN_PROMPT
    # History-selection clauses that cannot apply in an empty conversation are gone.
    assert 'scope-checked summary' not in instruction
    assert PARSE_PROMPT != instruction
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['target_task_id'] == {'type': 'null'}
    assert proposal['properties']['task_version'] == {'type': 'null'}
    assert proposal['properties']['pending_id'] == {'type': 'null'}
    assert len(proposal['allOf']) == 2

    # The completed span/relation remain valid model output.
    result = results[0]
    assert result.context_trace['FINAL_STATUS'] == 'ACCEPTED'
    assert result.context_trace['FINAL_RELATION'] == 'NEW_TASK'
    mention = result.parse.mentions[0]
    assert mention.surface == '销售额'

    # Record branch input sizes (character counts, not timed seconds).
    full_instruction_size = len(PARSE_PROMPT)
    lightweight_size = len(instruction) + len(json.dumps(schema, ensure_ascii=False))
    print(f'lightweight instruction+schema chars={lightweight_size}')
    print(f'full instruction chars alone={full_instruction_size}')
    assert lightweight_size > 0


@pytest.mark.asyncio
async def test_second_turn_with_history_keeps_full_contract(catalog):
    steps = [metric_step('销售额'), metric_step('再看销售额', follow=True)]
    engine, transport = planner(catalog, steps)
    await turns(engine, steps)

    # Turn two has stored tasks; the full frozen contract must be used.
    second_turn_call = transport.calls[2]
    instruction, schema, role = split_instruction_and_schema(second_turn_call)
    assert role == 'system'
    assert instruction.startswith(PARSE_PROMPT)
    assert 'scope-checked summary' in instruction
    assert instruction != LIGHTWEIGHT_CURRENT_TURN_PROMPT
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['target_task_id'] != {'type': 'null'}
    context = json.loads(data_message(second_turn_call)['content'])
    assert context['task_context']['candidate_tasks']


@pytest.mark.asyncio
async def test_deferred_follow_up_uses_short_chinese_contract(catalog):
    # defer 追问轮走窄合同：只下发关系判定+补全问题两个字段的通用短提示词，
    # mentions/操作标记等没有消费方的字段不进 schema，也不吃长英文合同。
    from app.semantic_v2.context_question import publish_context_task
    from app.semantic_v2.models import ContextQuestionState
    first = request(question='查看2025年安徽省各城市每月销售额', message_id='short-first')
    def new_task_proposal(context):
        return dict(status='ACCEPTED', relation='NEW_TASK', target_task_id=None,
            state_version=context['task_context']['state_version'],
            task_version=None, pending_id=None)
    engine, _ = planner(catalog, [(first.question,
        {'context_proposal': new_task_proposal, 'completed_question': first.question}, {})])
    engine.defer_new_task_binding = True
    result = await engine.run(first, IDENTITY, allow_standalone_new_task_passthrough=True)
    state = publish_context_task(result.next_state, chat=first, created_at=NOW,
        frame=ContextQuestionState(original_question=first.question,
            execution_question=first.question, source_message_id=first.message_id))

    def supplied_proposal(context):
        ctx = context['task_context']
        target = next(r for r in ctx['candidate_tasks'] if r['task_id'] == ctx['active_task_id'])
        return dict(status='ACCEPTED', relation='MODIFY', target_task_id=target['task_id'],
            state_version=ctx['state_version'], task_version=target['task_version'],
            pending_id=None, data_source='NEW_QUERY')
    current = {'context_proposal': supplied_proposal,
        'completed_question': '查看2025年上海市各城市每月销售额'}
    engine, transport = planner(catalog, [('上海市的', current, {})])
    engine.defer_new_task_binding = True
    await engine.run(request(question='上海市的', message_id='short-next'),
        IDENTITY, state=state, allow_standalone_new_task_passthrough=True)

    instruction, schema, role = split_instruction_and_schema(transport.calls[0])
    assert role == 'system'
    # 数据在前、合同在后：打开节点先看到本轮问题与上下文摘要
    assert transport.calls[0]['messages'][0]['role'] == 'user'
    assert transport.calls[0]['messages'][1]['role'] == 'system'
    assert instruction == FOLLOWUP_COMPLETION_PROMPT
    assert not instruction.startswith(PARSE_PROMPT)
    # 通用短合同：没有写死的业务域例子，没有提及级提取条款
    assert 'Extract only facts' not in instruction
    assert 'Prismaflex' not in instruction
    assert 'mentions' not in schema['properties']
    assert set(schema['properties']) == {'context_proposal', 'completed_question'}
    # 关系判定与补全的硬约束仍在
    assert 'UNRESOLVED' in instruction
    assert '查询明细' in instruction
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['target_task_id'] != {'type': 'null'}
    context = json.loads(data_message(transport.calls[0])['content'])
    assert context['task_context']['candidate_tasks']
    print(f'follow-up deferred instruction chars={len(instruction)}'
          f' schema chars={len(json.dumps(schema, ensure_ascii=False))}')


@pytest.mark.asyncio
async def test_deferred_empty_context_uses_standalone_slim_contract(catalog):
    # 空会话独立新问题走窄合同：关系由空上下文确定性给出 NEW_TASK，模型只判
    # off_topic 并原样返回补全问题；提及级字段连同类型定义都不进 schema。
    def supplied_proposal(context):
        return dict(status='ACCEPTED', relation='NEW_TASK', target_task_id=None,
            state_version=context['task_context']['state_version'],
            task_version=None, pending_id=None)
    steps = [('查询销售额', {'context_proposal': supplied_proposal,
        'completed_question': '查询销售额'}, None)]
    engine, transport = planner(catalog, steps)
    engine.defer_new_task_binding = True
    # 生产 bridge 的空会话独立新问题走直通（见 context_v1_execution 的
    # allow_standalone_new_task_passthrough=True），不会进入语义编辑调用。
    result = await engine.run(
        request(question='查询销售额', message_id='turn0'), IDENTITY,
        allow_standalone_new_task_passthrough=True)
    results = [result]

    instruction, schema, role = split_instruction_and_schema(transport.calls[0])
    assert role == 'system'
    assert transport.calls[0]['messages'][0]['role'] == 'user'
    assert instruction == STANDALONE_COMPLETION_PROMPT
    assert not instruction.startswith(PARSE_PROMPT)
    assert not instruction.startswith(LIGHTWEIGHT_CURRENT_TURN_PROMPT)
    # 窄合同：没有提及级字段，也没有它们的类型定义
    assert set(schema['properties']) == {'context_proposal', 'completed_question'}
    assert 'Mention' not in schema.get('$defs', {})
    assert 'OperationMarker' not in schema.get('$defs', {})
    assert 'DialogueAct' not in schema.get('$defs', {})
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['target_task_id'] == {'type': 'null'}
    assert proposal['properties']['task_version'] == {'type': 'null'}
    assert proposal['properties']['pending_id'] == {'type': 'null'}
    assert len(proposal['allOf']) == 2
    result = results[0]
    assert result.context_trace['FINAL_STATUS'] == 'ACCEPTED'
    assert result.context_trace['FINAL_RELATION'] == 'NEW_TASK'
    print(f'standalone deferred instruction chars={len(instruction)}'
          f' schema chars={len(json.dumps(schema, ensure_ascii=False))}')


@pytest.mark.asyncio
async def test_gate_confirmed_new_task_skips_recognition_model(catalog):
    # 门控判定全新业务问题：不做承接识别，零模型调用按独立新问题直通；
    # 状态推进与模型判 NEW_TASK 的路径一致
    engine, transport = planner(catalog, [])
    engine.defer_new_task_binding = True
    result = await engine.run(
        request(question='查询销售额', message_id='gate-new'), IDENTITY,
        allow_standalone_new_task_passthrough=True,
        gate_confirmed_new_task=True)

    assert len(transport.calls) == 0
    assert result.completed_question == '查询销售额'
    assert result.context_trace['FINAL_STATUS'] == 'ACCEPTED'
    assert result.context_trace['FINAL_RELATION'] == 'NEW_TASK'
    assert result.fallback_reason == 'ASL_OWNS_CATALOG_BINDING'
    from app.semantic_v2.state_machine import ConversationState
    assert ConversationState.model_validate(
        result.next_state.payload).state_version == 1


@pytest.mark.asyncio
async def test_free_form_role_labels_pass_through_extraction_items():
    from app.semantic_v2.pipeline import CurrentTurnSemanticParse
    from app.semantic_v2.recognition import current_turn_extraction_items

    parse = CurrentTurnSemanticParse.model_validate({
        'mentions': [
            {'mention_id': 'm0', 'surface': '南京', 'normalized_surface': '南京',
             'start_char': 0, 'end_char': 2, 'candidate_roles': ['城市'],
             'source_turn_id': 't1'},
            {'mention_id': 'm1', 'surface': '费森尤斯', 'normalized_surface': '费森尤斯',
             'start_char': 2, 'end_char': 6, 'candidate_roles': ['品牌', '厂家'],
             'source_turn_id': 't1'},
            {'mention_id': 'm2', 'surface': '销售额', 'normalized_surface': '销售额',
             'start_char': 6, 'end_char': 9, 'candidate_roles': ['MEASURE'],
             'source_turn_id': 't1'},
        ],
    })
    items = current_turn_extraction_items(parse)
    assert [item['surface'] for item in items] == ['南京', '费森尤斯', '销售额']
    assert items[0]['labels'] == ('城市',)
    assert items[1]['labels'] == ('品牌', '厂家')
    # Governed enum labels still map to Chinese display text.
    assert items[2]['labels'] == ('指标',)


@pytest.mark.asyncio
async def test_pending_resume_keeps_full_contract_not_lightweight(catalog):
    previous, _ = await ask(catalog)
    option = previous.decision.options[0]
    # Replay the exact recorded resume path while keeping the transport so the
    # actual model request can be inspected.
    text = option.option_id
    step = (text, parse(text, (), follow=True), {})
    engine, transport = planner(catalog, [step])
    result = await engine.run(request(question=text, message_id='answer'), IDENTITY,
        state=previous.next_state, pending=previous.pending_state)

    # Exactly one model call answers the Pending, and it must use the full
    # frozen contract with the offered pending identity, never the lightweight
    # branch.
    assert len(transport.calls) == 1
    instruction, schema, role = split_instruction_and_schema(transport.calls[0])
    assert role == 'system'
    assert instruction.startswith(PARSE_PROMPT)
    assert 'scope-checked summary' in instruction
    assert BUSINESS_SEMANTIC_EXTRACTION_RULES in instruction
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['pending_id'] != {'type': 'null'}
    assert result.resolution['dialogue_act'] == 'ANSWER_CLARIFICATION'
