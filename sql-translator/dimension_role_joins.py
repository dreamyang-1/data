"""Keep shared dimension joins on the explicitly filtered catalog edge."""
import re


def scoped_dimension_joins(translator, entity_code, dimensions, filters, model_id, existing):
    targets = set()
    for dimension in dimensions:
        field = translator._dimension_physical_field(dimension, entity_code, model_id)
        if field and '.' in field:
            targets.add(field.split('.', 1)[0])
    paths = []
    for table in sorted(targets - existing):
        candidates = set()
        for item in filters or []:
            field = str(item.get('field') or '')
            if '.' not in field or field.split('.', 1)[0] == table:
                continue
            owner_table = field.split('.', 1)[0]
            for owner in translator._find_all_entities_by_table_name(owner_table, model_id):
                for target in translator._find_all_entities_by_table_name(table, model_id):
                    tail = translator._find_join_path(owner, target, model_id)
                    # Only a direct, catalog-backed equality using this exact
                    # filter key supplies ownership. Other filters imply nothing.
                    if not tail or translator._extract_tables_from_join(tail) != {table}:
                        continue
                    normalized = tail.replace('`', '')
                    other = re.escape(table) + r'\.[A-Za-z_][A-Za-z_0-9]*'
                    key = re.escape(field)
                    if not re.search(r'(?<![\w.])(?:' + key + r'\s*=\s*' + other
                                     + '|' + other + r'\s*=\s*' + key + r')(?![\w.])', normalized):
                        continue
                    lead = '' if owner_table in existing else translator._find_join_path(entity_code, owner, model_id)
                    if lead is None or table in translator._extract_tables_from_join(lead):
                        continue
                    candidates.add((lead + ' ' + tail).strip())
        if len(candidates) > 1:
            raise ValueError('共享维度存在多个筛选归属，无法唯一确定关联路径：' + table)
        if candidates:
            path = translator._filter_existing_joins(candidates.pop(), existing)
            if path:
                paths.append(path)
                existing.update(translator._extract_tables_from_join(path))
    return paths
