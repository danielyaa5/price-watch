"""Bounded eBay result store. Raw seller identifiers and notification bodies are not kept."""
import hashlib, hmac, json, sqlite3
from pathlib import Path
from contextlib import contextmanager, closing
from watch_config import now, fingerprint

def dbpath(root):return Path(root)/'state/ebay/results.sqlite3'
@contextmanager
def connect(root):
    p=dbpath(root);p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    db=sqlite3.connect(p,timeout=15);p.chmod(0o600)
    db.execute('PRAGMA secure_delete=ON');db.execute('PRAGMA journal_mode=DELETE')
    db.executescript('''CREATE TABLE IF NOT EXISTS listings (key TEXT PRIMARY KEY, watch TEXT, owner TEXT, data TEXT);
    CREATE TABLE IF NOT EXISTS checks (watch TEXT PRIMARY KEY, data TEXT);
    CREATE TABLE IF NOT EXISTS blocked (owner TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, at TEXT, removed INTEGER);
    CREATE TABLE IF NOT EXISTS health (key TEXT PRIMARY KEY, data TEXT);
    CREATE TABLE IF NOT EXISTS annotations (key TEXT PRIMARY KEY, data TEXT);''')
    try:
        with db:yield db
    finally:db.close()

def owner_hash(root,identity):
    secret=json.loads((Path(root)/'ebay-private.json').read_text())['verification_token']
    return hmac.new(secret.encode(),str(identity).strip().casefold().encode(),hashlib.sha256).hexdigest()

def save_check(root,watch,rows,report):
    with connect(root) as db:
        for row in rows:
            owner=row.pop('_owner',None)
            if not owner:continue
            if db.execute('SELECT 1 FROM blocked WHERE owner=?',(owner,)).fetchone():continue
            key=watch['id']+':'+row['id'];old=db.execute('SELECT data FROM listings WHERE key=?',(key,)).fetchone();old=json.loads(old[0]) if old else {}
            history=old.get('price_history',[])
            if row['price']!=old.get('price'):history=(history+[{'at':report['checked_at'],'price':row['price']}])[-20:]
            value={**row,'key':key,'watch_id':watch['id'],'first_seen':old.get('first_seen',report['checked_at']),'last_seen':report['checked_at'],'fingerprint':fingerprint(watch),'price_history':history}
            db.execute('INSERT OR REPLACE INTO listings VALUES(?,?,?,?)',(key,watch['id'],owner,json.dumps(value)))
        db.execute('INSERT OR REPLACE INTO checks VALUES(?,?)',(watch['id'],json.dumps(report)))
        db.execute("DELETE FROM listings WHERE key NOT IN (SELECT key FROM listings ORDER BY json_extract(data,'$.last_seen') DESC LIMIT 3000)")

def snapshot(root):
    if not dbpath(root).exists():return [],{},{}
    with closing(sqlite3.connect('file:'+str(dbpath(root))+'?mode=ro',uri=True,timeout=5)) as db:
        rows=[json.loads(r[0]) for r in db.execute('SELECT data FROM listings')]
        annotations={k:json.loads(v) for k,v in db.execute('SELECT key,data FROM annotations')}
        for row in rows:row.update(annotations.get(row['key'],{}))
        checks={k:json.loads(v) for k,v in db.execute('SELECT watch,data FROM checks')}
        health={k:json.loads(v) for k,v in db.execute('SELECT key,data FROM health')}
    return rows,checks,health

def health(root,key,value):
    with connect(root) as db:db.execute('INSERT OR REPLACE INTO health VALUES(?,?)',(key,json.dumps(value)))

def delete_account(root,notification):
    data=notification['data'];identities=[data.get(k) for k in ('username','userId','eiasToken') if data.get(k)]
    if not identities:raise ValueError('Missing account identity')
    receipt=hashlib.sha256(str(notification['notificationId']).encode()).hexdigest()
    owners=[owner_hash(root,x) for x in identities]
    with connect(root) as db:
        if db.execute('SELECT 1 FROM receipts WHERE id=?',(receipt,)).fetchone():return 0
        # Browse supplies seller.username. A notification without that identifier cannot
        # be mapped safely, so clear this source's cache instead of retaining unknown data.
        if data.get('username'):
            marks=','.join('?' for _ in owners)
            keys=[r[0] for r in db.execute(f'SELECT key FROM listings WHERE owner IN ({marks})',owners)]
        else:keys=[r[0] for r in db.execute('SELECT key FROM listings')]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='alert_delivery'").fetchone():
            if data.get('username'):
                db.execute(f'DELETE FROM alert_delivery WHERE owner IN ({marks})',owners)
            else:db.execute('DELETE FROM alert_delivery')
        for key in keys:
            db.execute('DELETE FROM listings WHERE key=?',(key,))
            db.execute('DELETE FROM annotations WHERE key=?',(key,))
        db.executemany('INSERT OR IGNORE INTO blocked VALUES(?)',[(o,) for o in owners])
        db.execute('INSERT INTO receipts VALUES(?,?,?)',(receipt,now(),len(keys)))
        db.execute('DELETE FROM receipts WHERE id NOT IN (SELECT id FROM receipts ORDER BY at DESC LIMIT 1000)')
        db.execute('INSERT OR REPLACE INTO health VALUES(?,?)',('notification',json.dumps({'status':'ok','at':now(),'removed':len(keys)})))
    return len(keys)


def annotate_listing(root,key,action,value):
    with connect(root) as db:
        if not db.execute('SELECT 1 FROM listings WHERE key=?',(key,)).fetchone():raise ValueError('This listing is no longer stored.')
        previous=db.execute('SELECT data FROM annotations WHERE key=?',(key,)).fetchone()
        item=json.loads(previous[0]) if previous else {};item[action]=value
        db.execute('INSERT OR REPLACE INTO annotations VALUES(?,?)',(key,json.dumps(item)))
    return {'ok':True}
