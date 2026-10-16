"""A bounded, varied daily shortlist and confirmed-delivery price memory."""
import hashlib
import json
from collections import Counter

from .grouping import category_name


def item_key(candidate):
    value=[candidate['product_key'],candidate.get('variant_key',''),candidate.get('warranty_key','')]
    return hashlib.sha256(json.dumps(value,ensure_ascii=False).encode()).hexdigest()


def item_due(history, channel_key, candidate, change_percent=5):
    history.db.execute('CREATE TABLE IF NOT EXISTS showcase_items (channel TEXT, item TEXT, price INTEGER, sent_at TEXT, PRIMARY KEY(channel,item))')
    row=history.db.execute('SELECT price FROM showcase_items WHERE channel=? AND item=?',(channel_key,item_key(candidate))).fetchone()
    if row is None:return True
    old=row[0]
    return old<=0 or abs(candidate['price_toman']-old)/old*100>=change_percent


def mark_item(history, channel_key, key, price, when):
    history.db.execute('CREATE TABLE IF NOT EXISTS showcase_items (channel TEXT, item TEXT, price INTEGER, sent_at TEXT, PRIMARY KEY(channel,item))')
    history.db.execute('INSERT OR REPLACE INTO showcase_items VALUES (?,?,?,?)',(channel_key,key,price,when))
    history.db.commit()


def select_showcase(discounts,bestsellers,settings,*,due=lambda c:True):
    maximum=int(settings.get('max_items',25))
    quotas=[int(settings.get('discount_items',15)),int(settings.get('bestseller_items',10))]
    per_type=int(settings.get('max_per_type',2))
    if not 1<=maximum<=50 or not 1<=per_type<=5 or any(n<0 or n>maximum for n in quotas) or sum(quotas)>maximum:
        raise ValueError('Invalid showcase limits')
    def discount_rank(row):
        p=row.get('price_comparison',{})
        tier=0 if p.get('confidence')=='high' and p.get('comparable_shop_count',0) else 1 if p.get('comparable_shop_count',0) else 2
        return (tier,-p.get('saving_percent',0),-p.get('saving_toman',0),-row['candidate'].get('discount_percent',0))
    pools=[sorted(discounts,key=discount_rank),sorted(bestsellers,key=lambda r:(0 if r.get('source_category_id') is None else 1,r['source_rank']))]
    selected,seen,types=[],set(),Counter()
    for section,(pool,quota) in enumerate(zip(pools,quotas)):
        count=0
        for row in pool:
            if count>=quota or len(selected)>=maximum:break
            c=row['candidate'];kind=category_name(row,'type')
            if c['product_key'] in seen or types[kind]>=per_type or c.get('supermarket') or not c.get('in_stock',True):continue
            comparison=row.get('price_comparison',{})
            if section==0 and comparison.get('comparable_shop_count',0) and comparison.get('saving_percent',0)<=0:continue
            if not due(c):continue
            selected.append(dict(row,showcase_section='discount' if section==0 else 'bestseller'))
            seen.add(c['product_key']);types[kind]+=1;count+=1
    return selected
