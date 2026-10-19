"""Monthly rate extrema use actual calendar predecessors, never row offsets."""
from datetime import date,timedelta
import re

from app.analysis.chain_tools.temporal_window import month_bucket_index,parse_absolute_range


def monthly_extremum_spec(question):
    text=re.sub(r'\s+','',question or '')
    if not re.search(r'月度|各月|每月|月份|哪个月|哪一月|逐月',text) or '平均' in text:
        return None
    lag=12 if '同比' in text else 1 if '环比' in text else None
    if lag is None:return None
    if re.search(r'降幅最大|跌幅最大|下降最多|增长最慢|(?:环比|同比).{0,6}最低',text):
        return lag,'min'
    if re.search(r'增幅最大|涨幅最大|增长最快|(?:环比|同比).{0,6}最高',text):
        return lag,'max'
    return None


def requested_month_window(request):
    extraction=getattr(request,'_planner_extraction',None)
    grain=(extraction.structured or {}).get('时间粒度') if extraction else None
    if isinstance(grain,dict):
        window=parse_monthly_window(str(grain.get('time_range') or ''))
        if window:return window
        from app.analysis.chain_tools.temporal_window import align_structured_time_window
        aligned=align_structured_time_window('',grain,intent='TREND_ANALYSIS',today=date.today())
        if aligned:
            window=parse_monthly_window(aligned['time_range'])
            if window:return window
    if request.time_range:
        return request.time_range.start,request.time_range.end_exclusive-timedelta(days=1)
    return None


def parse_monthly_window(text):
    if match:=re.fullmatch(r'(20\d{2})年?',str(text).strip()):
        year=int(match[1]);return date(year,1,1),date(year,12,31)
    return parse_absolute_range(text)


def calculate_monthly_extremum(question,labels,values,window):
    spec=monthly_extremum_spec(question)
    if spec is None:return None
    lag,direction=spec
    indexes=[month_bucket_index(label) for label in labels]
    if None in indexes or len(set(indexes))!=len(indexes):
        raise ValueError('月度比较需要带年份且唯一的月份')
    observed=dict(zip(indexes,zip(labels,values)))
    if window:
        start,end=window
        first,last=start.year*12+start.month-1,end.year*12+end.month-1
    else:
        first,last=min(indexes)+lag,max(indexes)
    comparisons=[];missing=[];undefined=[]
    for index in range(first,last+1):
        if index not in observed or index-lag not in observed:
            missing.append(index);continue
        label,current=observed[index];base_label,base=observed[index-lag]
        if base<=0:
            undefined.append(label);continue
        comparisons.append({'from':base_label,'to':label,'base':base,'current':current,
            'absolute_change':current-base,'change_rate':(current-base)/base})
    if not comparisons:
        raise ValueError('缺少可比较的完整月份或有效基期，尚不能确定月度增长极值')
    best=(min if direction=='min' else max)(item['change_rate'] for item in comparisons)
    selected=[item for item in comparisons if item['change_rate']==best]
    return {'comparison':'YOY' if lag==12 else 'MOM','direction':direction,
        'compared_period_count':len(comparisons),'missing_period_count':len(missing),
        'undefined_base_periods':undefined,'complete':not missing,'selected':selected,
        'comparisons':comparisons}
