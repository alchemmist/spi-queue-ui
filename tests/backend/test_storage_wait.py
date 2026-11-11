"""Immutable S3 publication can wait; unsafe writes/auth/conflicts cannot."""
import copy,hashlib,socket,unittest
from unittest.mock import patch
import test_s3_engine as fixture
from core import Failure
from s3_evidence import StorageTransient,StoragePermanent,StorageConflict,S3Evidence

class StorageWaitTests(unittest.TestCase):
    verify=fixture.EngineStorageTests.verify
    def setUp(self):
        self.now=1000;clock=patch('time.time',lambda:self.now);clock.start();self.addCleanup(clock.stop)
        fixture.EngineStorageTests.setUp(self)
        row=self.store.owners.bind(self.key,'1234','durable-v1','publish',self.now)
        with self.store.db()as c:c.execute("update workflow_wait_owner set phase='ready' where job_id=?",(self.key,))
        self.data=self.store.owners.envelope(row);self.deadline=row['deadline']
    def test_partial_upload_wait_reuses_binding_and_report_without_model_or_new_claim(self):
        calls=[]
        def upload(k,raw):
            calls.append(k)
            if len(calls)==2:raise StorageTransient(120,reason='DNS_RESOLUTION')
            return self.verify(k,raw)
        self.s.ensure_object.side_effect=upload
        waiting=self.engine.run_v1('publish',self.data)
        self.assertEqual((waiting['status'],waiting['code'],waiting['stage']),('deferred','STORAGE_TRANSIENT','publish'))
        self.assertEqual(waiting['deadline'],self.deadline);self.assertEqual(waiting['execution_id'],'1234')
        retained=copy.deepcopy(self.store.get(self.key)['result']);self.assertEqual(len(retained['storage_publication']['verified']),1)
        self.assertEqual(retained['report'],self.r['report']);self.g.request.assert_not_called()
        self.assertEqual(self.engine.run_v1('publish',self.data),waiting);self.assertEqual(len(calls),2)
        self.now=waiting['due_at'];done=self.engine.run_v1('publish',waiting)
        self.assertEqual(done['status'],'complete');self.assertEqual(calls.count(calls[0]),1)
        self.engine.model.analyze.assert_not_called();self.assertEqual(self.store.get(self.key)['attempts'],1)
    def test_permanent_and_conflict_storage_errors_stay_fatal(self):
        self.s.ensure_object.side_effect=StoragePermanent()
        with self.assertRaisesRegex(Failure,'STORAGE_PERMANENT'):self.engine.run_v1('publish',self.data)
        self.engine.model.analyze.assert_not_called()
    def test_conflicting_bytes_stay_fatal(self):
        self.s.ensure_object.side_effect=StorageConflict()
        with self.assertRaisesRegex(Failure,'STORAGE_CONFLICT'):self.engine.run_v1('publish',self.data)
        self.engine.model.analyze.assert_not_called()
    def test_storage_wait_does_not_extend_absolute_deadline(self):
        self.s.ensure_object.side_effect=StorageTransient();self.now=self.deadline-200
        with self.store.db()as c:c.execute('update workflow_wait_owner set expires=? where job_id=?',(self.now+180,self.key))
        with self.assertRaisesRegex(Failure,'WAIT_DEADLINE_EXCEEDED'):self.engine.run_v1('publish',self.data)
    def test_backend_rejects_storage_defer_outside_publish(self):
        row={'stage':'analyze'}
        with self.assertRaisesRegex(Failure,'UNSAFE_WAIT_ERROR'):self.store.owners.finish(row,True,self.now,code='STORAGE_TRANSIENT')

class StorageReasonTests(unittest.TestCase):
    def test_dns_exception_is_typed_not_raw_message(self):
        def wire(*a):raise socket.gaierror('credential-must-not-leak')
        s=S3Evidence('access','secret',transport=wire)
        with self.assertRaises(StorageTransient)as cm:s.ensure_object('spi/v1/'+'a'*64+'/'+'b'*64+'/manifest.json',b'{}')
        self.assertEqual(cm.exception.reason,'DNS_RESOLUTION');self.assertNotIn('credential',str(cm.exception))
    def test_http_503_temporary_but_403_not(self):
        key='spi/v1/'+'a'*64+'/'+'b'*64+'/manifest.json'
        for status in(503,403):
            s=S3Evidence('access','secret',transport=lambda *a:(status,{},b''))
            if status==503:
                with self.assertRaises(StorageTransient)as cm:s.ensure_object(key,b'{}')
                self.assertEqual(cm.exception.reason,'HTTP_5XX')
            else:
                with self.assertRaises(StoragePermanent):s.ensure_object(key,b'{}')
