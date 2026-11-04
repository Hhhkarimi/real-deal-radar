"""Read-only Emalls public search; listed model prices are not variant proof."""
import datetime as dt
import dataclasses
import html
import gzip
import io
import json
import re
import time
import urllib.request
from urllib.parse import quote, urlencode, urljoin, urlsplit

from .core import UTC, Offer
from .sources import HTTP
from .pricing import model_terms, model_match


def search_url(query):
    return 'https://emalls.ir/'+quote('لیست-قیمت')+'~skey~'+quote(query,safe='')


def read_search(query):
    # Fields and path are those used by the public site's pricelist JS.
    form=dict(entekhab='listitemv2',currenturl=search_url(query),attfilters='',minprice=0,
              maxprice=0,brandid=0,findstr=query,pagenum=1,exist='',shop=0,sort='vd')
    request=urllib.request.Request('https://emalls.ir/swservice/site_search',data=urlencode(form).encode(),
        headers={'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','Accept':'application/json',
                 'User-Agent':'RealDealRadar/0.9 (price-research)','X-Requested-With':'XMLHttpRequest',
                 'Referer':'https://emalls.ir/'})
    with HTTP.open(request,timeout=12) as response:
        if urlsplit(response.url).hostname not in ('emalls.ir','www.emalls.ir'):
            raise ValueError('unexpected_emalls_response_host')
        data=response.read(4_000_001)
        if data.startswith(b'\x1f\x8b'):
            data=gzip.GzipFile(fileobj=io.BytesIO(data)).read(4_000_001)
        if len(data)>4_000_000:raise ValueError('emalls_response_too_large')
        result=json.loads(data)
    if not isinstance(result,dict) or not isinstance(result.get('lstsearchresualt'),list):
        raise ValueError('emalls_schema_changed')
    return result['lstsearchresualt']


def listed_comparison(candidate,items,when):
    result=dict(source='emalls',confidence='none',reason='no_matching_model',comparable_shop_count=0,
                fetched_at=when.isoformat(),emalls_url=search_url(candidate.title[:120]),evidence=[])
    matches=[]
    for item in items:
        if item.get('isTak') or item.get('agahi') or item.get('used') or not item.get('offcount'):continue
        title=html.unescape(str(item.get('title','')))
        combined=title+' '+html.unescape(str(item.get('titlefa','')))
        accessory=r'کاور|قاب|روکش|محافظ|کیس'
        if re.search(accessory,combined) and not re.search(accessory,candidate.title):continue
        matches_model=model_match(candidate,combined) or model_match(candidate,title)
        if not matches_model:
            # A parent-model reference is useful, but never variant evidence.
            terms=model_terms(candidate.title)
            if not terms:continue
            parent=dataclasses.replace(candidate,title='مدل '+terms[0],brand_name=candidate.brand_name)
            if not model_match(parent,combined):continue
        value=str(item.get('price','')).translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
        if not re.fullmatch(r'[\d\s,٬]+',value):continue
        price=int(re.sub(r'\D','',value) or 0)
        link=urljoin('https://emalls.ir/',str(item.get('link','')))
        if price<=0 or urlsplit(link).hostname not in ('emalls.ir','www.emalls.ir') or '~id~' not in link:continue
        matches.append(dict(title=title,price_toman=price,url=link,listed_offer_count=item.get('offcount'),source_update_text=item.get('lupdate')))
    if matches:
        lowest=min(matches,key=lambda r:r['price_toman'])
        result.update(confidence='partial',reason='model_price_only_variant_unconfirmed',reference_only=True,
                      listed_low_toman=lowest['price_toman'],listed_high_toman=max(m['price_toman'] for m in matches),
                      reference_saving_percent=round((lowest['price_toman']-candidate.price_toman)/lowest['price_toman']*100,2),
                      emalls_url=lowest['url'],reference_listings=matches)
    return result


def enrich_emalls(rows,config,*,reader=read_search):
    cfg=config.get('emalls',{})
    if not cfg.get('enabled'):return
    coverage=dict(provider='emalls',provider_status='available',checked=0,failure_counts={})
    deadline=time.monotonic()+min(120,float(cfg.get('max_duration_seconds',60)))
    for row in rows:
        row['price_comparison']=dict(source='emalls',confidence='none',reason='not_checked',comparable_shop_count=0,
            emalls_url=search_url(row['candidate']['title'][:120]),evidence=[])
    unique=set()
    for row in rows:
        if coverage['checked']>=int(cfg.get('max_candidates',5)) or time.monotonic()>=deadline:break
        c=Offer.parse(row['candidate'])
        if c.product_key in unique:continue
        unique.add(c.product_key)
        terms=model_terms(c.title)
        query=(c.brand_name+' '+' '.join(terms)).strip() if terms else c.title[:120]
        try:
            coverage['checked']+=1
            row['price_comparison']=listed_comparison(c,reader(query),dt.datetime.now(UTC))
        except Exception as error:
            code='emalls_http_'+str(error.code) if hasattr(error,'code') else 'emalls_unavailable_or_schema_changed'
            coverage['failure_counts'][code]=coverage['failure_counts'].get(code,0)+1
            coverage['provider_status']='unavailable'
            row['price_comparison']['reason']=code
            # Refusal/timeout applies to the source, not to one product.
            break
        time.sleep(max(1,float(cfg.get('request_delay_seconds',1))))
    config['_market_coverage']=coverage
