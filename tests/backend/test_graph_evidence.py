import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import Failure
from evidence_bundle import prepare_bundle, storage_objects, publish_parts
from graph_contract import GraphError, validate_png
import graph_evidence as graphs
from test_graph_contract import annotation, graph, png


class GraphEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = patch.object(graphs, 'ARTIFACT_ROOT', Path(self.tmp.name)/'graphs')
        root.start(); self.addCleanup(root.stop)
        space=patch.object(graphs,'MIN_FREE_BYTES',0);space.start();self.addCleanup(space.stop)
        self.event = {'event_key': 'a'*64, 'episode': 'b'*64, 'status': 'firing',
                      'startsAt': '2020-01-01T00:00:00Z', 'endsAt': None,
                      'labels': {'service': 'api'}, 'annotations': {'spi_graphs': annotation()}}
        self.raw = png()
        self.client = Mock()
        self.client.render.return_value = (self.raw, validate_png(self.raw))

    def test_retry_reuses_snapshot_without_transport(self):
        first, missing = graphs.collect(self.event, self.client)
        self.assertFalse(missing)
        second, missing = graphs.collect(self.event, self.client)
        self.assertFalse(missing)
        self.assertEqual(first, second)
        self.assertEqual(self.client.render.call_count, 1)
        self.assertEqual(graphs.read_artifact(first[0]['reference']), self.raw)

    def test_model_context_and_binary_roundtrip(self):
        captured, missing = graphs.collect(self.event, self.client)
        bundle = prepare_bundle(self.event, {}, [{'name': 'logs.json', 'content': 'original text'}])
        original_context = copy.deepcopy(bundle['model_context'])
        result = graphs.attach(bundle, captured, missing)
        self.assertEqual(result['model_context'], original_context)
        self.assertEqual(bundle['manifest']['format'], 'socrat-evidence-v2')
        self.assertEqual(result['manifest']['format'], 'socrat-evidence-v3')
        self.assertNotIn('data_base64', json.dumps(result))
        objects = storage_objects(result)
        self.assertEqual(objects[-1][1], self.raw)
        self.assertTrue(objects[-1][0].endswith('.png'))
        manifest = json.loads(objects[0][1])
        self.assertEqual(manifest['artifacts'][-1]['sha256'], hashlib.sha256(self.raw).hexdigest())
        # The external Reader contract already supports v3 and must read exact bytes.
        path = Path(__file__).resolve().parents[2]/'integrations/evidence/gateway.py'
        spec = importlib.util.spec_from_file_location('graph_reader_contract', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        reader = object.__new__(module.Reader)
        reader.object = dict(objects).__getitem__
        self.assertEqual(reader.read(objects[-1][0]), self.raw)
        self.assertEqual(reader.read(objects[1][0]), objects[1][1])
        with self.assertRaisesRegex(Failure, 'GRAPH_REQUIRES_S3'):
            publish_parts(Mock(), 1, result, {}, Mock())

    def test_capture_is_partial_and_honest(self):
        self.event['annotations']['spi_graphs'] = annotation([graph(), graph(id='missing', variables={'node': {'label': 'node'}})])
        captured, missing = graphs.collect(self.event, self.client)
        self.assertEqual(len(captured), 1)
        self.assertEqual(missing, [{'source': 'graph:missing', 'reason': 'MISSING_LABEL'}])

    def test_spec_change_cannot_rewrite_immutable_capture(self):
        graphs.collect(self.event, self.client)
        self.event['annotations']['spi_graphs'] = annotation([graph(panel_id=13)])
        with self.assertRaisesRegex(GraphError, 'GRAPH_CHECKPOINT_CONFLICT'):
            graphs.collect(self.event, self.client)

    def test_collection_budget_defers_without_losing_captured_png(self):
        self.event['annotations']['spi_graphs']=annotation([graph(),graph(id='context',panel_id=13)])
        now=[0]
        def render(*args,**kwargs):
            now[0]+=10
            return self.raw,validate_png(self.raw)
        self.client.render.side_effect=render
        with patch('graph_evidence.time.monotonic',side_effect=lambda:now[0]):
            with self.assertRaisesRegex(GraphError,'GRAPH_CAPTURE_DEFERRED'):
                graphs.collect(self.event,self.client,deadline=10)
            captured,missing=graphs.collect(self.event,self.client,deadline=30)
        self.assertEqual(len(captured),2);self.assertFalse(missing)
        self.assertEqual(self.client.render.call_count,2)

    def test_no_path_traversal_or_symlink(self):
        for ref in ({'event_key': '../outside', 'render_digest': 'a'*64},
                    {'event_key': 'a'*64, 'render_digest': '../outside'}):
            with self.assertRaises(GraphError): graphs.read_artifact(ref)
        captured, _ = graphs.collect(self.event, self.client)
        ref = captured[0]['reference']
        path = graphs.ARTIFACT_ROOT/ref['event_key']/(ref['render_digest']+'.png')
        path.unlink(); path.symlink_to('/etc/passwd')
        with self.assertRaises(OSError): graphs.read_artifact(ref)

    def test_tamper_detected_before_s3(self):
        captured, missing = graphs.collect(self.event, self.client)
        bundle = graphs.attach(prepare_bundle(self.event, {}, []), captured, missing)
        ref = captured[0]['reference']
        path = graphs.ARTIFACT_ROOT/ref['event_key']/(ref['render_digest']+'.png')
        path.write_bytes(b'not a png')
        with self.assertRaisesRegex(Failure, 'STORAGE_INTEGRITY'):
            storage_objects(bundle)


if __name__ == '__main__': unittest.main()
