"""Reviewed UTF-8 development inputs; no production lexical rules or Gold replacement."""

ROWS = [
    ('A', 'REGION_REPLACE', ['换成北京市'], 'SEED'),
    ('B', 'TIME_REPLACE', ['换今年'], 'SEED'),
    ('C', 'ADD', ['再加销售总数量'], 'SEED'),
    ('D', 'REMOVE', ['再加销售总数量', '不要销售总数量'], 'SEED'),
    ('E', 'CLEAR_NO_RESURRECTION', ['不限地区', '换今年'], 'SEED'),
    ('F', 'NEW_TASK', ['查询今年北京市销售总数量'], 'NEW'),
    ('G', 'HISTORICAL_RETURN', ['查询今年北京市销售总数量', '返回刚才江苏订单笔数那个任务'], 'SEED'),
    ('H', 'PENDING_NEW_TASK', ['查询今年北京市订单笔数'], 'NEW'),
    ('I', 'TRUE_AMBIGUITY', ['查询今年北京市销售总数量', '前面两个任务选一个继续，具体哪个我还没确定'], 'CLARIFICATION'),
    ('J', 'SELF_CONTAINED_NEW_TASK', ['查询今年北京市订单笔数'], 'NEW'),
]


def validate_case(case):
    from tools.cutover.live_followup_round5_1 import cases, CLOCK, SCOPE
    if case.get('scope') != SCOPE or case.get('clock') != CLOCK:
        raise ValueError('LIVE_FIXTURE_SCOPE_CLOCK_MISMATCH')
    text = case.get('utterances')
    if not isinstance(text, list) or any(not isinstance(t,str) or '\ufffd' in t
            or not any(c.isalnum() for c in t) for t in text):
        raise ValueError('LIVE_FIXTURE_TEXT_ENCODING_INVALID')
    identifier = case['case_id']
    if identifier.startswith('LF51-'):
        expected = next(c['utterances'] for c in cases() if c['case_id']==identifier)
    elif identifier=='HC53-CDB':
        expected = ['查询去年江苏订单笔数','再加销售总数量','不要销售总数量','换今年']
    else:
        row = next(r for r in ROWS if 'HC53-'+r[0]==identifier)
        expected = ([] if row[0]=='H' else ['查询去年江苏订单笔数']) + row[2]
    if text != expected:
        raise ValueError('LIVE_FIXTURE_REVIEWED_INPUT_MISMATCH')
