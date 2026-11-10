"""Fixed-topic routing, immutable evidence and no automatic issue creation."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from core import Failure
from integrations import GitHub
from managed_catalog import ManagedCatalog
from test_wait_integration import IntegrationTests
import test_triage as f


class PolicyTests(unittest.TestCase):
    def test_bounded_unique_catalog_and_complete_historical_aliases(self):
        p=ManagedCatalog()
        self.assertEqual(len(p.by_issue),20)
        self.assertEqual(len(p.aliases),152)
        self.assertEqual(p.route(f.event(),previous=164)['issue'],12)
        self.assertEqual(p.route(f.event(),previous=54)['issue'],21)

    def test_empty_overfull_and_duplicate_policies_fail_closed(self):
        with self.assertRaises(Failure):ManagedCatalog({})
        p=copy.deepcopy(ManagedCatalog().policy)
        p['topics']['bad']={'issue':999,'members':[999],'title':'bad'}
        with self.assertRaises(Failure):ManagedCatalog(p)
        p=copy.deepcopy(ManagedCatalog().policy);p['topics']['review']['issue']=12
        with self.assertRaises(Failure):ManagedCatalog(p)

    def test_unknown_critical_incident_has_visible_review_bucket(self):
        e=f.event();e['labels']={'alertname':'NewServiceFailure','severity':'critical'}
        self.assertEqual(ManagedCatalog().route(e)['topic'],'review')

    def test_pod_hash_changes_do_not_create_separate_workstreams(self):
        p=ManagedCatalog()
        for pod in ('harbor-jobservice-abcd-123','harbor-jobservice-xyzt-456'):
            e=f.event();e['labels']={'namespace':'harbor','pod':pod,'alertname':'KubePodNotReady'}
            self.assertEqual(p.route(e)['issue'],25)

    def test_no_post_issues_even_with_query_or_slash_and_before_reading_credentials(self):
        http=Mock();g=GitHub(http,'nonexistent',allow_issue_creation=False)
        for path in ('/issues','/issues/','/issues?x=1'):
            with self.assertRaisesRegex(Failure,'AUTOMATIC_SPI_CREATION_DISABLED'):
                g.request('POST',path,{'title':'[SPI] not allowed'})
        http.request.assert_not_called()


class ManagedFlowTests(IntegrationTests):
    def managed(self):
        self.engine.catalog=ManagedCatalog()
        self.posts=[]
        def request(method,path,body=None):
            if method=='GET':
                number=int(path.split('/')[-1]);key=self.engine.catalog.by_issue[number]
                return {'number':number,'state':'open','title':'[SPI] managed fixture',
                        'body':'<!-- socrat-spi-catalog:v1:'+key+' -->','updated_at':'2026-11-10T00:00:00Z'}
            self.assertTrue(path.endswith('/comments'))
            self.posts.append((path,body))
            return {'id':444,'html_url':'https://github.com/alchemmist/homelab/issues/17#issuecomment-444'}
        self.github.request.side_effect=request

    def test_full_run_has_no_paid_match_or_catalog_scan_and_keeps_report_evidence(self):
        self.managed()
        for stage in ('match','collect','analyze','publish'):
            self.data=self.engine.run_v1(stage,self.data)
        self.assertEqual(self.data['outcome'],'complete')
        self.model.match.assert_not_called();self.model.analyze.assert_called_once()
        self.github.snapshot.assert_not_called();self.github.open_signature.assert_not_called()
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.posts[0][0],'/issues/17/comments')
        self.assertIn('not proven root causes',self.posts[0][1]['body'])
        self.assertIn('A service symptom requires investigation.',self.posts[0][1]['body'])
        self.assertEqual(self.store.get(self.key)['result']['report']['issue_number'],0)
        self.assertEqual(self.store.get(self.key)['issue'],17)
        self.assertEqual(self.store.get(self.key)['attempts'],1)

    def test_closed_or_unmarked_anchor_stops_before_any_paid_analysis(self):
        self.managed();self.github.request.side_effect=None
        for row in ({'number':17,'state':'closed','title':'[SPI] x','body':'<!-- socrat-spi-catalog:v1:review -->'},
                    {'number':17,'state':'open','title':'[SPI] x','body':'wrong'}):
            with self.subTest(row=row):
                self.github.request.return_value=row
                with self.assertRaisesRegex(Failure,'CATALOG_TARGET_UNAVAILABLE'):
                    self.engine.catalog.issue(self.github,self.engine.catalog.route(f.event()))
        self.model.match.assert_not_called();self.model.analyze.assert_not_called()

    def test_topic_comment_churn_does_not_repeat_paid_work(self):
        self.managed()
        for stage in ('match','collect','analyze'):
            self.data=self.engine.run_v1(stage,self.data)
        self.github.open_signature.side_effect=AssertionError('No global snapshot retry allowed')
        self.data=self.engine.run_v1('publish',self.data)
        self.assertEqual(self.data['status'],'complete')
        self.model.analyze.assert_called_once()

    def test_ambiguous_comment_keeps_original_intent_without_new_issue(self):
        self.managed()
        for stage in ('match','collect','analyze'):
            self.data=self.engine.run_v1(stage,self.data)
        original=self.github.request.side_effect
        def fail(method,path,body=None):
            if method=='POST':raise Failure('UPSTREAM_NETWORK_ERROR')
            return original(method,path,body)
        self.github.request.side_effect=fail
        with self.assertRaises(Failure):self.engine.run_v1('publish',self.data)
        self.assertIn('17',self.store.get(self.key)['intent'])
        self.model.analyze.assert_called_once()

    def test_high_impact_probe_is_promoted_without_another_analysis_call(self):
        self.managed()
        import json
        event=self.store.get(self.key)['event'];event['labels']={'alertname':'NewServiceFailure','severity':'warning'}
        with self.store.db() as c:
            c.execute('UPDATE jobs SET event=? WHERE id=?',(json.dumps(event),self.key))
        report=f.report();report['severity']='high';self.model.analyze.return_value=report
        for stage in ('match','collect','analyze','publish'):
            self.data=self.engine.run_v1(stage,self.data)
        row=self.store.get(self.key)
        self.assertEqual(row['result']['catalog_original_route']['issue'],50)
        self.assertEqual(row['issue'],17)
        self.model.analyze.assert_called_once()

    def test_s3_diagnostics_are_verified_before_comment_and_kept_private(self):
        import hashlib
        from unittest.mock import patch
        from evidence_bundle import prepare_bundle
        self.managed()
        bundle=prepare_bundle(f.event(),{},[{'name':'logs.json','content':'private diagnostic bytes'}])
        self.evidence.collect.return_value={'bundle':bundle}
        self.engine.storage_backend='s3';self.engine.storage=Mock()
        def verify(key,raw):
            self.assertEqual(len(self.posts),0)
            return {'bucket':'spi-evidence','key':key,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'verified':True}
        self.engine.storage.ensure_object.side_effect=verify
        with patch.dict('os.environ',{'SPI_EVIDENCE_PUBLIC_ORIGIN':'https://rustfs.socrat.alchemmist.xyz'}):
            for stage in ('match','collect','analyze','publish'):
                self.data=self.engine.run_v1(stage,self.data)
        body=self.posts[0][1]['body']
        self.assertIn('https://rustfs.socrat.alchemmist.xyz/spi/v1/',body)
        self.assertNotIn('private diagnostic bytes',body)
        self.assertGreaterEqual(self.engine.storage.ensure_object.call_count,2)
        self.model.analyze.assert_called_once()


class ProductionContractTests(unittest.TestCase):
    def test_every_production_github_client_blocks_new_issue_creation(self):
        import ast
        import inspect
        import service
        tree=ast.parse(inspect.getsource(service.serve))
        clients=[n for n in ast.walk(tree)if isinstance(n,ast.Call)and isinstance(n.func,ast.Name)and n.func.id=='GitHub']
        self.assertGreaterEqual(len(clients),2)
        for node in clients:
            flags=[k.value for k in node.keywords if k.arg=='allow_issue_creation']
            self.assertEqual(len(flags),1)
            self.assertIs(ast.literal_eval(flags[0]),False)


class ResolutionAliasTests(unittest.TestCase):
    setUp=f.CoreTests.setUp
    tearDown=f.CoreTests.tearDown

    def test_resolved_episode_follows_alias_without_rewriting_old_job(self):
        from service import Engine
        old=self.store.enqueue(f.event())['job_id'];self.store.claim()
        self.store.save(old,4,{},state='complete',issue=54)
        key=self.store.enqueue(f.event('resolved'))['job_id']
        github=Mock();model=Mock();evidence=Mock();evidence.collect.return_value={}
        posts=[]
        def request(method,path,body=None):
            if method=='GET':
                self.assertEqual(path,'/issues/21')
                return {'number':21,'state':'open','title':'[SPI] LMS','body':'<!-- socrat-spi-catalog:v1:lms -->'}
            self.assertEqual(path,'/issues/21/comments');posts.append(body)
            return {'id':555}
        github.request.side_effect=request
        engine=Engine(self.store,github,model,evidence,write=True,catalog=ManagedCatalog())
        data=engine.claim({'execution_id':'1001','workflow_revision':'durable-v1'})
        self.assertEqual(data['job_id'],key)
        for stage in ('match','collect','analyze','publish'):data=engine.run_v1(stage,data)
        self.assertEqual(data['status'],'complete')
        self.assertEqual(self.store.get(old)['issue'],54)
        self.assertEqual(self.store.get(key)['issue'],21)
        self.assertEqual(len(posts),1)
        self.assertIn('does not establish root-cause remediation',posts[0]['body'])
        model.match.assert_not_called();model.analyze.assert_not_called()
