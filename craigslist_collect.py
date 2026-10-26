"""Bounded Craigslist public HTML searches with local price/condition/distance checks."""
import fcntl, hashlib, json, math, re, time, urllib.request, urllib.error
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit, parse_qs
from shopping_collect import Document, dollars, save, snapshot as stored_snapshot
from watch_config import search_bounds, definitions, fingerprint, atomic, read, now, SEARCH_AREA

CENTER=(SEARCH_AREA['latitude'],SEARCH_AREA['longitude'])
CENTER_NAME=SEARCH_AREA['name']

class CraigslistError(Exception):pass

def canonical_url(value):
    try:
        u=urlsplit(value)
        if u.scheme!='https' or u.username or u.password or u.port not in (None,443) or not re.fullmatch(r'[a-z]+\.craigslist\.org',u.hostname or ''):return None
        if not (re.fullmatch(r'/view/d/[a-z0-9-]+/[A-Za-z0-9]{12,40}',u.path) or re.fullmatch(r'/[a-z]{3}/d/[a-z0-9-]+/\d+\.html',u.path) or re.fullmatch(r'/[a-z]{3}/[a-z]{3}/d/[a-z0-9-]+/\d+\.html',u.path)):return None
        return u._replace(query='',fragment='').geturl()
    except (ValueError,TypeError):return None

def allowed(url):
    u=urlsplit(url)
    return u.scheme=='https' and not u.username and not u.password and u.port in (None,443) and bool(re.fullmatch(r'[a-z]+\.craigslist\.org',u.hostname or '')) and (u.path.startswith('/search/') or canonical_url(url))

class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not allowed(newurl):raise CraigslistError('Craigslist redirected outside its public search or listing pages.')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(url):
    if not allowed(url):raise CraigslistError('Unsupported Craigslist address.')
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept-Language':'en-US,en;q=0.9'})
        with urllib.request.build_opener(Redirect).open(req,timeout=20) as response:raw=response.read(3_000_001)
        if len(raw)>3_000_000:raise CraigslistError('Craigslist response exceeded the size limit.')
        return raw.decode('utf-8')
    except urllib.error.HTTPError as e:raise CraigslistError(f'Craigslist rejected the request (HTTP {e.code}); earlier results retained.') from None
    except (OSError,ValueError):raise CraigslistError('Craigslist did not return a readable page; earlier results retained.') from None

def schema(doc,key):
    nodes=list(doc.all(lambda n:n.tag=='script' and n.attrs.get('id')==key and n.attrs.get('type')=='application/ld+json'))
    try:
        value=json.loads(''.join(c for c in nodes[0].children if isinstance(c,str)))
        if not isinstance(value,dict):raise ValueError()
        return value
    except (IndexError,TypeError,ValueError):raise CraigslistError('Craigslist page structure changed; no empty results inferred.') from None

def distance(offer):
    geo=offer.get('availableAtOrFrom',{}).get('geo',{})
    try:
        lat,lon=float(geo['latitude']),float(geo['longitude'])
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:return None
        p1,p2=map(math.radians,(CENTER[0],lat));dp=p2-p1;dl=math.radians(lon-CENTER[1]);a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return round(3958.7613*2*math.asin(min(1,math.sqrt(a))),2)
    except (ValueError,TypeError,KeyError):return None

def search_url(watch,query):
    return 'https://losangeles.craigslist.org/search/sss?'+urlencode({'query':query,'min_price':search_bounds(watch)[0],'max_price':search_bounds(watch)[1],'lat':CENTER[0],'lon':CENTER[1],'search_distance':watch['radius'],'sort':'date'})

def parse_search(html,watch,query):
    doc=Document(html).root
    links=list(doc.all(lambda n:n.tag=='link' and n.attrs.get('rel')=='canonical'))
    canonical=links[0].attrs.get('href','') if links else ''
    if parse_qs(urlsplit(canonical).query).get('query',[''])[0].casefold()!=query.casefold():raise CraigslistError('Craigslist did not confirm the requested search phrase.')
    meta=schema(doc,'ld_searchpage_data');location=meta.get('contentLocation',{})
    if location.get('name')!=CENTER_NAME:raise CraigslistError(f'Craigslist did not confirm {CENTER_NAME} as the search area.')
    breadcrumbs=meta.get('breadcrumb',{}).get('itemListElement',[])
    params=[parse_qs(urlsplit(str(x.get('item',''))).query) for x in breadcrumbs]
    if not any(all(p.get(k)==[str(v)] for k,v in {'lat':CENTER[0],'lon':CENTER[1],'radius':watch['radius']}.items()) for p in params):raise CraigslistError('Craigslist did not confirm the requested search center and radius.')
    products=schema(doc,'ld_searchpage_results').get('itemListElement')
    cards=list(doc.all(lambda n:'cl-static-search-result' in n.attrs.get('class','').split()))
    if not isinstance(products,list) or len(products)!=len(cards):raise CraigslistError('Craigslist listing data could not be reconciled with its search cards.')
    rows=[]
    for card,entry in zip(cards[:50],products[:50]):
        product=entry.get('item',{});offer=product.get('offers',{});title=product.get('name','')
        anchors=list(card.all(lambda n:n.tag=='a'))
        url=canonical_url(anchors[0].attrs.get('href','')) if anchors else None
        if not title or not url or title!=card.attrs.get('title'):raise CraigslistError('Craigslist listing title or link could not be confirmed.')
        price=dollars('$'+str(offer.get('price',''))) if offer.get('priceCurrency')=='USD' else None
        if price is None:raise CraigslistError('Craigslist returned a price without a verified US dollar amount.')
        locality=offer.get('availableAtOrFrom',{}).get('address',{}).get('addressLocality','')
        rows.append({'id':'craigslist_'+hashlib.sha256(url.encode()).hexdigest()[:24],'source':'craigslist','title':title[:600],
            'post_url':url,'description':'','price':f'${price:,.2f}','price_value':price,'shipping':None,'total':None,
            'condition':'','location':str(locality)[:100],'distance_miles':distance(offer),
            'evidence':'Public Craigslist search. Distance is approximate from the listing map marker to '+CENTER_NAME+'; exact pickup and availability need confirmation.'})
    return rows,len(products)>=50

def detail(html,row):
    doc=Document(html).root;product=schema(doc,'ld_posting_data');offer=product.get('offers',{})
    if product.get('@type')!='Product' or product.get('name')!=row['title']:raise CraigslistError('Craigslist listing changed while it was being checked.')
    price=dollars('$'+str(offer.get('price',''))) if offer.get('priceCurrency')=='USD' else None
    if price is None:raise CraigslistError('Craigslist listing price could not be confirmed.')
    attrs=list(doc.all(lambda n:'condition' in n.attrs.get('class','').split() and 'attr' in n.attrs.get('class','').split()))
    condition=re.sub(r'^condition\s*:\s*','',attrs[0].text().strip(),flags=re.I).strip().casefold() if attrs else ''
    if not condition:
        legacy=list(doc.all(lambda n:n.tag=='span' and n.text().strip().startswith('condition:')))
        if legacy:condition=legacy[0].text().split(':',1)[1].strip().casefold()
    bodies=list(doc.all(lambda n:n.attrs.get('id')=='postingbody'))
    description=bodies[0].text() if bodies else ''
    return {**row,'price':f'${price:,.2f}','price_value':price,'description':description[:12000],
            'condition':condition,'distance_miles':distance(offer),
            'evidence':'Public Craigslist title, price, condition and description checked. Distance is approximate from its map marker to '+CENTER_NAME+'. Confirm exact pickup and availability with the seller.'}

def snapshot(root):return stored_snapshot(root,'craigslist')

def run(root,matcher,item_id=None,fetcher=fetch):
    root=Path(root);directory=root/'state/craigslist';directory.mkdir(parents=True,exist_ok=True)
    with (directory/'scan.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'running'}
        path=directory/'results.json';started=now();data=read(path,{});data['scan']={'status':'running','started_at':started};atomic(path,data)
        deadline=time.monotonic()+180;calls=failed=total=0;blocked=False
        def get(url):
            nonlocal calls,blocked
            if blocked:raise CraigslistError('Craigslist denied access earlier in this run; remaining checks stopped.')
            if calls>=36 or time.monotonic()>deadline:raise CraigslistError('Craigslist scan limit reached; narrow the search phrases.')
            if calls:time.sleep(.5)
            calls+=1
            try:return fetcher(url)
            except CraigslistError as e:
                if 'HTTP 403' in str(e) or 'HTTP 429' in str(e):blocked=True
                raise
        for watch in definitions(root)[1].values():
            if item_id and item_id!=watch['id']:continue
            if not watch['enabled'] or watch['archived'] or 'craigslist' not in watch['sources']:continue
            report={'status':'ok','checked_at':now(),'fingerprint':fingerprint(watch),'limited':False};rows={};details=0
            try:
                for query in watch['queries']:
                    found,limited=parse_search(get(search_url(watch,query)),watch,query);report['limited']|=limited
                    for row in found:
                        if row['distance_miles'] is not None and row['distance_miles']>watch['radius']:continue
                        why=matcher(watch['id'],SimpleNamespace(**row),watch)
                        potential=not why or why.startswith('missing') or 'needs review' in why or why=='new condition not confirmed'
                        if potential and details<20:
                            details+=1
                            try:row=detail(get(row['post_url']),row);why=matcher(watch['id'],SimpleNamespace(**row),watch)
                            except CraigslistError as e:
                                if blocked:raise
                                why='listing details need review';report['limited']=True
                        elif potential:why='listing detail limit reached; needs review';report['limited']=True
                        if not why and row['condition']=='salvage':why='salvage condition'
                        if not why and watch['condition']=='used' and row['condition'] not in ('like new','excellent','good','fair','used','refurbished'):why='used condition needs review'
                        if not why and watch['condition']=='new' and row['condition']!='new':why='new condition needs review'
                        # The listing page can move the map marker; recheck before keeping it.
                        if row['distance_miles'] is not None and row['distance_miles']>watch['radius']:continue
                        if not why and row['distance_miles'] is None:why='pickup distance needs review'
                        if row['distance_miles'] is not None:row['location']+=f" · about {row['distance_miles']:g} mi from {CENTER_NAME}"
                        # Matching uses public details in memory. Store only the result and public search fields.
                        row['description']='';row.pop('distance_miles',None)
                        row.update(reason=why or '',category='candidate' if why else 'match');rows[row['id']]=row
                latest=definitions(root)[1].get(watch['id'],{})
                if not latest.get('enabled') or latest.get('archived') or 'craigslist' not in latest.get('sources',[]) or fingerprint(latest)!=fingerprint(watch):continue
                total+=len(rows);report.update(checked_at=now(),candidate_count=len(rows),matches=sum(r['category']=='match' for r in rows.values()),detail=f'{len(rows)} public listings checked. Up to 50 search results per phrase and 20 relevant listing details per watch. {watch["radius"]}-mile radius from {CENTER_NAME}, checked against approximate map markers.')
                save(root,'craigslist',watch,list(rows.values()),report)
            except CraigslistError as e:failed+=1;report.update(status='error',checked_at=now(),detail=str(e));save(root,'craigslist',watch,[],report)
            except Exception:failed+=1;report.update(status='error',checked_at=now(),detail='Craigslist response could not be processed; previous results retained.');save(root,'craigslist',watch,[],report)
        final={'status':'error' if failed else 'ok','started_at':started,'finished_at':now(),'requests':calls,'listings':total,'failed_watches':failed}
        data=read(path,{});data['scan']=final;atomic(path,data);return final
