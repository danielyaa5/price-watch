"""Slack delivery for Price Watch phone alerts (the 'ntfy' channel key is kept for stored watch settings).

The incoming webhook lives only in ~/.config/slack-alerts/webhooks.env on the Mac, never in this repo:
SLACK_WEBHOOK_HOME_SERVER (the home-server alerts channel). 'alerts' posts start with @channel; 'fyi' posts are quiet.
"""
import json, re, socket, urllib.request
from datetime import datetime, timezone
from pathlib import Path

WEBHOOKS = Path.home()/'.config/slack-alerts/webhooks.env'
HOOK = re.compile(r'https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+')

def webhook(channel='fyi',path=None):
    values={}
    try:
        for line in Path(path or WEBHOOKS).read_text().splitlines():
            key,sep,value=line.strip().partition('=')
            if sep and not key.startswith('#'):values[key.strip()]=value.strip().strip('"\'')
    except OSError:pass
    url=values.get('SLACK_WEBHOOK_HOME_SERVER') or values.get('SLACK_WEBHOOK_ALERTS','')
    if not HOOK.fullmatch(url):raise RuntimeError(f'No Slack webhook for {channel} in {path or WEBHOOKS}')
    return url

def esc(text):return str(text).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

def post(title,message,channel='fyi',path=None,opener=urllib.request.urlopen):
    """Post one message; returns a local receipt once Slack answers 'ok' (webhooks give no message id)."""
    mention='<!channel> ' if channel=='alerts' else ''
    text=f"{mention}*{esc(title)}*\n{esc(message)}\n_Price Watch · {esc(socket.gethostname().split('.')[0])}_"
    request=urllib.request.Request(webhook(channel,path),data=json.dumps({'text':text,'unfurl_links':False}).encode(),
                                   headers={'Content-Type':'application/json'},method='POST')
    with opener(request,timeout=15) as response:answer=response.read(200).decode(errors='replace').strip()
    if answer!='ok':raise RuntimeError(f'Slack did not accept the alert: {answer[:80]}')
    return f"slack-{channel}:{datetime.now(timezone.utc).isoformat(timespec='milliseconds')}"
