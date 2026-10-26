"""Production eBay application credentials; tokens stay in memory."""
import json, time
from pathlib import Path
import requests

class EbayError(RuntimeError): pass
class EbayClient:
    def __init__(self, root):
        self.root=Path(root);self.session=requests.Session();self.token=None;self.expires=0;self.keys={};self.key_requests=[]
    def access_token(self):
        if self.token and time.time()<self.expires:return self.token
        try:c=json.loads((self.root/'ebay-private.json').read_text())
        except (OSError,ValueError):raise EbayError('eBay credentials need setup in ebay-private.json.') from None
        if not c.get('client_id') or not c.get('client_secret'):raise EbayError('eBay credentials need setup in ebay-private.json.')
        try:r=self.session.post('https://api.ebay.com/identity/v1/oauth2/token',auth=(c['client_id'],c['client_secret']),data={'grant_type':'client_credentials','scope':'https://api.ebay.com/oauth/api_scope'},timeout=20)
        except requests.RequestException:raise EbayError('Could not reach eBay authentication.') from None
        if r.status_code!=200:raise EbayError(f'eBay authentication rejected the application (HTTP {r.status_code}).')
        d=r.json();self.token=d['access_token'];self.expires=time.time()+int(d.get('expires_in',7200))-120
        return self.token
    def get(self,path,params=None):
        if not path.startswith(('/buy/browse/v1/','/commerce/notification/v1/public_key/')):raise ValueError('Unsupported eBay API route')
        for attempt in range(2):
            try:r=self.session.get('https://api.ebay.com'+path,params=params,headers={'Authorization':'Bearer '+self.access_token(),'X-EBAY-C-MARKETPLACE-ID':'EBAY_US'},timeout=25)
            except requests.RequestException:raise EbayError('eBay request timed out or the connection failed.') from None
            if r.status_code==401 and not attempt:self.token=None;continue
            if r.status_code!=200:
                try:codes=','.join(str(x.get('errorId')) for x in r.json().get('errors',[])[:3])
                except ValueError:codes=''
                raise EbayError(f'eBay API returned HTTP {r.status_code}'+(f' (code {codes})' if codes else '')+'.')
            return r.json()
    def public_key(self,kid):
        current=time.time();cached=self.keys.get(kid)
        if cached and cached[0]>current:return cached[1]
        self.key_requests=[t for t in self.key_requests if current-t<3600]
        if len(self.key_requests)>=20:raise EbayError('Notification key lookup limit reached; eBay will retry.')
        self.key_requests.append(current)
        data=self.get('/commerce/notification/v1/public_key/'+kid)
        if len(self.keys)>30:self.keys.clear()
        self.keys[kid]=(current+3600,data['key']);return data['key']
