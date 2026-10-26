#!/usr/bin/env python3
"""Local adapter for ai-marketplace-monitor 0.10.16; direct collectors plus optional Claude CLI research."""
import argparse, fcntl, json, logging, os, re, signal, sqlite3, subprocess, sys, time, tomllib
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, InvalidOperation
from dataclasses import asdict
from watch_config import load_config, definitions, fingerprint, price_rule, condition_bucket
from places import check
import requests
from playwright.sync_api import sync_playwright
from ai_marketplace_monitor.facebook import FacebookMarketplace, FacebookSearchResultPage, FacebookLocationDialog, SESSION_FILE

class DeadlineExceeded(BaseException):
    pass

ROOT = Path(__file__).resolve().parent
STATE = ROOT/'state'
STATE.mkdir(mode=0o700, exist_ok=True)
OBSERVATIONS=[]
SEARCH_LISTING_LIMIT=30  # Facebook listings opened per search phrase, in Facebook's result order
ITEM_BUDGET=1080  # seconds per Facebook watch; the four-phrase Ducati watch needs more than 6 minutes
RUN_BUDGET=1380  # seconds for all Facebook watches, under the 30-minute schedule
LABELS = {'macbook':'MacBook Pro 16-inch M5 Pro, 64GB, 1TB', 'ams':'Bambu Lab AMS 2 Pro', 'studio':'Mac Studio'}

def now(): return datetime.now(timezone.utc).isoformat()
def atomic_json(p, obj):
    q=p.with_suffix('.tmp'); q.write_text(json.dumps(obj,indent=2,ensure_ascii=False)+'\n');q.chmod(0o600);q.replace(p)

def price_value(value):
    s=value.strip()
    if re.search(r'\b(CA|AU|NZ|HK)\$|[€£]|/\s*(mo|month)|per month',s,re.I):return None
    m=re.fullmatch(r'(?:US\s*)?\$?\s*([\d,]+(?:\.\d{1,2})?)',s)
    if not m:return None
    try:return Decimal(m.group(1).replace(',',''))
    except InvalidOperation:return None

def reason(item, listing, definition=None):
    original_item=item
    if definition:item=definition["profile"]
    text=' '.join([listing.title,listing.description]).lower()
    p=price_value(listing.price)
    if p is None or p <= 0:return 'unverified price'
    if item not in (*LABELS,'custom','thunderbolt4','thunderbolt5'):return 'unknown watch'
    maximum=definition['max_price'] if definition else {'macbook':3000,'ams':200,'studio':4000}.get(item,0)
    minimum=definition['min_price'] if definition else (3000 if item=='studio' else 0)
    if definition:
        price_reason=price_rule(definition,listing,float(p))
        if price_reason:return price_reason
    else:
        if p>Decimal(str(maximum)):return 'over maximum'
        if p<Decimal(str(minimum)):return 'below target range'
    if definition:
        for phrase in definition['required']:
            if phrase.casefold() not in text.casefold():return 'missing required phrase: '+phrase
        for phrase in definition['excluded']:
            if phrase.casefold() in text.casefold():return 'excluded phrase: '+phrase
        condition=str(getattr(listing,'condition','')).lower()
        is_new=condition_bucket(listing)=='new'
        if definition['condition']=='used' and is_new:return 'new condition outside used watch'
        if definition['condition']=='new' and not is_new:return 'new condition not confirmed'
    title=listing.title.lower()
    if re.search(r'\b(parts only|for parts|not working|icloud locked|activation locked|mdm locked|repair only)\b',text):return 'non-working or locked'
    if item in ('thunderbolt4','thunderbolt5'):
        generation=item[-1]
        if not re.search(r'\b(?:thunderbolt\s*'+generation+r'|tb'+generation+r')\b',title):return 'missing Thunderbolt '+generation+' in title'
        if re.search(r'\b(?:ac|dc|power)\s*(?:supply|adapter)|\b(?:replacement|spare)\b',title) and not re.search(r'(?:\b(?:with|and|includes?)|\+)\s*(?:a\s+)?(?:ac\s+|dc\s+|power\s+)?(?:adapter|supply)\b',title):return 'power accessory or replacement needs review'
        if re.search(r'\b(?:thunderbolt\s*[45]\s*compatible|compatible\s+with\s+thunderbolt\s*[45])\b',title):return 'compatibility claim only; Thunderbolt hub needs review'
        if not re.search(r'\bthunderbolt\s*[45]?(?:\s+(?:&|usb4|usb-c|laptop|desktop|mini|core|pro|blaze|slim|and|element|\d+-port|\d+-in-\d+)){0,3}\s+(?:hub|dock(?:ing)?(?:\s+station)?)\b',title):return 'Thunderbolt hub or dock identity needs review'
        if re.search(r'\b(?:cable|cord|mount|bracket|case|cover)\s+(?:for|compatible)\b|\b(?:box|cable|adapter)\s+only\b',title):return 'hub accessory'
    elif item=='macbook':
        for pattern,name in [(r'\bmac\s*book\s*pro\b','MacBook Pro'),(r'\bm5\s*pro\b','M5 Pro chip'),(r'\b64\s*(gb|g)\b','64GB RAM'),(r'\b1\s*tb\b|\b1000\s*gb\b','1TB SSD'),(r'\b16(?:\.2)?\s*(?:["″”]|inch|in\b)|\b16\s*[- ]?inch','16-inch display')]:
            if not re.search(pattern,text):return 'missing '+name
        if re.search(r'\b(m[1-4]|m5\s*max)\b',title):return 'wrong chip in title'
        if re.search(r'\b(14(?:\.2)?\s*(?:["″”]|inch|in\b)|(?:24|32|36|48|96|128)\s*gb)\b',title):return 'wrong variant in title'
        if re.search(r'\b(case|sleeve|screen protector|keyboard cover)\b',title):return 'accessory'
    elif item=='studio':
        return studio_reason(listing, used_only=not definition or definition["condition"]=="used")
    elif item=='ams':
        if not re.search(r'\bams\s*[- ]?2\s*pro\b|\bams\s*pro\s*2\b|\bautomatic material system\s*2\s*pro\b',text):return 'not AMS 2 Pro'
        if re.search(r'\b(bracket|stand|mount|printed|riser|buffer|cable|hub|rack|holder|adapter|lid|cover|tray|desiccant|heating unit|mainboard|power board|main frame|upgrade kit|motors?|gears?|feeder unit|guards?|inlets?|protectors?|spare|replacement|couplers?|tubes?)\b',title):return 'accessory'
        if re.search(r'\b(printer|p1s|p2s|h2d|a1|x1c)\b',title):return 'printer bundle needs review'
    return None

def studio_reason(listing, used_only=True):
    title=listing.title.lower()
    text=title+' '+listing.description.lower()
    if not re.search(r'\bmac\s*studio\b',title):return 'not a Mac Studio'
    if re.search(r'\b(display|stand|mount|case|enclosure|dock|box only|empty box)\b',title):return 'accessory or bundle needs review'
    if used_only and (re.search(r'\b(brand new|sealed|unopened)\b',text) or getattr(listing,'condition','').lower()=='new'):return 'new condition outside used watch'
    chips=set(re.findall(r'\bm[1-9]\s*(?:max|ultra)\b',title))
    if not chips:chips=set(re.findall(r'\bm[1-9]\s*(?:max|ultra)\b',text))
    chips={re.sub(r'\s+','',chip) for chip in chips}
    if len(chips)!=1:return 'missing or conflicting chip specifications'
    chip=chips.pop()
    minimum={'m5max':64,'m3ultra':96,'m5ultra':96,'m2ultra':128,'m4max':128}.get(chip)
    if minimum is None:return 'chip outside target configurations'
    # Explicit RAM labels win; otherwise a single RAM-sized value in the title.
    explicit=re.findall(r'\b(\d+)\s*(?:gb|g)\s*(?:unified\s*(?:memory)?|ram|memory)\b',text)
    explicit+=re.findall(r'\b(?:ram|memory)\s*[:=]?\s*(\d+)\s*gb\b',text)
    values={int(x) for x in explicit}
    title_values={int(x) for x in re.findall(r'\b(\d+)\s*(?:gb|g)\b',title) if int(x) in (24,32,36,48,64,96,128,192,256)}
    if len(title_values)>1 or (values and title_values and values!=title_values):return 'missing or conflicting RAM specifications'
    values=values or title_values
    if len(values)!=1:return 'missing or conflicting RAM specifications'
    if values.pop()<minimum:return 'RAM below target for this chip'
    if not re.search(r'\b(?:[1-9]|1[0-6])\s*tb\b|\b(?:512|1000|1024|2000|2048)\s*gb\s*(?:ssd|storage)\b|\b512\s*gb\b',text):return 'missing SSD capacity'
    return None

def facebook_results(market, item, definition):
    from itertools import islice
    results=market.search(item)
    return islice(results,5) if definition['profile'] in ('thunderbolt4','thunderbolt5') else results

def delivery_allowed(definition, latest):
    return bool(latest.get("enabled") and not latest.get("archived") and latest.get("alerts") and 'facebook' in latest.get('sources',['facebook']) and fingerprint(latest)==fingerprint(definition))

def notifications_enabled(config,name,args):
    return bool(args.notify and config.get('watch',{}).get(name,{}).get('notify',True))

def facebook_order(items):
    """Watches whose last Facebook check failed or is oldest go first, so none is always starved by the run budget."""
    try:saved=json.loads((STATE/'items.json').read_text())
    except (OSError,ValueError):saved={}
    last=lambda name:saved.get(name,{}).get('checked_at','') if saved.get(name,{}).get('status')=='ok' else ''
    return sorted(items.items(),key=lambda pair:last(pair[0]))

def save_item_result(name,result):
    path=STATE/'items.json'
    try:items=json.loads(path.read_text())
    except (OSError,ValueError):items={}
    items[name]=result
    atomic_json(path,items)
    catalog_path=STATE/'catalog.json'
    try:catalog=json.loads(catalog_path.read_text())
    except (OSError,ValueError):catalog={}
    for category,rows in [('match',result.get('accepted',[])),('candidate',result.get('rejected',[]))]:
        for row in rows:
            key=name+':'+str(row['id']);old=catalog.get(key,{})
            changes=old.get('price_history',[])
            if row.get('price')!=old.get('price'):changes=(changes+[{'at':result['checked_at'],'price':row.get('price')}])[-20:]
            catalog[key]={**row,'key':key,'watch_id':name,'category':category,'first_seen':old.get('first_seen',result['checked_at']),
                'last_seen':result['checked_at'],'fingerprint':result.get('fingerprint'),'price_history':changes}
    catalog=dict(sorted(catalog.items(),key=lambda pair:pair[1]['last_seen'],reverse=True)[:3000])
    atomic_json(catalog_path,catalog)

def save_history(report):
    path=STATE/'history.json'
    try:history=json.loads(path.read_text())
    except (OSError,ValueError):history=[]
    history.append({k:report.get(k) for k in ('started_at','finished_at','status') } | {
        'items':[name for name,result in report.get('sources',{}).items() if result.get('status')!='disabled'],
        'matches':sum(len(s.get('accepted',[])) for s in report.get('sources',{}).values()),
        'sent':sum(len(s.get('sent',[])) for s in report.get('sources',{}).values())})
    atomic_json(path,history[-60:])

def payload(item,listing):
    return {'title':'Price watch: '+LABELS.get(item,item), 'message':f'{listing.title}\n{listing.price} · {listing.location}\n{listing.post_url}'}

def deliver_once(db,item,listing,baseline=False,sender=None):
    old=db.execute('SELECT status FROM seen WHERE item=? AND id=?',(item,listing.id)).fetchone()
    if old:return 'duplicate',None
    if baseline:
        db.execute('INSERT INTO seen VALUES(?,?,?,?,?,?)',(item,listing.id,'baseline',listing.price,now(),None));db.commit();return 'baseline',None
    db.execute('INSERT INTO seen VALUES(?,?,?,?,?,?)',(item,listing.id,'pending',listing.price,now(),None));db.commit()
    receipt=(sender or send)(item,listing)
    db.execute('UPDATE seen SET status=?,receipt=? WHERE item=? AND id=?',('sent',receipt,item,listing.id));db.commit()
    return 'sent',receipt

def send_ntfy(item,listing):
    dest=json.loads((ROOT/'notification-private.json').read_text())
    msg=payload(item,listing)
    # JSON preserves Unicode in body/title. No third-party attribution or attachment.
    response=requests.post(dest['server'].rstrip('/'),json={'topic':dest['topic'],'priority':4,**msg},timeout=15)
    response.raise_for_status()
    receipt=response.json()
    if receipt.get('event')!='message' or not receipt.get('id'):raise RuntimeError('Notification server supplied no message receipt')
    with (STATE/'receipts.jsonl').open('a') as f:
        f.write(json.dumps({'at':now(),'item':item,'listing_id':listing.id,'message_id':receipt['id'],'title':msg['title'],'message':msg['message']})+'\n')
    return receipt['id']

def email_payload(item,listing):
    return {'subject':f'Price watch: {LABELS.get(item,item)} at {listing.price}',
            'body':payload(item,listing)['message']}

def send_email(item,listing):
    runtime=Path.home()/'.local/share/workspace-mcp/venv/bin/python'
    r=subprocess.run([str(runtime),str(ROOT/'mail_alert.py')],input=json.dumps(email_payload(item,listing)),text=True,capture_output=True,timeout=45)
    if r.returncode:raise RuntimeError('Gmail delivery failed; inspect channel status before retrying')
    receipt=json.loads(r.stdout)['message_id']
    with (STATE/'email-receipts.jsonl').open('a') as f:
        f.write(json.dumps({'at':now(),'item':item,'listing_id':listing.id,'message_id':receipt})+'\n')
    return receipt

def send_channels(db,item,listing,senders):
    db.execute('CREATE TABLE IF NOT EXISTS channel_delivery (item TEXT,id TEXT,channel TEXT,status TEXT,receipt TEXT,PRIMARY KEY(item,id,channel))')
    receipts={};errors=[]
    for channel,sender in senders.items():
        old=db.execute('SELECT status,receipt FROM channel_delivery WHERE item=? AND id=? AND channel=?',(item,listing.id,channel)).fetchone()
        if old:
            if old[0]=='sent':receipts[channel]=old[1]
            else:errors.append(channel)
            continue
        db.execute('INSERT INTO channel_delivery VALUES(?,?,?,?,?)',(item,listing.id,channel,'pending',None));db.commit()
        try:
            receipt=sender(item,listing)
            db.execute('UPDATE channel_delivery SET status=?,receipt=? WHERE item=? AND id=? AND channel=?',('sent',receipt,item,listing.id,channel));db.commit()
            receipts[channel]=receipt
        except Exception:errors.append(channel)
    if errors:raise RuntimeError('Unconfirmed notification delivery: '+', '.join(errors)+'; other channels were still attempted; no automatic retry')
    return json.dumps(receipts)

def send(item,listing):
    with sqlite3.connect(STATE/'seen.sqlite3') as db:
        return send_channels(db,item,listing,{'ntfy':send_ntfy,'email':send_email})

class BoundedFacebook(FacebookMarketplace):
    def set_search_radius(self,radius):
        # Facebook can time out while clicking its location button between watches.
        # Reload once after an unconfirmed change, then require the visible radius.
        for attempt in range(2):
            changed=super().set_search_radius(radius)
            dialog=FacebookLocationDialog(self.page,self.translator,self.logger)
            deadline=time.monotonic()+10
            actual=dialog.current_radius()
            waited=actual!=radius
            while actual!=radius and time.monotonic()<deadline:
                self.page.wait_for_timeout(250)
                actual=dialog.current_radius()
            if actual==radius:
                if self.logger and attempt:self.logger.info('Facebook radius confirmed after one page reload')
                elif self.logger and waited:self.logger.info('Facebook radius confirmed after the page updated')
                return changed or bool(attempt)
            if not attempt:
                if self.logger:self.logger.warning('Facebook radius change did not complete; reloading once before retry')
                dialog.close()
                self.page.reload(wait_until='domcontentloaded',timeout=15000)
        raise RuntimeError(f'Facebook did not apply the requested radius: requested {radius}, observed {actual}')
    pickup_radius=None
    def _exclude(self,listing,item_config,description_available=True):
        # Skip shipped and far-away listings before opening them; each open costs about a minute.
        if self.pickup_radius and check([listing.location],self.pickup_radius).get(listing.location)=='outside':
            if self.logger:self.logger.info('Skipping %s from %s: outside the pickup area',listing.title,listing.location)
            return True
        return super()._exclude(listing,item_config,description_available)
    def wait_for_login(self):
        if not self.logged_in():raise RuntimeError('Facebook authentication required; run the separate login command')

# Upstream treats an unrecognized/blocked results page as an empty search. Fail closed.
original_results=FacebookSearchResultPage.get_listings
def checked_results(self):
    result=original_results(self)
    label=FacebookLocationDialog(self.page,self.translator,self.logger).current_label()
    OBSERVATIONS.append({'url':self.page.url,'location':label,'parsed_results':len(result),'examples':[{'title':x.title,'price':x.price,'location':x.location,'id':x.id} for x in result[:8]]})
    self.page.screenshot(path=str(STATE/('facebook-search-'+str(len(OBSERVATIONS))+'.png')),full_page=False)
    if not result:
        page=getattr(self,'page',None)
        if page is None:raise RuntimeError('No Facebook results page available')
        body=page.locator('body').inner_text(timeout=10000).lower()
        if not re.search(r'no results|no listings|couldn.t find any|nothing found',body):
            raise RuntimeError('Facebook returned no parsable listings and no explicit empty-results message')
    # Opening every listing and seller profile made busy searches run past their time limit.
    return result[:SEARCH_LISTING_LIMIT]
FacebookSearchResultPage.get_listings=checked_results

def marketplace(browser,login=False):
    cls=FacebookMarketplace if login else BoundedFacebook
    m=cls('facebook',browser,logger=logging.getLogger('price-watch'))
    c=load_config(ROOT)
    # Manual login uses the upstream session mechanism without keeping a password.
    options=c['marketplace']['facebook']
    m.configure(cls.get_config(name='facebook',**options))
    return m,c

def record_health(source,report,args):
    try:
        from source_health import observe_report
        observe_report(ROOT,source,report,args.item,bool(args.notify))
    except Exception:
        logging.error('Source health could not be recorded; inspect the local state files.')

def collect_source(source,args):
    if source=='claude_research':
        from claude_research import run as research
        return research(ROOT,reason,args.item,force=not args.notify)
    if source=='ebay':from ebay_collect import run as collect
    elif source=='offerup':from offerup_collect import run as collect
    elif source=='retail':from retail_collect import run as collect
    elif source=='craigslist':from craigslist_collect import run as collect
    else:from shopping_collect import run as collect
    started=now()
    try:
        report=collect(ROOT,reason,source,args.item,force=not args.notify and args.source=='google_shopping') if source in ('amazon','google_shopping') else collect(ROOT,reason,args.item)
    except Exception:
        report={'status':'error','started_at':started,'finished_at':now(),'collector_failed':True}
    record_health(source,report,args)
    from alert_delivery import collect_alerts
    collect_alerts(ROOT,source,report,notify=bool(args.notify),item_id=args.item)
    return report

def run(args):
    if not args.login and getattr(args,'source','all') in ('ebay','offerup','retail','amazon','google_shopping','craigslist','claude_research'):
        report=collect_source(args.source,args)
        print(json.dumps(report));return 1 if report['status']=='error' else 0
    with (STATE/'run.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another price-watch run is active')
            if not SESSION_FILE.exists():atomic_json(STATE/'status.json',{'status':'waiting_for_login','checked_at':now(),'error':'Dedicated Facebook login is still waiting for user input'})
            return 0
        if not args.login and getattr(args,'source','all')!='facebook':
            from claude_research import launch_if_due
            launch_if_due(ROOT)
            for source in ('ebay','offerup','retail','amazon','google_shopping','craigslist'):collect_source(source,args)
        report={'started_at':now(),'status':'running','sources':{},'notification_delivery':'not exercised','phone_delivery':json.loads((STATE/'phone-status.json').read_text()) if (STATE/'phone-status.json').exists() else {'status':'unverified'}}
        if not args.login: atomic_json(STATE/'status.json',report)
        active_name=None
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=not args.login)
                try:
                    m,c=marketplace(browser,login=args.login)
                    if args.login:
                        print('Complete Facebook login in the dedicated Chromium window on this Mac.',flush=True)
                        m.login()
                        report.update(status='login_verified',finished_at=now())
                        atomic_json(STATE/'login-status.json',report)
                        print('Facebook login verified and session saved.',flush=True)
                        return 0
                    if not SESSION_FILE.exists():raise RuntimeError('Facebook authentication required; no saved watcher session')
                    # Each watch gets its own deadline, so one slow search fails alone instead of
                    # sinking the others; the run as a whole stays inside the 30-minute schedule.
                    signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(DeadlineExceeded('Facebook watch check exceeded its time limit')))
                    run_deadline=time.monotonic()+RUN_BUDGET
                    db=sqlite3.connect(STATE/'seen.sqlite3')
                    db.execute('CREATE TABLE IF NOT EXISTS seen (item TEXT,id TEXT,status TEXT,price TEXT,at TEXT,receipt TEXT,PRIMARY KEY(item,id))')
                    db.execute('CREATE TABLE IF NOT EXISTS initialized (item TEXT PRIMARY KEY,at TEXT)')
                    for name,opts in facebook_order(c['item']):
                        if args.item and args.item!=name:continue
                        if opts.get('enabled') is False:
                            report['sources'][name]={'status':'disabled','sent':[],'reason':'Watch is paused or archived'}
                            continue
                        remaining=int(run_deadline-time.monotonic())
                        if remaining<60:
                            result={'status':'error','checked_at':now(),'error':'Skipped: Facebook run time budget was used up by earlier watches','accepted':[],'rejected':[],'sent':[]}
                            report['sources'][name]=result;save_item_result(name,result);continue
                        signal.alarm(min(ITEM_BUDGET,remaining))
                        try:
                          active_name=name
                          definition=c['_definitions'][name]
                          LABELS[name]=definition['name']
                          notify=notifications_enabled(c,name,args)
                          baseline_key=name if definition['revision']==0 else name+':'+fingerprint(definition)
                          item=m.get_item_config(name=name,marketplace='facebook',**opts)
                          m.pickup_radius=definition['radius']
                          results=[];accepted=[];rejected=[];sent=[];duplicates=0; baselined=0
                          first_scan=db.execute('SELECT 1 FROM initialized WHERE item=?',(baseline_key,)).fetchone() is None
                          for listing in facebook_results(m,item,definition):
                              results.append(asdict(listing))
                              why=reason(name,listing,definition)
                              if why:rejected.append({'id':listing.id,'title':listing.title,'price':listing.price,'location':listing.location,'post_url':listing.post_url,'reason':why});continue
                              accepted.append(asdict(listing))
                          # Deliver after the bounded search completes successfully. Each channel
                          # and source maintains its own baseline and receipt ledger.
                          from alert_delivery import process
                          deliveries=process(ROOT,definition,'facebook',accepted,notify=bool(args.notify))
                          baselined=deliveries['baseline']
                          sent=[{'channel_delivery':True} for _ in range(deliveries['sent'])]
                          report['source_observations']=OBSERVATIONS
                          report['sources'][name]={'status':'ok','fingerprint':fingerprint(definition),'baselined':baselined,'checked_at':now(),'candidate_count':len(results),'accepted':accepted,'rejected':rejected,'sent':sent,'duplicates_suppressed':duplicates,'alerts_enabled':c.get('watch',{}).get(name,{}).get('notify',True)}
                          if definition['profile'] in ('thunderbolt4','thunderbolt5'):
                              report['sources'][name].update(limited=len(results)>=5,detail='Up to five Facebook listing details per hub watch per check, keeping the shared monitoring cycle bounded. Other marketplaces have separate coverage.')
                          save_item_result(name,report['sources'][name])
                          active_name=None
                        except (DeadlineExceeded,Exception) as e:
                            # A lost login fails every watch, so it still ends the run.
                            if 'authentication required' in str(e):raise
                            # The page may be mid-navigation; continue on a fresh marketplace page.
                            result={'status':'error','checked_at':now(),'error':str(e).split('\n')[0],'accepted':[],'rejected':[],'sent':[]}
                            report['sources'][name]=result;save_item_result(name,result);active_name=None
                            logging.warning('Facebook watch %s failed (%s); continuing with the next watch',name,str(e).split('\n')[0])
                            try:m.page.close()
                            except Exception:pass
                            m,_=marketplace(browser)
                        finally:signal.alarm(0)
                        atomic_json(STATE/'status.json',report)
                    failed=[n for n,r in report['sources'].items() if r.get('status')=='error']
                    report.update(status='partial' if failed else 'ok',finished_at=now(),notification_delivery='provider accepted' if any(s['sent'] for s in report['sources'].values()) else 'no notifications sent')
                    if failed:report['error']='Facebook check failed for: '+', '.join(failed)
                    atomic_json(STATE/'status.json',report)
                    save_history(report)
                    record_health('facebook',report,args)
                    print(json.dumps({'status':report['status'],'checked':list(report['sources']),'finished_at':report['finished_at']}))
                    if failed:print('Price watch error: '+report['error'],file=sys.stderr)
                    return 1 if failed else 0
                finally:browser.close()
        except (Exception,DeadlineExceeded) as e:
            report.update(status='error',finished_at=now(),error=str(e),source_observations=OBSERVATIONS);atomic_json(STATE/'status.json',report)
            if active_name:save_item_result(active_name,{'status':'error','checked_at':now(),'error':'Source check failed; inspect private watcher logs','accepted':[],'rejected':[],'sent':[]})
            save_history(report)
            if not args.login:
                record_health('facebook',report,args)
            print('Price watch error: '+str(e),file=sys.stderr);return 1

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--login',action='store_true');ap.add_argument('--notify',action='store_true');ap.add_argument('--item');ap.add_argument('--source',choices=('all','facebook','ebay','offerup','retail','amazon','google_shopping','craigslist','claude_research'),default='all');args=ap.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    sys.exit(run(args))
