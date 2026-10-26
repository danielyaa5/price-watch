"""Per-channel, per-source buying alerts. No retries after an uncertain send."""
import hashlib, json, sqlite3, subprocess, urllib.request
from contextlib import contextmanager, closing
from pathlib import Path
from watch_config import definitions, fingerprint, channel_settings, now

CHANNELS=('email','ntfy')
SCHEMA='''CREATE TABLE IF NOT EXISTS alert_delivery (
 watch TEXT, source TEXT, channel TEXT, item TEXT, status TEXT, at TEXT, receipt TEXT, owner TEXT,
 PRIMARY KEY(watch,source,channel,item));
 CREATE TABLE IF NOT EXISTS alert_baseline (watch TEXT, source TEXT, channel TEXT, epoch TEXT, at TEXT,
 PRIMARY KEY(watch,source,channel,epoch));'''

@contextmanager
def connect(root,source):
    if source=='ebay':
        from ebay_store import connect as ebay_connect
        with ebay_connect(root) as db:
            db.executescript(SCHEMA);yield db
    else:
        path=Path(root)/'state/alerts/ledger.sqlite3';path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(path,timeout=15)) as db:
            path.chmod(0o600);db.executescript(SCHEMA)
            with db:yield db

def message(watch,row):
    price=row['price']
    if row.get('source')=='ebay' and row.get('total') is not None:price=f"${row['total']:,.2f} including shipping"
    if row.get('source') in ('retail','amazon','google_shopping'):price+=' before shipping and tax'
    return {'subject':f"Price watch: {watch['name']} at {price}",
            'title':f"Price watch: {watch['name']}",
            'body':f"{row['title']}\n{price} · {row.get('location','')}\n{row['post_url']}"}

def send(root,channel,value):
    if channel=='email':
        runtime=Path.home()/'.local/share/workspace-mcp/venv/bin/python'
        result=subprocess.run([str(runtime),str(Path(root)/'mail_alert.py')],input=json.dumps({'subject':value['subject'],'body':value['body']}),text=True,capture_output=True,timeout=45)
        if result.returncode:raise RuntimeError('Email delivery could not be confirmed.')
        receipt=json.loads(result.stdout).get('message_id')
    else:
        dest=json.loads((Path(root)/'notification-private.json').read_text())
        request=urllib.request.Request(dest['server'].rstrip('/'),data=json.dumps({'topic':dest['topic'],'priority':4,'title':value['title'],'message':value['body']}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=15) as response:result=json.load(response)
        receipt=result.get('id') if result.get('event')=='message' else None
    if not receipt:raise RuntimeError('Provider supplied no receipt.')
    return str(receipt)

def eligible(root,watch,source,channel):
    latest=definitions(root)[1].get(watch['id'],{})
    if not latest.get('enabled') or latest.get('archived') or fingerprint(latest)!=fingerprint(watch):return None
    setting=channel_settings(latest)[channel]
    return setting if setting['enabled'] and source in setting['sources'] else None

def process(root,watch,source,rows,notify=False,sender=None,bucket=None):
    if not notify:return {'sent':0,'baseline':0,'uncertain':0}
    bucket=bucket or source;counts={'sent':0,'baseline':0,'uncertain':0}
    matches=[r for r in rows if r.get('category','match')=='match']
    for channel in CHANNELS:
        settings=eligible(root,watch,source,channel)
        if not settings or (source=='retail' and bucket.split(':',1)[-1] not in settings.get('retail_stores',[])):continue
        approval=settings['approval'];epoch=approval['epoch']+':'+fingerprint(watch)
        with connect(root,source) as db:
            first=db.execute('SELECT 1 FROM alert_baseline WHERE watch=? AND source=? AND channel=? AND epoch=?',(watch['id'],bucket,channel,epoch)).fetchone() is None
            for row in matches:
                current=eligible(root,watch,source,channel)
                if not current or current['approval']!=approval:break
                if source=='retail' and row.get('store_id') not in definitions(root)[1][watch['id']].get('retail_stores',[]):continue
                key=watch['id']+':'+str(row['id']);owner=None
                if source=='ebay':
                    saved=db.execute('SELECT owner FROM listings WHERE key=?',(key,)).fetchone()
                    if not saved:continue
                    owner=saved[0]
                # Respect the legacy Facebook ledger across this migration.
                legacy=Path(root)/'state/seen.sqlite3'
                seen=False
                if source=='facebook' and legacy.exists():
                    with closing(sqlite3.connect('file:'+str(legacy)+'?mode=ro',uri=True)) as old:
                        try:seen=old.execute('SELECT 1 FROM seen WHERE item=? AND id=?',(watch['id'],row['id'])).fetchone() is not None
                        except sqlite3.OperationalError:pass
                state='baseline' if first or seen else 'pending'
                inserted=db.execute('INSERT OR IGNORE INTO alert_delivery VALUES(?,?,?,?,?,?,?,?)',(watch['id'],bucket,channel,key,state,now(),None,owner)).rowcount
                db.commit()
                if not inserted:continue
                if state=='baseline':counts['baseline']+=1;continue
                try:
                    receipt=(sender or (lambda c,m:send(root,c,m)))(channel,message(watch,row))
                    if not receipt:raise RuntimeError('No delivery receipt.')
                    db.execute('UPDATE alert_delivery SET status=?,receipt=?,at=? WHERE watch=? AND source=? AND channel=? AND item=?',('sent',str(receipt),now(),watch['id'],bucket,channel,key));db.commit();counts['sent']+=1
                except Exception:counts['uncertain']+=1
            else:
                db.execute('INSERT OR IGNORE INTO alert_baseline VALUES(?,?,?,?,?)',(watch['id'],bucket,channel,epoch,now()));db.commit()
    return counts

def collect_alerts(root,source,report,notify=False,item_id=None):
    if not notify or report.get('status')=='running':return
    if source=='ebay':from ebay_store import snapshot
    elif source=='offerup':from offerup_collect import snapshot
    elif source=='retail':from retail_collect import snapshot
    elif source=='craigslist':from craigslist_collect import snapshot
    elif source in ('amazon','google_shopping'):
        from shopping_collect import snapshot as shopping_snapshot
        snapshot=lambda root:shopping_snapshot(root,source)
    rows,checks,_=snapshot(root)
    for watch in definitions(root)[1].values():
        if item_id and item_id!=watch['id']:continue
        check=checks.get(watch['id'],{})
        if check.get('status') not in ('ok','partial') or check.get('fingerprint')!=fingerprint(watch) or check.get('checked_at','')<report.get('started_at',''):continue
        latest=[r for r in rows if r.get('watch_id')==watch['id'] and r.get('last_seen')==check.get('checked_at')]
        if source=='retail':
            for store in check.get('stores',[]):
                if store['status']=='ok':process(root,watch,source,[r for r in latest if r.get('store_id')==store['id']],True,bucket='retail:'+store['id'])
        else:process(root,watch,source,latest,True)

def status(root):
    result={}
    for path in (Path(root)/'state/alerts/ledger.sqlite3',Path(root)/'state/ebay/results.sqlite3'):
        if not path.exists():continue
        with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=5)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='alert_delivery'").fetchone():continue
            for watch,source,channel,state,count,at in db.execute('SELECT watch,source,channel,status,count(*),max(at) FROM alert_delivery GROUP BY watch,source,channel,status'):
                key=watch+':'+channel;value=result.setdefault(key,{'sent':0,'baseline':0,'uncertain':0,'last_sent':None})
                if state in ('sent','baseline'):value[state]+=count
                else:value['uncertain']+=count
                if state=='sent':value['last_sent']=max(filter(None,[value['last_sent'],at]))
    return result
