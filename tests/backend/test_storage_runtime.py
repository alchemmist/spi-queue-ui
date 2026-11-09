import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
import storage_runtime as runtime
import service


class Runtime(unittest.TestCase):
    def test_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime.load_storage(), (None, 'legacy'))

    def test_projected_file_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)/'real'; link = Path(d)/'credentials.json'
            link.symlink_to(target)
            real_open = os.open
            def opened(path, flags, *args):
                return real_open(link if str(path) == '/s3-writer/credentials.json' else path, flags, *args)
            for backend in ('legacy', 's3'):
                with patch.dict(os.environ, {'SPI_EVIDENCE_BACKEND': backend, 'SPI_S3_WRITER_FILE':'/s3-writer/credentials.json'}, clear=True), patch.object(runtime.os, 'open', side_effect=opened):
                    target.touch(); target.chmod(0o600); target.write_text(json.dumps({'access':'writer', 'secret':'private'})); target.chmod(0o440)
                    client, flag = runtime.load_storage()
                    self.assertIsInstance(client, runtime.S3Evidence)
                    self.assertEqual(flag, backend)
                    for content, mode in [('{}',0o600), ('{"access":"a","secret":"b","secret":"c"}',0o600), ('x'*8193,0o600), ('{"access":"a","secret":"b"}',0o644)]:
                        target.chmod(0o600); target.write_text(content); target.chmod(mode)
                        with self.assertRaisesRegex(RuntimeError, '^S3_WRITER_CONFIGURATION_INVALID$'):
                            runtime.load_storage()

    def test_serve_wires_engine_before_dispatch(self):
        class Stop(Exception): pass
        storage = object()
        with patch.object(service, 'load_storage', return_value=(storage,'legacy')), patch.object(service, 'Store'), patch.object(service,'HTTP'), patch.object(service,'GitHub'), patch.object(service,'Model'), patch.object(service,'Evidence'), patch.object(service,'Delivery'), patch.object(service,'log_event'), patch.object(service,'Engine') as engine, patch.object(service,'Dispatcher',side_effect=Stop), patch.dict(os.environ, {'LITELLM_KEY':'fake','GRAFANA_TOKEN':'fake','GRAFANA_URL':'fake','WORKER_TOKEN':'fake'}):
            with self.assertRaises(Stop): service.serve()
            self.assertIs(engine.call_args.kwargs['storage'], storage)
            self.assertEqual(engine.call_args.kwargs['storage_backend'],'legacy')
        with patch.dict(os.environ, {'SPI_EVIDENCE_BACKEND':'s3'}, clear=True), patch.object(service,'Dispatcher') as dispatch, patch.object(service,'Store') as store:
            with self.assertRaisesRegex(RuntimeError, 'S3_WRITER_CONFIGURATION_INVALID'): service.serve()
            dispatch.assert_not_called(); store.assert_not_called()
