"""Bounded public storefront search, with exact variant prices and stock checks."""
import fcntl, hashlib, json, re, time, urllib.error, urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit
from watch_config import definitions, fingerprint, now, atomic, read

STORES = {
    'universe': {'name':'3D Universe','host':'shop3duniverse.com'},
    'expercom': {'name':'Expercom','host':'expercom.com'},
    'printed': {'name':'Printed Solid','host':'www.printedsolid.com'},
}
HOSTS={s['host'] for s in STORES.values()}
class RetailError(Exception):pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise RetailError('Store redirected the request; connection needs review.')

def fetch(url):
    u=urlsplit(url)
    if u.scheme!='https' or u.hostname not in HOSTS or u.port not in (None,443) or u.username or u.password:
        raise RetailError('Unsupported store URL.')
    try:
        with urllib.request.build_opener(NoRedirect).open(url,timeout=18) as r:
            raw=r.read(4_000_001)
            if len(raw)>4_000_000:raise RetailError('Store response exceeded the size limit.')
            return json.loads(raw)
    except urllib.error.HTTPError as e:raise RetailError(f'Store search returned HTTP {e.code}; no results inferred.') from None
    except (OSError,ValueError) as e:raise RetailError('Store could not return a readable response.') from None

def variants(store,product,currency):
    if currency!='USD':raise RetailError('Store did not confirm USD pricing.')
    if not isinstance(product,dict) or not isinstance(product.get('variants'),list):raise RetailError('Store product format changed.')
    handle=product.get('handle','')
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,250}',handle):raise RetailError('Store product address needs review.')
    rows=[]
    for v in product['variants'][:40]:
        if not str(v.get('id','')).isdigit():raise RetailError('Variant identifier is missing.')
        amount=v.get('price')
        if isinstance(amount,bool) or not isinstance(amount,(int,float)) or amount<=0:raise RetailError('Variant price could not be verified.')
        amount=Decimal(str(amount))/100
        if not amount.is_finite():raise RetailError('Variant price could not be verified.')
        title=str(product.get('title',''))
        variant=str(v.get('title',''))
        if variant and variant!='Default Title':title+=' — '+variant
        # Do not use a product-wide description to prove a selected variant's specs.
        row={'id':f"retail_{store}_{v['id']}",'source':'retail','store':STORES[store]['name'],
             'store_id':store,'title':title[:600],'description':'','condition':'new','available':v.get('available') is True,
             'price':f'${amount:,.2f}','price_value':float(amount),'shipping':None,'total':None,
             'location':STORES[store]['name']+' · online store',
             'post_url':f"https://{STORES[store]['host']}/products/{handle}?variant={v['id']}",
             'evidence':'Exact product variant price and store stock flag. Item price before shipping and tax; confirm final cost at checkout.'}
        rows.append(row)
    return rows

def snapshot(root):
    d=read(Path(root)/'state/retail/results.json',{})
    return list(d.get('catalog',{}).values()),d.get('checks',{}),d.get('scan',{})

def save(root,watch,rows,report):
    path=Path(root)/'state/retail/results.json';d=read(path,{})
    catalog=d.setdefault('catalog',{})
    for row in rows:
        key=watch['id']+':'+row['id'];old=catalog.get(key,{})
        history=old.get('price_history',[])
        if row['price']!=old.get('price'):history=(history+[{'at':report['checked_at'],'price':row['price']}])[-20:]
        catalog[key]={**row,'key':key,'watch_id':watch['id'],'first_seen':old.get('first_seen',report['checked_at']),
                      'last_seen':report['checked_at'],'fingerprint':fingerprint(watch),'price_history':history}
    d['catalog']=dict(sorted(catalog.items(),key=lambda x:x[1]['last_seen'],reverse=True)[:3000])
    d.setdefault('checks',{})[watch['id']]=report;atomic(path,d)

def run(root,matcher,item_id=None,fetcher=fetch):
    root=Path(root);directory=root/'state/retail';directory.mkdir(parents=True,exist_ok=True)
    with (directory/'scan.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'running'}
        started=now();path=directory/'results.json';d=read(path,{})
        d['scan']={'status':'running','started_at':started};atomic(path,d)
        deadline=time.monotonic()+180;calls=0;failures=0;total=0;cache={}
        def get(url):
            nonlocal calls
            if url in cache:return cache[url]
            if calls>=65 or time.monotonic()>deadline:raise RetailError('Retail scan limit reached; reduce active stores or phrases.')
            if calls:time.sleep(.25)
            calls+=1;cache[url]=fetcher(url);return cache[url]
        for watch in definitions(root)[1].values():
            if item_id and watch['id']!=item_id:continue
            if not watch['enabled'] or watch['archived'] or 'retail' not in watch['sources']:continue
            report={'status':'ok','checked_at':now(),'fingerprint':fingerprint(watch),'stores':[], 'limited':False}
            if watch['condition']=='used':
                report.update(status='disabled',detail='Retail search applies to New or Any condition.');save(root,watch,[],report);continue
            rows=[]
            for store in watch.get('retail_stores',list(STORES)):
                host=STORES[store]['host'];result={'id':store,'name':STORES[store]['name'],'status':'ok','listings':0}
                store_rows={}
                try:
                    currency=get(f'https://{host}/cart.js').get('currency')
                    if currency!='USD':raise RetailError('Store did not confirm USD pricing.')
                    for query in watch['queries']:
                        params=urlencode({'q':query,'resources[type]':'product','resources[limit]':10,'resources[options][unavailable_products]':'last'})
                        response=get(f'https://{host}/search/suggest.json?{params}')
                        try:products=response['resources']['results']['products']
                        except (KeyError,TypeError):raise RetailError('Store search format changed; no results inferred.')
                        if not isinstance(products,list):raise RetailError('Store search format changed.')
                        if len(products)>=10:report['limited']=True
                        for product in products[:10]:
                            handle=product.get('handle','')
                            if not re.fullmatch(r'[a-zA-Z0-9-]{1,250}',handle):raise RetailError('Store product address needs review.')
                            detail=get(f'https://{host}/products/{handle}.js')
                            if detail.get('handle')!=handle:raise RetailError('Store returned a different product.')
                            if len(detail.get('variants',[]))>40:report['limited']=True
                            for row in variants(store,detail,currency):
                                why=matcher(watch['id'],SimpleNamespace(**row),watch)
                                if not why and not row['available']:why='out of stock or availability needs review'
                                if not why and re.search(r'\b(used|refurbished|renewed|open.box|pre.owned)\b',row['title'],re.I):why='retail condition needs review'
                                if not why and watch['profile']=='custom':
                                    terms=re.findall(r'\w+',query.casefold())
                                    if not all(t in row['title'].casefold() for t in terms):why='search relevance needs review'
                                row.update(reason=why or '',category='candidate' if why else 'match');store_rows[row['id']]=row
                    rows.extend(store_rows.values());result['listings']=len(store_rows)
                except RetailError as error:result.update(status='error',detail=str(error))
                except Exception:result.update(status='error',detail='Store response could not be processed; earlier results retained.')
                report['stores'].append(result)
            latest=definitions(root)[1].get(watch['id'],{})
            if not latest.get('enabled') or latest.get('archived') or 'retail' not in latest.get('sources',[]) or fingerprint(latest)!=fingerprint(watch) or latest.get('retail_stores',list(STORES))!=watch.get('retail_stores',list(STORES)):continue
            failed=sum(s['status']=='error' for s in report['stores']);failures+=bool(failed)
            report.update(status=('error' if failed==len(report['stores']) else 'partial') if failed else 'ok',candidate_count=len(rows),matches=sum(r['category']=='match' for r in rows),checked_at=now(),
                          detail='; '.join(s['name']+(': '+s['detail'] if s['status']=='error' else f": {s['listings']} variants") for s in report['stores']))
            save(root,watch,rows,report);total+=len(rows)
        final={'status':'partial' if failures else 'ok','started_at':started,'finished_at':now(),'requests':calls,'listings':total,'failed_watches':failures}
        d=read(path,{});d['scan']=final;atomic(path,d);return final
