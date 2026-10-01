"""Declared scenario oracles for contract evaluation, not model-generated gold."""
from app.semantic_v2.enums import *
from app.semantic_v2.models import *
from app.semantic_v2.pipeline import *
from app.semantic_v2.slot_reducer import TaskPatch, apply_task_patch, semantic_fingerprint
from app.semantic_v2.state_machine import *
from tools.phase25_1.fixtures import NOW, ref, time_spec, conversation, operation


CONTRAST_CASES = [
    {'id': f'contrast:{category}:{index}', 'category': category, 'left': left, 'right': right, 'parameter': index + 1}
    for category, pairs in {
        'LIMIT_RANK': [('展示前5条','显示销售額前5名'), ('保留前10条','取销量前10名'), ('前3条记录','销量最低3名'), ('先展示2条','销售额最高2名'), ('当前结果前8条','所有医院销售额前8名')],
        'METRIC': [('查询销售额','查询销售量'), ('销售金额趋势','销售数量趋势'), ('医院销售额','医院销售量'), ('商品收入','商品销量'), ('季度销售金额','季度销售数量')],
        'ADD_REPLACE': [('再加销售量','改成销售量'), ('同时显示利润','只保留利润'), ('补充订单笔数','改为订单笔数'), ('加上退货数量','只看退货数量'), ('保留销售额并增加成本','把销售额换成成本')],
        'FOLLOWUP_NEW': [('还有呢','换个问题'), ('继续看这些医院','新问题：查库存'), ('那它呢','重新开始查询'), ('上面这些还有吗','另外查询供应商'), ('接着展示','切换话题')],
        'TREND_COMPARE': [('销售额月度趋势','销售额同比去年'), ('销售量走势','销售量环比上期'), ('近一年收入变化','今年收入同比去年'), ('订单数季度趋势','订单数季度同比'), ('利润历史走势','本月利润环比')],
        'LIST_GROUP': [('列出医院名单','按医院统计销售额'), ('列出供应商','按供应商统计销量'), ('商品名单','按商品汇总收入'), ('城市列表','按城市统计订单数'), ('科室名单','按科室统计销售额')],
        'LINEAGE_DEFINITION': [('医院名称来自哪个表','销售额怎么计算'), ('amount_with_tax上游是什么','含税销售額是什么口径'), ('商品名称来源字段','订单数指标含义'), ('库存字段来源','库存金额指标定义'), ('订单表上游','订单金额计算公式')],
        'REFRESH_REVISE': [('刷新这个结果','将指标改为销量'), ('用最新数据重跑','更改筛选条件'), ('重试同一查询','时间改为去年'), ('重新获取相同口径结果','把月度改为季度'), ('保持口径刷新','只保留利润指标')],
    }.items() for index, (left, right) in enumerate(pairs)
]


def contrast_semantics(case):
    category, n = case['category'], case['parameter']
    if category == 'LIMIT_RANK':
        limit = [5,10,3,2,8][n-1]
        return DatasetLimitOperation(limit=limit), RankingSpec(rank_by=ref('volume' if n in {2,3} else 'sales'), direction='ASC' if n == 3 else 'DESC', limit=limit)
    if category == 'METRIC':
        prefix = ['sales', 'monthly_sales', 'hospital_sales', 'product_sales', 'quarterly_sales'][n-1]
        return TaskSemanticState(metrics=[ref(prefix+'_amount')]), TaskSemanticState(metrics=[ref(prefix+'_quantity')])
    if category == 'ADD_REPLACE':
        state = TaskSemanticState(metrics=[ref()])
        code = ['volume', 'profit', 'order_count', 'return_quantity', 'cost'][n-1]
        results = [apply_task_patch(state, TaskPatch.compile([operation(kind, value=[ref(code).model_dump(mode='json')])], base_task_version=1)).semantics for kind in ('ADD', 'REPLACE')]
        return tuple(results)
    if category == 'FOLLOWUP_NEW':
        return CurrentTurnSemanticParse(reference_signals=['ELLIPSIS']), CurrentTurnSemanticParse(topic_shift_signals=['EXPLICIT_NEW_TASK'])
    if category == 'TREND_COMPARE':
        metric = ref(['sales', 'volume', 'income', 'orders', 'profit'][n-1])
        kind = 'MOM' if n in {2,5} else 'YOY'
        return TimeSeriesPayload(measures=[metric], time=time_spec()), ComparisonPayload(measures=[metric], time=time_spec(), comparison=ComparisonSpec(comparison_type=kind, baseline=TimeBaseline(relative_period='PREVIOUS_PERIOD' if kind == 'MOM' else 'PREVIOUS_YEAR'), calculation='GROWTH_RATE', output_metrics=[metric]))
    if category == 'LIST_GROUP':
        r = ref('hospital', SemanticRole.PROJECTION_FIELD, CatalogType.ATTRIBUTE)
        return DetailRowsPayload(source_entity=ref('hospital', SemanticRole.SOURCE_ENTITY, CatalogType.ENTITY), projection_spec=ProjectionSpec(items=[ProjectionItem(output_field_id='hospital', ref=r, role='PROJECTION_FIELD', position=0)])), GroupedAggregatePayload(measures=[ref()], group_by=[ref('hospital', SemanticRole.GROUP_BY, CatalogType.DIMENSION)])
    if category == 'LINEAGE_DEFINITION':
        return LineagePayload(lineage_target=FieldTarget(ref=ref('name', SemanticRole.SOURCE_ENTITY, CatalogType.ATTRIBUTE))), MetricDefinitionPayload(metric_refs=[ref()])
    return ControlPayload(control_action='REFRESH'), ControlPayload(control_action='REVISE')


METAMORPHIC_CASES = [dict(id=f'metamorphic:{i}', left=f'查询{a}和{b}两个指标', right=f'同时查看{b}及{a}',
    codes=[f'metric_{i}_a', f'metric_{i}_b'], ground_truth_scope=['TaskSemanticState.metrics SET equality'])
    for i, (a,b) in enumerate([
        ('销售额','销售量'), ('收入','订单数'), ('利润','成本'), ('退货额','退货量'), ('库存金额','库存数量'),
        ('医院数','经销商数'), ('含税金额','未税金额'), ('采购额','采购量'), ('净销售额','折扣额'), ('毛利','净利润'),
        ('预算金额','目标金额'), ('付款金额','回款金额'), ('出库数量','入库数量'), ('客单价','订单笔数'), ('发货额','发货量'),
        ('成交金额','成交数量'), ('运费','税额'), ('开票金额','未开票金额'), ('应收金额','实收金额'), ('坏账金额','逾期金额')])]


def pending_record(state, topic='a', profit_code='profit', dimension_code='region'):
    task = state.tasks['task:' + topic]
    options = [ClarificationOption(option_id=profit_code, display_label='利润', canonical_ref=ref(profit_code), evidence=['DECLARED_TEST_CATALOG']),
               ClarificationOption(option_id=dimension_code, display_label='地区', canonical_ref=ref(dimension_code, SemanticRole.GROUP_BY, CatalogType.DIMENSION), evidence=['DECLARED_TEST_CATALOG'])]
    blockers = [PendingBlocker(blocker_id='metric-choice', plan_path='metrics', expected_answer_type='OPTION_ID', information_gain=2,
                              already_asked=True, options=[options[0], ClarificationOption(option_id=profit_code+':alternative', display_label='另一种利润口径', canonical_ref=ref(profit_code+':alternative'), evidence=['DECLARED_TEST_CATALOG'])]),
                PendingBlocker(blocker_id='dimension-choice', plan_path='dimensions', expected_answer_type='OPTION_ID', information_gain=1,
                               options=[options[1], ClarificationOption(option_id=dimension_code+':alternative', display_label='另一种地区口径', canonical_ref=ref(dimension_code+':alternative', SemanticRole.GROUP_BY, CatalogType.DIMENSION), evidence=['DECLARED_TEST_CATALOG'])])]
    return PendingRecord(pending_id='pending:a', topic_id='topic:' + topic, task_id=task.task_id, task_version=task.active_version,
        slot_path='metrics', question='请选择指标', asked_at=NOW, blockers=blockers, active_blocker_id='metric-choice',
        asked_slots=['metrics'], clarification_rounds=1, created_at=NOW, updated_at=NOW)


def long_conversation(group):
    state = conversation()
    scene = {
        'hospital_sales': ('医院', '医院销售额', '医院销售量', '医院利润', '城市'),
        'dealer_orders': ('经销商', '订单金额', '订单笔数', '订单毛利', '经销商级别'),
        'product_returns': ('退货商品', '退货金额', '退货数量', '退款金额', '商品类别'),
        'warehouse_stock': ('仓库', '库存金额', '库存数量', '库存成本', '仓库地区'),
        'department_purchases': ('科室', '采购金额', '采购数量', '采购成本', '医院等级'),
    }[group]
    code_map = {key: group+':'+key for key in ('sales', 'volume', 'profit', 'region')}
    initial = state.model_dump()
    for task in initial['tasks'].values():
        task['versions'][0]['semantics']['metrics'] = [ref(code_map['sales']).model_dump()]
    state = ConversationState.model_validate(initial)
    rows = []
    specifications = [
        ('查询销售额', 'SET', 'sales'), ('再加销售量', 'ADD', 'volume'), ('销售量也加上', 'ADD', 'volume'),
        ('只保留销售额', 'REPLACE', 'sales'), ('清空指标', 'CLEAR', None), ('恢复之前的指标', 'INHERIT', 'volume'),
        ('指定销售额', 'SET', 'sales'), ('同时增加销售量', 'ADD', 'volume'), ('去掉销售额', 'REMOVE', 'sales'),
        ('指标改成销售额', 'REPLACE', 'sales'), ('分析时先确认口径和维度', 'PENDING', None),
        ('选择利润指标', 'ANSWER_METRIC', 'profit'), ('换到库存话题', 'SWITCH', None),
        ('库存话题指标改为数量', 'REPLACE', 'volume'), ('刷新库存话题结果', 'REFRESH', None),
        ('返回刚才的销售话题', 'RETURN', None), ('选择地区维度', 'ANSWER_DIMENSION', 'region'),
        ('刷新销售话题结果', 'REFRESH', None), ('仍按地区分析', 'SET_DIMENSION', 'region'), ('指标改回销售额', 'REPLACE', 'sales')]
    expected_versions = [1,2,2,3,4,4,5,6,7,8,8,9,1,2,2,9,10,10,10,11]
    expected_topics = ['a']*12 + ['b']*3 + ['a']*5
    for index, (text, kind, code) in enumerate(specifications):
        for old, new in zip(('销售额','销售量','利润','地区'), scene[1:]):
            text = text.replace(old, new)
        text = scene[0] + '分析：' + text
        code = code_map.get(code, code)
        current_task = state.topics[state.active_topic_id].active_task_id
        task = state.tasks[current_task]
        base = task.active_version
        before = state.model_dump(mode='json')
        ops = []
        pending_patch = None
        pointers = PointerUpdates()
        attempt = None
        if kind in {'SET', 'ADD', 'REPLACE', 'INHERIT', 'CLEAR', 'REMOVE'}:
            ops = [operation(kind, value=[ref(code).model_dump(mode='json')] if code else None, base=base, target='fixture:'+code if kind == 'REMOVE' else None)]
        elif kind == 'PENDING':
            pending_patch = PendingPatch(pending_id='pending:a', action='CREATE', record=pending_record(state, profit_code=code_map['profit'], dimension_code=code_map['region']))
        elif kind in {'ANSWER_METRIC', 'ANSWER_DIMENSION'}:
            slot = 'metrics' if kind == 'ANSWER_METRIC' else 'dimensions'
            r = ref(code) if slot == 'metrics' else ref(code, SemanticRole.GROUP_BY, CatalogType.DIMENSION)
            ops = [operation('SET', value=[r.model_dump(mode='json')], slot=slot, base=base)]
            pending_patch = PendingPatch(pending_id='pending:a', action='ANSWER', selected_option_id=code)
        elif kind in {'SWITCH', 'RETURN'}:
            pointers = PointerUpdates(active_topic_id='topic:b' if kind == 'SWITCH' else 'topic:a')
        elif kind == 'SET_DIMENSION':
            ops = [operation('SET', value=[ref(code_map['region'], SemanticRole.GROUP_BY, CatalogType.DIMENSION).model_dump(mode='json')], slot='dimensions', base=base)]
        elif kind == 'REFRESH':
            attempt = ExecutionAttemptRecord(execution_id=f'execution:{group}:{index}', task_id=current_task, task_version=base,
                attempt_number=1, execution_backend='SEMANTIC_QUERY', snapshot_id='fixture-snapshot', catalog_version='fixture-catalog-v1',
                vector_index_version='fixture-index-v1', semantic_model_version='fixture-model-v1', policy_version='fixture-policy')
        patch = TaskPatch.compile(ops, base_task_version=base)
        semantic_parse = CurrentTurnSemanticParse(reference_signals=['HISTORICAL'] if kind == 'RETURN' else [] if index == 0 or kind == 'SWITCH' else ['ELLIPSIS'],
                                                  topic_shift_signals=['EXPLICIT_NEW_TASK'] if kind == 'SWITCH' else [])
        parsed = CurrentTurnParser.parse(text=text, turn_id=f'turn:{group}:{index}', text_ref=f'message:{group}:{index}', parsed=semantic_parse)
        resolution = TurnResolver.resolve(parsed, state=state, task_patch=patch, semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'), historical_task_id='task:a' if kind == 'RETURN' else None)
        mutation = StateMutation(mutation_id=f'mutation:{group}:{index}', message_id=f'message:{group}:{index}', turn_id=parsed.turn_id,
            task_id=current_task, expected_state_version=state.state_version, base_task_version=base, task_patch=patch,
            pending_patch=pending_patch, pointer_updates=pointers, execution_attempt=attempt, created_at=NOW)
        state = apply_state_mutation(state, mutation)
        target = 'task:' + expected_topics[index]
        assert state.tasks[target].active_version == expected_versions[index]
        assert state.active_topic_id == 'topic:' + expected_topics[index]
        expected_pending = ('NONE' if index < 10 else 'ACTIVE' if index in {10,11,15} else 'SUSPENDED' if index in {12,13,14} else 'RESOLVED')
        actual_pending = state.pending_records.get('pending:a')
        assert (actual_pending.status if actual_pending else 'NONE') == expected_pending
        rows.append(dict(turn_number=index+1, state_before=before, current_turn=text,
            expected_parse=parsed.model_dump(mode='json'), expected_resolution=resolution.model_dump(mode='json'),
            expected_task_patch=patch.model_dump(mode='json'), expected_topic=state.active_topic_id,
            expected_task_version=expected_versions[index], expected_pending=expected_pending,
            expected_dataset_reference=None, expected_execution_attempt=attempt.model_dump(mode='json') if attempt else None,
            mutation=mutation.model_dump(mode='json'), expected_state_after=state.model_dump(mode='json'),
            ground_truth_scope=['CONTRACT_REPLAY', 'DECLARED_TEST_STATE'], annotation_status='COMPLETE'))
    return rows
