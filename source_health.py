"""Source failures and recovery, independent of buying alerts. Delivery is opt-in."""
import fcntl, json, uuid
from contextlib import contextmanager
from pathlib import Path
from watch_config import definitions, atomic, now, fingerprint, local_settings

NAMES={'facebook':'Facebook','ebay':'eBay','offerup':'OfferUp','craigslist':'Craigslist','retail':'Retail stores','amazon':'Amazon','google_shopping':'Google Shopping'}
URL=local_settings().get('dashboard',{}).get('sources_url','http://localhost:8088/price-watch/#sources')
VERSION=1

def read(path, default):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return default

@contextmanager
def locked(root):
    folder=Path(root)/'state/source-health';folder.mkdir(parents=True,exist_ok=True)
    with (folder/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield folder

def config(root):
    return read(Path(root)/'state/source-health/settings.json',{'channels':{'email':False,'ntfy':False},'approved_version':None})

def configure(payload,root):
    channels=payload.get('channels')
    if not isinstance(channels,dict) or set(channels)!={'email','ntfy'} or any(type(v) is not bool for v in channels.values()):raise ValueError('Choose email and ntfy switches.')
    with locked(root) as folder:
        old=config(root)
        if any(channels.values()) and old.get('approved_version')!=VERSION and payload.get('approve') is not True:raise ValueError('Approve the exact source alert template and destinations first.')
        value={'channels':channels,'approved_version':VERSION if payload.get('approve') is True else old.get('approved_version'),'updated_at':now()}
        atomic(folder/'settings.json',value)
    return {'ok':True,'message':'Source alert preferences saved. Manual checks stay quiet.'}

def active(root,source):
    return {k:w for k,w in definitions(root)[1].items() if w['enabled'] and not w['archived'] and source in w.get('sources',['facebook']) and not (source in ('retail','amazon','google_shopping') and w['condition']=='used')}

def issue(source,text):
    text=str(text).lower()
    if source=='facebook' and any(s in text for s in ('authentication','login','logged out','session')):
        return 'Sign-in required.','Sign in to Facebook in the dedicated browser on the watcher Mac, then run a check.'
    if any(s in text for s in ('exceeded','timeout','timed out','deadline')):
        return 'The search did not finish in time.','Open Sources to review the failed check and search scope.'
    if any(s in text for s in ('quota','budget','limit reached')):
        return 'The search allowance was reached.','Open Sources to review the search allowance and next available check.'
    return 'A search failed or completed only partly.','Open Sources to review the failed check and connection.'

def message(source,event):
    name=NAMES[source];recovery=event['kind']=='recovery'
    title=f"Price Watch: {name} {'recovered' if recovery else 'needs attention'}"
    body=(f"{name} searches are working again.\nAffected watches: {', '.join(event['watches'])}\nConfirmed: {event['at']}\n" if recovery else f"{name} could not complete searches.\nAffected watches: {', '.join(event['watches'])}\nProblem: {event['problem']}\nAction: {event['action']}\nDetected: {event['at']}\n")+URL
    return {'subject':title,'title':title,'body':body}

def observe(root,source,checks,notify=False,sender=None):
    """Only fresh completed checks; untouched failures persist across partial scans."""
    if source not in NAMES:return
    watches=active(root,source)
    with locked(root) as folder:
        path=folder/'state.json';state=read(path,{});s=state.setdefault(source,{'checks':{},'events':[]})
        prior_failed={k for k,v in s['checks'].items() if v['failed']}
        # Re-link the current event after JSON loading so its delivery stays visible.
        if s.get('outage'):
            for index,event in enumerate(s['events']):
                if event['id']==s['outage']['id']:s['events'][index]=s['outage']
        s['checks']={k:v for k,v in s['checks'].items() if k in watches}
        changed=False
        for key,check in checks.items():
            if key not in watches or check.get('status') not in ('ok','error','partial'):continue
            if check.get('fingerprint') and check['fingerprint']!=fingerprint(watches[key]):continue
            stamp=check.get('checked_at') or check.get('finished_at')
            if not stamp or stamp<=s['checks'].get(key,{}).get('at',''):continue
            problem,action=issue(source,check.get('error') or check.get('detail',''))
            s['checks'][key]={'at':stamp,'failed':check['status']!='ok','problem':problem,'action':action};changed=True
        failures={k:v for k,v in s['checks'].items() if v['failed']}
        outage=s.get('outage')
        if failures and not outage:
            sample=next(iter(failures.values()));outage={'id':uuid.uuid4().hex,'kind':'failure','at':max(v['at'] for v in failures.values()),'watches':[watches[k]['name'] for k in failures],'problem':sample['problem'],'action':sample['action'],'delivery':{}}
            s['outage']=outage;s['events'].append(outage)
        elif failures and outage:
            outage['watches']=[watches[k]['name'] for k in failures]
            sample=max(failures.values(),key=lambda value:value['at'])
            outage.update(problem=sample['problem'],action=sample['action'])
        elif outage and prior_failed-set(watches) and not failures:
            s['outage']=None  # Removing or pausing a failed watch is not recovery.
        elif outage and changed and watches:
            # All previously failing active watches have now passed a real check.
            event={'id':uuid.uuid4().hex,'kind':'recovery','at':max(v['at'] for v in s['checks'].values()),'watches':outage['watches'],'delivery':{},'failure_delivery':dict(outage['delivery'])}
            s['events'].append(event);s['outage']=None
        elif not watches:s['outage']=None
        s['events']=s['events'][-40:];atomic(path,state)
        settings=config(root)
        if not notify or settings.get('approved_version')!=VERSION:return
        # Only the current outage or latest recovery; never replay historical events.
        event=s.get('outage') or (s['events'][-1] if s['events'] and s['events'][-1]['kind']=='recovery' else None)
        if not event or not watches:return
        for channel,enabled in settings['channels'].items():
            if not enabled or channel in event['delivery']:continue
            if event['kind']=='recovery' and event.get('failure_delivery',{}).get(channel,{}).get('status')!='sent':continue
            event['delivery'][channel]={'status':'uncertain','at':now()};atomic(path,state)
            try:
                from alert_delivery import send
                receipt=(sender or (lambda c,m:send(root,c,m)))(channel,message(source,event))
                if not receipt:raise RuntimeError('No receipt')
                event['delivery'][channel]={'status':'sent','at':now()}
            except Exception:pass  # Persist uncertainty; never retry and risk duplicate delivery.
            atomic(path,state)

def observe_report(root,source,report,item_id=None,notify=False):
    if report.get('status') in ('running','not_due','disabled'):return
    if source=='facebook' or report.get('collector_failed'):
        checks=dict(report.get('sources',{}))
        if report.get('status')=='error':
            for key in active(root,source):
                if (not item_id or key==item_id) and checks.get(key,{}).get('status')!='ok':checks[key]={'status':'error','checked_at':report.get('finished_at'),'error':report.get('error','')}
    else:
        if source=='ebay':from ebay_store import snapshot
        elif source=='offerup':from offerup_collect import snapshot
        elif source=='retail':from retail_collect import snapshot
        elif source=='craigslist':from craigslist_collect import snapshot
        else:
            from shopping_collect import snapshot as shopping_snapshot
            snapshot=lambda root:shopping_snapshot(root,source)
        _,saved,_=snapshot(root)
        checks={k:v for k,v in saved.items() if (not item_id or k==item_id) and v.get('checked_at','')>=report.get('started_at','~')}
    observe(root,source,checks,notify)

def dashboard(root):
    settings=config(root);state=read(Path(root)/'state/source-health/state.json',{})
    events=[dict(e,source=source,name=NAMES[source]) for source,s in state.items() if source in NAMES for e in s.get('events',[])]
    return {**settings,'events':sorted(events,key=lambda e:e['at'],reverse=True)[:30],'outages':[dict(s['outage'],source=source,name=NAMES[source]) for source,s in state.items() if source in NAMES and s.get('outage')]}
