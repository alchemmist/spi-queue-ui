"""Run next to the existing App secret; export ONLY a repo-scoped one-hour token."""
import base64
import json
import re
from datetime import datetime, timezone
import os
import ssl
import time
import urllib.request
import urllib.error
from pathlib import Path
import jwt


def log_event(event, level='info', **fields):
    # Only operational counters and bounded machine codes, never evidence or IDs.
    allowed = {'stage', 'code', 'phase', 'recovered_jobs', 'oldest_pending_seconds', 'dead_jobs'}
    record = {k: v for k, v in fields.items() if k in allowed}
    for key in ('stage', 'phase', 'code'):
        if key in record:
            value = record[key]
            valid = (isinstance(value, str) and bool(re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', value))) if key == 'code' else value in {'match', 'collect', 'analyze', 'publish', 'sign', 'installation', 'mint', 'publish_secret', 'bootstrap'}
            if not valid:
                record[key] = 'REDACTED'
    record.update(ts=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                  level=level, service='spi-triage', component='spi-token-broker', event=event, msg=event)
    print(json.dumps(record, separators=(',', ':')), flush=True)

PHASE='bootstrap'
class BrokerFailure(Exception):pass

def request(method,url,token,body=None,context=None):
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,method=method,
        headers={'Authorization':'Bearer '+token,'Accept':'application/json' if method=='PATCH' else 'application/vnd.github+json','Content-Type':'application/merge-patch+json' if method=='PATCH' else 'application/json','X-GitHub-Api-Version':'2022-11-28'})
    try:
        with urllib.request.urlopen(req,context=context,timeout=25) as r:return json.load(r)
    except urllib.error.HTTPError as e:raise BrokerFailure('HTTP_'+str(e.code)) from None

def main():
    global PHASE
    PHASE='sign'
    now=int(time.time());auth=jwt.encode({'iat':now-30,'exp':now+300,'iss':os.environ['GITHUB_APP_ID']},os.environ['GITHUB_PRIVATE_KEY'],algorithm='RS256')
    PHASE='installation'
    installation=request('GET','https://api.github.com/repos/alchemmist/homelab/installation',auth)
    if installation.get('suspended_at') or installation['permissions'].get('issues')!='write':raise ValueError('APP_PERMISSION_MISSING')
    PHASE='mint'
    grant=request('POST','https://api.github.com/app/installations/'+str(installation['id'])+'/access_tokens',auth,
                  {'repositories':['homelab'],'permissions':{'issues':'write','contents':'read','metadata':'read'}})
    if grant['permissions'].get('pull_requests') or grant['permissions'].get('issues')!='write':raise ValueError('INVALID_TOKEN_SCOPE')
    root=Path('/var/run/secrets/kubernetes.io/serviceaccount')
    context=ssl.create_default_context(cafile=str(root/'ca.crt'))
    PHASE='publish_secret'
    request('PATCH','https://kubernetes.default.svc/api/v1/namespaces/automation/secrets/spi-github-installation',(root/'token').read_text(),
            {'data':{'token':base64.b64encode(grant['token'].encode()).decode()},'metadata':{'annotations':{'socrat.alchemmist.xyz/expires-at':grant['expires_at']}}},context)
    log_event('refresh_ok')

if __name__=='__main__':
    try:main()
    except Exception as e:
        code=str(e) if isinstance(e,BrokerFailure) else type(e).__name__
        log_event('refresh_failed', 'error', phase=PHASE, code=code);raise SystemExit(1)
