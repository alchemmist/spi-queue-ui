import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from core import digest, Failure, Store, REPORT_SCHEMA, clean, normalize, render, validate
from integrations import GitHub, Model, Evidence, metric_summary
from service import Engine

def event(status='firing',start='2026-11-03T13:00:00Z'):
    return normalize({'source':'test','alert':{'status':status,'startsAt':start,'endsAt':'2026-11-03T13:01:00Z' if status=='resolved' else '',
                    'fingerprint':'abc','labels':{'alertname':'TestAlert','namespace':'test'},'annotations':{'summary':'Test alert'}}})
def report(number=0):
    return {'title':'[SPI] Test service interruption','summary':'A service symptom requires investigation.',
            'severity':'unknown','confidence':'low','issue_number':number,'match_reason':'No established shared root cause.',
            'impact':'User impact has not been established.','facts':['An alert fired.'],
            'hypotheses':['A transient dependency failure is possible.'],'alert_quality':'insufficient_evidence',
            'alert_assessment':'Recovery alone does not prove a false positive.','service_actions':['Inspect the dependency.'],
            'monitoring_actions':['Measure duration before changing the threshold.'],'missing_evidence':['Request success metrics.']}

class CoreTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'jobs.sqlite')
    def tearDown(self):self.tmp.cleanup()
    def test_duplicate_delivery_is_not_new_job(self):
        self.assertFalse(self.store.enqueue(event())['duplicate']);self.assertTrue(self.store.enqueue(event())['duplicate'])
        self.assertEqual(self.store.get(event()['event_key'])['deliveries'],2)
    def test_new_episode_and_resolve_are_distinct(self):
        self.assertNotEqual(event()['event_key'],event('resolved')['event_key'])
        self.assertNotEqual(event()['episode'],event(start='2026-11-03T14:00:00Z')['episode'])
        self.assertEqual(event()['episode'],event('resolved')['episode'])
    def test_claim_is_exclusive_and_survives_restart(self):
        self.store.enqueue(event());first=self.store.claim();second=Store(self.store.path).claim()
        self.assertTrue(first['job_id']);self.assertFalse(second['job_id'])
    def test_expired_lease_is_recovered(self):
        self.store.enqueue(event());first=self.store.claim()
        with self.store.db() as c:c.execute('UPDATE jobs SET lease=0')
        self.assertEqual(first,self.store.claim())
    def test_process_restart_releases_claim_but_preserves_progress(self):
        self.store.enqueue(event());key=self.store.claim()['job_id'];self.store.save(key,1,{'match':{'candidates':[]}})
        restored=Store(self.store.path);self.assertEqual(restored.recover_on_start(),1)
        self.assertEqual(restored.claim()['job_id'],key);self.assertEqual(restored.get(key)['stage'],1)
    def test_group_insert_rolls_back_atomically(self):
        first=event();second=event(start='2026-11-03T14:00:00Z');second.pop('episode')
        with self.assertRaises(KeyError):self.store.enqueue_many([first,second])
        self.assertEqual(self.store.health()['counts'],{})
    def test_invalid_annotations_rejected_before_storage(self):
        with self.assertRaises(Failure):normalize({'alert':{'status':'firing','startsAt':'2026-11-03T13:00:00Z','labels':{'alertname':'Test'},'annotations':[]}})
    def test_bad_timestamp_is_rejected(self):
        with self.assertRaises(Failure):event(start='not a time')
    def test_redaction_precedes_model(self):
        secret='ghp_'+'x'*25
        self.assertNotIn(secret,json.dumps(clean({'password':'private','message':secret,'description':'token=private'})))
        self.assertEqual(clean({'Authorization':'Bearer xyz'})['Authorization'],'[REDACTED]')
    def test_english_contract(self):
        r=report();r['summary']='Ошибка сервиса'
        with self.assertRaisesRegex(Failure,'NON_ENGLISH'):render(r,event())
    def test_strict_schema_and_boolean_target(self):
        r=report();r['issue_number']=True
        with self.assertRaises(Failure):validate(r,REPORT_SCHEMA)
        r=report();r['unexpected']='x'
        with self.assertRaises(Failure):validate(r,REPORT_SCHEMA)
    def test_publication_marker_and_prefix(self):
        r=report();r['title']='Service issue';title,body=render(r,event())
        self.assertTrue(title.startswith('[SPI] '));self.assertIn(event()['event_key'],body)
        self.assertIn('## Alert quality',body)
    def test_reject_model_secret(self):
        r=report();r['summary']='Authorization: Bearer ghp_'+'x'*25
        with self.assertRaises(Failure):render(r,event())

class AdapterTests(unittest.TestCase):
    def test_github_paginates_all_open_and_excludes_pr(self):
        http=Mock();http.request.side_effect=[[
            {'number':i,'title':'[SPI] Test','state':'open','updated_at':'first'} for i in range(1,21)],
            [{'number':101,'title':'[SPI] Last','updated_at':'first'},{'number':102,'title':'[SPI] PR','pull_request':{}}]]
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('fake-token');f.flush();items=GitHub(http,f.name).issues()
        self.assertEqual(len(items),21)
        self.assertIn('state=open',http.request.call_args_list[0].args[1]);self.assertIn('page=2',http.request.call_args_list[1].args[1])
    def test_model_matches_every_batch(self):
        model=Model(Mock(),'fake');model.call=Mock(return_value={'issue_number':0,'reason':'Different','confidence':'low'})
        issues=[{'number':i}for i in range(25)];r=model.match(event(),issues)
        self.assertEqual(model.call.call_count,3);self.assertEqual(r['reviewed_numbers'],list(range(25)))
    def test_invented_issue_is_rejected(self):
        model=Model(Mock(),'fake');model.call=Mock(return_value={'issue_number':999,'reason':'Same','confidence':'high'})
        with self.assertRaisesRegex(Failure,'INVALID_MATCH_TARGET'):model.match(event(),[{'number':1}])
    def test_schema_retry_is_bounded(self):
        http=Mock();http.request.return_value={'choices':[{'message':{'content':'not json'}}]}
        model=Model(http,'fake',min_interval=0,store=Mock())
        with self.assertRaises(Failure):model.match(event(),[])
        self.assertEqual(http.request.call_count,1)

class EvidenceTests(unittest.TestCase):
    def test_metric_summary_preserves_transient_peak_and_empty_data(self):
        data={'data':{'resultType':'matrix','result':[{'metric':{'name':'example'},'values':[[1,'0'],[2,'7'],[3,'0']]}]}}
        result=metric_summary(data);self.assertEqual(result['series'][0]['maximum'],7);self.assertEqual(result['series'][0]['last']['value'],0)
        self.assertNotIn('values',result['series'][0]);self.assertIn('do not establish',result['limitation'])
    def test_http_200_observability_error_is_not_evidence(self):
        http=Mock();http.request.return_value={'status':'error','error':'not secret'}
        with self.assertRaisesRegex(Failure,'OBSERVABILITY_QUERY_ERROR'):Evidence(http,'fake','http://grafana').get('/api/test')
    def test_host_profile_and_no_raw_trace_attributes(self):
        http=Mock()
        def request(method,url,*args,**kwargs):
            if '/api/v1/query?' in url:return {'data':{'result':[{'metric':{'nodename':'socrat-0'}}]}}
            if '/api/traces/' in url:return {'resourceSpans':[{'scopeSpans':[{'spans':[{'status':{'code':2},'attributes':[{'key':'password','value':'do-not-publish'}]}]}]}]}
            return {'data':{}}
        http.request.side_effect=request
        e=event();e['labels'].update(instance='test:9100',name='example.service',trace_id='a'*32)
        data=Evidence(http,'fake','http://grafana').collect(e)
        sources=[q['source']for q in data['queries']]
        self.assertIn('node_identity',sources);self.assertIn('host_log_error_frequency',sources);self.assertIn('tempo_trace_summary',sources)
        self.assertNotIn('do-not-publish',json.dumps(data));self.assertNotIn('attributes',json.dumps(data['queries']))
    def test_repository_reference_is_not_deployed_proof(self):
        import base64
        http=Mock();http.request.return_value={'encoding':'base64','content':base64.b64encode(b'# Safe runbook').decode(),'sha':'abc','size':14}
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('fake');f.flush();g=GitHub(http,f.name)
            self.assertIsNone(g.source_reference('https://evil.example/secret'))
            reference=g.source_reference('https://github.com/alchemmist/homelab/blob/main/docs/n8n/spi/setup.md')
        self.assertFalse(reference['deployed_version_verified']);self.assertEqual(reference['ref'],'main')

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db');self.github=Mock();self.github.snapshot.return_value=[];self.github.open_signature.side_effect=lambda:digest([(x['number'],x['updated_at']) for x in self.github.snapshot.return_value])
        self.model=Mock();self.model.match.return_value={'candidates':[]};self.model.analyze.return_value=report()
        self.evidence=Mock();self.evidence.collect.return_value={'missing':['logs']}
        self.engine=Engine(self.store,self.github,self.model,self.evidence,write=False)
    def tearDown(self):self.tmp.cleanup()
    def process(self,e=None,through=4):
        key=self.store.enqueue(e or event())['job_id'];self.store.claim()
        for stage in ['match','collect','analyze','publish'][:through]:self.engine.run(stage,key)
        return key
    def test_dry_run_never_writes(self):
        key=self.process();self.assertEqual(self.store.get(key)['state'],'dry_run');self.github.request.assert_not_called()
    def test_new_issue(self):
        self.engine.write=True;self.github.request.return_value={'number':7};key=self.process()
        self.assertEqual(self.store.get(key)['issue'],7);self.assertEqual(self.github.request.call_args.args[1],'/issues')
    def test_existing_issue_adds_comment(self):
        self.engine.write=True;self.model.match.return_value={'candidates':[{'number':7}]};self.model.analyze.return_value=report(7);key=self.process()
        self.assertEqual(self.github.request.call_args.args[1],'/issues/7/comments')
    def test_ambiguous_write_is_reconciled_not_repeated(self):
        self.engine.write=True;key=self.process(through=3);self.github.request.side_effect=Failure('UPSTREAM_NETWORK_ERROR')
        with self.assertRaises(Failure):self.engine.run('publish',key)
        self.github.reconcile.return_value=7
        with self.store.db() as c:c.execute('UPDATE jobs SET available=0,lease=0')
        self.store.claim();self.engine.run('publish',key)
        self.assertEqual(self.github.request.call_count,1);self.assertEqual(self.store.get(key)['issue'],7)
    def test_unreconciled_write_requires_review(self):
        self.engine.write=True;key=self.process(through=3);self.github.request.side_effect=Failure('HTTP_500')
        with self.assertRaises(Failure):self.engine.run('publish',key)
        self.github.reconcile.return_value=0
        with self.store.db() as c:c.execute('UPDATE jobs SET available=0,lease=0')
        self.store.claim()
        with self.assertRaisesRegex(Failure,'WRITE_NEEDS_REVIEW'):self.engine.run('publish',key)
        self.assertEqual(self.store.get(key)['state'],'dead');self.assertEqual(self.github.request.call_count,1)
    def test_changed_open_issues_restarts_matching(self):
        self.engine.write=True;key=self.process(through=3);self.github.snapshot.return_value=[{'number':3,'updated_at':'later'}]
        with self.assertRaisesRegex(Failure,'OPEN_SPI_CHANGED'):self.engine.run('publish',key)
        self.assertEqual(self.store.get(key)['stage'],0);self.github.request.assert_not_called()
    def test_resolved_does_not_create_issue(self):
        self.engine.write=True;key=self.process(event('resolved'));self.github.request.assert_not_called();self.model.match.assert_not_called()
    def test_resolved_comments_but_does_not_close(self):
        self.engine.write=True;self.github.request.return_value={'number':7};first=self.process()
        self.github.reset_mock();self.github.request.return_value={'state':'open'};self.process(event('resolved'))
        self.assertEqual(self.github.request.call_args.args[0:2],('POST','/issues/7/comments'))
        self.assertNotIn('state',self.github.request.call_args.args[2])
    def test_closed_issue_not_reopened_on_resolution(self):
        self.engine.write=True;self.github.request.return_value={'number':7};self.process()
        self.github.reset_mock();self.github.request.return_value={'state':'closed'};self.process(event('resolved'))
        self.assertEqual(self.github.request.call_count,1)
    def test_saved_stages_are_idempotent(self):
        key=self.process(through=2);self.engine.run('match',key);self.engine.run('collect',key)
        self.assertEqual(self.model.match.call_count,1);self.assertEqual(self.evidence.collect.call_count,1)

if __name__=='__main__':unittest.main()
