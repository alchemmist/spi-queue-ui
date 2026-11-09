"""Offline contract tests: never contact n8n, Telegram, or the SPI worker."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
RELATIVE = 'integrations/n8n/triage-workflow.json'
sys.path.insert(0, str(ROOT / 'integrations/n8n'))
spec = importlib.util.spec_from_file_location('spi_event_sync', ROOT / 'integrations/n8n/sync_spi.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class EventWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = json.loads((ROOT / RELATIVE).read_text())
        self.mapping = {'worker': {'id': 'existing-worker-id', 'name': 'SPI worker internal'}}

    def test_authenticated_immediate_webhook_and_linear_pipeline(self):
        w = self.workflow
        nodes = {n['name']: n for n in w['nodes']}
        self.assertFalse(any('schedule' in n['type'].lower() for n in w['nodes']))
        trigger = nodes['Dispatch Triage Job']
        self.assertEqual(trigger['id'], '9c393df1-a66f-41bf-acaf-b2b66f31a393')
        self.assertEqual(trigger['webhookId'],trigger['id']) # n8n otherwise reports active but registers no production hook
        self.assertEqual(trigger['type'], 'CUSTOM.socratSpiDispatchWebhook')
        self.assertEqual(trigger['parameters'], {
            'path': 'spi-incident-triage-dispatch'})
        chain = ['Dispatch Triage Job', 'Claim Triage Job', 'Select Claimed Job',
                 'Match Open SPI', 'Collect Evidence', 'Analyze Incident', 'Publish SPI']
        self.assertEqual(set(nodes), set(chain)|{'Route Stage Outcome','Wait For Admission','Resume Pinned Stage'})
        self.assertEqual(len(nodes),10)
        original_ids=['9c393df1-a66f-41bf-acaf-b2b66f31a393','ace6035e-4fe4-4742-a2ac-e48d24fecf04','e7d658f0-698a-4a49-b66c-2b5c3336b52c','1f4819ea-709f-4a56-8ffe-22f6396ac78b','0f300417-871e-4478-8af5-c067e051209b','10869f3b-8aa7-4ba2-b185-3a35c921cabf','656d32a2-5cdd-475b-b9a4-53d6f289832b']
        self.assertEqual([nodes[n]['id'] for n in chain],original_ids)
        def targets(name):return [[x['node'] for x in output] for output in w['connections'][name]['main']]
        self.assertEqual(targets('Dispatch Triage Job'),[['Claim Triage Job']])
        self.assertEqual(targets('Claim Triage Job'),[['Select Claimed Job']])
        self.assertEqual(targets('Select Claimed Job'),[['Route Stage Outcome']])
        self.assertEqual(targets('Route Stage Outcome'),[['Wait For Admission'],['Resume Pinned Stage']])
        self.assertEqual(targets('Wait For Admission'),[['Resume Pinned Stage']])
        self.assertEqual(targets('Resume Pinned Stage'),[[n] for n in chain[3:]]+[[]])
        for name in chain[3:]:self.assertEqual(targets(name),[['Select Claimed Job']])
        self.assertEqual(nodes['Wait For Admission']['type'],'n8n-nodes-base.wait')
        self.assertIn('INVALID_SPI_WAIT_ENVELOPE',nodes['Select Claimed Job']['parameters']['jsCode'])
        for name, endpoint in [('Claim Triage Job', 'claim'), ('Match Open SPI', 'match'),
                               ('Collect Evidence', 'collect'), ('Analyze Incident', 'analyze'),
                               ('Publish SPI', 'publish')]:
            node = nodes[name]
            p = node['parameters']
            self.assertEqual(p['url'], 'http://spi-triage.automation.svc:8080/' + endpoint)
            self.assertEqual(p['method'], 'POST')
            self.assertEqual(p['genericAuthType'], 'httpHeaderAuth')
            self.assertEqual(p['authentication'], 'genericCredentialType')
            self.assertEqual(p['options']['timeout'], 20000 if endpoint == 'claim' else 1920000)
            self.assertEqual(p['jsonBody'], "={{ JSON.stringify({execution_id: String($execution.id), workflow_revision: 'durable-v1'}) }}" if endpoint == 'claim'
                             else '={{ JSON.stringify($json) }}' )
            self.assertEqual(node['credentials'], trigger['credentials'])
            self.assertFalse(node.get('retryOnFail', False))
        self.assertEqual(w['settings'], {'errorWorkflow': 'iZn7biFh6321a1w8',
                                        'executionOrder': 'v1', 'timezone': 'Europe/Moscow',
                                        'executionTimeout': 21600, 'saveExecutionProgress': True,
                                        'saveDataSuccessExecution': 'all',
                                        'saveDataErrorExecution': 'all',
                                        'saveManualExecutions': True})

    def test_dispatch_execution_persistence_defense_in_depth(self):
        """Authenticated trigger strips secret headers; requested history stays enabled."""
        settings = self.workflow['settings']
        for key in ('saveDataSuccessExecution', 'saveDataErrorExecution'):
            self.assertEqual(settings[key], 'all')
        for key in ('saveManualExecutions', 'saveExecutionProgress'):
            self.assertIs(settings[key], True)

    def test_actual_envelope_validator_rejects_unknown_and_early_terminal(self):
        import subprocess,time
        code=next(n for n in self.workflow['nodes'] if n['name']=='Select Claimed Job')['parameters']['jsCode']
        base={'contract':1,'status':'deferred','job_id':'a'*64,'execution_id':'1234','workflow_revision':'durable-v1','fence':'b'*64,'stage':'match','due_at':time.time()+80,'deadline':time.time()+21600,'code':'LLM_ADMISSION_DEFERRED'}
        inputs=[base,{**base,'code':'WORKER_ADMISSION_DEFERRED'},{**base,'code':'GITHUB_READ_TIMEOUT'},
                {**base,'code':'LLM_INVALID_EVIDENCE_REFERENCE'},
                {**base,'code':'UNKNOWN'},{**base,'due_at':'tomorrow'},
                {**base,'execution_id':'9999'},
                {**base,'status':'complete','outcome':'complete'},
                {**base,'status':'complete','stage':'publish','outcome':'complete'},
                {'contract':1,'status':'empty','job_id':''}]
        js='const run=new Function("items","$execution",'+json.dumps(code)+');const inputs='+json.dumps(inputs)+';console.log(JSON.stringify(inputs.map(x=>{try{return run([{json:x}],{id:"1234"}).length}catch(e){return "error"}})));'
        results=json.loads(subprocess.check_output(['node','-e',js],text=True))
        self.assertEqual(results,[1,1,1,'error','error','error','error','error',1,0])

    def test_render_reuses_worker_identity_without_mutating_template(self):
        original = copy.deepcopy(self.workflow)
        rendered = sync.render(self.workflow, self.mapping)
        for node in rendered['nodes']:
            if 'credentials' in node:
                self.assertEqual(node['credentials'], {'httpHeaderAuth': self.mapping['worker']})
        self.assertEqual(self.workflow, original)
        with self.assertRaisesRegex(RuntimeError, 'Missing private SPI credential mapping: worker'):
            sync.render(self.workflow, {})

    def run_sync(self, pages):
        with tempfile.TemporaryDirectory() as config:
            Path(config, 'spi-credentials.json').write_text(json.dumps(self.mapping))
            with patch.object(sync, 'CONFIG', Path(config)), patch.object(sync, 'api', side_effect=pages) as api:
                result = sync.sync(RELATIVE)
                return result, api.call_args_list

    def test_sync_updates_existing_id_on_later_page_without_activation(self):
        result, calls = self.run_sync([
            {'data': [], 'nextCursor': 'next/page'},
            {'data': [{'name': self.workflow['name'], 'id': 'original-id', 'active': False}]},
            {'id': 'original-id', 'name': self.workflow['name']},
        ])
        self.assertEqual(result['id'], 'original-id')
        self.assertEqual(calls[1].args, ('GET', 'workflows?limit=250&cursor=next%2Fpage'))
        method, path, body = calls[-1].args
        self.assertEqual((method, path), ('PUT', 'workflows/original-id'))
        self.assertEqual(set(body), {'name', 'nodes', 'connections', 'settings'})
        self.assertEqual(body['nodes'], sync.render(self.workflow, self.mapping)['nodes'])

    def test_sync_refuses_active_or_ambiguous_workflows_before_write(self):
        for entries in [
            [{'name': self.workflow['name'], 'id': 'original-id', 'active': True}],
            [{'name': self.workflow['name'], 'id': str(i), 'active': False} for i in range(2)],
        ]:
            with self.subTest(entries=entries), self.assertRaises(RuntimeError):
                self.run_sync([{'data': entries}])

    def test_sync_refuses_missing_original_without_write(self):
        with tempfile.TemporaryDirectory() as config:
            Path(config, 'spi-credentials.json').write_text(json.dumps(self.mapping))
            with patch.object(sync, 'CONFIG', Path(config)), patch.object(
                    sync, 'api', side_effect=[{'data': [], 'nextCursor': 'next'},
                                              {'data': []}]) as api:
                with self.assertRaisesRegex(RuntimeError, 'refusing creation'):
                    sync.sync(RELATIVE)
                self.assertEqual(api.call_count, 2)
                self.assertTrue(all(call.args[0] == 'GET' for call in api.call_args_list))

    def test_sync_refuses_duplicate_names_across_pages(self):
        entry = {'name': self.workflow['name'], 'id': 'original-id', 'active': False}
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous workflow name'):
            self.run_sync([{'data': [entry], 'nextCursor': 'next'}, {'data': [dict(entry, id='other')]}])


if __name__ == '__main__':
    unittest.main()
