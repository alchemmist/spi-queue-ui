import copy
import json
import struct
import unittest
import zlib
from graph_contract import (GraphError, canonical, parse_annotation, resolve,
                            render_path, validate_png)


def graph(**changes):
    return {'id': 'primary', 'title': 'Latency', 'dashboard_uid': 'service-health',
            'panel_id': 12, 'variables': {'service': {'label': 'service'}}, **changes}


def annotation(graphs=None, **changes):
    return json.dumps({'version': 1, 'graphs': graphs or [graph()], **changes})


def png(width=1200, height=600, pixels=None):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind+body) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels if pixels is not None else b'\0' * ((width*3+1)*height)))
            + chunk(b'IEND', b''))


class GraphContractTests(unittest.TestCase):
    def test_missing_is_compatible(self):
        self.assertEqual(parse_annotation(None), [])

    def test_order_and_defaults(self):
        specs = parse_annotation(annotation([graph(), graph(id='context', panel_id=18)]))
        self.assertEqual([s['id'] for s in specs], ['primary', 'context'])
        self.assertEqual(specs[0]['before_seconds'], 1800)
        self.assertEqual(specs[0]['after_seconds'], 300)

    def test_strict_contract(self):
        bad = ['', '{}', '[]', '{"version":1,"version":1,"graphs":[]}']
        bad += [annotation(version=True), annotation(version=2), annotation([graph(), graph()])]
        bad += [annotation([graph(panel_id=True)]), annotation([graph(dashboard_uid='../secret')]),
                annotation([graph(variables={'service': {'url': 'https://evil'}})]),
                annotation([graph(before_seconds=604800)]), annotation([graph(title='bad\nlabel')]),
                annotation([graph() for _ in range(17)]), ' ' * 32769]
        for raw in bad:
            with self.subTest(raw=raw[:80]), self.assertRaises(GraphError):
                parse_annotation(raw)

    def request(self, **changes):
        args = dict(starts_at='2026-11-08T12:00:00Z', observed_at='2026-11-08T12:02:00Z')
        args.update(changes)
        return resolve(graph(), {'service': 'api &worker/α'}, **args)

    def test_time_and_variable_encoding(self):
        req = self.request()
        self.assertEqual(req['to_ms'] - req['from_ms'], 32*60000)
        self.assertEqual(req['mode'], 'onset')
        path = render_path(req)
        self.assertTrue(path.startswith('/render/d-solo/service-health/_?'))
        self.assertIn('var-service=api+%26worker%2F%CE%B1', path)
        self.assertIn('tz=UTC', path)

    def test_ongoing_and_spi_onset(self):
        req = self.request(observed_at='2026-11-08T13:00:00Z')
        spi = self.request(observed_at='2026-11-08T13:00:00Z', purpose='spi')
        self.assertEqual(req['mode'], 'ongoing')
        self.assertEqual(spi['mode'], 'onset')
        self.assertEqual(spi['to_ms'] - spi['from_ms'], 35*60000)
        self.assertNotEqual(req['digest'], spi['digest'])

    def test_resolved(self):
        req = self.request(status='resolved', ends_at='2026-11-08T12:01:00Z')
        self.assertEqual(req['mode'], 'resolved')
        with self.assertRaises(GraphError):
            self.request(status='resolved', ends_at='2026-11-08T12:03:00Z')

    def test_missing_label_never_all(self):
        for value in (None, '', ' ', 'All', '$__all', '.*', 123):
            with self.subTest(value=value), self.assertRaises(GraphError):
                resolve(graph(), {'service': value}, starts_at='2026-11-08T12:00:00Z',
                        observed_at='2026-11-08T12:02:00Z')

    def test_digest_deterministic_and_isolated(self):
        self.assertEqual(self.request(), self.request())
        self.assertNotEqual(self.request()['digest'], self.request(observed_at='2026-11-08T12:03:00Z')['digest'])
        self.assertEqual(canonical({'b': 1, 'a': 2}), canonical({'a': 2, 'b': 1}))

    def test_timestamp_rejections(self):
        for value in ('bad', '2026-11-08T12:00:00', '2026-11-08T11:00:00Z', None):
            with self.subTest(value=value), self.assertRaises(GraphError):
                self.request(observed_at=value)

    def test_render_revalidates_untrusted_request(self):
        for key, value in [('dashboard_uid', 'http://evil'), ('width', 100000),
                           ('from_ms', -1), ('variables', {'x': 'All'})]:
            req = copy.deepcopy(self.request()); req[key] = value
            with self.subTest(key=key), self.assertRaises(GraphError):
                render_path(req)

    def test_valid_png(self):
        data = png()
        result = validate_png(data)
        self.assertEqual((result['width'], result['height']), (1200, 600))
        self.assertEqual(result['size'], len(data))
        self.assertEqual(len(result['sha256']), 64)

    def test_invalid_pngs(self):
        good = png()
        for data in (b'<html>Login</html>', good[:-1], good+b'extra',
                     good[:40]+b'x'+good[41:], png(width=1),
                     png(pixels=b'\0' * (1200*600*6)), png(pixels=b'\xff' * (3601*600)),
                     b'\x89PNG\r\n\x1a\n' + b'x' * (2*1024*1024)):
            with self.subTest(size=len(data)), self.assertRaises(GraphError):
                validate_png(data)


if __name__ == '__main__':
    unittest.main()
