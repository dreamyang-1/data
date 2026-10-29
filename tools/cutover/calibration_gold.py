"""Small, independently specified full semantic-plan gold and strict scoring.

Expected values are authored from named catalog facts and explicit business
operations. No runtime/model result is accepted by the label constructor.
Full semantic-plan labels are separate from SQL text and source-result truth.
"""
from copy import deepcopy
from tools.cutover.evaluation_contract import digest

VERSION='certified-full-semantic-plan-v1'
AXES=('target_task','task_operation','metric_binding','dimension_binding','entity_binding',
      'entity_value_binding','filter','time_range','time_grain','query_shape',
      'expected_task_semantic_state','expected_semantic_query_ir','expected_dry_plan_outcome','scope')
SOURCES={'CATALOG_PROVEN','BUSINESS_CONTRACT','DETERMINISTIC_RULE','HUMAN_ADJUDICATED','REGRESSION_CONFIRMED'}
TAX='METRIC:sales_total_including_tax'; COST='METRIC:sales_total_cost'
FREE='METRIC:tax_free_sales_amount'; QUANTITY='METRIC:sales_total_quantity'

def full_scope(catalog):
    return {**catalog['scope'],'database_id':None,'knowledge_base_names':[],
            'source':'TRUSTED_UPSTREAM_BACKEND'}

def semantic_state(metrics,dimensions=()):
    return {'subject':None,'metrics':[{'fact_id':v,'role':'MEASURE'} for v in metrics],
            'dimensions':[{'fact_id':v,'role':'GROUP_BY'} for v in dimensions],
            'projection_spec':{'mode':'EXPLICIT','items':[],'default_display_policy_id':None},
            'filter_expression':None,'time_spec':None,'ranking_spec':None,'comparison_spec':None,
            'relationship_spec':None,'source_dataset_ref':None,
            'delivery_spec':{'modes':['TABLE'],'export_format':None,'report_template_id':None,'chart_preferences':None},
            'analysis_goals':[],'policy_decisions':[]}

def build_slice(catalog,public_cases):
    facts={f['fact_id']:f for f in catalog['facts']};public={c['case_id']:c for c in public_cases}
    specs=[]
    # Exact governed metric names; do not reinterpret order grain or identity.
    for idx,key in enumerate((TAX,FREE,COST,QUANTITY)):
        for offset in (0,1):
            source='G81-'+str(15+idx*2+offset).zfill(3)
            specs.append((source,public[source], [key], [], 'NEW', 'INITIALIZE', [key], ['single_metric']))
    specs.extend([
        ('G81-079',public['G81-079'],[TAX],[], 'PREVIOUS','REMOVE',[COST],['multi_metric_history','REMOVE','ordinary_followup']),
        ('G81-089',public['G81-089'],[FREE],[], 'PREVIOUS','REPLACE',[FREE],['REPLACE','ordinary_followup']),
    ])
    def new_input(text,history=()):
        return {'current_utterance':text,'history':list(history),'scope':catalog['scope'],
                'clock':'2026-09-09T09:00:00+08:00'}
    specs.extend([
        ('C81-201',new_input('查询含税销售总额和销售总成本'),[TAX,COST],[], 'NEW','INITIALIZE',[TAX,COST],['multi_metric']),
        ('C81-202',new_input('按医院等级统计含税销售总额'),[TAX],['DIMENSION:hospital_level'], 'NEW','INITIALIZE',[TAX],['single_dimension']),
        ('C81-203',new_input('按医院等级和渠道业态统计含税销售总额'),[TAX],['DIMENSION:hospital_level','DIMENSION:channel_format'], 'NEW','INITIALIZE',[TAX],['multi_dimension']),
        ('C81-204',new_input('再加销售总成本',['查询含税销售总额']),[TAX,COST],[], 'PREVIOUS','ADD',[COST],['ADD','multi_metric','ordinary_followup']),
        ('C81-205',new_input('取消所有分组',['按医院等级统计含税销售总额']),[TAX],[], 'PREVIOUS','CLEAR_DIMENSIONS',[],['CLEAR','ordinary_followup']),
        ('C81-206',new_input('回到第一个问题',['查询含税销售总额','换个问题，查询销售总成本']),[TAX],[], 'HISTORY:0','NO_EDIT',[],['historical_return']),
    ])
    cases=[]
    for source,original,metrics,dimensions,target,operation,operation_values,coverage in specs:
        for key in [*metrics,*dimensions,*operation_values]:
            if key not in facts:raise ValueError('FULL_GOLD_FACT_NOT_IN_FROZEN_CATALOG')
        expected_state=semantic_state(metrics,dimensions)
        shape='GROUPED_AGGREGATE' if dimensions else 'SCALAR_AGGREGATE'
        ir={'payload_type':shape,'measures':deepcopy(expected_state['metrics']),'group_by':deepcopy(expected_state['dimensions']),'time':None,
            'filters':None,'projection_spec':deepcopy(expected_state['projection_spec'])}
        labels={'target_task':target,'task_operation':[{'slot_path':'metrics','operation':operation,
                                                      'values':[{'fact_id':v,'role':'MEASURE'} for v in operation_values]}],
                'metric_binding':deepcopy(expected_state['metrics']),'dimension_binding':deepcopy(expected_state['dimensions']),'entity_binding':None,
                'entity_value_binding':[],'filter':None,'time_range':None,'time_grain':None,
                'query_shape':shape,'expected_task_semantic_state':expected_state,
                'expected_semantic_query_ir':ir,
                'expected_dry_plan_outcome':{'status':'SUCCESS','same_runtime_entry':True,'SQL_executed':False,
                                             'scope':full_scope(catalog),'semantic_ir':ir},
                'scope':full_scope(catalog)}
        if dimensions:
            labels['task_operation'].append({'slot_path':'dimensions','operation':'INITIALIZE',
                                            'values':deepcopy(expected_state['dimensions'])})
        if operation=='CLEAR_DIMENSIONS':labels['task_operation']=[{'slot_path':'dimensions','operation':'CLEAR','values':None}]
        if operation=='NO_EDIT':labels['task_operation']=[]
        if 'DIMENSION:hospital_level' in dimensions:
            # Independently audited frozen catalog: this dimension binds the
            # hospital entity, whereas the named metrics bind sales_order.
            # The current ASL2 direct-owner contract must reject this boundary;
            # a future joined-dimension capability needs a new label version.
            labels['expected_dry_plan_outcome'].update(status='FAIL_CLOSED_UNSUPPORTED',
                reason_code='ASL2_DIMENSION_OWNER_UNRESOLVED')
        provenance={axis:{'label_source':'BUSINESS_CONTRACT',
                          'evidence':['USER_EXPLICIT_QUERY_AND_SLOT_OPERATION_CONTRACT',
                                      'V2_NO_IMPLICIT_TIME_OR_NEW_TASK_INHERITANCE_CONTRACT'],
                          'claim':'Full semantic plan only; not physical SQL or source business result correctness'} for axis in AXES}
        provenance['task_operation']['evidence'].append('Empty new-task slot initialization permits SET/ADD/REPLACE; follow-up operations remain distinct. Single-item list edits may use a scalar item under the slot contract.')
        for axis in ('metric_binding','dimension_binding','entity_binding','entity_value_binding'):
            provenance[axis]={'label_source':'CATALOG_PROVEN','evidence':[catalog['artifact_hash'],*metrics,*dimensions],
                              'claim':'Explicitly named catalog identities; absence means no additional role requested'}
        for axis in ('expected_task_semantic_state','expected_semantic_query_ir','expected_dry_plan_outcome'):
            provenance[axis]={'label_source':'DETERMINISTIC_RULE',
                              'evidence':['EXPLICIT_SET_ADD_REPLACE_REMOVE_CLEAR_CONTRACT','TYPED_SCALAR_AGGREGATE_CONTRACT',catalog['artifact_hash']],
                              'claim':'Desired complete semantic contract; missing native DryPlan integration remains a capability blocker'}
        if 'DIMENSION:hospital_level' in dimensions:
            provenance['expected_dry_plan_outcome']={'label_source':'CATALOG_PROVEN',
                'evidence':[catalog['artifact_hash'],catalog['source_snapshot_sha256'],
                    'hospital_level.bind_entities targets hospital; metric source_dependency targets sales_order',
                    'app/semantic_v2/asl2.py:_Compiler.dimension_binding requires exactly one direct subject binding'],
                'claim':'Fail closed at the currently specified native boundary; does not classify the legitimate grouping request as wrong'}
        case={'case_id':'FP81-'+source.split('-')[1],'source_case_id':source,'full_plan_gold_version':VERSION,
              'input':{k:deepcopy(original[k]) for k in ('history','current_utterance','scope','clock')},
              'expected':labels,'label_provenance':provenance,'catalog_version':catalog['catalog_version'],
              'catalog_hash':catalog['artifact_hash'],'as_of':original['clock'],'timezone':'Asia/Shanghai',
              'coverage':coverage,'certification':'ENGINEER_REVIEWED_CATALOG_AND_EXPLICIT_CONTRACT',
              'not_certified':['physical_SQL_text','source_result','metric_formula_business_adjudication'],
              'input_hash':digest({k:original[k] for k in ('history','current_utterance','scope','clock')})}
        validate(case,catalog);case['label_hash']=digest(labels);cases.append(case)
    return cases

def validate(case,catalog):
    if case['full_plan_gold_version']!=VERSION or set(case['expected'])!=set(AXES):
        raise ValueError('INCOMPLETE_FULL_PLAN_CONTRACT')
    if set(case['label_provenance'])!=set(AXES):raise ValueError('FULL_PLAN_PROVENANCE_REQUIRED')
    if case['expected']['scope']!=full_scope(catalog) or case['catalog_hash']!=catalog['artifact_hash']:
        raise ValueError('FULL_PLAN_SCOPE_OR_CATALOG_MISMATCH')
    for truth in case['label_provenance'].values():
        if truth['label_source'] not in SOURCES or not truth.get('evidence'):
            raise ValueError('RUNTIME_OUTPUT_CANNOT_CERTIFY_GOLD')
    if case['expected']['expected_task_semantic_state']['metrics']!=case['expected']['metric_binding']:
        raise ValueError('INCONSISTENT_FULL_PLAN_METRICS')
    if case['expected']['expected_semantic_query_ir']['measures']!=case['expected']['metric_binding']:
        raise ValueError('INCONSISTENT_FULL_PLAN_IR')

def normalize_bound(value):
    """Keep semantic identity, roles and scope; remove no arbitrary state fields.

    Slot contracts supply roles independently. This projection replaces only a
    validated BoundSemanticRef with its canonical fact ID after verifying scope
    separately; it does not generate a bound runtime ref or permission proof.
    """
    if isinstance(value,dict):
        if 'canonical_code' in value and 'catalog_type' in value and 'semantic_role' in value:
            return {'fact_id':value['catalog_type']+':'+value['canonical_code'],'role':value['semantic_role']}
        return {k:normalize_bound(v) for k,v in value.items()}
    if isinstance(value,list):return [normalize_bound(v) for v in value]
    return value

def project_observation(observation,catalog):
    axes=observation.get('axes',{});out={}
    if 'scope' in axes:
        scope=axes['scope']
        out['scope']=deepcopy(scope)
    if 'target_task' in axes:out['target_task']=axes['target_task']
    if 'task_patch' in axes:
        patch=axes['task_patch'];out['task_operation']=[]
        for key in ('sets','adds','replacements','removes','clears'):
            for item in patch.get(key,[]):
                operation=item['operation'];values=normalize_bound(item['new_value'])
                if item['slot_path'] in {'metrics','dimensions'} and operation in {'ADD','REMOVE'} and isinstance(values,dict):
                    values=[values]
                if axes.get('target_task')=='NEW' and patch['base_task_version']==0 and operation in {'SET','ADD','REPLACE'}:
                    operation='INITIALIZE'
                out['task_operation'].append({'slot_path':item['slot_path'],'operation':operation,'values':values})
        # Multiple atomic initializations of one empty list slot are one
        # semantic assignment. Preserve duplicates for corruption detection.
        merged=[]
        for op in out['task_operation']:
            previous=next((p for p in merged if p['slot_path']==op['slot_path'] and p['operation']==op['operation']),None)
            if previous is not None and op['operation']=='INITIALIZE' and isinstance(previous['values'],list) and isinstance(op['values'],list):
                previous['values'].extend(op['values'])
            else:merged.append(op)
        out['task_operation']=merged
    if 'task_state' in axes:
        state=normalize_bound(axes['task_state'])
        out.update(metric_binding=state['metrics'],dimension_binding=state['dimensions'],entity_binding=state['subject'],
                   entity_value_binding=[],filter=state['filter_expression'],
                   time_range=state['time_spec']['range'] if state['time_spec'] else None,
                   time_grain=state['time_spec']['grain'] if state['time_spec'] else None,
                   expected_task_semantic_state=state)
        # Complete empty value binding is known only for an absent filter.
        if state['filter_expression'] is not None:out.pop('entity_value_binding')
    if 'query_shape' in axes:out['query_shape']=axes['query_shape']
    if 'semantic_query_ir' in axes:out['expected_semantic_query_ir']=normalize_bound(axes['semantic_query_ir'])
    # No DryPlan observation is manufactured from the independent seam.
    return out

def score(case,observation,catalog):
    validate(case,catalog);observed=project_observation(observation,catalog)
    def canonical(value,key=''):
        if isinstance(value,dict):return {k:canonical(v,k) for k,v in value.items()}
        if isinstance(value,list):
            values=[canonical(v) for v in value]
            return sorted(values,key=digest) if key in {'metrics','measures','dimensions','group_by','metric_binding','dimension_binding','task_operation','values'} else values
        return value
    results={k:('NOT_OBSERVED' if k not in observed else 'PASS' if digest(canonical(v,k))==digest(canonical(observed[k],k)) else 'FAIL')
             for k,v in case['expected'].items()}
    # Whole-plan gates fail on known wrong semantics; absence stays blocked.
    error=observation.get('error')
    semantic_error=error and error['type'] not in {'MODEL_HTTP_ERROR','MODEL_TIMEOUT','FIXTURE_ERROR','EVALUATOR_ERROR','IMPLEMENTATION_GAP'}
    status='FAIL' if 'FAIL' in results.values() or semantic_error else 'BLOCKED' if error or 'NOT_OBSERVED' in results.values() else 'PASS'
    return {'case_id':case['case_id'],'source_case_id':case['source_case_id'],'status':status,
            'axes':results,'observed_hash':digest(observed),'error':error,
            'missing_runtime_capabilities':['DRY_PLAN_RUNTIME_INTEGRATION_GAP'],
            'whole_plan_runtime_success':status=='PASS','label_hash':case['label_hash']}
