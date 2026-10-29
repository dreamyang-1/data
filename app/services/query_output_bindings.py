"""Carry a few proven SQL result roles into bounded downstream computation.

This reads aliases from the existing generator's SQL, not arbitrary SQL input;
unsupported expressions yield no binding. It does not choose fields or joins.
"""
import re

from app.planning.dependency_contract import sql_column_bindings


def calculation_columns(request,result):
    columns=result.dataset.columns
    asl=result.asl or {}
    anchor=(asl.get('time_context') or {}).get('anchor')
    month_columns=[]
    monthly=any(isinstance(item,dict) and str(item.get('granularity') or '').lower()
        in {'month','months','月','月份','自然月'} for item in asl.get('dimensions',[]))
    if monthly and isinstance(anchor,str) and re.fullmatch(r'[A-Za-z_]\w*\.[A-Za-z_]\w*',anchor):
        table,field=anchor.split('.')
        physical=rf'(?:`{re.escape(table)}`|{re.escape(table)})\s*\.\s*(?:`{re.escape(field)}`|{re.escape(field)})'
        expression=rf"(?:DATE_FORMAT\(\s*{physical}\s*,\s*'%Y-%m'\s*\)|TO_CHAR\(\s*{physical}\s*,\s*'YYYY-MM'\s*\)|DATE_TRUNC\(\s*'month'\s*,\s*{physical}\s*\)|CONVERT\(\s*varchar\(7\)\s*,\s*{physical}\s*,\s*120\s*\))"
        aliases=re.findall(expression+r'\s+AS\s+(?:`([^`]+)`|"([^"]+)"|\[([^\]]+)\]|([A-Za-z_]\w*))',result.sql,re.I)
        month_columns=list(dict.fromkeys(next(value for value in match if value) for match in aliases
            if any(value in columns for value in match if value)))
    extraction=getattr(request,'_planner_extraction',None)
    declared=(extraction.structured or {}).get('维度',[]) if extraction else []
    physical_columns=sql_column_bindings(result.sql,columns)
    identity_columns={}
    for item in declared:
        label=item.get('name') or item.get('field') if isinstance(item,dict) else item
        if not isinstance(label,str):
            continue
        # This suffix is the SQL translator's existing governed identity /
        # display-companion contract, not an inferred ID from numeric values.
        identity=label+'（编码）'
        if identity in physical_columns:
            identity_columns[label]=identity
    return {'month':month_columns,'dimension_identity':identity_columns}
