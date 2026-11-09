import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]/'backend'))
from core import Store
from dispatcher import Dispatcher

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name+'/queue.sqlite')
    def enqueue(self,key='a'):
        return self.store.enqueue({'event_key':key,'episode':key})
    def test_empty_disabled_and_coalescing(self):
        calls=[]
        d=Dispatcher(self.store,'unused',post=lambda:calls.append(1))
        self.enqueue();d.tick();self.assertEqual(calls,[])
        d.enabled=True;d.tick();d.tick();self.assertEqual(calls,[1])
        self.assertGreater(self.store.dispatch_status()['deadline'],0)
    def test_empty(self):
        d=Dispatcher(self.store,'unused',True,lambda:self.fail('network'))
        self.assertIsNone(d.tick())
    def test_ambiguous_persists_and_ack_race(self):
        self.enqueue()
        def post():
            self.assertEqual(self.store.claim()['job_id'],'a')
            raise TimeoutError()
        Dispatcher(self.store,'unused',True,post).tick()
        self.assertEqual(self.store.dispatch_status()['attempts'],0)
        self.assertIsNone(self.store.reserve_dispatch()[0])
    def test_deadlines_recovery_and_terminal_lease(self):
        self.enqueue();self.enqueue('b')
        self.store.claim();self.store.save('a',4,{},state='dry_run')
        self.assertIsNotNone(self.store.reserve_dispatch()[0])
        self.assertEqual(self.store.claim()['job_id'],'b')
        self.assertEqual(self.store.recover_on_start(),1)
        self.assertIsNotNone(self.store.reserve_dispatch()[0])
    def test_backoff_restart_and_due_retry(self):
        with patch('core.time.time',return_value=100),patch('core.random.uniform',return_value=1):
            self.enqueue();token,deadline=self.store.reserve_dispatch()
            self.assertEqual(deadline,130)
            reopened=Store(self.store.path)
            self.assertEqual(reopened.reserve_dispatch(129),(None,130))
            self.assertEqual(reopened.reserve_dispatch(130)[1],190)
            self.store.claim();self.store.fail('a','INTERNAL_ERROR')
            self.assertEqual(self.store.reserve_dispatch(101),(None,160))
    def test_expired_processing_lease(self):
        with patch('core.time.time',return_value=100):
            self.enqueue();self.store.claim()
        self.assertEqual(self.store.reserve_dispatch(2079),(None,2080))
        self.assertIsNotNone(self.store.reserve_dispatch(2080)[0])


class ServiceSafetyTests(unittest.TestCase):
    def test_expired_lease_while_stage_busy(self):
        from service import Engine
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp+'/queue.sqlite')
            with patch('core.time.time',return_value=100):
                store.enqueue({'event_key':'a','episode':'a'})
                store.claim()
            engine=Engine(store,None,None,None)
            calls=[]
            dispatcher=Dispatcher(store,'unused',True,lambda:calls.append(1),busy=engine.lock.locked)
            engine.lock.acquire()
            try:
                self.assertEqual(engine.claim(),{'job_id':''})
                self.assertIsNone(dispatcher.tick())
                self.assertEqual(calls,[])
                self.assertEqual(store.get('a')['attempts'],1)
            finally:engine.lock.release()
            dispatcher.tick()
            self.assertEqual(calls,[1])
            self.assertEqual(engine.claim(),{'job_id':'a'})

    def test_body_validation_and_partial_timeout(self):
        import socket
        from types import SimpleNamespace
        from service import read_request_body
        from core import Failure
        def check(raw,length,code,shutdown=True):
            left,right=socket.socketpair()
            try:
                right.sendall(raw)
                if shutdown:right.shutdown(socket.SHUT_WR)
                with left.makefile('rb') as stream:
                    handler=SimpleNamespace(headers={'Content-Length':length},connection=left,rfile=stream)
                    with self.assertRaises(Failure) as caught:read_request_body(handler,timeout=0.03)
                    self.assertEqual(caught.exception.code,code)
                    self.assertIsNone(left.gettimeout())
            finally:left.close();right.close()
        check(b'{', '2','INCOMPLETE_BODY')
        check(b'{', '2','REQUEST_BODY_TIMEOUT',False)
        check(b'[]','2','INVALID_REQUEST')
        check(b'', '-1','INVALID_BODY_SIZE')
        check(b'', 'bad','INVALID_BODY_SIZE')

class DispatchTransportTests(unittest.TestCase):
    def test_trickling_headers_are_interrupted_without_body_read(self):
        import socket
        import threading
        import http.client
        class Connection:
            def __init__(self,*args,**kwargs):
                self.sock,self.peer=socket.socketpair()
                self.closed=False
            def connect(self):pass
            def request(self,*args,**kwargs):pass
            def getresponse(self):
                # Simulates a parser waiting indefinitely for complete headers.
                self.sock.recv(1)
                raise http.client.RemoteDisconnected()
            def close(self):
                self.closed=True
                self.sock.close();self.peer.close()
        connection=Connection()
        real_timer=threading.Timer
        with patch('dispatcher.http.client.HTTPConnection',return_value=connection), \
             patch('dispatcher.threading.Timer',side_effect=lambda seconds,callback:real_timer(0.02,callback)):
            with self.assertRaises(http.client.RemoteDisconnected):
                Dispatcher(None,'unused')._post()
        self.assertTrue(connection.closed)

if __name__=='__main__':unittest.main()
