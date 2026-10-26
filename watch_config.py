"""Validated watch definitions shared by the collector and dashboard."""
import copy, fcntl, hashlib, json, math, os, re, tempfile, tomllib, uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BASE_LABELS={'studio':'Mac Studio','macbook':'MacBook Pro 16-inch M5 Pro, 64GB, 1TB','ams':'Bambu Lab AMS 2 Pro'}
def local_settings(root=ROOT):
    """Machine-specific settings from config.toml, which stays out of the repository."""
    try:return tomllib.loads((root/'config.toml').read_text())
    except (OSError,tomllib.TOMLDecodeError):return {}

# Every pickup source searches around this center; shipping sources ignore it.
# Override it with a [search_area] table in config.toml (see config.example.toml).
SEARCH_AREA={'name':'Los Angeles','state':'CA','zip':'90012','latitude':34.0522,'longitude':-118.2437,**local_settings().get('search_area',{})}
DEFAULT_RADIUS=5
STORES=['universe','expercom','printed']
PROFILES={'studio':'Mac Studio configurations','macbook':'16-inch M5 Pro / 64GB / 1TB','ams':'Complete AMS 2 Pro','thunderbolt4':'Thunderbolt 4 hubs / docks','thunderbolt5':'Thunderbolt 5 hubs / docks','custom':'Custom keywords'}

def now():return datetime.now(timezone.utc).isoformat()
def read(path,default):
    try:return json.loads(path.read_text())
    except FileNotFoundError:return copy.deepcopy(default)

def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')
        os.chmod(name,0o600);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

def base_watches(root=ROOT):
    c=tomllib.loads((root/'config.toml').read_text()); watches={}
    for key,item in c['item'].items():
        meta=c.get('watch',{}).get(key,{})
        watches[key]={'id':key,'name':BASE_LABELS.get(key,key),'profile':key if key in PROFILES else 'custom',
          'queries':item['search_phrases'],'min_price':item.get('min_price',0),'max_price':item.get('max_price'),
          'radius':item.get('radius',c.get('marketplace',{}).get('facebook',{}).get('radius',DEFAULT_RADIUS)),
          'condition':'used' if key in ('studio','macbook') else 'any','required':[],'excluded':[],
          'enabled':item.get('enabled',True),'archived':False,'alerts':meta.get('notify',True) and item.get('enabled',True),
          'notes':'Existing DealHunter watch; verify the pickup area before resuming.' if key=='ams' else '',
          'sources':meta.get('sources',['facebook']),'revision':0,'updated_at':None}
    return c,watches

def definitions(root=ROOT):
    c,watches=base_watches(root)
    registry=read(root/'watches.json',{'version':0,'watches':{}})
    watches.update(registry['watches'])
    for watch in watches.values():watch.setdefault('sources',['facebook'])
    return c,watches,registry

def fingerprint(watch):
    keys=('name','profile','queries','min_price','max_price','radius','condition','required','excluded')
    criteria={k:watch.get(k) for k in keys}
    if watch.get('condition')=='any':
        ranges=price_ranges(watch)
        if ranges['new']!=ranges['used']:criteria['price_ranges']=ranges
    return hashlib.sha256(json.dumps(criteria,sort_keys=True).encode()).hexdigest()[:16]

def price_ranges(watch):
    fallback={'min':watch['min_price'],'max':watch['max_price']}
    return {c:dict(watch.get('price_ranges',{}).get(c,fallback)) for c in ('new','used')}

def search_bounds(watch):
    ranges=price_ranges(watch)
    selected=list(ranges.values()) if watch.get('condition')=='any' else [ranges.get(watch.get('condition'),ranges['used'])]
    return min(r['min'] for r in selected),max(r['max'] for r in selected)

def condition_bucket(listing):
    condition=str(getattr(listing,'condition','')).lower()
    title=str(getattr(listing,'title','')).lower()
    # An explicit used/refurbished label wins over promotional wording in a title.
    used=r'\b(?:used|refurbished|renewed|pre[ -]?owned|open[ -]?box|like new|excellent|good|fair|salvage)\b'
    if re.search(used,condition):return 'used'
    if re.search(r'\bnew\b',condition) and not re.search(r'\b(?:not|without)\b',condition):return 'new'
    if re.search(used,title):return 'used'
    if re.search(r'\b(?:brand new|sealed|unopened)\b',title) and not re.search(r'\b(?:not|no longer|unsealed)\b',title):return 'new'
    return 'unknown'

def price_rule(watch,listing,price):
    ranges=price_ranges(watch);bucket=condition_bucket(listing)
    if watch.get('condition')=='any' and ranges['new']!=ranges['used'] and bucket=='unknown':
        return 'condition needs review for separate new and used budgets'
    target=ranges.get(bucket,{'min':watch['min_price'],'max':watch['max_price']})
    if price>target['max']:return 'over maximum'+(' for '+bucket+' condition' if ranges['new']!=ranges['used'] else '')
    if price<target['min']:return 'below target range'+(' for '+bucket+' condition' if ranges['new']!=ranges['used'] else '')
    return None

def load_config(root=ROOT):
    c,watches,registry=definitions(root)
    for key,w in watches.items():
        options=dict(c['item'].get(key,{}))
        if w['revision']>0 or w['profile']=='custom':
            for field in ('title_keywords','keywords','antikeywords'):options.pop(field,None)
        preset_titles={'studio':'Mac AND Studio','macbook':'(MacBook OR "Mac Book") AND Pro','ams':'AMS AND Pro'}
        preset_titles.update(thunderbolt4='Thunderbolt AND (Hub OR Dock)',thunderbolt5='Thunderbolt AND (Hub OR Dock)')
        if w['profile'] in preset_titles:options['title_keywords']=preset_titles[w['profile']]
        if w['profile']=='ams':options['antikeywords']=['bracket','stand','mount','printed','riser','buffer','cable','hub','rack','holder','adapter','lid','cover','tray','desiccant']
        # Facebook's upstream filters accept whole dollars; the local matcher enforces exact cents.
        lower,upper=search_bounds(w)
        options.update(search_city=c['marketplace']['facebook'].get('search_city','la'),city_name=c['marketplace']['facebook'].get('city_name','Los Angeles'),search_phrases=w['queries'],min_price=math.floor(lower),max_price=math.ceil(upper),radius=w['radius'],enabled=w['enabled'] and not w['archived'] and 'facebook' in w['sources'],ai=[])
        # Facebook's own pickup filter drops "Ships to you" listings from anywhere in the country.
        options['delivery_method']=['local_pick_up']
        if key not in c['item']:options['sort_by']='suggested'
        c['item'][key]=options
        c.setdefault('watch',{}).setdefault(key,{})['notify']=w['alerts']
    c['_definitions']=watches
    return c

def validate(value):
    if not isinstance(value,dict):raise ValueError('Expected a watch object.')
    w={}
    for key in ('name','notes'):
        s=value.get(key,'')
        if not isinstance(s,str) or len(s)>(120 if key=='name' else 2000):raise ValueError('Name or notes are too long.')
        w[key]=s.strip()
    if not w['name']:raise ValueError('Give this watch a name.')
    w['profile']=value.get('profile','custom')
    if w['profile'] not in PROFILES:raise ValueError('Choose a supported matching preset.')
    for key in ('queries','required','excluded'):
        a=value.get(key,[])
        if not isinstance(a,list) or len(a)>8 or any(not isinstance(s,str) or not s.strip() or len(s)>160 for s in a):raise ValueError('Use up to eight nonempty search phrases or keywords.')
        w[key]=list(dict.fromkeys(s.strip() for s in a))
    if not w['queries']:raise ValueError('Add at least one search phrase.')
    for key in ('min_price','max_price'):
        v=value.get(key,0)
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 or v>100000:raise ValueError('Enter a valid price between $0 and $100,000.')
        w[key]=round(v,2)
    if w['max_price']<=0 or w['min_price']>w['max_price']:raise ValueError('The maximum price must be positive and at least the minimum.')
    w['radius']=value.get('radius',DEFAULT_RADIUS)
    if w['radius'] not in (1,2,5,10,20,40,60,80,100,250,500):raise ValueError('Choose one of the available search radii.')
    sources=value.get('sources',['facebook'])
    if not isinstance(sources,list) or not sources or any(x not in ('facebook','ebay','offerup','retail','amazon','google_shopping','craigslist') for x in sources):raise ValueError('Choose at least one connected marketplace.')
    w['sources']=list(dict.fromkeys(sources))
    w['condition']=value.get('condition','used')
    if w['condition'] not in ('any','used','new'):raise ValueError('Choose a supported condition.')
    if 'price_ranges' in value and w['condition']=='any':
        ranges=value['price_ranges']
        if not isinstance(ranges,dict) or set(ranges)!={'new','used'}:raise ValueError('Provide both new and used price ranges.')
        w['price_ranges']={}
        for condition,limits in ranges.items():
            if not isinstance(limits,dict) or set(limits)!={'min','max'}:raise ValueError('Each condition needs a minimum and maximum.')
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 or v>100000 for v in limits.values()):raise ValueError('Enter valid condition prices between $0 and $100,000.')
            if limits['max']<=0 or limits['min']>limits['max']:raise ValueError('Each condition maximum must be positive and at least its minimum.')
            w['price_ranges'][condition]={k:round(v,2) for k,v in limits.items()}
        w['min_price'],w['max_price']=search_bounds(w)
    stores=value.get('retail_stores',STORES)
    if not isinstance(stores,list) or not stores or any(x not in ('universe','expercom','printed') for x in stores):raise ValueError('Choose at least one supported retail store.')
    w['retail_stores']=list(dict.fromkeys(stores))
    if 'channels' in value:
        channels=value['channels']
        if not isinstance(channels,dict) or set(channels)!={'email','ntfy'} or any(not isinstance(x,bool) for x in channels.values()):raise ValueError('Choose email and phone alert switches.')
        w['channels']=dict(channels)
    for key in ('enabled','archived','alerts'):
        if not isinstance(value.get(key,False),bool):raise ValueError('Invalid switch value.')
        w[key]=value.get(key,False)
    if 'channels' in w:w['alerts']=any(w['channels'].values())
    if w['archived']:
        w['enabled']=False;w['alerts']=False
        if 'channels' in w:w['channels']={'email':False,'ntfy':False}
    return w

def mutate_watch(payload,root=ROOT):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'settings.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        c,watches,registry=definitions(root)
        if payload.get('version')!=registry['version']:raise ValueError('Settings changed in another window. Refresh and try again.')
        w=validate(payload.get('watch'));key=payload.get('id')
        if key is not None and key not in watches:raise ValueError('This watch no longer exists.')
        previous=watches.get(key)
        old_channels=channel_settings(previous or {})
        requested=w.get('channels',{'email':w['alerts'],'ntfy':w['alerts']})
        # Switching a channel on arms it for every selected source. A channel that was off
        # gets a new epoch, so its first scan records a quiet baseline instead of a backlog.
        w['alert_epochs']={c:(old_channels[c]['approval']['epoch'] if previous and old_channels[c]['enabled'] else uuid.uuid4().hex) for c in ('email','ntfy') if requested[c]}
        w['channels']=requested;w['alerts']=any(requested.values())
        # Remember the search area behind each criteria fingerprint, so listings found
        # with a wider radius or another center drop out of the dashboard.
        w['areas']=dict((previous or {}).get('areas',{}))
        if previous:w['areas'].setdefault(fingerprint(previous),search_area(previous))
        w['areas'][fingerprint(w)]=search_area(w)
        w['areas']=dict(list(w['areas'].items())[-50:])
        key=key or 'watch_'+uuid.uuid4().hex[:12]
        w.update(id=key,revision=(previous or {}).get('revision',0)+1,updated_at=now())
        path=root/'watches.json'
        if path.exists():atomic(root/'watches.previous.json',registry)
        registry['watches'][key]=w;registry['version']+=1
        atomic(path,registry)
        return {'id':key,'version':registry['version']}

def annotate(payload,root=ROOT):
    key=payload.get('key');action=payload.get('action');value=payload.get('value')
    if not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9_]+:[a-zA-Z0-9_-]+',key):raise ValueError('Invalid listing.')
    if action not in ('saved','dismissed','note'):raise ValueError('Invalid listing action.')
    if action=='note':
        if not isinstance(value,str) or len(value)>2000:raise ValueError('Keep listing notes under 2,000 characters.')
    elif not isinstance(value,bool):raise ValueError('Invalid listing action.')
    if ':ebay_' in key:
        from ebay_store import annotate_listing
        return annotate_listing(root,key,action,value)
    with (root/'settings.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=root/'state/annotations.json';d=read(path,{})
        if key not in d and len(d)>=5000:raise ValueError('Saved listing limit reached.')
        d.setdefault(key,{})[action]=value;atomic(path,d)
    return {'ok':True}


def search_area(w):return {'center':SEARCH_AREA['name'],'radius':w['radius']}

def outside_area(watch,row_fingerprint):
    """True when a pickup listing came from a wider radius or another center than the watch now uses."""
    if row_fingerprint==fingerprint(watch):return False
    areas=watch.get('areas',{})
    wider=lambda a:a['center']!=SEARCH_AREA['name'] or a['radius']>watch['radius']
    if row_fingerprint in areas:return wider(areas[row_fingerprint])
    # A listing of unknown origin is suspect only once this watch has searched outside its current area.
    return any(wider(a) for a in areas.values())

def alert_sources(w):
    return [s for s in w.get('sources',['facebook']) if s not in ('retail','amazon','google_shopping') or w.get('condition')!='used']

def channel_settings(w):
    """An enabled channel alerts for every eligible source of the watch."""
    channels=w.get('channels',{'email':bool(w.get('alerts')),'ntfy':bool(w.get('alerts'))})
    out={}
    for channel in ('email','ntfy'):
        epoch=w.get('alert_epochs',{}).get(channel) or w.get('alert_approvals',{}).get(channel,{}).get('epoch') or 'legacy'
        out[channel]={'enabled':bool(channels.get(channel)),'sources':alert_sources(w),'approval':{'epoch':epoch,'fingerprint':fingerprint(w)},'retail_stores':w.get('retail_stores',STORES)}
    return out
