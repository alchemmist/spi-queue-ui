"""Integrated worker/Store protocol tests, fake clock + fake model only."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import test_triage as f
from core import Store, Failure, MATCH_SCHEMA
from integrations import Model, REQUEST_DEADLINE
from service import Engine


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.clock=patch('time.time',lambda:self.now);self.clock.start()
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name+'/db',minute_token_limit=60000)
        self.github=Mock();self.github.snapshot.return_value=[]
        self.github.open_signature.return_value=f.digest([])
        self.github.request.return_value={'number':7}
        self.model=Mock();self.model.match.return_value={'candidates':[]}
        self.model.analyze.return_value=f.report()
        self.evidence=Mock();self.evidence.collect.return_value={'missing':['logs']}
        self.engine=Engine(self.store,self.github,self.model,self.evidence,write=True)
        self.key=self.store.enqueue(f.event())['job_id']
        self.identity={'execution_id':'1234','workflow_revision':'durable-v1'}
        self.data=self.engine.claim(self.identity)

    def tearDown(self):
        self.clock.stop();self.tmp.cleanup()

    def test_consecutive_deferrals_same_execution_then_published(self):
        self.model.match.side_effect=[Failure('LLM_ADMISSION_DEFERRED',61),Failure('LLM_ADMISSION_DEFERRED',180),{'candidates':[]}]
        original=self.data
        for _ in range(2):
            sent=self.data
            self.data=self.engine.run_v1('match',sent)
            self.assertEqual(self.data['status'],'deferred')
            self.assertEqual(self.data['execution_id'],'1234')
            self.assertEqual(self.store.get(self.key)['stage'],0)
            self.evidence.collect.assert_not_called()
            self.assertFalse(self.engine.lock.locked())
            calls=self.model.match.call_count
            self.assertEqual(self.engine.run_v1('match',sent),self.data)
            self.assertEqual(self.model.match.call_count,calls)
            self.assertEqual(self.store.claim(),{'job_id':''})
            self.store=Store(self.store.path,minute_token_limit=60000)
            self.store.recover_on_start()
            self.engine.store=self.store
            self.now=self.data['due_at']
        self.data=self.engine.run_v1('match',self.data)
        for stage in ('collect','analyze','publish'):
            self.assertEqual(self.data['stage'],stage)
            self.data=self.engine.run_v1(stage,self.data)
            if stage!='publish':self.assertNotEqual(self.data['status'],'complete')
        self.assertEqual(self.data['status'],'complete')
        self.assertEqual(self.data['outcome'],'complete')
        self.assertEqual(self.github.request.call_count,1)
        self.assertEqual(self.store.get(self.key)['attempts'],1)
        with self.store.db() as c:
            self.assertEqual(c.execute('select count(*) from job_failure_budget').fetchone()[0],0)
        with self.assertRaises(Failure):self.engine.run_v1('match',original)

    def test_finite_reference_error_retained_no_rewait(self):
        self.data=self.engine.run_v1('match',self.data)
        self.data=self.engine.run_v1('collect',self.data)
        self.model.analyze.side_effect=Failure('LLM_INVALID_EVIDENCE_REFERENCE')
        with self.assertRaisesRegex(Failure,'LLM_INVALID_EVIDENCE_REFERENCE'):
            self.engine.run_v1('analyze',self.data)
        job=self.store.get(self.key)
        self.assertEqual(job['state'],'retry');self.assertEqual(job['stage'],2)
        with self.store.db() as c:
            self.assertEqual(c.execute('select failures from job_failure_budget').fetchone()[0],1)
        with self.assertRaises(Failure):self.engine.claim(self.identity)
        self.github.request.assert_not_called()

    def test_early_wrong_stale_resume_and_legacy_cannot_bypass(self):
        self.model.match.side_effect=Failure('LLM_ADMISSION_DEFERRED',61)
        waiting=self.engine.run_v1('match',self.data)
        for bad in ({**waiting,'fence':'0'*64},{**waiting,'workflow_revision':'wrong'},waiting):
            with self.assertRaises(Failure):self.engine.run_v1('match',bad)
        with self.assertRaises(Failure):self.engine.run('match',self.key)
        self.assertEqual(self.model.match.call_count,1)
        self.assertEqual(self.engine.claim(self.identity)['fence'],waiting['fence'])
        for bad in ('0','-1','１２','01',True,'9'*33):
            with self.assertRaises(Failure):self.engine.claim({**self.identity,'execution_id':bad})

    def test_cancelled_wait_expiry_reclaims_progress_fences_old_execution(self):
        self.data=self.engine.run_v1('match',self.data)
        self.data=self.engine.run_v1('collect',self.data)
        self.model.analyze.side_effect=Failure('LLM_ADMISSION_DEFERRED',61)
        waiting=self.engine.run_v1('analyze',self.data)
        self.now=waiting['due_at']+180
        fresh=self.engine.claim({**self.identity,'execution_id':'1235'})
        self.assertEqual(fresh['job_id'],self.key);self.assertEqual(fresh['stage'],'analyze')
        with self.assertRaises(Failure):self.engine.run_v1('analyze',waiting)
        with self.assertRaises(Failure):self.engine.claim(self.identity)
        self.assertIn('evidence',self.store.get(self.key)['result'])

    def test_owned_completion_priority_analysis_due_and_publish_ready(self):
        a=self.engine.run_v1('match',self.data)
        a=self.engine.run_v1('collect',a)
        # A pre-upgrade cohort can contain multiple owners. Seed B explicitly;
        # new admissions must no longer create that backlog during A's Wait.
        self.engine.lock.acquire()
        try:a=self.engine.run_v1('analyze',a)
        finally:self.engine.lock.release()
        key=self.store.enqueue(f.event(start='2026-11-03T14:00:00Z'))['job_id']
        with self.store.db() as c:
            c.execute("UPDATE jobs SET state='processing',lease=0,updated=? WHERE id=?",(self.now,key))
        self.store.owners.bind(key,'4000','durable-v1','match',self.now)
        with self.store.db() as c:
            c.execute("UPDATE workflow_wait_owner SET phase='ready',expires=? WHERE job_id=?",(self.now+180,key))
            b=self.store.owners.envelope(c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?',(key,)).fetchone())
        self.assertEqual(b['stage'],'match')
        self.now=a['due_at']
        self.model.analyze.side_effect=Failure('LLM_ADMISSION_DEFERRED',61)
        a=self.engine.run_v1('analyze',a)
        # Protect the short known analysis slot even before its due time.
        b=self.engine.run_v1('match',b)
        self.assertEqual(b['status'],'deferred')
        self.assertEqual(self.model.match.call_count,1)
        self.now=max(a['due_at'],b['due_at'])
        old=b;b=self.engine.run_v1('match',b)  # Low stage arrives before analysis wake.
        self.assertEqual(b['code'],'WORKER_ADMISSION_DEFERRED')
        self.assertEqual(self.engine.run_v1('match',old),b)
        self.assertEqual(self.model.match.call_count,1)
        self.model.analyze.side_effect=None
        a=self.engine.run_v1('analyze',a)
        self.assertEqual(a['stage'],'publish')
        self.now=b['due_at'];b=self.engine.run_v1('match',b)
        self.assertEqual(b['status'],'deferred')  # Ready publish also wins.
        self.assertEqual(self.model.match.call_count,1)
        a=self.engine.run_v1('publish',a)
        self.assertEqual(a['status'],'complete')
        self.now=b['due_at'];b=self.engine.run_v1('match',b)
        self.assertEqual(b['stage'],'collect')
        self.assertEqual(self.model.match.call_count,2)

    def test_priority_hold_is_bounded_and_rejects_invalid_higher_owner(self):
        a=self.engine.run_v1('match',self.data);a=self.engine.run_v1('collect',a)
        self.model.analyze.side_effect=Failure('LLM_ADMISSION_DEFERRED',180)
        a=self.engine.run_v1('analyze',a)
        with self.store.db() as c:
            self.assertEqual(self.store._owned_priority_until(c,0,self.now),0)  # Far future is not a reservation.
            self.assertGreater(self.store._owned_priority_until(c,0,a['due_at']),a['due_at'])
            c.execute("update workflow_wait_owner set revision='foreign' where job_id=?",(self.key,))
            self.assertEqual(self.store._owned_priority_until(c,0,a['due_at']),0)
            c.execute("update workflow_wait_owner set revision='durable-v1' where job_id=?",(self.key,))
            c.execute('update jobs set stage=1 where id=?',(self.key,))
            self.assertEqual(self.store._owned_priority_until(c,0,a['due_at']),0)
            c.execute('update jobs set stage=2 where id=?',(self.key,))
            expiry=a['due_at']+180
            self.store._reconcile_owners(c,expiry)
            self.assertEqual(self.store._owned_priority_until(c,0,expiry),0)
        self.assertEqual(self.store.get(self.key)['state'],'retry')

    def test_fresh_queued_match_waits_for_short_owned_analysis_slot(self):
        a=self.engine.run_v1('match',self.data);a=self.engine.run_v1('collect',a)
        self.model.analyze.side_effect=Failure('LLM_ADMISSION_DEFERRED',61)
        a=self.engine.run_v1('analyze',a)
        key=self.store.enqueue(f.event(start='2026-11-03T14:00:00Z'))['job_id']
        empty=self.engine.claim({**self.identity,'execution_id':'4000'})
        self.assertEqual(empty['status'],'empty')
        self.assertEqual(self.store.get(key)['state'],'queued')
        token,deadline=self.store.reserve_dispatch(self.now)
        self.assertIsNone(token);self.assertEqual(deadline,a['due_at']+180)
        self.now=a['due_at']+180
        next_job=self.engine.claim({**self.identity,'execution_id':'4001'})
        self.assertEqual(next_job['job_id'],self.key)
        self.assertEqual(next_job['stage'],'analyze')

    def test_fresh_v1_claim_acks_dispatch_replay_does_not_erase_new_wake(self):
        data=self.data
        for stage in ('match','collect','analyze','publish'):
            data=self.engine.run_v1(stage,data)
        self.assertEqual(data['status'],'complete')
        saved=None
        for i in range(8):
            self.store.enqueue(f.event(start='2026-11-03T14:%02d:00Z'%i))
            token,_=self.store.reserve_dispatch(self.now)
            self.assertIsNotNone(token)
            self.assertEqual(self.store.dispatch_status()['attempts'],1)
            identity={**self.identity,'execution_id':str(2000+i)}
            data=self.engine.claim(identity)
            self.assertEqual(self.store.dispatch_status()['attempts'],0)
            for stage in ('match','collect','analyze','publish'):
                data=self.engine.run_v1(stage,data)
            self.assertEqual(data['status'],'complete')
            saved=identity
        self.store.enqueue(f.event(start='2026-11-03T15:00:00Z'))
        token,_=self.store.reserve_dispatch(self.now)
        self.assertIsNotNone(token)
        self.engine.claim(saved)  # Duplicate old workflow must not ACK this wake.
        self.assertEqual(self.store.dispatch_status()['attempts'],1)
        data=self.engine.claim({**self.identity,'execution_id':'3000'})
        self.assertEqual(self.store.dispatch_status()['attempts'],0)
        # A fresh empty callback also acknowledges once; its replay never does.
        with self.store.db() as c:c.execute("INSERT INTO dispatch VALUES(1,'fixture',?,4)",(self.now+30,))
        empty_id={**self.identity,'execution_id':'3001'}
        self.assertEqual(self.engine.claim(empty_id)['status'],'empty')
        self.assertEqual(self.store.dispatch_status()['attempts'],0)
        with self.store.db() as c:c.execute("INSERT INTO dispatch VALUES(1,'fixture2',?,1)",(self.now+30,))
        self.assertEqual(self.engine.claim(empty_id)['status'],'empty')
        self.assertEqual(self.store.dispatch_status()['attempts'],1)

    def test_busy_worker_30min_renews_wait_without_http_queue(self):
        reservation=self.store.reserve_llm(1,timeout=1800,now=self.now)
        self.engine.lock.acquire()
        try:
            start=self.now
            while self.now-start<1800:
                old=self.data
                self.data=self.engine.run_v1('match',old)
                self.assertEqual(self.data['code'],'WORKER_ADMISSION_DEFERRED')
                self.assertEqual(self.engine.run_v1('match',old),self.data)
                self.store=Store(self.store.path,minute_token_limit=60000)
                self.store.recover_on_start();self.engine.store=self.store
                self.now=self.data['due_at']
            self.model.match.assert_not_called()
            with self.store.db() as c:
                self.assertEqual(c.execute('select count(*) from llm_admission').fetchone()[0],1)
                self.assertEqual(c.execute('select count(*) from job_failure_budget').fetchone()[0],0)
            self.assertEqual(self.data['deadline'],start+21600)
        finally:self.engine.lock.release()
        self.store.finish_llm(reservation,now=self.now)
        result=self.engine.run_v1('match',self.data)
        self.assertEqual(result['stage'],'collect')

    def test_concurrent_same_claim_and_duplicate_stage_do_not_double_pay(self):
        import concurrent.futures,threading
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.store.owners.claim('1234','durable-v1',self.now),range(2)))
        self.assertEqual(results[0],results[1])
        entered=threading.Event();release=threading.Event()
        def model(*args,**kwargs):
            entered.set();self.assertTrue(release.wait(3));return {'candidates':[]}
        self.model.match.side_effect=model
        other_engine=Engine(Store(self.store.path),self.github,self.model,self.evidence)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            future=pool.submit(self.engine.run_v1,'match',self.data)
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(Failure):other_engine.run_v1('match',self.data)
            finally:release.set()
            result=future.result(timeout=3)
        self.assertEqual(self.model.match.call_count,1)
        self.assertEqual(self.engine.run_v1('match',self.data),result)

    def test_running_crash_no_blind_model_replay(self):
        _,owner=self.store.owners.begin('match',self.data,self.now)
        restored=Store(self.store.path)
        self.assertEqual(restored.recover_on_start(),0)
        self.now+=1980
        self.assertEqual(restored.claim(),{'job_id':''})
        self.assertEqual(restored.get(self.key)['state'],'dead')
        self.assertEqual(restored.get(self.key)['error'],'WRITE_NEEDS_REVIEW')

    def test_wait_releases_http_lock_but_retains_logical_owner(self):
        self.model.match.side_effect=Failure('LLM_ADMISSION_DEFERRED',61)
        self.engine.run_v1('match',self.data)
        other=self.store.enqueue(f.event(start='2026-11-03T14:00:00Z'))['job_id']
        claimed=self.engine.claim({**self.identity,'execution_id':'1235'})
        self.assertEqual(claimed['status'],'empty')
        self.assertFalse(self.engine.lock.locked())
        self.assertEqual(self.store.get(other)['state'],'queued')
        self.assertEqual(self.store.get(other)['attempts'],0)


class ModelTimerTests(unittest.TestCase):
    @patch.dict('os.environ',{'SPI_TOKEN_ACCOUNTING_MODE':'byte-bound-v1'})
    def test_active_reservation_survives_old_661_second_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp+'/db',minute_token_limit=60000)
            http=Mock();model=Model(http,'fake',store=store)
            now=time.time()
            def request(*args,**kwargs):
                self.assertGreater(kwargs['timeout'],1700)
                with store.db() as c:
                    row=c.execute('select * from llm_admission').fetchone()
                    self.assertGreater(row['expires'],now+1800)
                with self.assertRaises(Failure):store.reserve_llm(1,now=now+661)
                return {'choices':[{'message':{'content':json.dumps({'issue_number':0,'reason':'Distinct cause','confidence':'low'})}}]}
            http.request.side_effect=request
            REQUEST_DEADLINE.at=time.monotonic()+1800
            try:model.call('test',{},MATCH_SCHEMA,time.monotonic()+3600)
            finally:del REQUEST_DEADLINE.at
            self.assertEqual(http.request.call_count,1)


if __name__=='__main__':unittest.main()
