import datetime
import hashlib
import sys
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'backend'))
import s3_evidence as s

KEY = 'spi/v1/'+'a'*64+'/'+'b'*64+'/E001-test.json'
class ClientTests(unittest.TestCase):
    def client(self, responses):
        self.calls=[]
        def transport(*args):
            self.calls.append(args)
            value=responses.pop(0)
            if isinstance(value, Exception): raise value
            return value
        return s.S3Evidence('access','secret',transport=transport)
    def test_resolver_capacity_survives_sequential_timeouts(self):
        release = threading.Event()
        workers = []
        slots = threading.BoundedSemaphore(s.MAX_RESOLVERS)
        def stuck(*args, **kwargs):
            workers.append(threading.current_thread())
            release.wait()
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 9))]
        with patch.object(s, '_RESOLVER_SLOTS', slots), patch.object(s.socket, 'getaddrinfo', stuck), patch.object(s.socket, 'socket') as make_socket:
            try:
                for _ in range(24):
                    conn = s.HTTPConnection('unused.invalid', 9000, timeout=.01)
                    conn.budget = .01
                    with self.assertRaises(TimeoutError):
                        conn.connect()
                    conn.close()
                self.assertEqual(len(workers), s.MAX_RESOLVERS)
                self.assertTrue(all(worker.is_alive() for worker in workers))
            finally:
                release.set()
                for worker in workers:
                    worker.join(1)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            make_socket.assert_not_called()
            # All slots are returned on actual completion, not caller timeout.
            for _ in range(s.MAX_RESOLVERS):
                self.assertTrue(slots.acquire(blocking=False))
            self.assertFalse(slots.acquire(blocking=False))

    def test_existing(self):
        c=self.client([(200,{},b'abc')]);self.assertTrue(c.ensure_object(KEY,b'abc')['verified']);self.assertEqual(len(self.calls),1)
    def test_private_bucket_qualified_paths_unchanged(self):
        for key in (KEY,KEY.rsplit('/',1)[0]+'/manifest.json'):
            c=self.client([(404,{},b''),(201,{},b''),(200,{},b'abc')])
            receipt=c.ensure_object(key,b'abc')
            self.assertEqual([call[0] for call in self.calls],['GET','PUT','GET'])
            self.assertEqual([call[1] for call in self.calls],['/spi-evidence/'+key]*3)
            self.assertEqual(receipt['bucket'],'spi-evidence')
            self.assertEqual(receipt['key'],key)
            self.assertEqual(receipt['sha256'],hashlib.sha256(b'abc').hexdigest())

    def test_conflict(self):
        c=self.client([(200,{},b'bad')])
        with self.assertRaises(s.StorageConflict):c.ensure_object(KEY,b'abc')
        self.assertEqual(len(self.calls),1)
    def test_conditional_and_races(self):
        for status in (200,201,204,409,412):
            c=self.client([(404,{},b''),(status,{},b''),(200,{},b'abc')]);c.ensure_object(KEY,b'abc')
            self.assertEqual(self.calls[1][2]['If-None-Match'],'*');self.assertEqual(self.calls[1][3],b'abc')
    def test_ambiguous_recovery(self):
        c=self.client([(404,{},b''),TimeoutError(),(200,{},b'abc'),(200,{},b'abc')]);c.ensure_object(KEY,b'abc');c.ensure_object(KEY,b'abc')
        self.assertEqual([x[0] for x in self.calls],['GET','PUT','GET','GET'])
    def test_status(self):
        for status in (408,429,500,503):
            c=self.client([(status,{'Retry-After':'12'},b'')])
            with self.assertRaises(s.StorageTransient) as ctx:c.ensure_object(KEY,b'abc')
            self.assertEqual(ctx.exception.retry_after,12)
        for status in (301,403,401):
            with self.assertRaises(s.StoragePermanent):self.client([(status,{},b'')]).ensure_object(KEY,b'abc')
    def test_validation(self):
        for origin in ('http://localhost',s.ORIGIN+'/',s.ORIGIN+'?x=1'):
            with self.assertRaises(s.StoragePermanent):s.S3Evidence('a','b',origin=origin)
        for key in (KEY.upper(),KEY+'?a',KEY.replace('test','..'),'/'+KEY):
            with self.assertRaises(s.StoragePermanent):self.client([]).ensure_object(key,b'')
        for body in ('abc',bytearray(b'a'),b'x'*(s.MAX_BYTES+1)):
            with self.assertRaises(s.StoragePermanent):self.client([]).ensure_object(KEY,body)
    def test_readback(self):
        for body,error in ((b'bad',s.StorageConflict),(b'x'*(s.MAX_BYTES+1),s.StoragePermanent)):
            with self.assertRaises(error):self.client([(404,{},b''),(200,{},b''),(200,{},body)]).ensure_object(KEY,b'abc')
    def test_retry_date(self):
        date=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=50)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        self.assertTrue(48 <= s.retry_after(date) <= 50)
        self.assertEqual(s.retry_after('999999999'),86400);self.assertIsNone(s.retry_after('invalid'))
    def test_signing(self):
        now=datetime.datetime(2025,1,2,tzinfo=datetime.timezone.utc)
        h=s.sign('PUT',s.ORIGIN+'/spi-evidence/'+KEY,'access','secret',now=now,payload=b'\x00\xff')
        self.assertEqual(h['x-amz-content-sha256'],hashlib.sha256(b'\x00\xff').hexdigest())
        self.assertEqual(h['host'],'rustfs.rustfs.svc.cluster.local:9000')
        self.assertIn('20250102/us-east-1/s3/aws4_request',h['Authorization'])
        self.assertIn('SignedHeaders=host;x-amz-content-sha256;x-amz-date',h['Authorization'])
        # Independent reference implementation already used by private gateway.
        import importlib.util
        spec=importlib.util.spec_from_file_location('gateway',Path(__file__).resolve().parents[2]/'integrations/evidence/gateway.py')
        gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)
        self.assertEqual(h,gateway.sign('PUT',s.ORIGIN+'/spi-evidence/'+KEY,'access','secret',now=now,payload=b'\x00\xff'))
    def test_close_body_deadline_and_cleanup(self):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
        def serve():
            conn,_=listener.accept()
            with conn:
                conn.recv(8192);conn.sendall(b'HTTP/1.0 200 OK\r\n\r\n')
                try:
                    for _ in range(100):conn.sendall(b'x');time.sleep(.02)
                except OSError:pass
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        conn=s.HTTPConnection('127.0.0.1',listener.getsockname()[1],timeout=1);conn.budget=.15
        start=time.monotonic();conn.request('GET','/');response=conn.getresponse()
        self.assertTrue(conn.deadline_timer.is_alive())
        try:
            try:response.read(1000)
            except OSError:pass
        finally:response.close();conn.close();listener.close()
        self.assertLess(time.monotonic()-start,.8)
        conn.deadline_timer.join(.3);self.assertFalse(conn.deadline_timer.is_alive())

if __name__=='__main__':unittest.main()
