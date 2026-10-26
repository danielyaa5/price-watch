#!/usr/bin/env python3
"""Small loopback-only eBay receiver; no dashboard, file serving, or admin routes."""
import base64, hashlib, json, re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from ebay_client import EbayClient, EbayError
from ebay_store import delete_account, health
from watch_config import now
ROOT=Path(__file__).resolve().parent

def verify(body,header,client):
    if not header or len(header)>8192:raise ValueError('Missing or invalid signature')
    value=json.loads(base64.b64decode(header,validate=True))
    kid=value.get('kid','')
    if not isinstance(kid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',kid):raise ValueError('Invalid public key ID')
    signature=base64.b64decode(value['signature'],validate=True)
    encoded=re.sub(r'-----BEGIN PUBLIC KEY-----|-----END PUBLIC KEY-----|\s','',client.public_key(kid))
    key=serialization.load_der_public_key(base64.b64decode(encoded,validate=True))
    if not isinstance(key,ec.EllipticCurvePublicKey):raise ValueError('Expected EC public key')
    payload=json.loads(body)
    # eBay's official SDK verifies compact JSON with ECDSA/SHA1. Raw JSON is also
    # accepted when it is exactly the signed representation; never alter values.
    compact=json.dumps(payload,separators=(',',':'),ensure_ascii=False).encode()
    for candidate in dict.fromkeys((body,compact)):
        try:key.verify(signature,candidate,ec.ECDSA(hashes.SHA1()));return payload
        except InvalidSignature:pass
    raise ValueError('Signature verification failed')

def handler(root=ROOT):
    client=EbayClient(root)
    class Handler(BaseHTTPRequestHandler):
        server_version='PriceWatch';sys_version=''
        def setup(self):super().setup();self.connection.settimeout(15)
        def log_message(self,*args):pass
        def reply(self,code,obj=None):
            raw=json.dumps(obj).encode() if obj is not None else b''
            self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def route(self):
            path=urlsplit(self.path).path
            health(root,'request',{'at':now(),'method':self.command,'path':path[:160]})
            return path in ('/','/ebay/account-deletion')
        def do_GET(self):
            if not self.route():return self.reply(404)
            params=parse_qs(urlsplit(self.path).query);challenge=params.get('challenge_code',[])
            if len(challenge)!=1 or not challenge[0] or len(challenge[0])>512:return self.reply(400)
            c=json.loads((root/'ebay-private.json').read_text())
            digest=hashlib.sha256((challenge[0]+c['verification_token']+c['endpoint']).encode()).hexdigest()
            health(root,'challenge',{'at':now(),'status':'ok'})
            return self.reply(200,{'challengeResponse':digest})
        def do_POST(self):
            if not self.route():return self.reply(404)
            try:size=int(self.headers.get('Content-Length','0'))
            except ValueError:return self.reply(400)
            if self.headers.get('Transfer-Encoding') or size<=0 or size>65536:return self.reply(413)
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':return self.reply(415)
            try:
                body=self.rfile.read(size)
                if len(body)!=size:return self.reply(400)
                value=verify(body,self.headers.get('X-EBAY-SIGNATURE'),client)
                if value.get('metadata',{}).get('topic')!='MARKETPLACE_ACCOUNT_DELETION':return self.reply(400)
                notification=value['notification']
                if not notification.get('notificationId') or not isinstance(notification.get('data'),dict):return self.reply(400)
                delete_account(root,notification)
            except EbayError:
                health(root,'receiver_error',{'at':now(),'detail':'eBay public-key lookup unavailable; notification will be retried.'})
                return self.reply(503)
            except (ValueError,KeyError,TypeError):return self.reply(412)
            except Exception:
                health(root,'receiver_error',{'at':now(),'detail':'Notification processing failed; eBay will retry.'})
                return self.reply(503)
            return self.reply(204)
    return Handler

if __name__=='__main__':HTTPServer(('127.0.0.1',8097),handler()).serve_forever()
