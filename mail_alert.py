"""Fixed-account Gmail delivery for price-watch alerts.

The sending and receiving addresses come from mail-private.json next to this file:
{"sender": "...", "recipient": "..."}. Both accounts need workspace-mcp Gmail credentials.
"""
import base64, json, sys
from pathlib import Path
from email.message import EmailMessage
from email import policy
from email.utils import parseaddr
from email.parser import BytesParser
_ADDRESSES=json.loads((Path(__file__).resolve().parent/'mail-private.json').read_text())
SENDER=_ADDRESSES['sender'].lower()
RECIPIENT=_ADDRESSES['recipient'].lower()
TEST_SUBJECT='Price watch: email delivery test'
TEST_BODY='This is a test of your price-watch email alerts. New matching listings will arrive here alongside your ntfy notifications.'

def service(address):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    d=json.loads((Path.home()/'.config/workspace-mcp/credentials'/f'{address}.json').read_text())
    c=Credentials(token=d.get('token'),refresh_token=d.get('refresh_token'),token_uri=d.get('token_uri'),client_id=d.get('client_id'),client_secret=d.get('client_secret'),scopes=d.get('scopes'))
    if not c.valid:c.refresh(Request())
    g=build('gmail','v1',credentials=c,cache_discovery=False)
    if g.users().getProfile(userId='me').execute().get('emailAddress','').lower()!=address:raise RuntimeError('Gmail account mismatch')
    return g

def send_email(subject,body):
    m=EmailMessage();m['From']=SENDER;m['To']=RECIPIENT;m['Subject']=subject;m.set_content(body)
    r=service(SENDER).users().messages().send(userId='me',body={'raw':base64.urlsafe_b64encode(m.as_bytes()).decode()}).execute(num_retries=0)
    if not r.get('id'):raise RuntimeError('Gmail supplied no message receipt')
    return r['id']

def verify_test():
    g=service(RECIPIENT)
    rows=g.users().messages().list(userId='me',q=f'in:anywhere subject:"{TEST_SUBJECT}" newer_than:1d',maxResults=5).execute().get('messages',[])
    for row in rows:
        r=g.users().messages().get(userId='me',id=row['id'],format='raw').execute()
        msg=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(r['raw']))
        body=msg.get_body(preferencelist=('plain',)).get_content().strip()
        if parseaddr(str(msg['From']))[1].lower()==SENDER and parseaddr(str(msg['To']))[1].lower()==RECIPIENT and str(msg['Subject'])==TEST_SUBJECT and ' '.join(body.split())==TEST_BODY:
            return {'received':True,'in_inbox':'INBOX' in r.get('labelIds',[]),'from':str(msg['From']),'to':str(msg['To']),'subject':str(msg['Subject']),'body_matches':True,'message_id':row['id']}
    return {'received':False}

if __name__=='__main__':
    try:
        if sys.argv[1:] == ['--test']:result={'message_id':send_email(TEST_SUBJECT,TEST_BODY)}
        elif sys.argv[1:]==['--verify-test']:result=verify_test()
        else:
            v=json.load(sys.stdin);result={'message_id':send_email(v['subject'],v['body'])}
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({'error':type(e).__name__}),file=sys.stderr);sys.exit(1)
