"""Pickup distance from a listing's town name, for sources that give no coordinates."""
import fcntl, json, math, re, subprocess
from pathlib import Path
from watch_config import SEARCH_AREA, atomic, read

ROOT=Path(__file__).resolve().parent
CACHE=ROOT/'state/places.json'
GEOCODER=ROOT/'bin/geocode'
SLACK=3  # miles; a town's center is not the seller's door
# The City of Los Angeles runs from the Valley to the harbor, so its name alone cannot place a seller.
AMBIGUOUS={'los angeles, ca'}

def key(location):
    town=str(location or '').split('·')[0].strip()
    return town if re.fullmatch(r"[A-Za-z .'-]+, [A-Z]{2}",town) else None

def miles(a,b):
    p1,p2=map(math.radians,(a[0],b[0]));dl=math.radians(b[1]-a[1])
    h=math.sin((p2-p1)/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 3958.8*2*math.asin(math.sqrt(h))

def lookup(names,root=ROOT,geocoder=None):
    """Coordinates for town names. Geocoded towns are cached, as are names the geocoder says do not exist (null)."""
    cache_path=Path(root)/'state/places.json'
    names=[n for n in dict.fromkeys(names) if n]
    cache=read(cache_path,{})
    missing=[n for n in names if n not in cache]
    if missing:
        try:
            found=(geocoder or geocode)(missing)
        except Exception:
            return {n:cache.get(n) for n in names}  # geocoder unavailable: leave unknown, retry next time
        with (Path(root)/'settings.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            cache=read(cache_path,{})
            for n,point in found.get('found',{}).items():cache[n]=point
            for n in found.get('unknown',[]):cache[n]=None
            atomic(cache_path,cache)
    return {n:cache.get(n) for n in names}

def geocode(names):
    result=subprocess.run([str(GEOCODER),*names],capture_output=True,text=True,timeout=30+2*len(names))
    if result.returncode:raise RuntimeError('geocoder failed')
    return json.loads(result.stdout or '{}')  # {'found': {name: [lat, lon]}, 'unknown': [name]}

def verdict(location,radius,coordinates):
    """None inside the area; 'outside' beyond it or shipping-only; 'review' when the town cannot place the seller."""
    if str(location or '').strip().lower()=='ships to you':return 'outside'
    town=key(location)
    if not town or town.lower() in AMBIGUOUS:return 'review'
    point=coordinates.get(town)
    if not point:return 'review'
    return 'outside' if miles((SEARCH_AREA['latitude'],SEARCH_AREA['longitude']),point)>radius+SLACK else None

def check(locations,radius,root=ROOT,geocoder=None):
    """Verdict for each location string, resolving town names in one batch."""
    coordinates=lookup([key(l) for l in locations if key(l) and key(l).lower() not in AMBIGUOUS],root,geocoder)
    return {l:verdict(l,radius,coordinates) for l in locations}

def cached(root=ROOT):
    """Known town coordinates without geocoding (for the dashboard)."""
    return read(Path(root)/'state/places.json',{})
