"""Busy LLM is durable pending work, not an exhausted job or repeated publication."""
import json,time,unittest,urllib.error
from unittest.mock import Mock,patch
import test_triage as f
from core import Store,Failure,LLM_TRANSIENT,MATCH_SCHEMA
from integrations import Model,HTTP

class DurableRetryTests(unittest.TestCase):
    setUp=f.CoreTests.setUp
    tearDown=f.CoreTests.tearDown
    def test_many_transient_failures_survive_restart_then_complete(self):
        e=f.event();key=self.store.enqueue(e)['job_id']
        for _ in range(20):
            self.assertEqual(self.store.claim()['job_id'],key)
            self.store.fail(key,'LLM_HTTP_429',retry_after=7200)
            r=self.store.get(key);self.assertEqual(r['state'],'retry');self.assertEqual(r['lease'],0)
            self.assertGreaterEqual(r['available']-r['updated'],7200)
            self.assertEqual(self.store.claim()['job_id'],'')
            self.store=Store(self.store.path)
            with self.store.db()as c:c.execute('update jobs set available=0 where id=?',(key,))
        self.store.claim();self.store.save(key,4,{'done':True},state='complete')
        self.assertEqual(self.store.get(key)['state'],'complete');self.assertEqual(self.store.get(key)['attempts'],21)
    def test_permanent_and_ambiguous_errors_still_stop(self):
        for index,code in enumerate(['HTTP_429','LLM_HTTP_400','LLM_HTTP_401','LLM_HTTP_403','WRITE_NEEDS_REVIEW']):
            key=self.store.enqueue(f.event(start='2026-11-03T13:00:%02dZ'%index))['job_id']
            with self.store.db()as c:c.execute('update jobs set attempts=50 where id=?',(key,))
            for i in range(8):
                self.store.fail(key,code)
                expected='dead'if code=='WRITE_NEEDS_REVIEW'or i==7 else 'retry'
                self.assertEqual(self.store.get(key)['state'],expected)
            self.store=Store(self.store.path)
            with self.store.db()as c:self.assertEqual(c.execute('select failures from job_failure_budget where job_id=?',(key,)).fetchone()[0],8)
    def test_jitter_cap_and_retry_hint_validation(self):
        key=self.store.enqueue(f.event())['job_id']
        with self.store.db()as c:c.execute('update jobs set attempts=10000 where id=?',(key,))
        self.store.fail(key,'LLM_UPSTREAM_TIMEOUT');r=self.store.get(key)
        self.assertGreaterEqual(r['available']-r['updated'],3000);self.assertLessEqual(r['available']-r['updated'],3600)
        self.assertIsNone(Failure('x',float('nan')).retry_after);self.assertEqual(Failure('x',999999).retry_after,86400)

class ModelRetryTests(unittest.TestCase):
    def test_transport_classification_and_hint_preserved(self):
        for code in ['HTTP_429','HTTP_503','HTTP_504','UPSTREAM_TIMEOUT','UPSTREAM_NETWORK_ERROR']:
            http=Mock();http.request.side_effect=Failure(code,retry_after=91)
            with self.assertRaises(Failure)as caught:Model(http,'fake',min_interval=0,store=Mock()).call('test',{},MATCH_SCHEMA,time.monotonic()+30)
            self.assertIn(caught.exception.code,LLM_TRANSIENT);self.assertEqual(caught.exception.retry_after,91)
            self.assertEqual(http.request.call_count,1) # durable queue retry, no busy loop
    def test_retry_after_seconds_and_date(self):
        from email.utils import formatdate
        for value in ['120',formatdate(time.time()+180,usegmt=True),'invalid','NaN']:
            h=HTTP();h.opener=Mock();h.opener.open.side_effect=urllib.error.HTTPError('http://internal',429,'busy',{'Retry-After':value},None)
            with self.assertRaises(Failure)as caught:h.request('GET','http://internal')
            if value in ['invalid','NaN']:self.assertIsNone(caught.exception.retry_after)
            else:self.assertGreater(caught.exception.retry_after,100)
    def test_batch_checkpoint_resumes_and_changed_snapshot_invalidates(self):
        good={'issue_number':0,'reason':'Different','confidence':'low'};issues=[{'number':i}for i in range(25)]
        m=Model(Mock(),'fake');m.call=Mock(side_effect=[good,Failure('LLM_HTTP_503')]);saved=[]
        with self.assertRaises(Failure):m.match(f.event(),issues,on_progress=lambda p:saved.append(json.loads(json.dumps(p))))
        self.assertEqual(len(saved[-1]['reviews']),1)
        m.call=Mock(return_value=good);r=m.match(f.event(),issues,progress=saved[-1]);self.assertEqual(m.call.call_count,2);self.assertEqual(r['reviewed_numbers'],list(range(25)))
        m.call.reset_mock();m.match(f.event(),issues+[{'number':25}],progress=saved[-1]);self.assertEqual(m.call.call_count,2)

class EngineCheckpointTests(unittest.TestCase):
    setUp=f.EngineTests.setUp
    tearDown=f.EngineTests.tearDown
    def test_partial_matching_checkpoint_saved_before_busy_failure(self):
        def matching(event,issues,progress=None,on_progress=None):
            on_progress({'signature':'test','decisions':[]});raise Failure('LLM_HTTP_429')
        self.model.match.side_effect=matching;key=self.store.enqueue(f.event())['job_id'];self.store.claim()
        with self.assertRaises(Failure):self.engine.run('match',key)
        r=self.store.get(key);self.assertEqual(r['result']['match_progress_v3']['signature'],'test');self.assertEqual(r['state'],'retry');self.github.request.assert_not_called()

    def test_many_llm_deferrals_do_not_exhaust_github_reconciliation(self):
        key=self.store.enqueue(f.event())['job_id'];self.model.match.side_effect=Failure('LLM_HTTP_429')
        for _ in range(20):
            self.store.claim()
            with self.assertRaises(Failure):self.engine.run('match',key)
            self.assertEqual(self.store.get(key)['state'],'retry')
            with self.store.db()as c:c.execute('update jobs set available=0 where id=?',(key,))
        self.model.match.side_effect=None;self.store.claim()
        for stage in ['match','collect','analyze']:self.engine.run(stage,key)
        self.engine.write=True;self.github.request.side_effect=Failure('UPSTREAM_TIMEOUT')
        with self.assertRaises(Failure):self.engine.run('publish',key)
        r=self.store.get(key);self.assertEqual(r['state'],'retry');self.assertIsNotNone(r['intent'])
        self.store=Store(self.store.path);self.engine.store=self.store
        with self.store.db()as c:
            self.assertEqual(c.execute('select failures from job_failure_budget where job_id=?',(key,)).fetchone()[0],1)
            c.execute('update jobs set available=0 where id=?',(key,))
        self.github.reconcile.return_value=7;self.store.claim();self.engine.run('publish',key)
        self.assertEqual(self.store.get(key)['state'],'complete');self.github.request.assert_called_once()
