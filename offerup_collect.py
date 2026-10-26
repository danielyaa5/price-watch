"""Read public OfferUp search pages and item location details; no login required.

Only the first search page is collected per phrase. Item coordinates and ZIP codes
are batch-queried from OfferUp's public GraphQL endpoint to place listings accurately.
"""
import fcntl
import json
import math
import re
import subprocess
import time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlencode
from watch_config import search_bounds, atomic, definitions, fingerprint, now, read, SEARCH_AREA
from places import AMBIGUOUS, check, coord_key, key, lookup, miles

AREA = {'city': SEARCH_AREA['name'], 'state': SEARCH_AREA['state'], 'zipCode': SEARCH_AREA['zip'],
        'latitude': SEARCH_AREA['latitude'], 'longitude': SEARCH_AREA['longitude'], 'source': 'zipcode'}
RADII = (5, 10, 20, 30, 50)
CONDITIONS = {'used': 'USED,REFURBISHED', 'new': 'NEW', 'any': ''}


class OfferUpError(Exception):
    pass


class SearchPayload(HTMLParser):
    def __init__(self):
        super().__init__(); self.capture = False; self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script': self.capture = dict(attrs).get('id') == '__NEXT_DATA__'

    def handle_endtag(self, tag):
        if tag == 'script': self.capture = False

    def handle_data(self, data):
        if self.capture: self.parts.append(data)


def effective_radius(requested):
    if requested < 5:
        raise OfferUpError('OfferUp supports searches of at least 5 miles. Increase this watch’s radius to use OfferUp.')
    return max(r for r in RADII if r <= requested)


def search_url(watch, query):
    params = {'q': query, 'radius': effective_radius(watch['radius']), 'sort': '-posted',
              'price_min': search_bounds(watch)[0], 'price_max': search_bounds(watch)[1]}
    if CONDITIONS[watch['condition']]: params['condition'] = CONDITIONS[watch['condition']]
    return 'https://offerup.com/search?' + urlencode(params, quote_via=quote)


def fetch(url):
    # This is only a public city preference, never an account/session cookie.
    cookie = 'ou.location=' + quote(json.dumps(AREA, separators=(',', ':')))
    try:
        result = subprocess.run(['/usr/bin/curl', '--silent', '--show-error', '--max-time', '25',
                                 '--max-filesize', '8000000', '--proto', '=https',
                                 '--cookie', cookie, '--write-out', '\n%{http_code}', url],
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise OfferUpError('OfferUp did not respond in time; previous results are retained.') from None
    if result.returncode:
        raise OfferUpError('OfferUp could not be reached; previous results are retained.')
    body, _, status = result.stdout.rpartition(b'\n')
    if status != b'200':
        raise OfferUpError('OfferUp blocked or rejected the search (HTTP ' + status.decode('ascii', 'replace')[:3] + '). Previous results are retained.')
    if len(body) > 8_000_000:
        raise OfferUpError('OfferUp search response exceeded the size limit.')
    return body.decode('utf-8')


def fetch_details(listing_ids):
    ids = [lid for lid in dict.fromkeys(listing_ids) if re.fullmatch(r'[a-f0-9-]{36}', str(lid))]
    if not ids:
        return {}
    fields = 'listingId locationDetails { latitude longitude locationName zipcode }'
    aliases = '\n'.join(f'l{i}: listing(listingId: "{lid}") {{{fields}}}' for i, lid in enumerate(ids[:45]))
    payload = json.dumps({'operationName': 'BatchLocations', 'variables': {}, 'query': f'query BatchLocations {{{aliases}}}'})
    try:
        result = subprocess.run(['/usr/bin/curl', '--silent', '--show-error', '--max-time', '25',
                                 '--max-filesize', '2000000', '--proto', '=https',
                                 '-H', 'Content-Type: application/json', '--data', payload,
                                 '--write-out', '\n%{http_code}', 'https://offerup.com/api/graphql'],
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise OfferUpError('OfferUp item details did not respond in time; previous results are retained.') from None
    if result.returncode:
        raise OfferUpError('OfferUp item details could not be reached; previous results are retained.')
    body, _, status = result.stdout.rpartition(b'\n')
    if status != b'200':
        raise OfferUpError('OfferUp rejected the item detail lookup (HTTP ' + status.decode('ascii', 'replace')[:3] + ').')
    try:
        data = json.loads(body.decode('utf-8')).get('data') or {}
    except ValueError:
        raise OfferUpError('OfferUp returned unreadable item details.') from None
    out = {}
    for entry in data.values():
        if isinstance(entry, dict) and entry.get('listingId'):
            out[str(entry['listingId'])] = entry.get('locationDetails') or {}
    return out


def resolve_locations(rows, radius, root, details):
    center = (SEARCH_AREA['latitude'], SEARCH_AREA['longitude'])
    center_name = SEARCH_AREA['name']
    kept = []
    ambiguous_coords = []
    for row in rows:
        item_id = str(row.get('id', '')).removeprefix('offerup_')
        loc = details.get(item_id) or {}
        try:
            lat, lon = float(loc['latitude']), float(loc['longitude'])
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            kept.append(row)
            continue
        dist = round(miles(center, (lat, lon)), 1)
        if dist > radius:
            continue
        loc_name = str(loc.get('locationName') or row.get('location') or '').strip()
        zipcode = str(loc.get('zipcode') or '').strip()
        ck = coord_key(lat, lon)
        if not key(loc_name) or key(loc_name).lower() in AMBIGUOUS:
            ambiguous_coords.append(ck)
        row = {**row, '_coord_key': ck, '_dist': dist, '_loc_name': loc_name, '_zip': zipcode if re.fullmatch(r'\d{5}', zipcode) else ''}
        kept.append(row)
    resolved = lookup(ambiguous_coords, root) if ambiguous_coords else {}
    out = []
    for row in kept:
        if '_dist' in row:
            ck = row.pop('_coord_key'); dist = row.pop('_dist'); loc_name = row.pop('_loc_name'); zipcode = row.pop('_zip')
            place = resolved.get(ck) if (not key(loc_name) or key(loc_name).lower() in AMBIGUOUS) else loc_name
            place = place or loc_name or 'Local pickup'
            suffix = (' ' + zipcode) if zipcode and zipcode not in place else ''
            row['location'] = f'{place}{suffix} · about {dist:g} mi from {center_name}'[:200]
            row['evidence'] = f'Search title, condition filter and item map coordinates checked (about {dist:g} mi from {center_name}). Open the listing to check its description and availability.'
        out.append(row)
    return out


def parse_search(html, watch, query):
    parser = SearchPayload(); parser.feed(html)
    try:
        page = json.loads(''.join(parser.parts))['props']['pageProps']
        location = page['appProps']['location']; params = page['searchFeedParams']
        feed = page['searchFeedResponse']; tiles = feed['looseTiles']
        filters = {x['targetName']: x for x in feed['filters']}
    except (ValueError, KeyError, TypeError):
        raise OfferUpError('OfferUp returned an unrecognized search page, not a verified empty result.') from None
    radius = effective_radius(watch['radius'])
    if location.get('city') != AREA['city'] or location.get('state') != AREA['state'] or location.get('zipCode') != AREA['zipCode']:
        raise OfferUpError(f"OfferUp did not apply the {AREA['city']} search location.")
    expected = {'q': query, 'radius': radius, 'sort': '-posted',
                'price_min': search_bounds(watch)[0], 'price_max': search_bounds(watch)[1]}
    if any(str(params.get(k)) != str(v) for k, v in expected.items()):
        raise OfferUpError('OfferUp did not apply the requested search filters.')
    def selected(name):
        return {str(x['value']) for x in filters.get(name, {}).get('options', []) if x.get('isSelected')}
    expected_conditions = set(CONDITIONS[watch['condition']].split(',')) - {''}
    if selected('DISTANCE') != {str(radius)} or selected('SORT') != {'-posted'} or selected('CONDITION') != expected_conditions:
        raise OfferUpError('OfferUp did not confirm the requested distance, condition or ordering.')
    if not isinstance(tiles, list): raise OfferUpError('OfferUp search listings could not be read.')
    found = {}
    for tile in tiles:
        if tile.get('tileType') != 'LISTING': continue
        item = tile.get('listing', {}); item_id = str(item.get('listingId', ''))
        if not re.fullmatch(r'[a-f0-9-]{36}', item_id):
            raise OfferUpError('OfferUp returned an invalid listing identifier.')
        try:
            amount = Decimal(str(item['price']))
            if not amount.is_finite() or amount < 0: raise InvalidOperation
        except (KeyError, InvalidOperation):
            raise OfferUpError('OfferUp returned a listing with an unreadable price.') from None
        if not item.get('title'): raise OfferUpError('OfferUp returned a listing without a title.')
        condition = item.get('conditionText') or {'used': 'Used / refurbished (search filter)', 'new': 'new', 'any': ''}[watch['condition']]
        found[item_id] = {'id': 'offerup_' + item_id, 'source': 'offerup', 'title': str(item['title'])[:600],
                          'description': '', 'price': f'${amount:,.2f}', 'price_value': float(amount),
                          'location': str(item.get('locationName') or '')[:200], 'condition': condition,
                          'post_url': 'https://offerup.com/item/detail/' + item_id,
                          'evidence': 'Search title and condition filter only. Open the listing to check its description, availability and exact pickup distance.'}
    return list(found.values()), bool(page.get('paginatedOptions', {}).get('nextPageCursor'))


def snapshot(root):
    data = read(Path(root) / 'state/offerup/results.json', {'catalog': {}, 'checks': {}, 'scan': {}})
    return list(data['catalog'].values()), data['checks'], data['scan']


def save_check(root, watch, rows, report, dropped=()):
    path = Path(root) / 'state/offerup/results.json'
    data = read(path, {'catalog': {}, 'checks': {}, 'scan': {}})
    for item_id in dropped:
        data['catalog'].pop(watch['id'] + ':' + item_id, None)
    for row in rows:
        key = watch['id'] + ':' + row['id']; old = data['catalog'].get(key, {})
        history = old.get('price_history', [])
        if old.get('price') != row['price']: history = (history + [{'at': report['checked_at'], 'price': row['price']}])[-20:]
        data['catalog'][key] = {**row, 'key': key, 'watch_id': watch['id'], 'fingerprint': fingerprint(watch),
                                'first_seen': old.get('first_seen', report['checked_at']), 'last_seen': report['checked_at'], 'price_history': history}
    data['catalog'] = dict(sorted(data['catalog'].items(), key=lambda x: x[1]['last_seen'], reverse=True)[:3000])
    data['checks'][watch['id']] = report; atomic(path, data)


def save_scan(root, scan):
    path = Path(root) / 'state/offerup/results.json'
    data = read(path, {'catalog': {}, 'checks': {}, 'scan': {}}); data['scan'] = scan; atomic(path, data)


def run(root, matcher, item_id=None, fetcher=fetch, detail_fetcher=None):
    if detail_fetcher is None:
        detail_fetcher = fetch_details if fetcher is fetch else (lambda _: {})
    root = Path(root); directory = root / 'state/offerup'; directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'scan.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {'status': 'running'}
        started = now(); deadline = time.monotonic() + 180; calls = errors = total = 0; blocked = None
        save_scan(root, {'status': 'running', 'started_at': started})
        for watch in definitions(root)[1].values():
            if item_id and watch['id'] != item_id: continue
            if not watch['enabled'] or watch['archived'] or 'offerup' not in watch['sources']: continue
            report = {'status': 'ok', 'checked_at': now(), 'fingerprint': fingerprint(watch), 'candidate_count': 0, 'matches': 0, 'limited': False}
            try:
                if blocked: raise OfferUpError(blocked)
                radius = effective_radius(watch['radius']); found = {}
                for query in watch['queries']:
                    if calls >= 24 or time.monotonic() > deadline: raise OfferUpError('OfferUp scan limit reached. Narrow the phrases or reduce active watches.')
                    if calls: time.sleep(1)
                    calls += 1
                    rows, limited = parse_search(fetcher(search_url(watch, query)), watch, query)
                    report['limited'] |= limited
                    found.update((r['id'], r) for r in rows)
                details = {}
                ids = [r['id'].removeprefix('offerup_') for r in found.values()]
                for start in range(0, len(ids), 45):
                    batch = ids[start:start + 45]
                    if detail_fetcher is fetch_details:
                        if calls >= 24 or time.monotonic() > deadline: raise OfferUpError('OfferUp scan limit reached. Narrow the phrases or reduce active watches.')
                        if calls: time.sleep(0.5)
                        calls += 1
                    details.update(detail_fetcher(batch))
                # OfferUp pads a small radius with listings from across the region; verify exact item coordinates and nearby towns.
                rows = resolve_locations(list(found.values()), watch['radius'], root, details)
                area = check([r['location'] for r in rows], watch['radius'], root)
                rows = [r for r in rows if area[r['location']] != 'outside']
                kept_ids = {r['id'] for r in rows}
                dropped = [lid for lid in found if lid not in kept_ids]
                for row in rows:
                    why = matcher(watch['id'], SimpleNamespace(**row), watch)
                    if not why and area[row['location']] == 'review': why = 'pickup distance needs review'
                    row.update(reason=why or '', category='candidate' if why else 'match')
                latest = definitions(root)[1].get(watch['id'], {})
                if not latest.get('enabled') or latest.get('archived') or 'offerup' not in latest.get('sources', []) or fingerprint(latest) != fingerprint(watch): continue
                report.update(checked_at=now(), candidate_count=len(rows), matches=sum(r['category'] == 'match' for r in rows), radius=radius,
                              detail=f'{radius}-mile OfferUp search from {AREA["city"]}, filtered to {watch["radius"]} mi using item map coordinates. First page only; descriptions and availability need review.')
                if radius != watch['radius']: report['detail'] += f' OfferUp’s supported radius is narrower than your {watch["radius"]}-mile target.'
                save_check(root, watch, rows, report, dropped); total += len(rows)
            except OfferUpError as error:
                errors += 1; detail = str(error)
                if 'HTTP 403' in detail or 'HTTP 429' in detail: blocked = detail
                report.update(status='error', detail=detail, checked_at=now()); save_check(root, watch, [], report)
            except Exception:
                errors += 1; report.update(status='error', detail='OfferUp response could not be processed. Previous results are retained.', checked_at=now()); save_check(root, watch, [], report)
        final = {'status': 'error' if errors else 'ok', 'started_at': started, 'finished_at': now(), 'requests': calls, 'listings': total, 'failed_watches': errors}
        save_scan(root, final); return final
