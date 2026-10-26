import base64, hashlib, json, tempfile, threading, unittest
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import HTTPServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from ebay_receiver import verify, handler
from ebay_store import save_check, snapshot, owner_hash, delete_account

class EbayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'ebay-private.json').write_text(json.dumps({'verification_token':'x'*48,'endpoint':'https://example.com/ebay/account-deletion'}))
    def tearDown(self):self.tmp.cleanup()
    def test_signature_accepts_signed_body_rejects_tampering(self):
        private=ec.generate_private_key(ec.SECP256R1());pem=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        class Client:
            def public_key(self,kid):return pem
        body=b'{"metadata":{"topic":"MARKETPLACE_ACCOUNT_DELETION"},"notification":{"notificationId":"test","data":{"username":"seller"}}}'
        sig=private.sign(body,ec.ECDSA(hashes.SHA1()))
        header=base64.b64encode(json.dumps({'kid':'example','signature':base64.b64encode(sig).decode()}).encode()).decode()
        self.assertEqual(verify(body,header,Client())['notification']['notificationId'],'test')
        with self.assertRaises(ValueError):verify(body.replace(b'seller',b'other'),header,Client())
        with self.assertRaises(ValueError):verify(body,'',Client())
    def test_challenge_and_public_route_limits(self):
        server=HTTPServer(('127.0.0.1',0),handler(self.root));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            origin='http://127.0.0.1:'+str(server.server_port)
            result=json.load(urlopen(origin+'/ebay/account-deletion?challenge_code=abc'))
            expected=hashlib.sha256(('abc'+'x'*48+'https://example.com/ebay/account-deletion').encode()).hexdigest()
            self.assertEqual(result,{'challengeResponse':expected})
            for path in ('/','/api/price-watch','/ebay/account-deletion'):
                with self.assertRaises(HTTPError) as exc:urlopen(origin+path)
                self.assertIn(exc.exception.code,(400,404))
            with self.assertRaises(HTTPError) as exc:urlopen(Request(origin+'/ebay/account-deletion',data=b'{}',headers={'Content-Type':'application/json'}))
            self.assertEqual(exc.exception.code,412)
        finally:server.shutdown();server.server_close();thread.join()
    def test_deletion_is_scoped_and_prevents_reintroduction(self):
        watch={'id':'test','name':'Test'}
        for seller,id in [('one','ebay_1'),('two','ebay_2')]:
            save_check(self.root,watch,[{'id':id,'price':'$100','_owner':owner_hash(self.root,seller)}],{'checked_at':'2026-10-24T12:00:00+00:00'})
        event={'notificationId':'delete-1','data':{'username':'one'}}
        self.assertEqual(delete_account(self.root,event),1);self.assertEqual(delete_account(self.root,event),0)
        rows,_,_=snapshot(self.root);self.assertEqual([r['id'] for r in rows],['ebay_2'])
        save_check(self.root,watch,[{'id':'ebay_1','price':'$100','_owner':owner_hash(self.root,'one')}],{'checked_at':'2026-10-24T12:01:00+00:00'})
        self.assertEqual(len(snapshot(self.root)[0]),1)

if __name__=='__main__':unittest.main()

class EbayCollectionTests(EbayTests):
    def test_shipping_is_counted_and_unknown_shipping_is_not_a_match(self):
        from ebay_collect import normalize, classify
        item={'itemId':'v1|123|0','seller':{'username':'example'},'title':'Example computer','price':{'value':'3900','currency':'USD'},'shippingOptions':[{'shippingCost':{'value':'150','currency':'USD'}}],'buyingOptions':['FIXED_PRICE']}
        row=normalize(item,self.root)
        self.assertEqual(row['total'],4050)
        def matcher(_,listing,watch):return 'over maximum' if float(listing.price.replace('$','').replace(',',''))>4000 else None
        self.assertEqual(classify(row,{'id':'test'},matcher),'over maximum')
        item.pop('shippingOptions');row=normalize(item,self.root)
        self.assertEqual(classify(row,{'id':'test'},matcher),'shipping cost needs review')
    def test_notes_are_removed_with_account_data(self):
        from ebay_store import annotate_listing
        save_check(self.root,{'id':'test'},[{'id':'ebay_1','price':'$100','_owner':owner_hash(self.root,'one')}],{'checked_at':'2026-10-24T12:00:00+00:00'})
        annotate_listing(self.root,'test:ebay_1','note','Private note')
        self.assertEqual(snapshot(self.root)[0][0]['note'],'Private note')
        delete_account(self.root,{'notificationId':'delete-note','data':{'username':'one'}})
        self.assertEqual(snapshot(self.root)[0],[])
        with self.assertRaises(ValueError):annotate_listing(self.root,'test:ebay_1','saved',True)

class EbayBudgetTests(unittest.TestCase):
    def test_enrichment_preserves_searches_for_later_watches(self):
        from unittest.mock import patch
        from contextlib import ExitStack
        from ebay_collect import run
        watches={str(i):dict(id=str(i),enabled=True,archived=False,sources=['ebay'],queries=['hub'],condition='any',min_price=0,max_price=150,profile='computer' if i==0 else 'thunderbolt4') for i in range(5)}
        requests=[];reports=[]
        item=dict(itemId='one',seller={'username':'example'},title='Thunderbolt 4 hub',price={'value':'100','currency':'USD'},shippingOptions=[{'shippingCost':{'value':'0','currency':'USD'}}],buyingOptions=['FIXED_PRICE'])
        class Client:
            def get(self,path,params=None):
                requests.append((path,params))
                if params is not None:return {'itemSummaries':[dict(item,itemId=str(i)) for i in range(100)],'next':'more' if params['offset']=='0' else None}
                return item
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            for name,value in [('definitions',(None,watches,None)),('snapshot',([],None,None)),('fingerprint','test'),('owner_hash','owner')]:
                stack.enter_context(patch('ebay_collect.'+name,return_value=value))
            stack.enter_context(patch('ebay_collect.EbayClient',return_value=Client()))
            stack.enter_context(patch('ebay_collect.health'))
            stack.enter_context(patch('ebay_collect.save_check',side_effect=lambda root,w,rows,report:reports.append(report)))
            result=run(directory,lambda *args:None)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(len(reports),5)
        self.assertEqual(sum(params is not None for _,params in requests),10)
        self.assertEqual(len(requests),85)
        # Only the first computer watch fetches specifics; hub identity comes from titles.
        self.assertTrue(all(params is not None for _,params in requests[-8:]))
