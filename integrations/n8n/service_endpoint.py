"""Validate the Git-owned Service VIP before native workflow enrollment.

No DNS monkeypatch, external IP, Pod IP, credential transport or HTTP retry.
The Helm service address must be pinned to this existing VIP before activation.
Service recreation/selector/address drift requires explicit operator review.
"""
import ipaddress,json
from pathlib import Path
CONTRACT=Path(__file__).with_name('worker-service.json')
PATHS={'Claim Triage Job':'claim','Match Open SPI':'match','Collect Evidence':'collect','Analyze Incident':'analyze','Publish SPI':'publish'}

def validate_service(service,contract=None):
    c=contract or json.loads(CONTRACT.read_text());m=service.get('metadata',{});s=service.get('spec',{})
    ip=ipaddress.ip_address(c['cluster_ip'])
    expected=[{'name':'http','port':8080,'protocol':'TCP','targetPort':'http'}]
    if (c.get('contract')!=1 or ip not in ipaddress.ip_network('10.43.0.0/16') or c.get('port')!=8080
        or any(m.get(k)!=c[k]for k in ('namespace','name','uid'))or s.get('clusterIP')!=str(ip)
        or s.get('type')!='ClusterIP'or s.get('selector')!={'app':'spi-triage'}or s.get('ports')!=expected):
        raise ValueError('SPI_SERVICE_IDENTITY_MISMATCH')
    return 'http://'+str(ip)+':8080'

def validate_workflow(workflow,base):
    nodes={n['name']:n for n in workflow['nodes']}
    for name,path in PATHS.items():
        n=nodes[name]
        if (n['type']!='n8n-nodes-base.httpRequest'or n['parameters']['url']!=base+'/'+path
            or n['parameters']['method']!='POST'or n.get('retryOnFail',False)):
            raise ValueError('SPI_ENDPOINT_CONTRACT_MISMATCH')
    return True
