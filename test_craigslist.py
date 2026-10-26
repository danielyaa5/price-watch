import json, tempfile, unittest
from pathlib import Path
from craigslist_collect import parse_search, detail, run, snapshot, canonical_url, CraigslistError, CENTER, CENTER_NAME
from watch_config import definitions,mutate_watch

URL='https://www.craigslist.org/view/d/los-angeles-bambu-lab-ams-2-pro/Ab123456789012345678'
def offer(lat=CENTER[0],price='199.00'):
 return {'price':price,'priceCurrency':'USD','availableAtOrFrom':{'geo':{'latitude':lat,'longitude':CENTER[1]},'address':{'addressLocality':'Los Angeles'}}}
def script(id,value):return '<script type="application/ld+json" id="'+id+'">'+json.dumps(value)+'</script>'
def search(empty=False):
 product={'@type':'Product','name':'Bambu Lab AMS 2 Pro','offers':offer()}
 meta={'contentLocation':{'name':CENTER_NAME},'breadcrumb':{'itemListElement':[{'item':f'https://www.craigslist.org/search/city/search-area?lat={CENTER[0]}&lon={CENTER[1]}&radius=10'}]}}
 return '<link rel="canonical" href="https://www.craigslist.org/search/city/los-angeles-ca?query=AMS%202%20Pro">'+script('ld_searchpage_data',meta)+script('ld_searchpage_results',{'itemListElement':[] if empty else [{'item':product}]})+('' if empty else '<li class="cl-static-search-result" title="Bambu Lab AMS 2 Pro"><a href="'+URL+'">Bambu Lab AMS 2 Pro</a></li>')
def post(lat=CENTER[0],condition='excellent',price='199.00'):
 return script('ld_posting_data',{'@type':'Product','name':'Bambu Lab AMS 2 Pro','offers':offer(lat,price)})+'<div class="attr condition">condition: '+condition+'</div><section id="postingbody">Complete working unit. contact private@example.com</section>'

class CraigslistTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  (self.root/'config.toml').write_text('[item.ams]\nsearch_phrases=["AMS 2 Pro"]\nmax_price=200\n[watch.ams]\nnotify=false\n')
  w=definitions(self.root)[1]['ams'];w.update(sources=['craigslist'],condition='used',radius=10,alerts=False);mutate_watch({'version':0,'id':'ams','watch':w},self.root)
  self.watch=definitions(self.root)[1]['ams']
 def fake(self,url):return search() if '/search/' in url else post()
 def test_search_and_details_apply_local_rules(self):
  report=run(self.root,lambda *a:None,fetcher=self.fake);row=snapshot(self.root)[0][0]
  self.assertEqual(report['status'],'ok');self.assertEqual(row['category'],'match');self.assertIn('0 mi',row['location']);self.assertEqual(row['condition'],'excellent');self.assertEqual(row['description'],'');self.assertNotIn('private@example.com',json.dumps(snapshot(self.root)))
 def test_wrong_query_or_radius_is_not_empty(self):
  for html in [search().replace('query=AMS%202%20Pro','query=wrong'),search().replace('radius=10','radius=40'),'Access denied']:
   with self.assertRaises(CraigslistError):parse_search(html,self.watch,'AMS 2 Pro')
 def test_verified_empty_search(self):self.assertEqual(parse_search(search(True),self.watch,'AMS 2 Pro')[0],[])
 def test_unconfirmed_condition_fails_closed(self):
  run(self.root,lambda *a:None,fetcher=lambda u:search() if '/search/' in u else post(condition='new'))
  self.assertEqual(snapshot(self.root)[0][0]['reason'],'used condition needs review')
 def test_listing_outside_radius_is_dropped(self):
  run(self.root,lambda *a:None,fetcher=lambda u:search() if '/search/' in u else post(lat=35))
  self.assertEqual(snapshot(self.root)[0],[])
 def test_incomplete_details_are_candidate(self):
  run(self.root,lambda *a:None,fetcher=lambda u:search() if '/search/' in u else 'posting removed')
  self.assertEqual(snapshot(self.root)[0][0]['reason'],'listing details need review')
 def test_failures_retain_earlier_rows(self):
  run(self.root,lambda *a:None,fetcher=self.fake)
  run(self.root,lambda *a:None,fetcher=lambda u:'unknown page')
  self.assertEqual(len(snapshot(self.root)[0]),1);self.assertEqual(snapshot(self.root)[1]['ams']['status'],'error')
 def test_only_public_listing_urls(self):
  self.assertEqual(canonical_url(URL+'?tracking=123'),URL)
  for url in ['https://accounts.craigslist.org/login','https://evil.example/view/d/item/123','https://www.craigslist.org/search/sss','https://www.craigslist.org:8443/view/d/item/Ab123456789012345678']:
   self.assertIsNone(canonical_url(url))

if __name__=='__main__':unittest.main()
