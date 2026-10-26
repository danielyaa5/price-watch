import json, tempfile, unittest
from pathlib import Path
from retail_collect import run, snapshot, variants, RetailError, fetch
from watch_config import definitions, mutate_watch

class RetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'config.toml').write_text('[item.ams]\nsearch_phrases=["AMS 2 Pro"]\nmax_price=200\n[watch.ams]\nnotify=false\n')
        w=definitions(self.root)[1]['ams'];w.update(sources=['retail'],retail_stores=['universe','expercom'],condition='new',alerts=False)
        mutate_watch({'version':0,'id':'ams','watch':w},self.root)
        self.product={'handle':'ams-2-pro','title':'Bambu Lab AMS 2 Pro','variants':[{'id':123,'title':'Default Title','price':19900,'available':True}]}
    def fake(self,url):
        if url.endswith('/cart.js'):return {'currency':'USD'}
        if '/search/' in url:return {'resources':{'results':{'products':[{'handle':'ams-2-pro'}]}}}
        return self.product
    def matcher(self,id,row,w):return 'over maximum' if row.price_value>w['max_price'] else None
    def test_two_stores_search_and_exact_variant_urls(self):
        result=run(self.root,self.matcher,fetcher=self.fake);rows,checks,_=snapshot(self.root)
        self.assertEqual(result['status'],'ok');self.assertEqual(len(rows),2);self.assertEqual(checks['ams']['matches'],2)
        self.assertTrue(all('?variant=123' in r['post_url'] for r in rows));self.assertEqual(rows[0]['price'],'$199.00')
    def test_variant_prices_not_product_lowest_price(self):
        self.product['price']=100;self.product['variants'][0]['price']=32900
        row=variants('universe',self.product,'USD')[0];self.assertEqual(row['price'],'$329.00')
    def test_unavailable_variant_stays_candidate(self):
        self.product['variants'][0]['available']=False
        run(self.root,self.matcher,fetcher=self.fake);self.assertTrue(all(r['category']=='candidate' for r in snapshot(self.root)[0]))
    def test_partial_store_failure_preserves_other_results(self):
        def fake(url):
            if 'expercom.com' in url:raise RetailError('HTTP 403')
            return self.fake(url)
        result=run(self.root,self.matcher,fetcher=fake)
        self.assertEqual(result['status'],'partial');self.assertEqual(len(snapshot(self.root)[0]),1)
    def test_unknown_format_is_error_not_empty(self):
        result=run(self.root,self.matcher,fetcher=lambda u:{'currency':'USD'} if u.endswith('cart.js') else {})
        self.assertEqual(result['failed_watches'],1);self.assertEqual(snapshot(self.root)[1]['ams']['status'],'error')
    def test_used_only_does_not_request_stores(self):
        w=definitions(self.root)[1]['ams'];w['condition']='used';mutate_watch({'version':1,'id':'ams','watch':w},self.root)
        run(self.root,self.matcher,fetcher=lambda u:self.fail('used watch fetched retail'))
        self.assertEqual(snapshot(self.root)[1]['ams']['status'],'disabled')
    def test_bad_currency_and_variant_price_fail_closed(self):
        with self.assertRaises(RetailError):variants('universe',self.product,'CAD')
        self.product['variants'][0]['price']='19900'
        with self.assertRaises(RetailError):variants('universe',self.product,'USD')
    def test_untrusted_urls_never_fetched(self):
        for url in ['http://localhost:8088','https://evil.example/products/a','https://expercom.com:8443/cart.js','https://user@expercom.com/cart.js']:
            with self.assertRaises(RetailError):fetch(url)

if __name__=='__main__':unittest.main()
