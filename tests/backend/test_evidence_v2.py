import json
import sys
import unittest
from unittest.mock import Mock,patch
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
from evidence_bundle import redact,prepare_bundle,publish_parts,index_markdown,index_marker,PART_SIZE
from provenance import DeploymentCollector
from telemetry import TelemetryCollector,absolute_window,grafana_explore_url
from core import digest, Failure
from test_triage import event

class ArtifactTests(unittest.TestCase):
    def bundle(self,text='diagnostic'):
        return prepare_bundle(event(),{'window':{'start':1,'end':2}},[{'name':'logs.json','content':text,'source':{'query':'test'}}])
    def test_recursive_redaction_keeps_topology(self):
        raw={'password':'very confidential','token':'abcdef','stack':'host=10.250.0.1 api_key="two words"\nCookie: foo=bar; session=abc\nAuthorization: Bearer abcdef\npostgres://user:pass@db/foo','pem':'-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----','safe':'socrat-1'}
        value=json.dumps(redact(raw));self.assertNotIn('two words',value);self.assertNotIn('foo=bar',value);self.assertNotIn('user:pass',value);self.assertNotIn('very confidential',value);self.assertIn('10.250.0.1',value);self.assertIn('socrat-1',value);self.assertNotIn('BEGIN RSA',value)
    def test_known_exact_secret(self):self.assertEqual(redact('a super-secret-value b',['super-secret-value']),'a [REDACTED] b')
    def test_deterministic_and_manifest(self):
        a=self.bundle();self.assertEqual(a,self.bundle());self.assertIn('sha256',a['manifest']['artifacts'][0]);self.assertEqual(a['parts'][0]['id'],'MANIFEST')
    def test_explicit_truncation_and_parts(self):
        b=self.bundle('x'*100000);self.assertTrue(b['manifest']['artifacts'][0]['truncated']);self.assertTrue(all(len(x['text'])<=PART_SIZE for x in b['parts']))
    def test_no_arbitrary_links(self):
        b=self.bundle();b['manifest']['links']=[{'title':'evil','url':'https://evil.example'}];self.assertNotIn('evil',index_markdown(b))
    def test_idempotent_parts(self):
        g=Mock();g.comments.return_value=[];g.request.return_value={'html_url':'https://github.com/a/b/issues/1#issuecomment-1'};b=self.bundle();state={};save=Mock()
        publish_parts(g,9,b,state,save);n=g.request.call_count;publish_parts(g,9,b,state,save);self.assertEqual(g.request.call_count,n)
    def test_lost_response_reconciles(self):
        g=Mock();g.comments.return_value=[];g.request.side_effect=Failure('UPSTREAM_TIMEOUT');b=self.bundle();state={};save=Mock()
        with self.assertRaises(Failure):publish_parts(g,9,b,state,save)
        marker=state['pending'];g.comments.return_value=[{'body':marker,'user':{'login':'renamed-socrat[bot]','type':'Bot'},'performed_via_github_app':{'id':5569869},'html_url':'https://github.com/test'}];g.request.side_effect=None;g.request.return_value={'html_url':'https://github.com/test'}
        publish_parts(g,9,b,state,save);self.assertTrue(state['index_done']);self.assertEqual(g.request.call_count,len(b['parts'])+1)
    def test_ambiguous_not_blindly_retried(self):
        g=Mock();g.comments.return_value=[];g.request.side_effect=Failure('HTTP_500');b=self.bundle();state={};save=Mock()
        with self.assertRaises(Failure):publish_parts(g,9,b,state,save)
        with self.assertRaisesRegex(Failure,'WRITE_NEEDS_REVIEW'):publish_parts(g,9,b,state,save)
        self.assertEqual(g.request.call_count,1)
    def test_index_ambiguous_reconciles(self):
        b=self.bundle();state={'done':{p['marker']:'url' for p in b['parts']},'index_pending':True};g=Mock();g.comments.return_value=[{'user':{'login':'renamed-socrat[bot]','type':'Bot'},'performed_via_github_app':{'id':5569869},'body':index_marker(b)}]
        publish_parts(g,9,b,state,Mock());g.request.assert_not_called()
    def test_rate_limit_is_retryable_without_ambiguous_intent(self):
        g=Mock();g.comments.return_value=[];g.request.side_effect=Failure('HTTP_429');progress={}
        with self.assertRaises(Failure):publish_parts(g,9,self.bundle(),progress,Mock())
        self.assertNotIn('pending',progress)
    def test_untrusted_marker_cannot_hide_missing_artifact(self):
        b=self.bundle();g=Mock();g.comments.return_value=[{'body':b['parts'][0]['marker'],'user':{'login':'unknown'}}];g.request.return_value={}
        publish_parts(g,9,b,{},Mock());self.assertEqual(g.request.call_count,len(b['parts'])+1)
    def test_fence_injection_remains_data(self):
        b=self.bundle('```\nmalicious\n````');g=Mock();g.comments.return_value=[];g.request.return_value={};publish_parts(g,9,b,{},Mock());self.assertIn('````text',g.request.call_args_list[1].args[2]['body']);self.assertIn('\\u0060',g.request.call_args_list[1].args[2]['body'])

class TelemetryTests(unittest.TestCase):
    def response(self,method,url,*args,**kw):
        if '/api/search?' in url:return {'traces':[{'traceID':'a'*32}]}
        if '/api/traces/' in url:return {'resourceSpans':[{'scopeSpans':[{'spans':[{'traceId':'a'*32,'spanId':'b'*16,'name':'request','status':{'code':2}}]}]}]}
        if '/query_range?' in url:return {'status':'success','data':{'resultType':'streams','result':[{'stream':{'pod':'test'},'values':[['1793710800000000000','error trace_id='+'a'*32]]}]}}
        return {'data':{'result':[]}}
    def collect(self,labels=None):
        h=Mock();h.request.side_effect=self.response;e=event();e['labels'].update(labels or {})
        with patch('telemetry.time.time',return_value=1793711400):r=TelemetryCollector(h,'http://grafana','fake').collect(e,{})
        return h,r
    def test_log_trace_discovery_and_bounds(self):
        h,r=self.collect({'pod':'test-pod','service':'test'});self.assertTrue(any('tempo-trace' in a['name']for a in r['artifacts']));self.assertLessEqual(h.request.call_count,10);self.assertTrue(r['links'])
    def test_unsafe_pod_does_not_widen(self):
        h,r=self.collect({'pod':'bad"} |= "secret'});self.assertFalse(any('/query_range?'in x.args[1]for x in h.request.call_args_list));self.assertTrue(any(x['reason']=='UNSAFE_LABEL'for x in r['missing']))
    def test_links_absolute_decodable(self):
        url=grafana_explore_url('https://grafana.socrat.alchemmist.xyz','prometheus','up{job="x"}',10,20,'expr');q=parse_qs(urlsplit(url).query);pane=json.loads(q['panes'][0])['spi'];self.assertEqual(pane['range'],{'from':'10000','to':'20000'});self.assertEqual(pane['queries'][0]['expr'],'up{job="x"}')
    def test_window_capping(self):
        w=absolute_window('2026-11-03T00:00:00Z',None,1793711400);self.assertLessEqual(w['end']-w['start'],3600);self.assertTrue(w['span_capped'])
    def test_failed_source_not_empty_success(self):
        h=Mock();h.request.side_effect=Failure('HTTP_403')
        with patch('telemetry.time.time',return_value=1793711400):r=TelemetryCollector(h,'http://grafana','fake').collect(event(),{})
        self.assertTrue(any(x['reason']=='HTTP_403'for x in r['missing']))
    def test_trace_exception_events_and_resource_version(self):
        from telemetry import _project_trace
        raw={'resourceSpans':[{'resource':{'attributes':[{'key':'service.version','value':{'stringValue':'v1.2'}}]},'scopeSpans':[{'spans':[{'traceId':'a'*32,'events':[{'name':'exception','attributes':[{'key':'exception.stacktrace','value':{'stringValue':'Traceback: real error'}},{'key':'password','value':{'stringValue':'hidden'}}]}]}]}]}]}
        value=_project_trace(raw,'a'*32);self.assertEqual(value['spans'][0]['resource_context']['service.version'],'v1.2');self.assertIn('Traceback: real error',json.dumps(value));self.assertNotIn('hidden',json.dumps(value))
    def test_no_data_explicit(self):
        h=Mock();h.request.return_value={'status':'success','data':{'resultType':'streams','result':[]}}
        with patch('telemetry.time.time',return_value=1793711400):r=TelemetryCollector(h,'http://grafana','fake').collect(event(),{})
        self.assertTrue(any(x['reason']=='NO_MATCHING_LINES'for x in r['missing']))

class ProvenanceTests(unittest.TestCase):
    def test_project_does_not_export_env_or_secret(self):
        obj={'kind':'Pod','metadata':{'name':'test','annotations':{'secret':'hidden'}},'spec':{'containers':[{'name':'app','image':'image:tag','env':[{'name':'PASSWORD','value':'hidden'}]}]},'status':{'containerStatuses':[{'name':'app','imageID':'containerd://sha256:'+'a'*64,'state':{'terminated':{'exitCode':1,'message':'hidden'}}}]}}
        data=DeploymentCollector.project(obj);self.assertNotIn('hidden',json.dumps(data));self.assertIn('imageID',json.dumps(data))
    def test_owner_chain_and_digest(self):
        reader=Mock();reader.side_effect=[{'kind':'Pod','metadata':{'name':'pod','ownerReferences':[{'kind':'ReplicaSet','name':'rs'}]},'status':{'containerStatuses':[{'name':'app','imageID':'digest'}]},'spec':{'containers':[{'name':'app','image':'img'}]}},{'kind':'ReplicaSet','metadata':{'name':'rs','ownerReferences':[{'kind':'Deployment','name':'dep'}]}},{'kind':'Deployment','metadata':{'name':'dep','annotations':{'deployment.kubernetes.io/revision':'3'}}}]
        e=event();e['labels']['pod']='pod';r=DeploymentCollector(reader=reader).collect(e);self.assertEqual(reader.call_count,3);self.assertEqual(len(r['artifacts'][0]['content']['workloads'][0]['owner_chain']),2)
    def test_invalid_scope_no_read(self):
        reader=Mock();e=event();e['labels']['namespace']='../secrets';r=DeploymentCollector(reader=reader).collect(e);reader.assert_not_called();self.assertEqual(r['missing'][0]['reason'],'INVALID_WORKLOAD_SCOPE')
    def test_systemd_not_exporter_pod(self):
        reader=Mock();e=event();e['labels']['name']='otel.service';DeploymentCollector(reader=reader).collect(e);reader.assert_not_called()
    def test_immutable_gitops_digest_mapping(self):
        import base64
        digest='sha256:'+'b'*64;revision='c'*40;ref='d'*40
        text='image:\n  repository: harbor.socrat.alchemmist.xyz/homelab/socrat-bot\n  tag: '+revision+'\n  digest: '+digest+'\n'
        g=Mock();g.request.side_effect=[[{'sha':ref}],{'encoding':'base64','size':len(text),'content':base64.b64encode(text.encode()).decode()}]
        h=Mock();h.request.return_value={'sha':revision}
        r=DeploymentCollector(h,g).gitops_source('homelab/socrat-bot',{'image':'harbor.socrat.alchemmist.xyz/homelab/socrat-bot@'+digest,'imageID':'child-digest'})
        self.assertEqual(r['content']['declared_source_revision'],revision);self.assertFalse(r['content']['source_equivalence_proven']);self.assertIn(ref,r['content']['release_manifest_url'])
    def test_different_digest_cannot_claim_main_is_deployed(self):
        import base64
        text='image:\n  repository: harbor.socrat.alchemmist.xyz/homelab/socrat-bot\n  tag: '+'c'*40+'\n  digest: sha256:'+'d'*64+'\n'
        g=Mock();g.request.side_effect=[[{'sha':'e'*40}],{'encoding':'base64','size':len(text),'content':base64.b64encode(text.encode()).decode()}]
        h=Mock();r=DeploymentCollector(h,g).gitops_source('homelab/socrat-bot',{'image':'harbor.socrat.alchemmist.xyz/homelab/socrat-bot@sha256:'+'b'*64})
        self.assertIsNone(r);h.request.assert_not_called()
    def test_source_excerpt_is_pinned_and_traversal_rejected(self):
        import base64
        ref='c'*40;h=Mock();h.request.return_value={'encoding':'base64','size':20,'content':base64.b64encode(b'line one\nline two').decode(),'sha':'blob'}
        proof={'artifacts':[{'name':'deployed-source-gitops.json','content':{'declared_source_revision':ref,'source_commit_url':'https://github.com/alchemmist/socrat-bot/tree/'+ref}}]}
        r=DeploymentCollector(h).source_fragments(proof,{'artifacts':[{'content':'File /app/socrat_bot/main.py, line 2; socrat_bot/../secret.py'}]})
        self.assertEqual(h.request.call_count,1);self.assertIn('?ref='+ref,h.request.call_args.args[1]);self.assertIn('line two',r['artifacts'][0]['content']['excerpt'])
    def test_absent_identity_explicit(self):
        e=event();e['labels']['pod']='app'
        with patch('provenance.ROOT',Path('/nonexistent')):r=DeploymentCollector().collect(e)
        self.assertEqual(r['missing'][0]['reason'],'KUBERNETES_READ_IDENTITY_MISSING')


class EngineArtifactTests(unittest.TestCase):
    def test_primary_is_not_repeated_after_artifact_timeout(self):
        import tempfile
        from core import Store
        from service import Engine
        from test_triage import report
        with tempfile.TemporaryDirectory()as folder:
            store=Store(Path(folder)/'db');g=Mock();g.snapshot.return_value=[];g.open_signature.return_value=digest([]);g.comments.return_value=[]
            g.request.side_effect=[{'number':9},Failure('UPSTREAM_TIMEOUT')]
            model=Mock();model.match.return_value={'candidates':[]};model.analyze.return_value=report()
            evidence=Mock();e=event();bundle=prepare_bundle(e,{},[{'name':'test.json','content':{'observation':'real fixture'}}]);evidence.collect.return_value={'bundle':bundle}
            engine=Engine(store,g,model,evidence,write=True);store.enqueue(e);key=store.claim()['job_id']
            for stage in ('match','collect','analyze'):engine.run(stage,key)
            with self.assertRaises(Failure):engine.run('publish',key)
            saved=store.get(key);marker=saved['result']['artifact_publication']['pending']
            self.assertTrue(json.loads(saved['intent'])['primary_done'])
            g.comments.return_value=[{'body':marker,'user':{'login':'renamed-socrat[bot]','type':'Bot'},'performed_via_github_app':{'id':5569869},'html_url':'https://github.com/comment'}]
            g.request.side_effect=None;g.request.return_value={'html_url':'https://github.com/comment'}
            with store.db()as c:c.execute('UPDATE jobs SET available=0,lease=0')
            store.claim();engine.run('publish',key)
            self.assertEqual(store.get(key)['state'],'complete');g.reconcile.assert_not_called()
            self.assertEqual(sum(x.args[1]=='/issues'for x in g.request.call_args_list),1)

class DownloadTests(unittest.TestCase):
    def test_roundtrip_and_checksum(self):
        import importlib.util
        path=Path(__file__).resolve().parents[2]/'integrations/evidence/download.py';spec=importlib.util.spec_from_file_location('spi_download',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        e=event();bundle=prepare_bundle(e,{},[{'name':'logs.json','content':'line one\nline two ` data'}]);g=Mock();g.comments.return_value=[];g.request.return_value={'html_url':'https://github.com/comment'}
        publish_parts(g,9,bundle,{},Mock());comments=[{'body':x.args[2]['body'],'user':{'login':'renamed-socrat[bot]','type':'Bot'},'performed_via_github_app':{'id':5569869}}for x in g.request.call_args_list]
        files=module.reconstruct(comments,e['event_key']);self.assertEqual(json.loads(files['E001-logs.json']),'line one\nline two ` data')
        comments[1]['body']=comments[1]['body'].replace('line one','tampered')
        with self.assertRaisesRegex(ValueError,'CHECKSUM'):module.reconstruct(comments,e['event_key'])
    def test_primary_and_all_parts_have_bounded_bodies(self):
        bundle=prepare_bundle(event(),{},[{'name':'test.json','content':'`'*100000}]);g=Mock();g.comments.return_value=[];g.request.return_value={};publish_parts(g,9,bundle,{},Mock())
        self.assertTrue(all(len(x.args[2]['body'])<65536 for x in g.request.call_args_list))

class ModelEvidenceTests(unittest.TestCase):
    def test_facts_require_valid_existing_evidence_ids(self):
        from integrations import Model
        from core import REPORT_SCHEMA
        from test_triage import report
        import time
        bad=report();bad['facts']=['A guessed fact [E999]'];good=report();good['facts']=['An alert fired [E001].']
        h=Mock();h.request.side_effect=[{'choices':[{'message':{'content':json.dumps(bad)}}]},{'choices':[{'message':{'content':json.dumps(good)}}]}]
        result=Model(h,'fake',min_interval=0,store=Mock()).call('analyze',{'evidence':{'artifact_context':{'index':[{'id':'E001'}]}}},REPORT_SCHEMA,time.monotonic()+20)
        self.assertEqual(result,good)
        self.assertEqual(h.request.call_count,2)


class PrimaryReconciliationTests(unittest.TestCase):
    def test_issue_without_app_metadata_uses_immutable_bot_id(self):
        from evidence_bundle import trusted_comment
        self.assertTrue(trusted_comment({'user':{'id':351189559,'type':'Bot','login':'renamed'}}))
        self.assertFalse(trusted_comment({'user':{'id':123,'type':'Bot','login':'socrat-bot[bot]'}}))

    def test_primary_marker_requires_actual_app_author(self):
        from integrations import GitHub
        g=GitHub(Mock(),'/unused');g.comments=Mock(return_value=[{'body':'marker','user':{'type':'User'}}])
        self.assertEqual(g.reconcile('marker',9),0)
        g.comments.return_value=[{'body':'marker','user':{'type':'Bot'},'performed_via_github_app':{'id':5569869}}]
        self.assertEqual(g.reconcile('marker',9),9)

class SQLiteTempStoreTests(unittest.TestCase):
    def test_all_connections_use_memory_temp_store(self):
        import tempfile
        from core import Store
        with tempfile.TemporaryDirectory()as d:
            store=Store(Path(d)/'db')
            with store.db()as c:self.assertEqual(c.execute('pragma temp_store').fetchone()[0],2)
            store.enqueue(event());store.claim();self.assertEqual(store.recover_on_start(),1)
            self.assertEqual(store.health()['counts'].get('retry'),1)

if __name__=='__main__':unittest.main()

class GrafanaLoginLinkTests(unittest.TestCase):
    def test_generator_requests_native_login_and_keeps_absolute_state(self):
        for uid,field,query in [('prometheus','expr','up{job="x"}'),('loki','expr','{unit="a.service"} |= "error"'),('tempo','query','{resource.service.name="socrat-bot"}')]:
            url=grafana_explore_url('https://grafana.socrat.alchemmist.xyz',uid,query,10,20,field)
            params=parse_qs(urlsplit(url).query)
            self.assertEqual(params['forceLogin'],['true'])
            self.assertTrue(url.endswith('&forceLogin=true'))
            pane=json.loads(params['panes'][0])['spi']
            self.assertEqual(pane['queries'][0][field],query)
            self.assertEqual(pane['range'],{'from':'10000','to':'20000'})
    def test_cached_manifest_is_not_mutated(self):
        b=ArtifactTests().bundle()
        old=grafana_explore_url('https://grafana.socrat.alchemmist.xyz','tempo','a'*32,10,20,'query').replace('&forceLogin=true','')
        b['manifest']['links']=[{'title':'Trace','url':old}]
        from copy import deepcopy
        snapshot=deepcopy(b)
        self.assertIn('forceLogin=true',index_markdown(b))
        self.assertEqual(b,snapshot)
    def test_repair_is_idempotent_and_does_not_touch_artifact_json(self):
        from grafana_links import repair_markdown_links
        url=grafana_explore_url('https://grafana.socrat.alchemmist.xyz','loki','{pod="test"}',10,20,'expr').replace('&forceLogin=true','')
        body='[Logs]('+url+')\n```json\n'+json.dumps({'original_url':url})+'\n```'
        repaired=repair_markdown_links(body)
        self.assertEqual(repaired,repair_markdown_links(repaired))
        self.assertIn(json.dumps({'original_url':url}),repaired)
        self.assertIn('[Logs]('+url+'&forceLogin=true)',repaired)
    def test_external_origin_unchanged_and_duplicate_flags_removed(self):
        from grafana_links import require_explore_login
        evil='https://grafana.socrat.alchemmist.xyz.evil.example/explore?forceLogin=false'
        self.assertEqual(require_explore_login(evil),evil)
        url='https://grafana.socrat.alchemmist.xyz/explore?orgId=1&forceLogin=false&forceLogin=true'
        self.assertEqual(parse_qs(urlsplit(require_explore_login(url)).query)['forceLogin'],['true'])
