"""Three deep public demo chains; labels precede model observations."""
from copy import deepcopy
import json
from unittest.mock import patch

from tools.cutover import round54_healthy_fixture as prior

BASE = '查询去年江苏省订单笔数'
NEW = '查询今年北京市销售总数量'
RECORDED_BASE = '查询去年江苏订单笔数'


def cases(base=BASE):
    rows = [
        ('A', [base, '换成北京市', '不限地区', '换今年']),
        ('B', [base, '再加销售总数量', '不要订单笔数', '换今年']),
        ('C', [base, NEW, '返回刚才江苏订单笔数那个任务']),
    ]
    return [dict(case_id='HC55-' + key, capability=key, utterances=utterances,
        initial_pending=False, scope=deepcopy(prior.SCOPE), clock=prior.CLOCK,
        split='PUBLIC_DEV') for key, utterances in rows]


def preflight(raw, snapshot, observations, catalog):
    expected = cases(RECORDED_BASE) if json.loads(raw.decode('utf-8')) == cases(RECORDED_BASE) else cases()
    with patch.object(prior, 'cases', lambda: expected):
        receipt = prior.preflight(raw, snapshot, observations, catalog)
    relations = {r['relation_code']: r for d in snapshot['documents']
        for entity in d['entities'] for r in entity.get('relationships', [])}
    if not relations:
        relations = {r['relation_code']: r for d in snapshot['documents']
            for entity in d['entities'] for r in entity.get('relations', [])}
    required = ['sales_order_belongs_to_hospital', 'hospital_located_in_province']
    assert all(code in relations for code in required), 'CATALOG_RELATIONSHIP_GAP'
    receipt['catalog_health']['relation_evidence'] = {code: relations[code] for code in required}
    receipt['catalog_health']['relation_boundary'] = (
        'Declared sales_order -> hospital -> province path; semantic-plan coverage only, '
        'not executed SQL or certification of every alternative attribution path')
    return receipt
