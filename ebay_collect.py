"""Bounded live eBay Browse searches, independent of Facebook availability."""
import fcntl, re, time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from decimal import Decimal
from ebay_client import EbayClient, EbayError
from ebay_store import owner_hash, save_check, snapshot, health
from watch_config import search_bounds, definitions, fingerprint, now

def number(value):
    if not isinstance(value,dict) or value.get('currency')!='USD':return None
    try:v=Decimal(value['value']);return v if v.is_finite() and v>=0 else None
    except Exception:return None

def normalize(item,root):
    item_id=str(item.get('itemId',''));safe_id='ebay_'+re.sub(r'[^a-zA-Z0-9_-]','_',item_id)
    if not item_id or len(safe_id)>160:return None
    seller=item.get('seller',{}).get('username')
    if not seller:return None
    amount=number(item.get('price'));shipping=[number(x.get('shippingCost')) for x in item.get('shippingOptions',[])]
    shipping=[x for x in shipping if x is not None];ship=min(shipping) if shipping else None
    total=amount+ship if amount is not None and ship is not None else None
    specs=[]
    for aspect in item.get('localizedAspects',[]):
        name=aspect.get('name','');value=aspect.get('value','')
        if not value or value.casefold() in ('none','n/a','does not apply'):continue
        if name in ('RAM Size','Memory'):specs.append(value+' RAM')
        elif name in ('SSD Capacity','Storage Capacity','Hard Drive Capacity'):specs.append(value+' SSD')
        elif name in ('Processor','Processor Model','Model','Screen Size'):specs.append(name+': '+value)
    condition=str(item.get('condition',''))
    loc=item.get('itemLocation',{})
    row={'id':safe_id,'source':'ebay','title':str(item.get('title',''))[:600],
        'description':' · '.join(dict.fromkeys(specs))[:2000], 'condition':'new' if condition.lower().startswith('new') else condition,
        'price':f'${amount:,.2f}' if amount is not None else 'Unverified price','price_value':float(amount) if amount is not None else None,
        'shipping':float(ship) if ship is not None else None,'total':float(total) if total is not None else None,
        'location':', '.join(str(loc[k]) for k in ('city','stateOrProvince','country') if loc.get(k)),
        'post_url':item.get('itemWebUrl',''),'buying_options':item.get('buyingOptions',[]),'_owner':owner_hash(root,seller)}
    return row

def classify(row,watch,matcher):
    if 'FIXED_PRICE' not in row['buying_options']:return 'auction or purchase price needs review'
    listing=SimpleNamespace(**row)
    if row['total'] is not None:listing.price=f"${row['total']:,.2f}"
    why=matcher(watch['id'],listing,watch)
    return why or ('shipping cost needs review' if row['shipping'] is None else None)

def run(root,matcher,item_id=None):
    root=Path(root);directory=root/'state/ebay';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (directory/'scan.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'running'}
        _,watches,_=definitions(root);client=EbayClient(root);started=now();calls=0;errors=0;totals=0
        old_rows,_,_=snapshot(root);old={r['key']:r for r in old_rows};deadline=time.monotonic()+240
        health(root,'scan',{'status':'running','started_at':started})
        active=[w for w in watches.values() if (not item_id or w['id']==item_id) and w['enabled'] and not w['archived'] and 'ebay' in w.get('sources',[])]
        for index,watch in enumerate(active):
            # Reserve both search pages per remaining query before optional enrichment.
            reserved=sum(2*len(w['queries']) for w in active[index+1:])
            detail_limit=max(0,85-reserved)
            report={'status':'ok','checked_at':now(),'fingerprint':fingerprint(watch),'candidate_count':0,'matches':0,'sent':0,'limited':False,'queries':len(watch['queries'])}
            found={};rows=[]
            try:
                for query in watch['queries']:
                    for offset in (0,100):
                        if calls>=85 or time.monotonic()>deadline:raise EbayError('Scan request budget reached; narrow the search or reduce active watches.')
                        conditions=',conditions:{USED|REFURBISHED}' if watch['condition']=='used' else ',conditions:{NEW}' if watch['condition']=='new' else ''
                        params={'q':query,'limit':'100','offset':str(offset),'sort':'newlyListed','filter':f"price:[{search_bounds(watch)[0]}..{search_bounds(watch)[1]}],priceCurrency:USD,itemLocationCountry:US,buyingOptions:{{FIXED_PRICE}}"+conditions}
                        result=client.get('/buy/browse/v1/item_summary/search',params);calls+=1
                        for summary in result.get('itemSummaries',[]):found[summary['itemId']]=summary
                        if not result.get('next'):break
                        if offset==100:report['limited']=True
                for summary in found.values():
                    row=normalize(summary,root)
                    if not row:continue
                    key=watch['id']+':'+row['id'];previous=old.get(key,{})
                    why=classify(row,watch,matcher)
                    # Structured item specifics resolve abbreviated titles. Cache those
                    # fields for 12h; every scan still rechecks current price/condition.
                    age=time.time()-previous.get('specs_checked_epoch',0)
                    if previous.get('description') and age<43200:
                        row['description']=previous['description'];row['specs_checked_epoch']=previous['specs_checked_epoch']
                        why=classify(row,watch,matcher)
                    elif watch.get('profile') not in ('thunderbolt4','thunderbolt5') and (why is None or why.startswith(('missing','RAM below'))) and calls<detail_limit and time.monotonic()<deadline:
                        detail=client.get('/buy/browse/v1/item/'+quote(summary['itemId'],safe=''));calls+=1
                        detail={**summary,**detail,'itemLocation':{**summary.get('itemLocation',{}),**detail.get('itemLocation',{})}}
                        if not detail.get('shippingOptions'):detail['shippingOptions']=summary.get('shippingOptions',[])
                        enriched=normalize(detail,root)
                        if enriched:row=enriched;row['specs_checked_epoch']=time.time();why=classify(row,watch,matcher)
                    row['reason']=why or '';row['category']='candidate' if why else 'match';rows.append(row)
                latest=definitions(root)[1].get(watch['id'],{})
                if not latest.get('enabled') or latest.get('archived') or 'ebay' not in latest.get('sources',[]) or fingerprint(latest)!=fingerprint(watch):continue
                report.update(candidate_count=len(rows),matches=sum(r['category']=='match' for r in rows),checked_at=now())
                save_check(root,watch,rows,report);totals+=len(rows)
            except EbayError as error:
                errors+=1;report.update(status='error',detail=str(error),checked_at=now());save_check(root,watch,[],report)
            except Exception:
                errors+=1;report.update(status='error',detail='eBay response could not be processed; last completed results are retained.',checked_at=now());save_check(root,watch,[],report)
        final={'status':'error' if errors else 'ok','started_at':started,'finished_at':now(),'api_calls':calls,'listings':totals,'failed_watches':errors}
        health(root,'scan',final);return final
