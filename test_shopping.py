import json, tempfile, time, unittest
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch, MagicMock
from shopping_collect import (parse_amazon, parse_google, canonical_url, configure_google, google_status, fetch_amazon,
    google_search, google_request, ShoppingError, run, snapshot, GOOGLE_CAP)
from watch_config import definitions, mutate_watch, atomic, channel_settings

AMAZON='''<input id="twotabsearchtextbox" value="AMS 2 Pro"><li id="p_n_condition-type/6461716011"><a aria-current="true"><span>New</span></a></li><div data-component-type="s-search-result" data-asin="B012345678"><h2>Bambu Lab AMS 2 Pro</h2><span class="a-price a-text-price"><span class="a-offscreen">$399.00</span></span><span class="a-price"><span class="a-offscreen">$199.00</span></span><span>Add to cart</span><span>Save $30 with coupon</span></div>'''

def google(token='newtoken', products=None):
    return {'search_metadata':{'status':'Success'},'search_parameters':{'engine':'google_shopping','gl':'us','shoprs':token},'filters':[{'type':'Condition','options':[{'text':'New','shoprs':'newtoken'}]}],
      'shopping_results': products if products is not None else [{'product_id':'123','title':'Bambu Lab AMS 2 Pro','source':'Example Store','price':'$199.00','product_link':'https://www.google.com/search?ibp=oshop&prds=catalogid%3A123&udm=28&utm_source=ignored'}]}

class ShoppingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        (self.root/'config.toml').write_text('[item.ams]\nsearch_phrases=["AMS 2 Pro"]\nmax_price=200\n[watch.ams]\nnotify=false\n')
        w=definitions(self.root)[1]['ams'];w.update(sources=['amazon','google_shopping'],condition='any',alerts=False)
        mutate_watch({'version':0,'id':'ams','watch':w},self.root)
        configure_google({'api_key':'a'*64},self.root)
    def test_amazon_uses_current_price_not_strike_or_coupon(self):
        row=parse_amazon(AMAZON,'AMS 2 Pro')[0]
        self.assertEqual(row['price_value'],199);self.assertTrue(row['available']);self.assertEqual(row['post_url'],'https://www.amazon.com/dp/B012345678')
    def test_amazon_requires_query_and_new_filter(self):
        for html in [AMAZON.replace('aria-current="true"',''),AMAZON.replace('value="AMS 2 Pro"','value="other"'),'Robot Check']:
            with self.assertRaises(ShoppingError):parse_amazon(html,'AMS 2 Pro')
    def test_amazon_default_collection_uses_browser_and_preserves_filters(self):
        with patch('shopping_collect.fetch_amazon',return_value=AMAZON) as browser:
            report=run(self.root,lambda *a:None,'amazon')
        self.assertEqual(report['status'],'ok')
        params=parse_qs(urlsplit(browser.call_args.args[0]).query)
        self.assertEqual(params,{'k':['AMS 2 Pro'],'rh':['p_n_condition-type:6461716011']})
    def test_amazon_browser_failure_preserves_stale_results(self):
        run(self.root,lambda *a:None,'amazon',fetcher=lambda u:AMAZON)
        with patch('shopping_collect.fetch_amazon',side_effect=ShoppingError('Amazon requires a human check')):
            report=run(self.root,lambda *a:None,'amazon')
        rows,checks,_=snapshot(self.root,'amazon')
        self.assertEqual(report['status'],'error');self.assertEqual(len(rows),1)
        self.assertEqual(checks['ams']['status'],'error')
    def test_amazon_browser_rejects_other_addresses_before_launch(self):
        for url in ['http://www.amazon.com/s','https://www.amazon.com/dp/B012345678',
                    'https://example.com/s','https://user:pass@www.amazon.com/s']:
            with self.assertRaisesRegex(ShoppingError,'Unsupported'):fetch_amazon(url)
    def test_amazon_canonical_redirect_is_revalidated_and_browser_closes(self):
        browser=MagicMock();page=browser.new_context.return_value.new_page.return_value
        page.url='https://www.amazon.com/s/ref=nb_sb_noss?k=AMS+2+Pro'
        page.goto.return_value.status=200;page.content.return_value=AMAZON
        runtime=MagicMock();runtime.sync_playwright.return_value.__enter__.return_value.chromium.launch.return_value=browser
        runtime.Error=RuntimeError
        with patch.dict('sys.modules',{'playwright.sync_api':runtime}):
            html=fetch_amazon('https://www.amazon.com/s?k=AMS+2+Pro')
        self.assertEqual(len(parse_amazon(html,'AMS 2 Pro')),1)
        with self.assertRaises(ShoppingError):parse_amazon(html,'another query')
        browser.close.assert_called_once()
    def test_amazon_external_redirect_fails_and_browser_closes(self):
        browser=MagicMock();page=browser.new_context.return_value.new_page.return_value
        page.url='https://example.com/s';page.goto.return_value.status=200
        runtime=MagicMock();runtime.sync_playwright.return_value.__enter__.return_value.chromium.launch.return_value=browser
        runtime.Error=RuntimeError
        with patch.dict('sys.modules',{'playwright.sync_api':runtime}):
            with self.assertRaisesRegex(ShoppingError,'redirected'):fetch_amazon('https://www.amazon.com/s?k=x')
        browser.close.assert_called_once()
    def test_amazon_unknown_price_is_candidate(self):
        report=run(self.root,lambda *a:None,'amazon',fetcher=lambda u:AMAZON.replace('$199.00','See options').replace('Add to cart','See buying choices'))
        self.assertEqual(report['status'],'ok');self.assertEqual(snapshot(self.root,'amazon')[0][0]['category'],'candidate')
    def test_filament_guards_are_not_complete_ams_units(self):
        run(self.root,lambda *a:None,'amazon',fetcher=lambda u:AMAZON.replace('Bambu Lab AMS 2 Pro','6pcs AMS 2Pro Guard Ceramic Filament Inlets for Bambu Lab'))
        row=snapshot(self.root,'amazon')[0][0];self.assertEqual(row['category'],'candidate');self.assertIn('accessory',row['reason'])
    def test_google_ams_query_includes_brand(self):
        calls=[]
        def fetch(url):calls.append(parse_qs(urlsplit(url).query)['q'][0]);return json.dumps(google())
        run(self.root,lambda *a:None,'google_shopping',fetcher=fetch)
        self.assertEqual(calls,['Bambu Lab AMS 2 Pro','Bambu Lab AMS 2 Pro'])
    def test_google_us_filter_and_currency_are_required(self):
        self.assertEqual(parse_google(google(),'newtoken')[0]['price_value'],199)
        for response in [google('wrong'),google(products=[dict(google()['shopping_results'][0],price='CA$199.00')])]:
            with self.assertRaises(ShoppingError):parse_google(response,'newtoken')
    def test_google_ebay_is_excluded_and_used_is_not_new(self):
        product=google()['shopping_results'][0]
        self.assertEqual(parse_google(google(products=[dict(product,source='eBay')]),'newtoken'),[])
        self.assertEqual(parse_google(google(products=[dict(product,second_hand=True)]),'newtoken')[0]['condition'],'used')
    def test_filter_discovery_then_cache_counts_requests(self):
        calls=[]
        def fetch(url):calls.append(parse_qs(urlsplit(url).query));return json.dumps(google())
        google_search(self.root,'AMS 2 Pro',fetch);google_search(self.root,'AMS 2 Pro',fetch)
        self.assertEqual(len(calls),3);self.assertNotIn('shoprs',calls[0]);self.assertEqual(calls[1]['shoprs'],['newtoken']);self.assertEqual(google_status(self.root)['used'],3)
    def test_quota_stops_before_network_even_manual(self):
        atomic(self.root/'state/google_shopping/usage.json',{'attempts':[time.time()]*GOOGLE_CAP})
        with self.assertRaisesRegex(ShoppingError,'safety cap'):google_request(self.root,{},lambda u:self.fail('network after cap'))
    def test_private_key_never_exposed_in_status_or_errors(self):
        self.assertNotIn('a'*64,json.dumps(google_status(self.root)))
        with self.assertRaises(ShoppingError) as error:google_request(self.root,{},lambda u:json.dumps({'error':u}))
        self.assertNotIn('a'*64,str(error.exception));self.assertEqual((self.root/'shopping-private.json').stat().st_mode&0o777,0o600)
    def test_six_hour_skip_keeps_last_real_scan(self):
        fetch=lambda u:json.dumps(google())
        first=run(self.root,lambda *a:None,'google_shopping',fetcher=fetch)
        result=run(self.root,lambda *a:None,'google_shopping',fetcher=lambda u:self.fail('fresh scan repeated'))
        self.assertEqual(result['skipped_watches'],1);self.assertEqual(snapshot(self.root,'google_shopping')[2],first)
    def test_failure_preserves_results_but_marks_check_failed(self):
        run(self.root,lambda *a:None,'amazon',fetcher=lambda u:AMAZON)
        run(self.root,lambda *a:None,'amazon',fetcher=lambda u:'Robot Check')
        rows,checks,_=snapshot(self.root,'amazon');self.assertEqual(len(rows),1);self.assertEqual(checks['ams']['status'],'error')
    def test_google_errors_also_respect_schedule(self):
        run(self.root,lambda *a:None,'google_shopping',fetcher=lambda u:'{}')
        result=run(self.root,lambda *a:None,'google_shopping',fetcher=lambda u:self.fail('failure retried too soon'))
        self.assertEqual(result['skipped_watches'],1);self.assertEqual(snapshot(self.root,'google_shopping')[1]['ams']['status'],'error')
    def test_used_watches_do_not_request_new_sources(self):
        w=definitions(self.root)[1]['ams'];w['condition']='used';mutate_watch({'version':1,'id':'ams','watch':w},self.root)
        for source in ('amazon','google_shopping'):
            run(self.root,lambda *a:None,source,fetcher=lambda u:self.fail('used fetched'))
            self.assertEqual(snapshot(self.root,source)[1]['ams']['status'],'disabled')
    def test_new_sources_join_enabled_alert_channels(self):
        w=definitions(self.root)[1]['ams'];w.update(sources=['retail'],channels={'email':True,'ntfy':False})
        mutate_watch({'version':1,'id':'ams','watch':w},self.root)
        w=definitions(self.root)[1]['ams'];w['sources']+=['amazon','google_shopping']
        mutate_watch({'version':2,'id':'ams','watch':w},self.root)
        self.assertEqual(channel_settings(definitions(self.root)[1]['ams'])['email']['sources'],['retail','amazon','google_shopping'])
    def test_product_links_reject_private_or_active_urls(self):
        for url in ['javascript:alert(1)','https://127.0.0.1/a','https://server.local/a','https://user:pass@example.com/a','https://www.google.com/search?q=x','https://example.com:8443/a']:
            self.assertIsNone(canonical_url(url))
        self.assertEqual(canonical_url('https://shop.example/product?id=123&utm_source=ad'),'https://shop.example/product?id=123')

if __name__=='__main__':unittest.main()
