import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'backend'))
from core import Store, normalize, Failure
from card_correlation import record_receipt

class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'jobs.sqlite')
        self.payload = {'version': 1, 'source': 'https://am.example', 'alert': {
            'status': 'firing', 'startsAt': '2026-01-01T00:00:00Z',
            'fingerprint': '', 'labels': {'alertname': 'Test', 'secret': 'private'},
            'annotations': {}}, 'receipt': {'key': 'Test/cluster', 'copies': [
                {'chat_id': 1, 'message_id': 2}, {'chat_id': 3, 'message_id': 4}]}}
    def tearDown(self): self.tmp.cleanup()
    def test_restart_duplicates_raw_identity_and_copies(self):
        expected = normalize(self.payload)
        for store in [self.store, Store(self.store.path)]:
            result = record_receipt(store, self.payload)
            self.assertEqual(result['event_key'], expected['event_key'])
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM telegram_receipts').fetchone()[0], 2)
        self.assertNotIn(b'private', Path(self.store.path).read_bytes())
    def test_resolved_flap_and_unknown(self):
        firing = record_receipt(self.store, self.payload)
        self.payload['alert']['status'] = 'resolved'
        resolved = record_receipt(self.store, self.payload)
        self.assertEqual(firing['episode'], resolved['episode'])
        self.assertNotEqual(firing['event_key'], resolved['event_key'])
        self.payload['alert']['startsAt'] = '2026-01-02T00:00:00Z'
        self.payload['receipt'] = {'error': {'message': 'private'}}
        new = record_receipt(self.store, self.payload)
        self.assertNotEqual(new['episode'], firing['episode'])
        self.assertEqual(new['state'], 'unknown')
    def test_invalid_copy_and_empty_receipt(self):
        self.payload['receipt']['copies'] = [{'chat_id': 1, 'message_id': '2'}]
        with self.assertRaises(Failure): record_receipt(self.store, self.payload)
        self.payload['receipt']['copies'] = []
        self.assertEqual(record_receipt(self.store, self.payload)['state'], 'unknown')
    def test_workflow_graph_and_actual_splitter(self):
        w = json.loads((ROOT/'integrations/n8n/alert-workflow.json').read_text())
        nodes = {n['name']: n for n in w['nodes']}
        self.assertEqual(nodes['Alertmanager Webhook']['id'], 'c0ffee00-0001-4000-8000-000000000001')
        handoff = nodes['Record Telegram Receipt']
        self.assertEqual(handoff['credentials']['httpHeaderAuth']['id'], 'SPI_WORKER_CREDENTIAL_ID')
        self.assertEqual(handoff['parameters']['options']['timeout'], 5000)
        self.assertIn("$('One item per alert').itemMatching(index)", nodes['Prepare Telegram Receipt']['parameters']['jsCode'])
        self.assertNotIn('.first()', handoff['parameters']['jsonBody'])
        self.assertEqual(w['connections']['Post Telegram Message']['main'][0][0]['node'], 'Prepare Telegram Receipt')
        self.assertEqual(w['connections']['Prepare Telegram Receipt']['main'][0][0]['node'],'Record Telegram Receipt')
        self.assertEqual(nodes['Post Telegram Message']['onError'], 'continueRegularOutput')
        script = nodes['One item per alert']['parameters']['jsCode']
        alert = self.payload['alert'];alert['endsAt'] = '2026-01-01T01:00:00Z'
        js = 'const items = '+json.dumps([{'json': {'body': {'externalURL':'https://am.example','alerts':[alert, alert]}}}])+';\nconsole.log(JSON.stringify((function(){'+script+'})()));'
        output = json.loads(subprocess.check_output(['node', '-e', js]))
        self.assertEqual(len(output), 2)
        for item in output:
            self.assertEqual(item['json']['source'], 'https://am.example')
            for field in ['startsAt', 'endsAt', 'fingerprint']:
                self.assertEqual(item['json']['alert'][field], alert[field])
