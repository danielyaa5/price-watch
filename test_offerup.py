import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from offerup_collect import OfferUpError, effective_radius, parse_search, run, snapshot, AREA
from watch_config import definitions, mutate_watch, fingerprint
from watch import reason


class OfferUpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        geocoder = patch('places.geocode', return_value={'found': {'Pasadena, CA': [34.1478, -118.1445], 'Sacramento, CA': [38.5816, -121.4944]}, 'unknown': []})
        geocoder.start(); self.addCleanup(geocoder.stop)
        (self.root/'config.toml').write_text('[item.studio]\nmin_price=3000\nmax_price=4000\nsearch_phrases=["Mac Studio"]\n[watch.studio]\nnotify=false\n')
        w = definitions(self.root)[1]['studio']; w['sources'] = ['offerup']; w['radius'] = 40
        mutate_watch({'version': 0, 'id': 'studio', 'watch': w}, self.root)
        self.watch = definitions(self.root)[1]['studio']
        self.page = {'appProps': {'location': {'city': AREA['city'], 'state': AREA['state'], 'zipCode': AREA['zipCode']}},
                     'searchFeedParams': {'q': 'Mac Studio', 'radius': '30', 'sort': '-posted', 'price_min': '3000', 'price_max': '4000'},
                     'searchFeedResponse': {'looseTiles': [{'tileType': 'LISTING', 'listing': {
                         'listingId': '12345678-1234-1234-1234-123456789abc', 'title': 'Mac Studio M2 Ultra 128GB RAM 2TB SSD',
                         'price': '3900', 'locationName': 'Pasadena, CA', 'seller_private': 'DO_NOT_RETAIN'}}],
                         'filters': [{'targetName': name, 'options': [{'value': value, 'isSelected': True} for value in values]}
                                     for name, values in [('DISTANCE', ['30']), ('SORT', ['-posted']), ('CONDITION', ['USED', 'REFURBISHED'])]]},
                     'paginatedOptions': {'nextPageCursor': 'public-pagination-cursor'}}

    def html(self): return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({'props': {'pageProps': self.page}}) + '</script>'

    def test_live_shape_title_match_and_private_fields_dropped(self):
        rows, limited = parse_search(self.html(), self.watch, 'Mac Studio')
        self.assertTrue(limited); self.assertEqual(len(rows), 1)
        self.assertIsNone(reason('studio', type('Listing', (), rows[0])(), self.watch))
        self.assertNotIn('DO_NOT_RETAIN', json.dumps(rows)); self.assertNotIn('cursor', json.dumps(rows))
        self.assertEqual(rows[0]['price_value'], 3900)

    def test_unrecognized_page_is_error_not_no_results(self):
        for html in ['<title>Just a moment...</title>', '<script id="__NEXT_DATA__">{}</script>']:
            with self.assertRaises(OfferUpError): parse_search(html, self.watch, 'Mac Studio')

    def test_wrong_location_radius_condition_or_query_fails(self):
        original = copy.deepcopy(self.page)
        mutations = [lambda: self.page['appProps']['location'].update(city='Other city'),
                     lambda: self.page['searchFeedParams'].update(q='Accessories'),
                     lambda: self.page['searchFeedResponse']['filters'][0]['options'][0].update(value='50'),
                     lambda: self.page['searchFeedResponse']['filters'][2].update(options=[])]
        for mutation in mutations:
            self.page = copy.deepcopy(original); mutation()
            with self.assertRaises(OfferUpError): parse_search(self.html(), self.watch, 'Mac Studio')

    def test_verified_empty_and_malformed_listing_are_different(self):
        self.page['searchFeedResponse']['looseTiles'] = []
        self.assertEqual(parse_search(self.html(), self.watch, 'Mac Studio')[0], [])
        self.page['searchFeedResponse']['looseTiles'] = [{'tileType': 'LISTING', 'listing': {}}]
        with self.assertRaises(OfferUpError): parse_search(self.html(), self.watch, 'Mac Studio')

    def test_price_change_dedup_error_and_recovery(self):
        run(self.root, reason, fetcher=lambda _: self.html())
        item = self.page['searchFeedResponse']['looseTiles'][0]['listing']; item['price'] = '3800'
        run(self.root, reason, fetcher=lambda _: self.html())
        rows, checks, scan = snapshot(self.root)
        self.assertEqual(len(rows), 1); self.assertEqual(len(rows[0]['price_history']), 2)
        self.assertEqual(rows[0]['category'], 'match'); self.assertEqual(checks['studio']['radius'], 30)
        def blocked(_): raise OfferUpError('OfferUp blocked search (HTTP 403).')
        run(self.root, reason, fetcher=blocked)
        rows, checks, scan = snapshot(self.root)
        self.assertEqual(len(rows), 1); self.assertEqual(checks['studio']['status'], 'error')
        self.assertEqual(scan['failed_watches'], 1)
        run(self.root, reason, fetcher=lambda _: self.html())
        self.assertEqual(snapshot(self.root)[1]['studio']['status'], 'ok')

    def test_inflight_pause_does_not_publish_results(self):
        def pause(_):
            _, watches, registry = definitions(self.root); w = watches['studio']; w['enabled'] = False
            mutate_watch({'version': registry['version'], 'id': 'studio', 'watch': w}, self.root)
            return self.html()
        run(self.root, reason, fetcher=pause)
        self.assertEqual(snapshot(self.root)[0], [])

    def test_narrower_radius_is_explicit_and_too_small_fails(self):
        self.assertEqual(effective_radius(40), 30); self.assertEqual(effective_radius(100), 50)
        with self.assertRaises(OfferUpError): effective_radius(2)

    def test_new_condition_and_incomplete_specs_are_not_invented(self):
        self.watch['condition'] = 'new'
        self.page['searchFeedResponse']['filters'][2]['options'] = [{'value': 'NEW', 'isSelected': True}]
        item = self.page['searchFeedResponse']['looseTiles'][0]['listing']; item['title'] = 'Mac Studio M5 Max'
        rows, _ = parse_search(self.html(), self.watch, 'Mac Studio')
        self.assertEqual(rows[0]['condition'], 'new')
        self.assertIn('missing', reason('studio', type('Listing', (), rows[0])(), self.watch))

    def test_far_towns_are_dropped_and_los_angeles_needs_review(self):
        tile = self.page['searchFeedResponse']['looseTiles'][0]
        far = copy.deepcopy(tile); far['listing'].update(listingId='22345678-1234-1234-1234-123456789abc', locationName='Sacramento, CA')
        city = copy.deepcopy(tile); city['listing'].update(listingId='32345678-1234-1234-1234-123456789abc', locationName='Los Angeles, CA')
        self.page['searchFeedResponse']['looseTiles'] += [far, city]
        run(self.root, reason, fetcher=lambda url: self.html())
        rows = {r['location']: r for r in snapshot(self.root)[0]}
        self.assertNotIn('Sacramento, CA', rows); self.assertEqual(rows['Pasadena, CA']['category'], 'match')
        self.assertEqual(rows['Los Angeles, CA']['reason'], 'pickup distance needs review')

    def test_item_detail_coordinates_resolve_neighborhood_and_drop_far_la_listings(self):
        w = definitions(self.root)[1]['studio']; w['radius'] = 5
        mutate_watch({'version': definitions(self.root)[2]['version'], 'id': 'studio', 'watch': w}, self.root)
        self.page['searchFeedParams']['radius'] = '5'
        self.page['searchFeedResponse']['filters'][0]['options'] = [{'value': '5', 'isSelected': True}]
        tile = self.page['searchFeedResponse']['looseTiles'][0]
        tile['listing'].update(listingId='11111111-1234-1234-1234-123456789abc', locationName='Los Angeles, CA')
        far_la = copy.deepcopy(tile)
        far_la['listing'].update(listingId='22222222-1234-1234-1234-123456789abc', locationName='Los Angeles, CA')
        self.page['searchFeedResponse']['looseTiles'] = [tile, far_la]
        near_lat = round(AREA['latitude'] + 0.02, 3)
        near_lon = round(AREA['longitude'] + 0.02, 3)
        ck = f'@{near_lat:.3f},{near_lon:.3f}'
        details = {
            '11111111-1234-1234-1234-123456789abc': {'latitude': str(near_lat), 'longitude': str(near_lon), 'locationName': 'Los Angeles, CA', 'zipcode': '91401'},
            '22222222-1234-1234-1234-123456789abc': {'latitude': str(AREA['latitude'] - 0.35), 'longitude': str(AREA['longitude'] + 0.25), 'locationName': 'Los Angeles, CA', 'zipcode': '90001'},
        }
        with patch('places.geocode', return_value={'found': {ck: 'Valley Glen, CA'}, 'unknown': []}):
            run(self.root, reason, fetcher=lambda url: self.html(), detail_fetcher=lambda ids: {k: details[k] for k in ids if k in details})
        rows = snapshot(self.root)[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'offerup_11111111-1234-1234-1234-123456789abc')
        self.assertEqual(rows[0]['category'], 'match')
        self.assertIn('Valley Glen, CA 91401 · about ', rows[0]['location'])
        self.assertIn(f"mi from {AREA['city']}", rows[0]['location'])

if __name__ == '__main__': unittest.main()

class RadiusConfirmationTests(unittest.TestCase):
    def test_facebook_apply_can_finish_before_its_visible_label_updates(self):
        from watch import BoundedFacebook
        from unittest.mock import Mock
        obj=object.__new__(BoundedFacebook);obj.page=Mock();obj.translator=Mock();obj.logger=Mock()
        with patch('watch.FacebookMarketplace.set_search_radius',return_value=True), patch('watch.FacebookLocationDialog') as dialog:
            dialog.return_value.current_radius.side_effect=[10,10,40]
            self.assertTrue(obj.set_search_radius(40));self.assertEqual(obj.page.wait_for_timeout.call_count,2)

    def test_facebook_wrong_radius_still_fails(self):
        from watch import BoundedFacebook
        from unittest.mock import Mock
        obj=object.__new__(BoundedFacebook);obj.page=Mock();obj.translator=Mock();obj.logger=Mock()
        with patch('watch.FacebookMarketplace.set_search_radius',return_value=True), patch('watch.FacebookLocationDialog') as dialog, patch('watch.time.monotonic',side_effect=[0,11,20,31]):
            dialog.return_value.current_radius.return_value=10
            with self.assertRaisesRegex(RuntimeError,'requested 40, observed 10'):obj.set_search_radius(40)

    def test_facebook_location_click_timeout_recovers_after_one_reload(self):
        from watch import BoundedFacebook
        from unittest.mock import Mock
        obj=object.__new__(BoundedFacebook);obj.page=Mock();obj.translator=Mock();obj.logger=Mock()
        with patch('watch.FacebookMarketplace.set_search_radius',side_effect=[False,True]) as setter, patch('watch.FacebookLocationDialog') as dialog, patch('watch.time.monotonic',side_effect=[0,11,20]):
            dialog.return_value.current_radius.side_effect=[10,40]
            self.assertTrue(obj.set_search_radius(40))
            self.assertEqual(setter.call_count,2);obj.page.reload.assert_called_once_with(wait_until='domcontentloaded',timeout=15000)
