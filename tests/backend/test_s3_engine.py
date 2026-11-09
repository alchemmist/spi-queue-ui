import hashlib
import importlib.util
import re
from urllib.parse import urlsplit
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_triage import event, report
from core import Store, digest, Failure
from service import Engine
from evidence_bundle import prepare_bundle, storage_objects, storage_markdown
from s3_evidence import StorageTransient

class EngineStorageTests(unittest.TestCase):
    def setUp(self):
        origin=patch.dict('os.environ',{'SPI_EVIDENCE_PUBLIC_ORIGIN':'https://rustfs.socrat.alchemmist.xyz'})
        origin.start();self.addCleanup(origin.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'db');self.key=self.store.enqueue(event())['job_id'];self.store.claim()
        self.bundle=prepare_bundle(event(),{},[{'name':'logs.json','content':'private diagnostic'}])
        self.r={'report':report(),'match':{'candidates':[]},'open_signature':digest([]),'evidence':{'bundle':self.bundle}}
        self.store.save(self.key,3,self.r)
        self.g=Mock();self.g.snapshot.return_value=[];self.g.request.return_value={'number':9}
        self.s=Mock();self.s.ensure_object.side_effect=self.verify
        self.engine=Engine(self.store,self.g,Mock(),Mock(),write=True,storage=self.s,storage_backend='s3')
    def verify(self,key,raw):
        self.assertEqual(self.store.get(self.key)['result']['publication_backend'],'s3')
        self.g.request.assert_not_called()
        return {'bucket':'spi-evidence','key':key,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'verified':True}
    def assert_public_links(self, body, objects):
        spec=importlib.util.spec_from_file_location('public_link_gateway',Path(__file__).resolve().parents[2]/'integrations/evidence/gateway.py')
        gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)
        urls=re.findall(r'\]\((https://(?:rustfs|spi-evidence)\.socrat\.alchemmist\.xyz/[^\s)]+)\)',body)
        self.assertEqual(len(urls),len(objects))
        for url,(key,raw) in zip(urls,objects):
            parsed=urlsplit(url)
            self.assertEqual(parsed.scheme,'https')
            self.assertEqual(parsed.netloc,urlsplit(gateway.ORIGIN).netloc)
            self.assertIsNone(parsed.username);self.assertIsNone(parsed.password)
            self.assertEqual(parsed.query,'');self.assertEqual(parsed.fragment,'')
            self.assertIsNotNone(gateway.KEY.fullmatch(parsed.path))
            self.assertEqual(parsed.path,'/'+key)
            self.assertFalse(parsed.path.startswith('/spi-evidence/'))
            # The historical bucket-qualified public URL must remain rejected.
            self.assertIsNone(gateway.KEY.fullmatch('/spi-evidence/'+key))

    def test_renderer_all_objects_match_actual_gateway(self):
        bundle=prepare_bundle(event(),{},[
            {'name':name,'content':{'text':'bounded'}}
            for name in ('logs.json','trace-lines.txt','metric_samples.json','Mixed.Case-1.json')])
        objects=storage_objects(bundle)
        self.assert_public_links(storage_markdown(objects),objects)
        self.assertEqual(storage_objects(bundle),objects)
        self.assertEqual(storage_markdown([]),'')

    def test_verified_before_single_primary(self):
        self.engine.run('publish',self.key)
        self.assertEqual(self.s.ensure_object.call_count,2);self.assertEqual(self.g.request.call_count,1)
        body=self.g.request.call_args.args[2]['body'];self.assertIn('https://rustfs.socrat.alchemmist.xyz/spi/v1/',body);self.assertNotIn('private diagnostic',body)
        self.assert_public_links(body,storage_objects(self.bundle))
    def test_per_file_checkpoint_restart_toggle(self):
        n=[0]
        def upload(k,b):
            n[0]+=1
            if n[0]==2:raise StorageTransient(2)
            return self.verify(k,b)
        self.s.ensure_object.side_effect=upload
        with self.assertRaisesRegex(Failure,'STORAGE_TRANSIENT'):self.engine.run('publish',self.key)
        saved=self.store.get(self.key);self.assertEqual(len(saved['result']['storage_publication']['verified']),1)
        self.g.request.assert_not_called()
        self.store.save(self.key,3,saved['result'],state='processing')
        self.s.ensure_object.side_effect=self.verify
        engine=Engine(self.store,self.g,Mock(),Mock(),write=True,storage=self.s)
        engine.run('publish',self.key);self.assertEqual(self.s.ensure_object.call_count,3)
        self.assertEqual(self.g.request.call_count,1)
    def test_patient_budgets_independent(self):
        for code,count in [('LLM_HTTP_429',20),('STORAGE_TRANSIENT',12)]:
            for _ in range(count):self.store.fail(self.key,code,2)
        with self.store.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM job_failure_budget').fetchone()[0],0)
        self.assertEqual(self.store.get(self.key)['state'],'retry')
        self.store.save(self.key,3,self.r,state='processing');self.engine.run('publish',self.key)
        self.assertEqual(self.g.request.call_count,1)
    def test_postupload_invalidation_keeps_original_audit_not_new_bundle_binding(self):
        import copy
        self.g.snapshot.side_effect=[[],[{'number':3,'updated_at':'new'}]]
        original=copy.deepcopy(self.bundle)
        with self.assertRaisesRegex(Failure,'OPEN_SPI_CHANGED'):self.engine.run('publish',self.key)
        result=self.store.get(self.key)['result'];audit=copy.deepcopy(result['snapshot_audit'])
        self.assertEqual(audit[0]['evidence']['bundle'],original)
        for (key,raw),receipt in zip(storage_objects(original),audit[0]['storage_publication']['verified']):
            self.assertEqual(receipt['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(receipt['key'],key)
        replacement=prepare_bundle(event(),{},[{'name':'new.json','content':'different fake bytes'}])
        result.update(evidence={'bundle':replacement},report=report(),match={'candidates':[]},open_signature=digest([]))
        self.store.save(self.key,3,result,state='processing')
        self.g.snapshot.side_effect=None;self.g.snapshot.return_value=[]
        self.engine.run('publish',self.key)
        final=self.store.get(self.key)['result']
        self.assertEqual(final['snapshot_audit'],audit)
        self.assertNotEqual(final['storage_publication']['binding'],audit[0]['storage_publication']['binding'])
        self.assertEqual(self.bundle,original);self.g.request.assert_called_once()

    def test_twenty_late_churns_are_durably_bounded(self):
        import copy
        original=copy.deepcopy(self.bundle)
        for cycle in range(20):
            current=self.store.get(self.key)
            if current['state']=='dead':
                with self.assertRaises(Failure):self.engine.run('publish',self.key)
                continue
            r=current['result']
            if current['stage']==0:
                r.update(evidence={'bundle':prepare_bundle(event(),{},[{'name':'logs.json','content':str(cycle)}])},
                         report=report(),match={'candidates':[]},open_signature=digest([]))
                self.store.save(self.key,3,r,state='processing')
            self.g.snapshot.side_effect=[[],[{'number':3,'updated_at':str(cycle)}]]
            with self.assertRaises(Failure):self.engine.run('publish',self.key)
        final=self.store.get(self.key)
        self.assertEqual(final['state'],'dead')
        self.assertEqual(final['error'],'WRITE_NEEDS_REVIEW')
        self.assertEqual(len(final['result']['snapshot_audit']),2)
        self.assertEqual(final['result']['snapshot_audit'][0]['evidence']['bundle'],original)
        self.assertIn('report',final['result'])
        self.assertEqual(final['result']['storage_publication']['binding'],final['result']['storage_publication']['verified'])
        self.assertEqual(self.s.ensure_object.call_count,6)
        self.g.request.assert_not_called()

    def test_real_stage_route_bounds_analysis_and_uploads(self):
        self.store.save(self.key,0,{},state='processing')
        self.engine.model.match.return_value={'candidates':[]}
        self.engine.model.analyze.return_value=report()
        self.g.open_signature.return_value=digest([])
        self.engine.evidence.collect.side_effect=lambda event: {'bundle':prepare_bundle(event,{},[{'name':'logs.json','content':str(self.engine.evidence.collect.call_count)}])}
        for cycle in range(20):
            if self.store.get(self.key)['state']=='dead':
                with self.assertRaises(Failure):self.engine.run('match',self.key)
                continue
            r=self.store.get(self.key)['result']
            self.store.save(self.key,0,r,state='processing')
            self.g.snapshot.side_effect=None;self.g.snapshot.return_value=[]
            for stage in ('match','collect','analyze'):self.engine.run(stage,self.key)
            self.g.snapshot.side_effect=[[],[{'number':3,'updated_at':str(cycle)}]]
            with self.assertRaises(Failure):self.engine.run('publish',self.key)
        self.assertEqual(self.engine.model.analyze.call_count,3)
        self.assertEqual(self.engine.evidence.collect.call_count,3)
        self.assertEqual(self.s.ensure_object.call_count,6)
        self.assertEqual(len(self.store.get(self.key)['result']['snapshot_audit']),2)
        self.g.request.assert_not_called()

    def test_replacement_transaction_rollback_and_stage_zero_replay(self):
        self.g.snapshot.return_value=[{'number':3,'updated_at':'new'}]
        with self.store.db() as c:
            c.execute("CREATE TRIGGER abort_reset BEFORE UPDATE ON jobs WHEN NEW.stage=0 BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(Failure):self.engine.run('publish',self.key)
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM snapshot_budget').fetchone()[0],0)
            c.execute('DROP TRIGGER abort_reset')
        current=self.store.get(self.key)
        self.store.save(self.key,3,current['result'],state='processing')
        with self.assertRaisesRegex(Failure,'OPEN_SPI_CHANGED'):self.engine.run('publish',self.key)
        saved=self.store.get(self.key)
        self.store.replace_snapshot(self.key,saved,saved['result'],True)
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT replacements FROM snapshot_budget').fetchone()[0],1)

    def test_late_preupload_bound_survives_restart(self):
        for cycle in range(3):
            self.store=Store(self.store.path);self.engine.store=self.store
            r=self.store.get(self.key)['result']
            r.update(report=report(),match={'candidates':[]},open_signature=digest([]),evidence={'bundle':self.bundle})
            self.store.save(self.key,3,r,state='processing')
            self.g.snapshot.return_value=[{'number':3,'updated_at':str(cycle)}]
            with self.assertRaisesRegex(Failure,'WRITE_NEEDS_REVIEW' if cycle==2 else 'OPEN_SPI_CHANGED'):
                self.engine.run('publish',self.key)
        self.assertEqual(self.store.get(self.key)['state'],'dead')
        self.s.ensure_object.assert_not_called();self.g.request.assert_not_called()

    def test_old_result_reset_fails_closed_with_independent_retention(self):
        self.g.snapshot.side_effect=[[],[{'number':3,'updated_at':'new'}]]
        with self.assertRaises(Failure):self.engine.run('publish',self.key)
        retained=self.store.get(self.key)['result']
        self.store.save(self.key,0,{},state='processing')
        self.engine.store=Store(self.store.path)
        with self.assertRaisesRegex(Failure,'WRITE_NEEDS_REVIEW'):self.engine.run('match',self.key)
        with self.store.db() as c:
            import json
            ledger=c.execute('SELECT * FROM snapshot_budget').fetchone()
            self.assertEqual(json.loads(ledger['retained']),retained)
            self.assertEqual(ledger['replacements'],1)
        self.engine.model.match.assert_not_called()

    def test_unknown_overbound_audit_retained(self):
        self.r['snapshot_audit']=[{'unknown':n} for n in range(4)]
        self.store.save(self.key,3,self.r)
        with self.assertRaisesRegex(Failure,'WRITE_NEEDS_REVIEW'):self.engine.run('publish',self.key)
        self.assertEqual(self.store.get(self.key)['result'],self.r)
        self.s.ensure_object.assert_not_called()

    def test_changed_snapshot_preserves_lock(self):
        self.g.snapshot.side_effect=[[],[{'number':3,'updated_at':'new'}]]
        with self.assertRaisesRegex(Failure,'OPEN_SPI_CHANGED'):self.engine.run('publish',self.key)
        self.g.request.assert_not_called();self.assertEqual(self.store.get(self.key)['result']['publication_backend'],'s3');self.assertEqual(self.store.get(self.key)['result']['snapshot_audit'][0]['storage_publication']['binding'],self.store.get(self.key)['result']['snapshot_audit'][0]['storage_publication']['verified'])
    def test_checkpoint_tampering_rejected(self):
        self.r['publication_backend']='s3';self.r['storage_publication']={'binding':[],'verified':[]}
        self.store.save(self.key,3,self.r)
        with self.assertRaisesRegex(Failure,'STORAGE_INTEGRITY'):self.engine.run('publish',self.key)
        self.g.request.assert_not_called();self.s.ensure_object.assert_not_called()
    def test_empty_bundle(self):
        self.r['evidence']={};self.store.save(self.key,3,self.r)
        self.engine.run('publish',self.key);self.s.ensure_object.assert_not_called();self.assertEqual(self.g.request.call_count,1)
    def test_new_legacy_lock_survives_toggle_after_rejection(self):
        self.engine.storage_backend='legacy';self.g.comments.return_value=[]
        self.g.request.side_effect=Failure('HTTP_429')
        with self.assertRaises(Failure):self.engine.run('publish',self.key)
        saved=self.store.get(self.key);self.assertEqual(saved['result']['publication_backend'],'legacy')
        self.store.save(self.key,3,saved['result'],state='processing')
        self.engine.storage_backend='s3';self.g.request.side_effect=None
        self.engine.run('publish',self.key);self.s.ensure_object.assert_not_called()
    def test_legacy_orphan_checkpoints_preserved(self):
        import copy
        for progress in ({'pending':self.bundle['parts'][0]['marker']}, {'done':{'original':'url'}}, {'index_pending':True}, {'index_done':True}):
            with self.subTest(progress=progress):
                r=copy.deepcopy(self.r);r['artifact_publication']=progress
                self.store.save(self.key,3,r,state='processing',intent=None)
                self.g.snapshot.return_value=[{'number':77,'updated_at':'changed'}]
                with self.assertRaisesRegex(Failure,'WRITE_NEEDS_REVIEW'):self.engine.run('publish',self.key)
                saved=self.store.get(self.key)['result']
                self.assertEqual(saved,{**r,'publication_backend':'legacy'})
                self.assertEqual(self.store.get(self.key)['stage'],3)
        self.g.snapshot.assert_not_called();self.g.request.assert_not_called();self.s.ensure_object.assert_not_called()

    def test_completed_legacy_orphan_is_noop(self):
        self.r['artifact_publication']={'index_pending':True}
        self.store.save(self.key,4,self.r,state='complete')
        self.engine.run('publish',self.key)
        self.assertEqual(self.store.get(self.key)['result'],self.r)
        self.g.request.assert_not_called();self.g.snapshot.assert_not_called();self.s.ensure_object.assert_not_called()

    def test_legacy_intent_reconciles_before_snapshot_each_checkpoint(self):
        import copy,json
        for field in ('pending','done','index_pending','index_done'):
            with self.subTest(field=field):
                progress={'done':{p['marker']:'url' for p in self.bundle['parts']},'index_done':True}
                if field=='pending':
                    progress['pending']=self.bundle['parts'][0]['marker']
                    progress['done'].pop(progress['pending'])
                if field=='index_pending':
                    progress['index_pending']=True
                    progress.pop('index_done')
                r=copy.deepcopy(self.r);r['artifact_publication']=progress
                self.store.save(self.key,3,r,state='processing',intent=json.dumps({'number':9}))
                from evidence_bundle import index_marker
                self.g.reconcile.return_value=9
                self.g.comments.return_value=[{'body':marker,'html_url':'url','user':{'type':'Bot'},'performed_via_github_app':{'id':5569869}} for marker in [self.bundle['parts'][0]['marker'],index_marker(self.bundle)]]
                original_save=self.store.save
                def checkpoint(*args,**kwargs):
                    original_save(*args,**kwargs)
                    if args[1]==3:self.assertIsNotNone(self.store.get(self.key)['intent'])
                with __import__('unittest').mock.patch.object(self.store,'save',side_effect=checkpoint):
                    self.engine.run('publish',self.key)
                self.assertEqual(self.store.get(self.key)['state'],'complete')
                self.assertEqual(self.store.get(self.key)['result']['evidence'],r['evidence'])
        self.g.snapshot.assert_not_called();self.g.request.assert_not_called();self.s.ensure_object.assert_not_called()

    def resolved_job(self):
        key=self.store.enqueue(event('resolved'))['job_id']
        r={'resolved_issue':9,'evidence':{'bundle':self.bundle}}
        self.store.save(key,3,r,state='processing')
        return key

    def test_resolved_closes_during_upload(self):
        key=self.resolved_job();self.g.request.side_effect=[{'state':'open'},{'state':'closed'}]
        def upload(k,b):
            return {'bucket':'spi-evidence','key':k,'sha256':hashlib.sha256(b).hexdigest(),'size':len(b),'verified':True}
        self.s.ensure_object.side_effect=upload
        result=self.engine.run('publish',key)
        self.assertEqual(result['status'],'resolved_without_open_spi')
        self.assertEqual([c.args[0] for c in self.g.request.call_args_list],['GET','GET'])
        self.assertEqual(self.s.ensure_object.call_count,2)

    def test_resolved_intent_not_abandoned_when_closed(self):
        import json
        key=self.resolved_job()
        r=self.store.get(key)['result'];r['publication_backend']='s3'
        self.store.save(key,3,r,intent=json.dumps({'number':9}))
        self.s.ensure_object.side_effect=lambda k,b: {'bucket':'spi-evidence','key':k,'sha256':hashlib.sha256(b).hexdigest(),'size':len(b),'verified':True}
        self.g.reconcile.return_value=9
        self.engine.run('publish',key)
        self.g.reconcile.assert_called_once();self.g.request.assert_not_called()
        self.assertEqual(self.store.get(key)['state'],'complete')

    def test_storage_conflict_never_falls_back(self):
        from s3_evidence import StorageConflict
        self.s.ensure_object.side_effect=StorageConflict()
        with self.assertRaisesRegex(Failure,'STORAGE_CONFLICT'):self.engine.run('publish',self.key)
        self.assertEqual(self.s.ensure_object.call_count,1)
        self.g.request.assert_not_called();self.g.comments.assert_not_called()
        self.assertEqual(self.store.get(self.key)['result']['publication_backend'],'s3')

    def test_ambiguous_storage_then_github_one_primary(self):
        # An accepted PUT can lose its response. The next ensure reconciles it;
        # an accepted primary can likewise lose its response, never POST twice.
        stored={};posts=[]
        def ensure(k,b):
            if k not in stored:
                stored[k]=b
                if len(stored)==1:raise StorageTransient(1)
            self.assertEqual(stored[k],b)
            return self.verify(k,b)
        self.s.ensure_object.side_effect=ensure
        with self.assertRaisesRegex(Failure,'STORAGE_TRANSIENT'):self.engine.run('publish',self.key)
        self.store.save(self.key,3,self.store.get(self.key)['result'],state='processing')
        def post(method,path,data):
            self.assertEqual(len(self.store.get(self.key)['result']['storage_publication']['verified']),2)
            posts.append(data);raise Failure('HTTP_502')
        self.g.request.side_effect=post
        with self.assertRaisesRegex(Failure,'HTTP_502'):self.engine.run('publish',self.key)
        saved=self.store.get(self.key)
        self.store.save(self.key,3,saved['result'],state='processing')
        self.g.reconcile.return_value=9
        self.engine.storage_backend='legacy'
        self.engine.run('publish',self.key)
        self.assertEqual(len(posts),1);self.g.comments.assert_not_called()
        self.assertEqual(self.store.get(self.key)['state'],'complete')
        from evidence_bundle import encoded
        values=list(stored.values())
        self.assertEqual(values[0],encoded(self.bundle['manifest']).encode('ascii'))
        self.assertEqual(values[1],''.join(p['text'] for p in self.bundle['parts'] if p['id']=='E001').encode('ascii'))

    def test_patient_engine_failures_then_github_recovery(self):
        for _ in range(20):self.store.fail(self.key,'LLM_HTTP_429',1)
        self.s.ensure_object.side_effect=StorageTransient(1)
        for _ in range(12):
            self.store.save(self.key,3,self.store.get(self.key)['result'],state='processing')
            with self.assertRaisesRegex(Failure,'STORAGE_TRANSIENT'):self.engine.run('publish',self.key)
        with self.store.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM job_failure_budget').fetchone()[0],0)
        self.s.ensure_object.side_effect=self.verify
        self.g.request.side_effect=Failure('HTTP_429')
        self.store.save(self.key,3,self.store.get(self.key)['result'],state='processing')
        with self.assertRaisesRegex(Failure,'HTTP_429'):self.engine.run('publish',self.key)
        self.store.save(self.key,3,self.store.get(self.key)['result'],state='processing')
        self.g.request.side_effect=None
        self.engine.run('publish',self.key)
        self.assertEqual(self.store.get(self.key)['state'],'complete')
