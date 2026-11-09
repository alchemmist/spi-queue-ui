import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from graph_contract import GraphError, canonical, resolve
from graph_gateway import Server, secret
from graph_renderer import Renderer
from test_graph_contract import graph, png


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.raw = png()
        self.request = resolve(graph(), {'service': 'api'},
                               starts_at='2026-11-08T12:00:00Z',
                               observed_at='2026-11-08T12:02:00Z')
        self.calls = []

    def wire(self, path, seconds):
        self.calls.append((path, seconds))
        return 200, 'image/png', self.raw

    def renderer(self, **kw):
        return Renderer('private-test-token', {'service-health:12': ['service']},
                        transport=kw.pop('transport', self.wire), **kw)

    def test_render_and_cache(self):
        engine = self.renderer()
        body, meta = engine.render(self.request, caller='telegram', seconds=4)
        self.assertEqual(body, self.raw)
        self.assertEqual(json.loads(meta)['sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(engine.render(self.request, caller='spi', seconds=20), (body, meta))
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('private', self.calls[0][0])

    def test_cache_expiry(self):
        now = [0]
        engine = self.renderer(clock=lambda: now[0])
        engine.render(self.request, caller='spi', seconds=20)
        now[0] = 3601
        engine.render(self.request, caller='spi', seconds=20)
        self.assertEqual(len(self.calls), 2)

    def test_policy_and_digest_before_network(self):
        for key, value in [('panel_id', 13), ('digest', 'fake'), ('version', True)]:
            req = {**self.request, key: value}
            if key == 'panel_id':
                req['digest'] = hashlib.sha256(canonical({k: v for k,v in req.items() if k != 'digest'}).encode()).hexdigest()
            with self.subTest(key=key), self.assertRaises(GraphError):
                self.renderer().render(req, caller='spi', seconds=20)
        self.assertEqual(self.calls, [])

    def test_invalid_upstreams_never_cached(self):
        for response in [(302, 'image/png', self.raw), (200, 'text/html', b'login'),
                         (200, 'image/png', b'not a png')]:
            engine = self.renderer(transport=lambda *args: response)
            for _ in range(2):
                with self.assertRaises(GraphError):
                    engine.render(self.request, caller='spi', seconds=20)
            self.assertFalse(engine._cache)

    def test_total_deadline(self):
        now = [0]
        def slow(*args):
            now[0] += 21
            return 200, 'image/png', self.raw
        engine = self.renderer(transport=slow, clock=lambda: now[0])
        with self.assertRaisesRegex(GraphError, 'RENDER_TIMEOUT'):
            engine.render(self.request, caller='spi', seconds=20)
        self.assertFalse(engine._cache)
        self.assertFalse(engine._inflight)

    def test_spi_cannot_consume_telegram_reserved_slot(self):
        started, release = threading.Event(), threading.Event()
        def blocking(*args):
            self.calls.append(args)
            if len(self.calls) == 1:
                started.set()
                release.wait(3)
            return 200, 'image/png', self.raw
        engine = self.renderer(transport=blocking)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.render, self.request, caller='spi', seconds=20)
            try:
                self.assertTrue(started.wait(2))
                other = dict(self.request, mode='resolved')
                other['digest'] = hashlib.sha256(canonical({k:v for k,v in other.items() if k != 'digest'}).encode()).hexdigest()
                with self.assertRaisesRegex(GraphError, 'BUSY'):
                    engine.render(other, caller='spi', seconds=20)
                self.assertEqual(engine.render(other, caller='telegram', seconds=4)[0], self.raw)
                self.assertFalse(first.done())
                release.set()
                first.result(timeout=3)
            finally:
                release.set()

    def test_single_flight(self):
        started, release, follower = threading.Event(), threading.Event(), threading.Event()
        def blocking(*args):
            self.calls.append(args); started.set(); release.wait(3)
            return 200, 'image/png', self.raw
        engine = self.renderer(transport=blocking)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.render, self.request, caller='spi', seconds=20)
            self.assertTrue(started.wait(2))
            def follow():
                follower.set()
                return engine.render(self.request, caller='telegram', seconds=4)
            second = pool.submit(follow)
            self.assertTrue(follower.wait(2)); release.set()
            self.assertEqual(first.result(timeout=3), second.result(timeout=3))
        self.assertEqual(len(self.calls), 1)

    def test_gateway_auth_and_exact_png(self):
        server = Server(('127.0.0.1', 0), self.renderer(),
                        {'telegram': 'test-tg', 'spi': 'test-spi'})
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def post(auth, raw=None, path="/v1/render"):
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                connection.request('POST', path, body=raw or canonical(self.request),
                                   headers={'Authorization': auth, 'Content-Type': 'application/json'})
                r = connection.getresponse()
                return r.status, dict(r.getheaders()), r.read()
            finally:
                connection.close()
        try:
            self.assertEqual(post('Bearer wrong')[0], 401)
            self.assertEqual(post('Bearer test-tg', '{}')[0], 422)
            status, headers, body = post('Bearer test-tg')
            self.assertEqual(status, 200)
            self.assertEqual(body, self.raw)
            self.assertEqual(headers['X-Content-SHA256'], hashlib.sha256(body).hexdigest())
            self.assertEqual(headers['Cache-Control'], 'no-store')
            primary={'annotation':canonical({'version':1,'graphs':[graph(),graph(id='context',panel_id=13)]}),
                     'labels':{'service':'api'},'starts_at':'2026-11-08T12:00:00Z',
                     'observed_at':'2026-11-08T12:02:00Z','ends_at':None,'status':'firing'}
            status,headers,body=post('Bearer test-tg',canonical(primary),'/v1/primary')
            self.assertEqual(status,200);self.assertEqual(body,self.raw)
            self.assertEqual(headers['X-Graph-Mode'],'onset')
            self.assertEqual(headers['X-Render-Digest'],self.request['digest'])
            self.assertEqual(len(self.calls),1)  # second graph isn't the primary
            self.assertEqual(post('Bearer test-tg',canonical({**primary,'labels':{}}),'/v1/primary')[0],422)
            self.assertEqual(post('Bearer test-tg',canonical({**primary,'url':'https://invalid.example'}),'/v1/primary')[0],422)

        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_secret_file_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            path.write_text('synthetic-token-only')
            path.chmod(0o600)
            self.assertEqual(secret(path), 'synthetic-token-only')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                secret(path)


if __name__ == '__main__':
    unittest.main()
