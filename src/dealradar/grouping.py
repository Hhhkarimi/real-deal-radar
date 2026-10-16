"""Select a separate top list for each actual product category."""


def category_name(row):
    c = row['candidate']
    return c.get('category_group') or c.get('category') or 'سایر کالاها'


def select_groups(rows, limit, *, bestsellers=False):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError('items_per_category must be an integer from 1 to 20')
    def ranking(row):
        if bestsellers:
            return (0 if row.get('source_category_id') is None else 1, row['source_rank'])
        c = row['candidate']
        return (-c.get('discount_percent', 0), -row.get('saving_percent', 0), c['price_toman'])
    groups, seen = {}, set()
    for row in sorted(rows, key=ranking):
        c = row['candidate']
        if c.get('supermarket') or not c.get('in_stock', True) or c['product_key'] in seen:
            continue
        category = category_name(row)
        items = groups.setdefault(category, [])
        if len(items) < limit:
            items.append(dict(row, report_category=category, category_position=len(items)+1))
            seen.add(c['product_key'])
    return [dict(category=name, items=items) for name, items in sorted(groups.items()) if items]


def flatten(groups):
    return [row for group in groups for row in group['items']]
