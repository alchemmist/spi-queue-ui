"""Snapshot churn tests use no provider, GitHub, storage or queue network writes."""
import copy
import unittest
from unittest.mock import Mock,patch
import test_triage as f
from core import Failure,digest
from integrations import Model,GitHub

GOOD={'issue_number':0,'reason':'Different problem','confidence':'low'}

class ReviewReuseTests(unittest.TestCase):
    def issues(self):
        return [{'number':i,'updated_at':'first','title':'[SPI] incident','body':'body','recent_comments':['comment']} for i in range(1,62)]
    def prepare(self,issues,event=None):
        m=Model(Mock(),'fake');m.call=Mock(return_value=GOOD);saved=[]
        m.match(event or f.event(),issues,on_progress=lambda p:saved.append(copy.deepcopy(p)))
        self.assertEqual(m.call.call_count,6)
        return m,saved[-1]
    def test_four_jobs_exact_partitions_survive_updates_inserts_deletes_reordering(self):
        original=self.issues()
        for index in range(4):
            m,proof=self.prepare(original,f.event(start='2026-11-03T13:00:0%dZ'%index))
            current=copy.deepcopy(original)
            if index==0:current[15]['recent_comments'].append('bot publication')
            elif index==1:current.insert(0,dict(current[0],number=99))
            elif index==2:current.pop(15)
            else:current.reverse()
            m.call.reset_mock();saved=[]
            result=m.match(f.event(start='2026-11-03T13:00:0%dZ'%index),current,proof,lambda p:saved.append(copy.deepcopy(p)))
            expected=1 if index!=3 else 0
            self.assertEqual(m.call.call_count,expected)
            self.assertEqual(set(result['reviewed_numbers']),{x['number'] for x in current})
            retained=[r for r in proof['reviews'] if r in saved[0]['reviews']]
            self.assertEqual(len(retained),5 if index in (0,2) else 6)
            self.assertEqual(proof['snapshot'],original)
    def test_owner_model_prompt_and_content_proofs_fail_closed(self):
        issues=self.issues();m,proof=self.prepare(issues)
        m.call.reset_mock();m.match(f.event(start='2026-11-03T14:00:00Z'),issues,proof)
        self.assertEqual(m.call.call_count,6)
        m.call.reset_mock()
        with patch('integrations.SYSTEM','new prompt'):m.match(f.event(),issues,proof)
        self.assertEqual(m.call.call_count,6)
        base_payload=m.payload
        m.payload=lambda *args,**kwargs:dict(base_payload(*args,**kwargs),model='different-model')
        m.call.reset_mock()
        with self.assertRaisesRegex(Failure,'LLM_ACCOUNTING_CONTRACT_MISMATCH'):m.match(f.event(),issues,proof)
        m.call.assert_not_called() # Unknown model fails accounting, never trusts the old proof.
        m.payload=base_payload
        for mutation in ('overlap','content','decision','snapshot'):
            bad=copy.deepcopy(proof)
            if mutation=='overlap':bad['reviews'].append(copy.deepcopy(bad['reviews'][0]))
            elif mutation=='content':bad['reviews'][0]['content']='bad'
            elif mutation=='decision':bad['reviews'][0]['decision']['issue_number']=999
            else:bad['snapshot'][0]['body']='changed'
            m.call.reset_mock()
            with self.assertRaises(Failure):m.match(f.event(),issues,bad)
            m.call.assert_not_called()
    def test_transfer_persisted_before_admission_and_legacy_never_rebound(self):
        issues=self.issues();m,proof=self.prepare(issues);current=copy.deepcopy(issues);current[0]['updated_at']='second'
        saved=[];m.call=Mock(side_effect=Failure('LLM_ADMISSION_DEFERRED'))
        with self.assertRaises(Failure):m.match(f.event(),current,proof,saved.append)
        self.assertEqual(len(saved[0]['reviews']),5)
        m.call=Mock(return_value=GOOD);m.match(f.event(),current,{'version':2,**proof,'contract':'unknown'})
        self.assertEqual(m.call.call_count,6)
    def test_closed_nominee_is_removed_and_semantic_final_recomputed(self):
        issues=self.issues();m=Model(Mock(),'fake');saved=[]
        def choose(task,data,schema,deadline):return dict(GOOD,issue_number=data['open_issues'][0]['number'])
        m.call=Mock(side_effect=choose);m.match(f.event(),issues,on_progress=lambda p:saved.append(copy.deepcopy(p)))
        current=issues[1:];m.call.reset_mock()
        result=m.match(f.event(),current,saved[-1])
        self.assertNotIn(1,[x['number'] for x in result['candidates']])
        self.assertEqual(len(result['reviewed_numbers']),60)
        self.assertGreater(m.call.call_count,1) # invalidated batch plus semantic nominee reduction
    def test_selection_proof_survives_repeated_restart_deferral(self):
        issues=self.issues();m=Model(Mock(),'fake');saved=[];calls=[0]
        def choose(task,data,schema,deadline):
            calls[0]+=1
            if calls[0]==8:raise Failure('LLM_ADMISSION_DEFERRED')
            return dict(GOOD,issue_number=data['open_issues'][0]['number'])
        m.call=choose
        with self.assertRaises(Failure):m.match(f.event(),issues,on_progress=lambda p:saved.append(copy.deepcopy(p)))
        self.assertEqual(len(saved[-1]['selection']['comparisons']),1)
        proof=copy.deepcopy(saved[-1]);m.call=Mock(side_effect=Failure('LLM_ADMISSION_DEFERRED'))
        with self.assertRaises(Failure):m.match(f.event(),issues,proof,lambda p:saved.append(copy.deepcopy(p)))
        self.assertEqual(saved[-1]['selection'],proof['selection'])
        m.call=Mock(side_effect=lambda task,data,*args:dict(GOOD,issue_number=data['open_issues'][0]['number']))
        m.match(f.event(),issues,saved[-1]);self.assertEqual(m.call.call_count,4)

    def test_cheap_signature_same_universe_without_comments(self):
        g=GitHub(Mock(),'unused');g.issues=Mock(return_value=self.issues());g.comments=Mock()
        self.assertEqual(g.open_signature(),digest([(x['number'],x['updated_at']) for x in self.issues()]))
        self.assertEqual(g.issues.call_count,2);g.comments.assert_not_called()

class EarlyFenceTests(unittest.TestCase):
    setUp=f.EngineTests.setUp
    tearDown=f.EngineTests.tearDown
    def test_twenty_safe_deferrals_zero_analysis_preserve_cache_and_budget(self):
        key=self.store.enqueue(f.event())['job_id'];self.store.claim()
        proof={'private':'fake-proof'}
        for _ in range(20):
            self.store.save(key,2,{'match':{},'match_progress_v3':proof,'open_signature':'old','evidence':{}})
            self.github.open_signature.side_effect=None;self.github.open_signature.return_value='new'
            with self.assertRaises(Failure) as e:self.engine.run('analyze',key)
            self.assertEqual(e.exception.code,'OPEN_SPI_CHANGED')
            row=self.store.get(key)
            self.assertEqual(row['stage'],0);self.assertEqual(row['state'],'retry')
            self.assertLessEqual(row['available']-row['updated'],20)
            self.assertEqual(row['result']['match_progress_v3'],proof)
            with self.store.db() as c:c.execute('update jobs set available=0 where id=?',(key,))
            self.store.claim()
        self.model.analyze.assert_not_called()
        with self.store.db() as c:self.assertEqual(c.execute('select count(*) from job_failure_budget').fetchone()[0],0)
    def test_rollback_result_reset_retains_separate_checkpoint(self):
        from core import Store
        model=Model(Mock(),'fake');model.call=Mock(return_value=GOOD)
        self.engine.model=model;self.github.snapshot.return_value=ReviewReuseTests().issues()
        key=self.store.enqueue(f.event())['job_id'];self.store.claim();self.engine.run('match',key)
        proof=copy.deepcopy(self.store.match_checkpoint(key))
        self.assertEqual(len(proof['reviews']),6)
        # Emulate the old reset without executing or deploying an old worker.
        self.store.save(key,0,{'publication_backend':'s3'},state='processing')
        self.store=Store(self.store.path);self.engine.store=self.store
        model.call.reset_mock();self.engine.run('match',key)
        model.call.assert_not_called();self.assertEqual(self.store.match_checkpoint(key),proof)

    def test_unsafe_intent_not_cleared_or_patient(self):
        key=self.store.enqueue(f.event())['job_id'];self.store.claim()
        self.store.save(key,3,{'publication_backend':'legacy'},intent='{"number":7}')
        job=self.store.get(key)
        with self.assertRaises(Failure) as e:self.engine.invalidate_snapshot(key,job['result'],job)
        self.assertEqual(e.exception.code,'WRITE_NEEDS_REVIEW');self.assertEqual(self.store.get(key)['intent'],job['intent'])
        for _ in range(8):self.store.fail(key,'OPEN_SPI_CHANGED')
        self.assertEqual(self.store.get(key)['state'],'dead')

class ChurnPublicationTests(unittest.TestCase):
    setUp=f.EngineTests.setUp
    tearDown=f.EngineTests.tearDown
    def test_four_prepared_jobs_eventually_publish_after_own_bot_updates(self):
        from types import SimpleNamespace
        from service import Engine
        from core import Store
        from evidence_bundle import prepare_bundle,storage_objects
        from telegram_links import Links
        import hashlib
        now=[1000]
        with patch('core.time.time',side_effect=lambda:now[0]):
            issues=ReviewReuseTests().issues();original=copy.deepcopy(issues)
            github=Mock();github.snapshot.side_effect=lambda:copy.deepcopy(issues)
            github.open_signature.side_effect=lambda:digest([(x['number'],x['updated_at']) for x in issues])
            model=Model(Mock(),'fake');model.call=Mock(side_effect=lambda task,data,*args:dict(GOOD,issue_number=1 if any(x['number']==1 for x in data['open_issues']) else 0))
            model.analyze=Mock(side_effect=lambda *args:f.report(1))
            evidence=Mock();bundle_count=[0]
            def collect(event):
                bundle_count[0]+=1
                return {'bundle':prepare_bundle(event,{},[{'name':'logs.json','content':'fake '+str(bundle_count[0])}])}
            evidence.collect.side_effect=collect
            stored={}
            def upload(key,raw):
                if key in stored:self.assertEqual(stored[key],raw)
                stored[key]=raw
                return {'bucket':'spi-evidence','key':key,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'verified':True}
            storage=Mock();storage.ensure_object.side_effect=upload
            links=Links(self.store,Mock(),guarded_protocol=True)
            engine=Engine(self.store,github,model,evidence,write=True,storage=storage,storage_backend='s3',delivery=SimpleNamespace(links=links,signal=Mock()))
            jobs=[];publications=[]
            for index in range(4):
                event=f.event(start='2026-11-03T13:00:0%dZ'%index)
                key=self.store.enqueue(event)['job_id'];self.assertEqual(self.store.claim()['job_id'],key)
                engine.run('match',key);engine.run('collect',key);engine.run('analyze',key)
                # Prepared jobs must not hold a live exclusive lease during setup.
                self.store.save(key,3,self.store.get(key)['result'],state='retry')
                with self.store.db() as c:c.execute('update jobs set available=999999,lease=0 where id=?',(key,))
                links.record_card_receipt(event['episode'],{'key':'Test/spi/'+event['episode'],'episode':event['episode'],'immutable_key':True,'copies':[{'chat_id':1,'message_id':index+1},{'chat_id':2,'message_id':index+1}]})
                jobs.append((key,event))
            def post(method,path,body):
                self.assertEqual(method,'POST');self.assertEqual(path,'/issues/1/comments')
                self.assertNotIn('fake ',body['body'])
                n=800+len(publications);url='https://github.com/alchemmist/homelab/issues/1#issuecomment-'+str(n)
                publications.append(url);issues[0]['updated_at']=str(n);issues[0]['recent_comments'].append('own bot publication '+str(n))
                return {'id':n,'html_url':url}
            github.request.side_effect=post
            # First prepared job writes; each following prepared report is now stale.
            for index,(key,event) in enumerate(jobs):
                with self.store.db() as c:c.execute('update jobs set available=? where id=?',(now[0],key))
                self.assertEqual(self.store.claim()['job_id'],key)
                if index:
                    before=model.call.call_count
                    with self.assertRaisesRegex(Failure,'OPEN_SPI_CHANGED'):engine.run('publish',key)
                    now[0]+=21
                    self.assertEqual(self.store.claim()['job_id'],key)
                    engine.run('match',key)
                    self.assertEqual(model.call.call_count-before,1)
                    engine.run('collect',key);engine.run('analyze',key)
                engine.run('publish',key);engine.run('publish',key)
                self.assertEqual(self.store.get(key)['state'],'complete')
                self.assertEqual(links.snapshot(event['episode'])['url'],publications[-1])
                links.request=Mock(return_value=(200,{'key':'Test/spi/'+event['episode'],'updated':[{'chat_id':1,'message_id':index+1},{'chat_id':2,'message_id':index+1}],'pending':[]}))
                links.once();self.assertEqual(links.snapshot(event['episode'])['state'],'applied')
                links.request.assert_called_once()
                for object_key,raw in storage_objects(self.store.get(key)['result']['evidence']['bundle']):self.assertEqual(stored[object_key],raw)
                self.store=Store(self.store.path);engine.store=self.store
            self.assertEqual(len(publications),4);self.assertEqual(issues[1:],original[1:])
