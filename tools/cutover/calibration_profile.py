"""Descriptive corpus calibration, isolated from all production decisions.

Feature counts are annotation/lexical lower bounds, not inferred ground truth or
a scalar difficulty score. Missing query-shape labels remain UNKNOWN.
"""
from collections import Counter
from tools.cutover.evaluation_contract import digest

VERSION = 'calibration-profile-v1'
MARKERS = {
    'ADD': ('再加', '另外增加', '增加按'),
    'REPLACE': ('换成', '改成', '换成经销商'),
    'REMOVE': ('不要', '去掉', '删除'),
    'CLEAR': ('不限地区', '取消地区限制', '取消时间限制'),
    'time': ('年', '月', '季度', '时间', '交易日期'),
    'comparison': ('同比', '环比', '相比', '对比'),
    'ranking': ('最高', '最低', '排名', '前五名'),
    'relationship': ('合作', '关联', '关系'),
}

def profile(cases, catalog):
    if any(c.get('split') == 'BLIND_HOLDOUT' for c in cases):
        raise ValueError('BLIND_HOLDOUT_FORBIDDEN')
    if len({c['case_id'] for c in cases}) != len(cases):
        raise ValueError('DUPLICATE_CASE')
    rows=[]
    for case in cases:
        labels=case['labels']; text=case['current_utterance']
        # Lexical feature annotation is intentionally never passed to a model,
        # runtime parser, label scorer, or production classifier.
        occupied=set();matched=[]
        # Prefer complete catalog surfaces over nested metric-name words.
        # These matches are diagnostic annotations, never runtime candidates.
        for fact in sorted(catalog['facts'],key=lambda f:(-len(f['name']),f['fact_id'])):
            start=text.find(fact['name'])
            if start<0:continue
            span=set(range(start,start+len(fact['name'])))
            if occupied & span:continue
            occupied.update(span);matched.append((fact,span))
        masked=list(text)
        for fact,span in matched:
            if fact['catalog_type']=='METRIC':
                for index in span:masked[index]=' '
        cue_text=''.join(masked)
        marker_features={k:any(s in cue_text for s in markers) for k,markers in MARKERS.items()}
        for operation in ('ADD','REPLACE','REMOVE','CLEAR'):
            marker_features[operation] |= labels.get('operation') == operation
        hits={kind:set() for kind in ('METRIC','DIMENSION','ENTITY','ATTRIBUTE')}
        for fact,_ in matched:hits[fact['catalog_type']].add(fact['fact_id'])
        mentions=labels.get('mentions')
        roles={role for mention in mentions or [] for role in mention.get('roles',[])}
        group=case['entry_group']
        row={'case_id':case['case_id'],'group':group,'required_turn_count':case['turn_count'],
             'executable_history_turns':len(case['history']) if case['history_kind']=='EXECUTABLE_HISTORY' else 0,
             'context_dependency':group!='TRUE_SINGLE_TURN',
             'pending':group=='PENDING_RESPONSE','dataset':group=='DATASET_FOLLOWUP',
             'historical_return':group=='HISTORICAL_RETURN',
             'query_shape':labels.get('query_shape','UNKNOWN_UNLABELED'),
             'selected_mention_count':len(mentions) if mentions is not None else None,
             'catalog_surface_fact_hits':{k:len(v) for k,v in hits.items()},
             'multi_metric_current_surface':len(hits['METRIC'])>1,
             'multi_metric_expected':len(labels.get('canonical_metrics',[]))>1,
             'multi_dimension_current_surface':len(hits['DIMENSION'])>1,
             'entity_value_labeled':bool(roles & {'FILTER_VALUE'} or labels.get('region_values')),
             'entity_value_label_available':mentions is not None or 'region_values' in labels,
             'multi_metric_label_available':'canonical_metrics' in labels,
             'time_any_turn_proxy':any(any(marker in utterance for marker in MARKERS['time'])
                                       for utterance in [*case['history'],text]),
             **marker_features}
        row['operation_family_count']=sum(row[k] for k in ('ADD','REPLACE','REMOVE','CLEAR'))
        rows.append(row)
    n=len(rows)
    boolean_keys=('context_dependency','pending','dataset','historical_return','multi_metric_current_surface',
                  'multi_metric_expected','multi_dimension_current_surface','entity_value_labeled',*MARKERS)
    feature_counts={k:{'n':sum(r[k] for r in rows),'N':n} for k in boolean_keys}
    feature_counts['multi_metric_expected']['N']=sum(r['multi_metric_label_available'] for r in rows)
    feature_counts['entity_value_labeled']['N']=sum(r['entity_value_label_available'] for r in rows)
    feature_counts['time_any_turn_proxy']={'n':sum(r['time_any_turn_proxy'] for r in rows),'N':n}
    return {'version':VERSION,'case_count':n,'input_hash':digest(cases),
            'required_turn_count':sum(r['required_turn_count'] for r in rows),
            'groups':dict(Counter(r['group'] for r in rows)),
            'query_shape':dict(Counter(r['query_shape'] for r in rows)),
            'features':feature_counts,
            'selected_mention_count':dict(Counter(str(r['selected_mention_count']) for r in rows)),
            'operation_family_count':dict(Counter(r['operation_family_count'] for r in rows)),
            'entity_value_label_coverage':{'n':sum(r['entity_value_label_available'] for r in rows),'N':n},
            'rows':rows,'method':'LABELS_PLUS_DOCUMENTED_LEXICAL_LOWER_BOUNDS',
            'limits':['Unknown labels are not negatives; overlapping catalog names can increase surface-hit counts.',
                      'Operation count measures marked families, not complete typed edits.',
                      'Time/relationship markers are descriptive proxies, never truth labels.',
                      'Curated related cases; no population accuracy or overall harder/easier claim.']}

def divergence(cases, inventory):
    ids={c['case_id'] for c in cases}; selected=[r for r in inventory if r['case_id'] in ids]
    if len(selected)!=len(ids):raise ValueError('INVENTORY_CASE_SET_MISMATCH')
    key='first_divergence_stage'
    rows=[]
    for case in selected:
        rows.append({'case_id':case['case_id'],'status':case['status'],key:case.get(key),
                     'downstream_effects':case.get('downstream_effects',[]),
                     'error':case.get('error')})
    stages=Counter(r[key] for r in rows if r['status']!='PASS')
    if None in stages:raise ValueError('NONPASS_WITHOUT_PRIMARY')
    return {'case_count':len(rows),'status_counts':dict(Counter(r['status'] for r in rows)),
            'distribution':{k:{'n':v,'N':len(rows),'case_ids':[r['case_id'] for r in rows if r[key]==k]}
                            for k,v in stages.items()},'cases':rows,'one_primary_per_case':True}

def distribution_shift(public, private):
    return {'public_cases':public['case_count'],'private_cases':private['case_count'],
            'feature_comparison':{k:{'PUBLIC_DEV':public['features'][k],
                                     'PRIVATE_VALIDATION':private['features'][k]}
                                  for k in public['features']},
            'query_shape_comparison':{'PUBLIC_DEV':public['query_shape'],'PRIVATE_VALIDATION':private['query_shape']},
            'global_difficulty_order':'NOT_IDENTIFIABLE_FROM_PARTIAL_LABELS',
            'prompt_overfit_from_zero_private_passes':'NOT_ESTABLISHED',
            'limits':['Different construction and label coverage confound aggregate pass fractions.',
                      'Private composition intentionally concentrates metric ADD/REMOVE; public includes metadata, entities and pending/dataset.']}

def catalog_complexity(cases,snapshot):
    """Dependency breadth from catalog declarations, not from model behavior."""
    metrics={m['metric_code']:m for doc in snapshot['documents'] for m in doc['metrics']}
    rows=[]
    for case in cases:
        labels=case['labels'];codes={s.split(':',1)[1] for s in labels.get('canonical_metrics',[]) if s.startswith('METRIC:')}
        if 'canonical_metrics' not in labels:
            for mention in labels.get('mentions',[]):
                if 'MEASURE' not in mention.get('roles',[]):continue
                matches=[code for code,m in metrics.items() if mention['surface']==m['metric_name']]
                if len(matches)==1:codes.add(matches[0])
        owners=[set(metrics[c]['source_dependency']['bind_entity']) for c in codes]
        rows.append({'case_id':case['case_id'],'metric_label_available':bool(codes),
            'metric_count':len(codes),'metric_owner_union_count':len(set.union(*owners)) if owners else None,
            'common_owner_count':len(set.intersection(*owners)) if owners else None,
            'contains_multi_owner_metric':any(len(v)>1 for v in owners),
            'no_common_owner_for_multiple_metrics':len(owners)>1 and not set.intersection(*owners),
            'catalog_evidence_hash':digest({c:metrics[c]['source_dependency'] for c in sorted(codes)})})
    n=sum(r['metric_label_available'] for r in rows)
    return {'case_count':len(cases),'metric_label_coverage':{'n':n,'N':len(cases)},
            'contains_multi_owner_metric':{'n':sum(r['contains_multi_owner_metric'] for r in rows),'N':n},
            'no_common_owner_for_multiple_metrics':{'n':sum(r['no_common_owner_for_multiple_metrics'] for r in rows),'N':n},
            'rows':rows,'basis':'FROZEN_CATALOG_SOURCE_DEPENDENCY_AND_INDEPENDENT_LISTED_METRIC_LABELS',
            'limits':'Dependency breadth is a difficulty proxy, not proof that a metric/query is invalid or that an early semantic rejection was correct.'}
