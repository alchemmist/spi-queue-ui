import copy,importlib.util,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('service_endpoint',ROOT/'integrations/n8n/service_endpoint.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class ServiceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.c=json.loads(module.CONTRACT.read_text());self.s={'metadata':{k:self.c[k]for k in('namespace','name','uid')},'spec':{'clusterIP':self.c['cluster_ip'],'selector':self.c['selector'],'type':'ClusterIP','ports':[{'name':'http','port':8080,'targetPort':'http','protocol':'TCP'}]}}
    def test_native_service_identity_and_source_workflow(self):
        base=module.validate_service(self.s);self.assertEqual(base,'http://10.43.194.185:8080')
        self.assertTrue(module.validate_workflow(json.loads((ROOT/'integrations/n8n/triage-workflow.json').read_text()),base))
    def test_changed_uid_vip_selector_port_and_service_type_fail_closed(self):
        for where,key,value in [('metadata','uid','other'),('spec','clusterIP','10.43.1.2'),('spec','selector',{'app':'other'}),('spec','type','ExternalName'),('spec','ports',[])]:
            s=copy.deepcopy(self.s);s[where][key]=value
            with self.assertRaisesRegex(ValueError,'SPI_SERVICE_IDENTITY_MISMATCH'):module.validate_service(s)
    def test_non_service_address_cannot_be_enrolled(self):
        for ip in('127.0.0.1','10.42.1.2','109.173.40.247'):
            c={**self.c,'cluster_ip':ip}
            with self.assertRaisesRegex(ValueError,'SPI_SERVICE_IDENTITY_MISMATCH'):module.validate_service(self.s,c)
    def test_generic_post_retry_stays_disabled(self):
        w=json.loads((ROOT/'integrations/n8n/triage-workflow.json').read_text());n=next(n for n in w['nodes']if n['name']=='Analyze Incident');n['retryOnFail']=True
        with self.assertRaisesRegex(ValueError,'SPI_ENDPOINT_CONTRACT_MISMATCH'):module.validate_workflow(w,'http://10.43.194.185:8080')
