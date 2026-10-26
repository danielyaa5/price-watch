"""Amazon public search and quota-limited Google Shopping through SerpApi."""
import fcntl
import hashlib
import ipaddress
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from watch_config import definitions, fingerprint, now, atomic, read

SOURCES = ('amazon', 'google_shopping')
NAMES = {'amazon': 'Amazon', 'google_shopping': 'Google Shopping'}
GOOGLE_CAP = 200
WINDOW = 31 * 86400


class ShoppingError(Exception):
    pass


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def all(self, predicate):
        for child in self.children:
            if isinstance(child, Node):
                if predicate(child):
                    yield child
                yield from child.all(predicate)

    def text(self):
        if self.tag in ('script', 'style'):
            return ''
        return ' '.join(c.text() if isinstance(c, Node) else c for c in self.children).strip()


class Document(HTMLParser):
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def canonical_url(value):
    """Public HTTPS product links only; this does not fetch merchant addresses."""
    try:
        u = urlsplit(value)
        host = u.hostname or ''
        if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
            return None
        if '.' not in host or host.endswith(('.local','.localhost','.internal')) or host == 'localhost':
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        if len(value) > 5000:
            return None
        if host in ('amazon.com', 'www.amazon.com'):
            m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)', u.path)
            return 'https://www.amazon.com/dp/' + m[1] if m else None
        query = parse_qs(u.query)
        if host in ('google.com','www.google.com'):
            if u.path != '/search' or not query.get('prds'):
                return None
            keep = ('q','prds','udm','ibp','hl','gl')
        else:
            keep = tuple(k for k in query if not k.startswith(('utm_','gclid','fbclid','srsltid','msclkid')))
        return urlunsplit(('https', host, u.path, urlencode({k:query[k][0] for k in keep if k in query}), ''))
    except (TypeError, ValueError):
        return None


def dollars(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:US\s*)?\$[\d,]+(?:\.\d{2})?', value.strip()):
        return None
    try:
        amount = Decimal(re.sub(r'[^\d.]', '', value))
        return float(amount) if amount.is_finite() and 0 < amount <= 1000000 else None
    except InvalidOperation:
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ShoppingError('The source redirected its request; connection needs review.')


def fetch(url):
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname not in ('www.amazon.com','serpapi.com') or u.port not in (None,443) or u.username or u.password:
        raise ShoppingError('Unsupported shopping source address.')
    try:
        request = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0','Accept-Language':'en-US,en;q=0.9'})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ShoppingError('The search response exceeded its size limit.')
        return raw.decode('utf-8')
    except urllib.error.HTTPError as error:
        raise ShoppingError(f'The source rejected the search (HTTP {error.code}); earlier results retained.') from None
    except (OSError, ValueError):
        # Never expose URLs containing provider credentials in exceptions/logs.
        raise ShoppingError('The source could not return a readable response; earlier results retained.') from None


AMAZON_STATE = Path(__file__).resolve().parent/'state/amazon/browser-state.json'


def fetch_amazon(url):
    """Read public search pages in an unsigned-in browser; never solve challenges."""
    u = urlsplit(url)
    if u.scheme != 'https' or u.netloc != 'www.amazon.com' or u.path != '/s':
        raise ShoppingError('Unsupported Amazon search address.')
    from playwright.sync_api import sync_playwright, Error as BrowserError
    # Amazon search pages take 12-25 s to load on the watcher Mac, so a 25 s limit failed now and then.
    # Allow 60 s and one fresh retry before reporting the source as failed.
    for attempt in range(2):
        try:
            return _fetch_amazon_once(url, sync_playwright)
        except BrowserError:
            if attempt:
                raise ShoppingError('Amazon browser search could not finish; earlier results retained.') from None


def _fetch_amazon_once(url, sync_playwright):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # Reusing cookies from an earlier normal results page keeps Amazon from showing its
            # "continue shopping" check page, which a brand-new session gets about half the time.
            context = browser.new_context(locale='en-US', accept_downloads=False,
                                          service_workers='block',
                                          storage_state=str(AMAZON_STATE) if AMAZON_STATE.exists() else None)
            # Search HTML supplies all matching evidence; skip heavy assets.
            context.route('**/*', lambda route: route.abort() if route.request.resource_type
                          in ('image', 'font', 'media') else route.continue_())
            page = context.new_page()
            response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
            final = urlsplit(page.url)
            # Amazon can canonicalize /s to /s/ref=... . The parser below
            # still requires the exact query and selected New filter.
            if final.scheme != 'https' or final.netloc != 'www.amazon.com':
                raise ShoppingError('Amazon redirected its search; connection needs review.')
            if response is None or response.status != 200:
                status = response.status if response else 'unknown'
                raise ShoppingError(f'Amazon rejected the search (HTTP {status}); earlier results retained.')
            html = page.content()
            if len(html.encode('utf-8')) > 8_000_000:
                raise ShoppingError('The search response exceeded its size limit.')
            if '/checkpoint/validate' in html:
                # Never click through it; start the next check from a fresh session instead.
                AMAZON_STATE.unlink(missing_ok=True)
                raise ShoppingError('Amazon showed its "continue shopping" check page; no results inferred.')
            AMAZON_STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            context.storage_state(path=str(AMAZON_STATE))
            if AMAZON_STATE.exists():AMAZON_STATE.chmod(0o600)
            return html
        finally:
            browser.close()


def parse_amazon(html, query):
    doc = Document(html).root
    visible = doc.text()
    if re.search(r'robot check|enter the characters you see|automated access|validatecaptcha', visible, re.I):
        raise ShoppingError('Amazon requires a human check; no results inferred.')
    inputs = list(doc.all(lambda n:n.tag=='input' and n.attrs.get('id')=='twotabsearchtextbox'))
    new_filter = list(doc.all(lambda n:n.attrs.get('id')=='p_n_condition-type/6461716011'))
    selected = any(a.attrs.get('aria-current')=='true' and a.text().strip()=='New' for n in new_filter for a in n.all(lambda a:a.tag=='a'))
    if not inputs or inputs[0].attrs.get('value') != query or not selected:
        raise ShoppingError('Amazon did not confirm the requested query and New-condition filter.')
    cards = list(doc.all(lambda n:n.attrs.get('data-component-type')=='s-search-result'))
    if not cards and not re.search(r'no results for|did not match any products', visible, re.I):
        raise ShoppingError('Amazon returned an unfamiliar page, not a verified empty search.')
    rows = {}
    for card in cards[:48]:
        asin = card.attrs.get('data-asin', '')
        headings = list(card.all(lambda n:n.tag=='h2'))
        if not re.fullmatch(r'[A-Z0-9]{10}', asin) or not headings:
            raise ShoppingError('Amazon product identifiers or titles could not be read.')
        title = headings[0].text().strip()
        prices = [n for n in card.all(lambda n:'a-price' in n.attrs.get('class','').split()) if 'a-text-price' not in n.attrs.get('class','').split()]
        values = [v.text().strip() for n in prices for v in n.all(lambda n:'a-offscreen' in n.attrs.get('class','').split())]
        price = next((p for p in values if dollars(p) is not None), '')
        text = card.text()
        condition = 'used' if re.search(r'\b(renewed|refurbished|pre-owned|open.box)\b', title, re.I) else 'new'
        available = any((n.attrs.get('value') or n.text()).strip()=='Add to cart' for n in card.all(lambda n:n.tag in ('button','input','span')))
        # Do not infer Prime, coupon, delivery or checkout eligibility from a headline price.
        row = {'id':'amazon_'+asin,'source':'amazon','title':title[:600],'description':'','condition':condition,
               'price':price or 'Price unavailable','price_value':dollars(price),'shipping':None,'total':None,
               'store':'Amazon','location':'Amazon · online','post_url':'https://www.amazon.com/dp/'+asin,
               'available':available,'evidence':'Amazon search price with the New filter selected. Shipping, tax, coupons, Prime eligibility and availability at your address need checking.'}
        rows[asin] = row
    return list(rows.values())


def google_settings(root):
    return read(Path(root)/'shopping-private.json', {})


def configure_google(payload, root):
    with (Path(root)/'settings.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = google_settings(root)
        key = payload.get('api_key','')
        interval = payload.get('interval_hours', current.get('interval_hours',6))
        if interval not in (6,12,24) or isinstance(interval,bool):
            raise ValueError('Choose Google checks every 6, 12 or 24 hours.')
        if not isinstance(key,str) or (key and not re.fullmatch(r'[a-fA-F0-9]{64}',key)):
            raise ValueError('Enter the 64-character SerpApi key.')
        if payload.get('disconnect') is True:
            current.pop('api_key',None)
        elif key:
            current['api_key']=key
        elif not current.get('api_key'):
            raise ValueError('Add a SerpApi key to connect Google Shopping.')
        current['interval_hours']=interval
        atomic(Path(root)/'shopping-private.json',current)
    return {'ok':True,'message':'Google Shopping connection saved. Check the source to verify it.'}


def google_status(root):
    config = google_settings(root)
    usage = read(Path(root)/'state/google_shopping/usage.json',{})
    used = sum(t > time.time()-WINDOW for t in usage.get('attempts',[]))
    return {'connected':bool(config.get('api_key')),'interval_hours':config.get('interval_hours',6),
            'cap':GOOGLE_CAP,'used':used,'remaining':max(0,GOOGLE_CAP-used),'window_days':31,
            'provider':'SerpApi Free','provider_remaining':usage.get('provider_remaining')}


def google_request(root, params, fetcher=fetch):
    config = google_settings(root)
    if not config.get('api_key'):
        raise ShoppingError('Connect the free SerpApi account in Sources to enable Google Shopping.')
    path = Path(root)/'state/google_shopping/usage.json'
    usage = read(path,{})
    attempts = [t for t in usage.get('attempts',[]) if t > time.time()-WINDOW]
    if len(attempts) >= GOOGLE_CAP:
        raise ShoppingError('Google’s 200-search safety cap is reached. Checks resume as older searches leave the 31-day window.')
    attempts.append(time.time());usage['attempts']=attempts;atomic(path,usage)
    raw = fetcher('https://serpapi.com/search.json?'+urlencode({**params,'api_key':config['api_key']}))
    try:
        response = json.loads(raw)
    except (TypeError,ValueError):
        raise ShoppingError('Google search provider returned an unreadable response.') from None
    if response.get('error'):
        # Provider text can include request details; expose a fixed message only.
        raise ShoppingError('Google search provider could not complete the search. Check the account and remaining quota.')
    if response.get('search_metadata',{}).get('status') != 'Success':
        raise ShoppingError('Google search provider did not confirm a successful response.')
    return response


def new_token(response):
    for group in response.get('filters',[]):
        for option in group.get('options',[]):
            if option.get('text','').casefold() == 'new' and isinstance(option.get('shoprs'),str):
                return option['shoprs']
    return None


def parse_google(response, token):
    params = response.get('search_parameters',{})
    if params.get('engine') != 'google_shopping' or params.get('gl') != 'us' or params.get('shoprs') != token:
        raise ShoppingError('Google did not confirm the US search and requested New filter.')
    products = response.get('shopping_results')
    if products is None and response.get('search_information',{}).get('shopping_results_state') == 'Fully empty':
        return []
    if not isinstance(products,list):
        raise ShoppingError('Google returned an unfamiliar result format, not a verified empty search.')
    rows = {}
    for product in products[:40]:
        title, store = product.get('title'), product.get('source')
        # eBay records remain exclusively in the account-deletion-aware eBay store.
        if re.search(r'\bebay\b',str(store),re.I):
            continue
        url = canonical_url(product.get('product_link') or product.get('link') or '')
        price = product.get('price','')
        if not title or not store or not url or dollars(price) is None:
            continue
        condition = 'used' if product.get('second_hand') or re.search(r'\b(used|refurbished|renewed|pre-owned|open.box)\b',str(product.get('condition',''))+' '+title,re.I) else 'new'
        identity = str(product.get('product_id') or url)+'|'+store.casefold()
        key = 'google_'+hashlib.sha256(identity.encode()).hexdigest()[:24]
        rows[key]={'id':key,'source':'google_shopping','title':str(title)[:600],'description':'','condition':condition,
                   'price':price,'price_value':dollars(price),'shipping':None,'total':None,'store':str(store)[:100],
                   'location':str(store)[:100]+' · via Google Shopping','post_url':url,'available':None,
                   'evidence':'Google Shopping indexed price with the New filter selected. Exact variant, merchant stock, shipping and tax require confirmation. Other stores may offer this product.'}
    if products and not rows and not all(re.search(r'\bebay\b',str(p.get('source','')),re.I) for p in products):
        raise ShoppingError('Google products lacked usable US prices or product links; previous results retained.')
    return list(rows.values())


def google_search(root, query, fetcher=fetch):
    cache_path = Path(root)/'state/google_shopping/filters.json'
    cache = read(cache_path,{})
    key = hashlib.sha256(query.encode()).hexdigest()
    params={'engine':'google_shopping','q':query,'gl':'us','hl':'en','google_domain':'google.com'}
    token = cache.get(key)
    if not token:
        token = new_token(google_request(root,params,fetcher))
        if not token:
            raise ShoppingError('Google did not offer a verifiable New-condition filter for this phrase.')
        cache[key]=token;atomic(cache_path,dict(list(cache.items())[-100:]))
    return parse_google(google_request(root,{**params,'shoprs':token},fetcher), token)


def snapshot(root, source):
    data = read(Path(root)/f'state/{source}/results.json',{})
    return list(data.get('catalog',{}).values()),data.get('checks',{}),data.get('scan',{})


def save(root, source, watch, rows, report):
    path=Path(root)/f'state/{source}/results.json';data=read(path,{})
    catalog=data.setdefault('catalog',{})
    for row in rows:
        key=watch['id']+':'+row['id'];old=catalog.get(key,{})
        history=old.get('price_history',[])
        if row['price']!=old.get('price'):
            history=(history+[{'at':report['checked_at'],'price':row['price']}])[-20:]
        catalog[key]={**row,'key':key,'watch_id':watch['id'],'fingerprint':fingerprint(watch),
                      'first_seen':old.get('first_seen',report['checked_at']),'last_seen':report['checked_at'],'price_history':history}
    data['catalog']=dict(sorted(catalog.items(),key=lambda item:item[1]['last_seen'],reverse=True)[:3000])
    data.setdefault('checks',{})[watch['id']]=report;atomic(path,data)


def run(root, matcher, source, item_id=None, fetcher=fetch, force=False):
    if source not in SOURCES:
        raise ValueError('Unknown shopping source.')
    root=Path(root);directory=root/f'state/{source}';directory.mkdir(parents=True,exist_ok=True)
    with (directory/'scan.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'running'}
        started=now();path=directory/'results.json';data=read(path,{})
        previous_scan=data.get('scan',{})
        data['scan']={'status':'running','started_at':started};atomic(path,data)
        deadline=time.monotonic()+180;failed=total=calls=skipped=0
        for watch in definitions(root)[1].values():
            if item_id and watch['id']!=item_id:continue
            if not watch['enabled'] or watch['archived'] or source not in watch['sources']:continue
            report={'status':'ok','checked_at':now(),'fingerprint':fingerprint(watch),'limited':True}
            if watch['condition']=='used':
                report.update(status='disabled',detail='This source searches New / Any-condition watches.');save(root,source,watch,[],report);continue
            previous=snapshot(root,source)[1].get(watch['id'],{})
            if source=='google_shopping' and not force and previous.get('status') in ('ok','error') and previous.get('fingerprint')==fingerprint(watch):
                age=time.time()-datetime.fromisoformat(previous['checked_at']).timestamp()
                if age<google_settings(root).get('interval_hours',6)*3600:
                    skipped+=1;continue
            try:
                found={}
                for query in watch['queries']:
                    if calls>=16 or time.monotonic()>deadline:
                        raise ShoppingError('Shopping scan limit reached; reduce active search phrases.')
                    calls+=1
                    if source=='amazon':
                        url='https://www.amazon.com/s?'+urlencode({'k':query,'rh':'p_n_condition-type:6461716011'})
                        rows=parse_amazon((fetch_amazon if fetcher is fetch else fetcher)(url),query)
                    else:
                        # Short model names can overlap unrelated products in Shopping.
                        search_query='Bambu Lab '+query if watch['profile']=='ams' and 'bambu' not in query.casefold() else query
                        rows=google_search(root,search_query,fetcher)
                    for row in rows:
                        why=matcher(watch['id'],SimpleNamespace(**row),watch)
                        if not why and row['condition']!='new':why='retail condition needs review'
                        if not why and source=='amazon' and not row['available']:why='availability needs review'
                        if not why and watch['profile']=='ams' and re.search(r'\b(heating unit|mainboard|power board|main frame|upgrade kit|motors?|gears?|feeder unit|guards?|inlets?|protectors?|spare|replacement|couplers?|tubes?)\b',row['title'],re.I):why='replacement part or upgrade accessory'
                        if not why and watch['profile']=='custom' and not all(t in row['title'].casefold() for t in re.findall(r'\w+',query.casefold())):why='search relevance needs review'
                        row.update(reason=why or '',category='candidate' if why else 'match');found[row['id']]=row
                latest=definitions(root)[1].get(watch['id'],{})
                if not latest.get('enabled') or latest.get('archived') or source not in latest.get('sources',[]) or fingerprint(latest)!=fingerprint(watch):continue
                rows=list(found.values());total+=len(rows)
                report.update(checked_at=now(),candidate_count=len(rows),matches=sum(r['category']=='match' for r in rows),
                              detail=f'{len(rows)} results from '+('Amazon’s first search page. New filter verified; final shipping, tax and local availability need checking.' if source=='amazon' else 'Google Shopping via SerpApi. New filter verified; indexed prices and merchant stock need checking.'))
                save(root,source,watch,rows,report)
            except ShoppingError as error:
                failed+=1;report.update(status='error',detail=str(error),checked_at=now());save(root,source,watch,[],report)
            except Exception:
                failed+=1;report.update(status='error',detail='Search response could not be processed; earlier results retained.',checked_at=now());save(root,source,watch,[],report)
        final={'status':'error' if failed else 'ok','started_at':started,'finished_at':now(),'requests':calls,'listings':total,'failed_watches':failed,'skipped_watches':skipped}
        data=read(path,{});data['scan']=previous_scan if skipped and not calls and not failed else final;atomic(path,data);return final
