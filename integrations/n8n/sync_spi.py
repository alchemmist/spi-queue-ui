#!/usr/bin/env python3
"""Render SPI workflows. Active workflows require explicit operator deactivation."""
import copy
import json
from urllib.parse import urlencode
from n8n_api import api, BASE, CONFIG

def render(template,mapping):
    w=copy.deepcopy(template)
    aliases={'SPI_WORKER_CREDENTIAL_ID':'worker','ALERT_INGRESS_CREDENTIAL_ID':'ingress','SOCRAT_BOT_CREDENTIAL_ID':'bot'}
    for n in w['nodes']:
        for kind,c in list(n.get('credentials',{}).items()):
            alias=aliases[c['id']]
            if not mapping.get(alias,{}).get('id'):raise RuntimeError('Missing private SPI credential mapping: '+alias)
            n['credentials'][kind]=mapping[alias]
    return w

def sync(relative):
    mapping=json.loads((CONFIG/'spi-credentials.json').read_text());template=json.loads((BASE/relative).read_text());w=render(template,mapping)
    # Search every page before resolving identity. SPI triage must never be
    # recreated when its original workflow is missing or renamed.
    matches=[]
    cursor=None
    seen=set()
    while True:
        query={'limit':250}
        if cursor:
            query['cursor']=cursor
        page=api('GET','workflows?'+urlencode(query))
        matches.extend(x for x in page['data'] if x['name']==w['name'])
        cursor=page.get('nextCursor')
        if not cursor:
            break
        if cursor in seen:
            raise RuntimeError('Repeated workflow pagination cursor')
        seen.add(cursor)
    if len(matches)>1:raise RuntimeError('Ambiguous workflow name')
    if relative=='integrations/n8n/triage-workflow.json' and not matches:
        raise RuntimeError('Existing SPI triage workflow not found; refusing creation')
    if matches and matches[0]['active']:raise RuntimeError('Deactivate explicitly before syncing')
    result=api('PUT' if matches else 'POST','workflows/'+matches[0]['id'] if matches else 'workflows',{k:w[k]for k in ['name','nodes','connections','settings']})
    print('Workflow synced inactive:',result['id'],result['name']);return result

if __name__=='__main__':
    import sys
    sync('integrations/n8n/triage-workflow.json' if '--triage' in sys.argv else 'integrations/n8n/alert-workflow.json')
