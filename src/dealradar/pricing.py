"""Comparable Torob listings, with explicit uncertainty and provenance."""
import datetime as dt
import math
import re
import time
from collections import Counter

from .core import UTC, Offer, fresh, timestamp
from .market import normalized, color_identity, warranty_identity
from .torob_mcp import TorobMCP, MCPError


def model_terms(title):
    part = re.split(r'مدل\s+', title, maxsplit=1)
    if len(part) < 2: return []
    latin = re.split(r'[\u0600-\u06ff]', part[1], maxsplit=1)[0]
    tokens = re.findall(r'[A-Za-z0-9][A-Za-z0-9.-]*', latin)
    codes = [t for t in tokens if re.search('[A-Za-z]', t) and re.search('[0-9]', t)]
    if codes: return [re.sub('[^a-z0-9]', '', t.lower()) for t in codes]
    if any(re.search('[A-Za-z]', t) for t in tokens) and any(t.isdigit() for t in tokens):
        return [normalized(t) for t in tokens]
    return []


def capacity(text):
    return sorted(re.findall(r'(\d+)\s*(gb|tb|گیگابایت|ترابایت)', normalized(text)))


def model_match(candidate, title):
    if normalized(candidate.title) == normalized(title): return True
    terms = model_terms(candidate.title)
    if not terms: return False
    words = {re.sub('[^a-z0-9]', '', t.lower()) for t in re.findall(r'[A-Za-z0-9][A-Za-z0-9.-]*', title)}
    if not set(terms) <= words: return False
    brands=[normalized(b) for b in [candidate.brand_name]+candidate.brand_names if normalized(b) not in ('','متفرقه','unknown')]
    if not brands or not any(' '+brand+' ' in ' '+normalized(title)+' ' for brand in brands): return False
    units = {'گیگابایت':'gb','ترابایت':'tb'}
    canon = lambda s: sorted((n,units.get(u,u)) for n,u in capacity(s))
    if canon(candidate.title) and canon(candidate.title) != canon(title): return False
    for pattern in [r'بسته\s*(\d+)\s*(?:عددی|تایی)', r'\b(pro|plus|max|mini|ultra|anc|nc|se|lte|nfc|4g|5g)\b']:
        if sorted(re.findall(pattern,normalized(candidate.title))) != sorted(re.findall(pattern,normalized(title))): return False
    return True


def positive(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value) and value > 0


def uncertain(reason='not_checked'):
    return dict(confidence='none', reason=reason, source='torob_mcp', comparable_shop_count=0,
                observed_shop_count=0, evidence=[], fetched_at=None, torob_url=None)


def compare_listings(candidate, detail, sellers, now, minimum=3):
    result = uncertain('no_comparable_offers')
    result.update(torob_url=detail.get('url'), fetched_at=sellers.get('fetched_at'),
                  loaded_list_complete=sellers.get('loaded_list_complete',False), truncated=sellers.get('truncated',False))
    if sellers.get('price_unit') != 'toman':
        result['reason']='invalid_currency'; return result
    try:
        age=(now-timestamp(sellers['fetched_at'])).total_seconds()
        if not 0 <= age <= 6*3600: raise ValueError('stale')
    except (ValueError,TypeError,KeyError):
        result['reason']='stale_or_unknown_time'; return result
    title=(detail.get('title') or '')+' '+(detail.get('title_en') or '')
    if not model_match(candidate,title.strip()):
        # English metadata may repeat a Persian exact title: evaluate separately.
        if not model_match(candidate,detail.get('title') or ''):
            result['reason']='model_mismatch'; return result
    attrs = {normalized(s.get('name','')):s.get('value','') for s in detail.get('specifications',{}).get('items',[])}
    matched, observed, listed, failures = [], set(), [], Counter()
    offers=sorted(sellers.get('items',[]),key=lambda o:o.get('price_toman') if positive(o.get('price_toman')) else float('inf'))
    seen_ids, seen_names = set(), set()
    for offer in offers:
        if offer.get('available') is not True or offer.get('shop_type') != 'online' or offer.get('is_ad') or offer.get('price_unreliable') or offer.get('purchase_warning') or not positive(offer.get('price_toman')):
            failures['unavailable_or_unreliable']+=1; continue
        shop_id, shop_name = offer.get('shop_id'), normalized(offer.get('shop_name') or '')
        if shop_id is None and not shop_name: continue
        description=(offer.get('title') or '')+' '+(offer.get('warranty_or_description') or '')
        condition=normalized(description+' '+(detail.get('condition_text') or ''))
        if re.search(r'کارکرده|استوک|دست دوم|بازسازی|غیراصل|غیر اصل|بدون رجیستری',condition):
            failures['condition_mismatch']+=1; continue
        observed.add(str(shop_id) if shop_id is not None else shop_name)
        listed.append(offer['price_toman'])
        if not model_match(candidate,description): failures['seller_model_unconfirmed']+=1; continue
        # The seller must declare the selected variant, not just the parent SKU.
        color=normalized(description+' '+str(attrs.get(normalized('رنگ'),'')))
        expected_color=color_identity(candidate.color_name)
        color_aliases={'مشکی':['مشکی','سیاه','black'],'سفید':['سفید','white'],'آبی':['آبی','blue'],
                       'قرمز':['قرمز','red'],'سبز':['سبز','green'],'نقره ای':['نقره ای','silver'],
                       'خاکستری':['خاکستری','gray','grey'],'طلایی':['طلایی','gold']}
        found={c for c,names in color_aliases.items() if any(' '+normalized(n)+' ' in ' '+color+' ' for n in names)}
        if expected_color and (found != {expected_color} if expected_color in color_aliases else ' '+expected_color+' ' not in ' '+color+' '):
            failures['color_unknown_or_mismatch']+=1; continue
        if candidate.size_name and ' '+normalized(candidate.size_name)+' ' not in ' '+normalized(description)+' ':
            failures['size_unknown_or_mismatch']+=1; continue
        warranty=normalized(offer.get('warranty_or_description') or '')
        expected=warranty_identity(candidate.warranty_key)
        actual=warranty_identity(warranty)
        months=re.findall(r'(\d+)\s*ماهه?',warranty)
        expected_months=re.findall(r'(\d+)\s*ماهه?',normalized(candidate.warranty_key))
        if not expected or candidate.warranty_key in ('نامشخص','unknown') or not set(expected) <= set(actual) or months != expected_months:
            failures['warranty_unknown_or_mismatch']+=1; continue
        if not re.search(r'\bنو\b|آکبند|اکبند|\bnew\b', condition):
            failures['new_condition_unconfirmed']+=1; continue
        if shop_id in seen_ids or shop_name and shop_name in seen_names: continue
        if shop_id is not None: seen_ids.add(shop_id)
        if shop_name: seen_names.add(shop_name)
        matched.append(dict(shop_id=shop_id,shop_name=offer.get('shop_name'),price_toman=offer['price_toman'],
                            url=offer.get('offer_url'),last_price_change_text=offer.get('last_price_change_text'),
                            shipping_fee_text=offer.get('shipping_fee_text')))
    result.update(observed_shop_count=len(observed),observed_shop_ids=sorted(observed),comparable_shop_count=len(matched),evidence=matched,exclusions=dict(failures))
    if listed:result.update(listed_low_toman=min(listed),listed_high_toman=max(listed))
    if matched:
        low=min(o['price_toman'] for o in matched); high=max(o['price_toman'] for o in matched)
        saving=low-candidate.price_toman
        result.update(confidence='high' if len(matched)>=minimum else 'partial',reason='comparable_listings',
                      market_low_toman=low,market_high_toman=high,saving_toman=round(saving),
                      saving_percent=round(saving/low*100,2),comparison_basis='item_only')
    elif observed:
        result.update(confidence='partial',reason='variant_or_condition_unconfirmed')
    return result


def combine_comparisons(candidate, comparisons, minimum=3, limited=False):
    """Merge only seller offers already matched to the same concrete variant."""
    if not comparisons:return uncertain('no_matching_model')
    result=dict(comparisons[0])
    evidence,ids,names=[],set(),set()
    offers=sorted([o for c in comparisons for o in c.get('evidence',[])],key=lambda o:o['price_toman'])
    for offer in offers:
        identifier,name=offer.get('shop_id'),normalized(offer.get('shop_name') or '')
        if identifier in ids or name and name in names:continue
        if identifier is not None:ids.add(identifier)
        if name:names.add(name)
        evidence.append(offer)
    urls=list(dict.fromkeys(c['torob_url'] for c in comparisons if c.get('torob_url')))
    times=[c['fetched_at'] for c in comparisons if c.get('fetched_at')]
    observed=set(v for c in comparisons for v in c.get('observed_shop_ids',[]))
    failures=Counter()
    for c in comparisons:failures.update(c.get('exclusions',{}))
    result.update(evidence=evidence,comparable_shop_count=len(evidence),observed_shop_count=len(observed),
                  fetched_at=min(times,key=timestamp) if times else None,torob_urls=urls,torob_url=urls[0] if urls else None,
                  loaded_list_complete=not limited and all(c.get('loaded_list_complete') for c in comparisons),
                  truncated=limited or any(c.get('truncated') for c in comparisons),exclusions=dict(failures))
    listed=[c for c in comparisons if positive(c.get('listed_low_toman'))]
    if listed:
        result.update(listed_low_toman=min(c['listed_low_toman'] for c in listed),listed_high_toman=max(c['listed_high_toman'] for c in listed))
        result['torob_url']=min(listed,key=lambda c:c['listed_low_toman']).get('torob_url')
    if evidence:
        low=min(o['price_toman'] for o in evidence);high=max(o['price_toman'] for o in evidence)
        saving=low-candidate.price_toman
        result.update(confidence='high' if len(evidence)>=minimum else 'partial',reason='comparable_listings',
                      market_low_toman=low,market_high_toman=high,saving_toman=round(saving),
                      saving_percent=round(saving/low*100,2),comparison_basis='item_only')
        result['torob_url']=min((c for c in comparisons if c.get('evidence')),key=lambda c:min(o['price_toman'] for o in c['evidence']))['torob_url']
    elif observed:result.update(confidence='partial',reason='variant_or_condition_unconfirmed')
    return result


def enrich_prices(rows, config, *, client_factory=TorobMCP):
    settings=config.get('torob',{})
    if not settings.get('enabled') or settings.get('backend') != 'mcp': return
    by_product={}
    for row in rows:
        row['price_comparison']=uncertain()
        c=Offer.parse(row['candidate'])
        if not c.supermarket and c.in_stock and fresh(c,dt.datetime.now(UTC),config['max_evidence_age_hours']) and (row.get('list_kind')=='bestseller' or c.discount_percent>=config['min_advertised_discount']):
            key=(c.product_key,c.variant_key,c.warranty_key,c.price_toman)
            by_product.setdefault(key,[]).append(row)
    # Interleave types and both report sections before applying the request budget.
    pools={}
    for key, entries in by_product.items():
        c=entries[0]['candidate']
        section='bestseller' if any(r.get('list_kind')=='bestseller' for r in entries) else 'discount'
        pools.setdefault((section,c['category']),[]).append(key)
    for pool in pools.values():
        pool.sort(key=lambda key:(not bool(model_terms(by_product[key][0]['candidate']['title'])), -by_product[key][0]['candidate']['discount_percent']))
    selected=[]
    while any(pools.values()):
        for pool in pools.values():
            if pool: selected.append(pool.pop(0))
    if config.get('_hunt_order'):
        selected=list(by_product)
    selected=selected[:int(settings.get('max_candidates',60))]
    deadline=time.monotonic()+min(600,float(settings.get('max_duration_seconds',480)))
    errors,checked=[],0
    coverage=dict(provider='torob_mcp',provider_status='available',checked=0,selected=len(selected),
                  matched_direct_quotes=0,budget_exhausted=False,failure_counts={})
    try:
        with client_factory(config) as client:
            for key in selected:
                if time.monotonic()>=deadline:
                    coverage['budget_exhausted']=True; break
                c=Offer.parse(by_product[key][0]['candidate']); checked+=1
                terms=model_terms(c.title)
                query=((c.brand_name+' '+' '.join(terms)).strip() if terms else c.title)[:200]
                try:
                    search=client.call('search_torob',dict(query=query,limit=10,include_ads=False))
                    matches=[p for p in search['items'] if model_match(c,(p.get('title') or '')+' '+(p.get('title_en') or '')) or model_match(c,p.get('title') or '')]
                    comparison=uncertain('no_matching_model')
                    if matches:
                        # Several Torob parents can represent the same model.
                        # Every seller still has to pass variant/condition checks.
                        comparisons=[]
                        parent_limit=min(3,max(1,int(settings.get('search_results',2))))
                        for parent in matches[:parent_limit]:
                            if time.monotonic()>=deadline:break
                            reference=parent['id']
                            detail=client.call('product_details',dict(product=reference))
                            sellers=client.call('product_sellers',dict(product=reference,limit=50,shop_type='online',include_ads=False,available_only=True))
                            comparisons.append(compare_listings(c,detail,sellers,dt.datetime.now(UTC),max(3,int(settings.get('min_comparable_shops',3)))))
                        comparison=combine_comparisons(c,comparisons,max(3,int(settings.get('min_comparable_shops',3))),limited=len(comparisons)<len(matches))
                    for row in by_product[key]: row['price_comparison']=comparison
                except MCPError as error:
                    errors.append(error.code)
                    for row in by_product[key]: row['price_comparison']=uncertain(error.code)
                    if error.code=='UPSTREAM_BLOCKED' or error.http_status in (401,403,429,490):
                        coverage['provider_status']='unavailable'; break
    except Exception as error:
        errors.append(error.code if isinstance(error,MCPError) else 'MCP_CONNECTION_FAILED')
        coverage['provider_status']='unavailable'
    coverage.update(checked=checked,failure_counts=dict(Counter(errors)))
    config['_market_coverage']=coverage


def selection_reason(row):
    comparison=row.get('price_comparison',{})
    if row.get('list_kind')=='bestseller':
        return f'جایگاه {row["source_rank"]} در فهرست {row["ranking_scope"]}'
    if comparison.get('comparable_shop_count',0):
        percent=comparison['saving_percent']
        return f'{abs(percent):g}٪ '+('پایین‌تر از' if percent>0 else 'بالاتر یا برابر با')+' کمترین پیشنهاد همسان مشاهده‌شده در ترب'
    return f'تخفیف اعلامی {row["candidate"]["discount_percent"]:g}٪؛ صرفه‌جویی بازار تأیید نشده'
