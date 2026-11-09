import unittest
from graph_client import GraphClient
from graph_contract import GraphError, resolve, validate_png
from test_graph_contract import graph, png


class GraphClientTests(unittest.TestCase):
    def setUp(self):
        self.request = resolve(graph(), {'service': 'api'}, starts_at='2026-11-08T12:00:00Z',
                               observed_at='2026-11-08T12:02:00Z')
        self.body = png()
        self.headers = {'content-type': 'image/png',
                        'x-content-sha256': validate_png(self.body)['sha256'],
                        'x-render-digest': self.request['digest']}

    def client(self, response):
        return GraphClient('synthetic-token-only', transport=lambda *args: response)

    def test_exact_bytes(self):
        data, meta = self.client((200, self.headers, self.body)).render(self.request)
        self.assertEqual(data, self.body)
        self.assertEqual(meta['render_digest'], self.request['digest'])

    def test_integrity_and_login_fail_closed(self):
        for response in [(302, {}, b''), (200, {'content-type': 'text/html'}, b'login'),
                         (200, {**self.headers, 'x-content-sha256': '0'*64}, self.body),
                         (200, {**self.headers, 'x-render-digest': '0'*64}, self.body),
                         (200, self.headers, self.body[:-1])]:
            with self.subTest(status=response[0]), self.assertRaises(GraphError):
                self.client(response).render(self.request)

    def test_typed_busy(self):
        with self.assertRaisesRegex(GraphError, 'BUSY'):
            self.client((503, {}, b'{"code":"BUSY"}')).render(self.request)

    def test_do_not_echo_upstream_error_text(self):
        with self.assertRaisesRegex(GraphError, '^RENDER_FAILED$'):
            self.client((500, {}, b'{"code":"secret-upstream-message"}')).render(self.request)

    def test_reject_invalid_configuration(self):
        for token in ('', 'short', 'x'*16+'\n', 'x'*4097):
            with self.subTest(length=len(token)), self.assertRaises(ValueError):
                GraphClient(token)


if __name__ == '__main__':
    unittest.main()
