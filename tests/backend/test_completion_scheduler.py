"""Synthetic clocks/transports only; no live queue or inference."""
import tempfile
import unittest
from unittest.mock import Mock, patch
import test_triage as f
from core import Store, Failure, digest
from service import Engine

class CompletionSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Store(self.tmp.name+'/db');self.now=100
        self.clock=patch('core.time.time',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
    def add(self,key,stage=0,state='queued',available=None,error=None):
        self.s.enqueue({'event_key':key,'episode':key})
        with self.s.db() as c:
            c.execute('UPDATE jobs SET stage=?,state=?,available=?,error=? WHERE id=?',(stage,state,self.now if available is None else available,error,key))
    def test_competition_expiry_jitter_publication_and_resume(self):
        for jitter in (1,2,3):
            with self.subTest(jitter=jitter):
                self.set_competition(jitter)
    def set_competition(self,jitter):
        with self.s.db() as c:
            c.execute('DELETE FROM jobs');c.execute('DELETE FROM llm_admission');c.execute('DELETE FROM dispatch')
        self.now=100;self.add('older-match')
        e=f.event();key=self.s.enqueue(e)['job_id']
        result={'match':{'candidates':[]},'evidence':{},'open_signature':digest([])}
        self.s.save(key,2,result,state='retry')
        reservation=self.s.reserve_llm(29929);self.s.finish_llm(reservation)
        with patch('core.random.uniform',return_value=jitter):self.s.fail(key,'LLM_ADMISSION_DEFERRED',retry_after=61)
        self.s=Store(self.s.path)  # restart retains ledger and policy
        for t in range(101,161+jitter):
            self.now=t;self.add('arrival-'+str(t))
            self.assertEqual(self.s.claim(),{'job_id':''})
            self.assertEqual(self.s.reserve_dispatch(),(None,161+jitter))
        self.now=161+jitter
        self.assertEqual(self.s.claim()['job_id'],key)
        http=Mock();http.request.return_value={'number':7}
        http.snapshot.return_value=[];http.open_signature.return_value=digest([])
        model=Mock()
        def analyze(*args):
            r=self.s.reserve_llm(28931);self.s.finish_llm(r);return f.report()
        model.analyze.side_effect=analyze
        engine=Engine(self.s,http,model,Mock(),write=True)
        engine.run('analyze',key);engine.run('publish',key)
        self.assertEqual(self.s.get(key)['state'],'complete')
        self.assertEqual(http.request.call_count,1)
        self.assertEqual(self.s.claim()['job_id'],'older-match')
    def test_safe_continuations_unknown_backoff_and_cancelled(self):
        self.add('match');self.add('analysis',2,'retry',3700,'LLM_HTTP_429')
        for i in range(119):self.add('cancelled-'+str(i),4,'cancelled')
        self.add('collect',1);self.add('write',3)
        self.assertEqual(self.s.claim()['job_id'],'write')
        self.s.save('write',4,{},state='complete')
        self.assertEqual(self.s.claim()['job_id'],'collect')
        self.s.save('collect',4,{},state='complete')
        self.assertEqual(self.s.claim()['job_id'],'match')
    def test_stale_unbounded_deadline_does_not_hold(self):
        self.add('match');self.add('analysis',2,'retry',4000,'LLM_ADMISSION_DEFERRED')
        self.s.reserve_llm(30000,timeout=4000)
        self.assertEqual(self.s.claim()['job_id'],'match')
    def test_due_analysis_priority_and_safe_write_during_hold(self):
        self.add('match');self.add('analysis',2,'retry',164,'LLM_ADMISSION_DEFERRED')
        r=self.s.reserve_llm(30000);self.s.finish_llm(r)
        self.add('write',3)
        self.assertEqual(self.s.claim()['job_id'],'write')
        self.s.save('write',4,{},state='complete')
        self.assertEqual(self.s.claim()['job_id'],'')
        self.now=164;self.assertEqual(self.s.claim()['job_id'],'analysis')
    def test_abandoned_active_ledger_bounded_wake_and_expiry(self):
        self.add('match');self.add('analysis',2,'retry',764,'LLM_ADMISSION_DEFERRED')
        self.s.reserve_llm(30000)  # active request lost on restart, expires at 761
        self.s=Store(self.s.path)
        self.now=761
        self.assertEqual(self.s.reserve_dispatch(),(None,764))
        self.assertEqual(self.s.claim()['job_id'],'')
        self.now=764
        self.assertEqual(self.s.claim()['job_id'],'analysis')
        self.s.reserve_llm(28931)
        with self.assertRaises(Failure):self.s.reserve_llm(1)
    def test_stage_ties_and_age_alert_remain_natural(self):
        self.add('old-match');self.now=110
        self.add('z-analysis',2);self.add('a-analysis',2)
        self.assertEqual(self.s.health()['oldest_pending_seconds'],10)
        self.assertEqual(self.s.claim()['job_id'],'a-analysis')
        self.s.save('a-analysis',4,{},state='complete')
        self.assertEqual(self.s.claim()['job_id'],'z-analysis')
        self.assertEqual(self.s.health()['oldest_pending_seconds'],10)
