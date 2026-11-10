"""Offline regressions for the two reported executions. No external requests."""
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import test_triage as f
from core import Store, Failure, OWNER_CONTEXT, REPORT_SCHEMA
from integrations import Model
from test_wait_integration import IntegrationTests


class ReadWaitTests(IntegrationTests):
    def test_read_timeout_replays_receipt_then_resumes_after_restart(self):
        self.github.snapshot.side_effect=[Failure('GITHUB_READ_TIMEOUT',60),[]]
        sent=self.data
        response=self.engine.run_v1('match',sent)
        self.assertEqual(response['code'],'GITHUB_READ_TIMEOUT')
        self.assertEqual(response['status'],'deferred')
        self.assertEqual(response['deadline'],sent['deadline'])
        self.assertEqual(self.engine.run_v1('match',sent),response)
        self.assertEqual(self.github.snapshot.call_count,1)
        self.engine.store=Store(self.store.path)
        self.engine.store.recover_on_start()
        self.now=response['due_at']
        result=self.engine.run_v1('match',response)
        self.assertEqual(result['stage'],'collect')
        self.assertEqual(self.store.get(self.key)['attempts'],1)
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM job_failure_budget').fetchone()[0],0)


    def test_github_network_error_waits_with_same_owner_and_survives_restart(self):
        from integrations import GitHub, HTTP
        from pathlib import Path
        import urllib.error
        token=Path(self.tmp.name)/'github-token';token.write_text('local-fixture-only')
        http=HTTP();http.opener=Mock()
        http.opener.open.side_effect=urllib.error.URLError(ConnectionResetError('private text must not escape'))
        self.engine.github=GitHub(http,str(token))
        sent=self.data
        response=self.engine.run_v1('match',sent)
        self.assertEqual(response['code'],'GITHUB_READ_NETWORK_ERROR')
        self.assertEqual(response['status'],'deferred')
        self.assertEqual(response['deadline'],sent['deadline'])
        self.assertEqual(response['execution_id'],sent['execution_id'])
        self.assertNotIn('private text',json.dumps(response))
        self.model.match.assert_not_called()
        self.assertEqual(self.engine.run_v1('match',sent),response)
        self.assertEqual(http.opener.open.call_count,1)
        self.assertEqual(self.store.claim(),{'job_id':''})
        self.engine.store=Store(self.store.path);self.engine.store.recover_on_start()
        self.now=response['due_at']
        self.engine.github=Mock();self.engine.github.snapshot.return_value=[]
        result=self.engine.run_v1('match',response)
        self.assertEqual(result['stage'],'collect')
        self.model.match.assert_called_once()
        self.assertEqual(self.store.get(self.key)['attempts'],1)
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM job_failure_budget').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM llm_admission').fetchone()[0],0)
        with self.assertRaises(Failure):self.engine.run_v1('match',sent)

    def test_github_network_wait_does_not_extend_absolute_deadline(self):
        with self.store.db() as c:
            c.execute('UPDATE workflow_wait_owner SET deadline=? WHERE job_id=?',(self.now+100,self.key))
        self.github.snapshot.side_effect=Failure('GITHUB_READ_NETWORK_ERROR',60)
        with self.assertRaisesRegex(Failure,'WAIT_DEADLINE_EXCEEDED'):
            self.engine.run_v1('match',self.data)
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT phase FROM workflow_wait_owner WHERE job_id=?',(self.key,)).fetchone()[0],'failed')
        self.model.match.assert_not_called()

    def test_graph_capture_budget_resumes_same_collect_owner_without_paid_replay(self):
        sent=self.engine.run_v1('match',self.data)
        self.evidence.collect.side_effect=[Failure('GRAPH_CAPTURE_DEFERRED',1),{'missing':[]}]
        waiting=self.engine.run_v1('collect',sent)
        self.assertEqual(waiting['status'],'deferred')
        self.assertEqual(waiting['code'],'GRAPH_CAPTURE_DEFERRED')
        self.assertEqual(waiting['deadline'],sent['deadline'])
        self.assertEqual(self.engine.run_v1('collect',sent),waiting)
        self.assertEqual(self.evidence.collect.call_count,1)
        self.engine.store=Store(self.store.path);self.engine.store.recover_on_start()
        self.now=waiting['due_at']
        result=self.engine.run_v1('collect',waiting)
        self.assertEqual(result['stage'],'analyze')
        self.assertEqual(self.model.match.call_count,1)
        self.model.analyze.assert_not_called()
        with self.store.db() as c:self.assertEqual(c.execute('select count(*) from job_failure_budget').fetchone()[0],0)


@patch.dict('os.environ',{'SPI_TOKEN_ACCOUNTING_MODE':'byte-bound-v1'})
class RepairTests(unittest.TestCase):
    def test_repair_budget_survives_admission_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp+'/db');key=store.enqueue(f.event())['job_id']
            OWNER_CONTEXT.binding=(key,'test-fence')
            bad=f.report();bad['facts']=['Unknown [E999]']
            good=f.report();good['facts']=['Verified [E001]']
            data={'evidence':{'artifact_context':{'index':[{'id':'E001'}]}}}
            http=Mock();http.request.return_value={'choices':[{'message':{'content':json.dumps(bad)}}]}
            try:
                with patch.object(store,'reserve_llm',side_effect=[1,Failure('LLM_ADMISSION_DEFERRED',61)]),patch.object(store,'finish_llm'):
                    with self.assertRaisesRegex(Failure,'LLM_ADMISSION_DEFERRED'):
                        Model(http,'fake',store=store).call('analysis',data,REPORT_SCHEMA,time.monotonic()+20)
                self.assertEqual(http.request.call_count,1)
                store=Store(store.path)
                with store.db() as c:self.assertEqual(c.execute('SELECT failures FROM analysis_output_budget').fetchone()[0],1)
                http.request.return_value={'choices':[{'message':{'content':json.dumps(good)}}]}
                with patch.object(store,'reserve_llm',return_value=2),patch.object(store,'finish_llm'):
                    self.assertEqual(Model(http,'fake',store=store).call('analysis',data,REPORT_SCHEMA,time.monotonic()+20),good)
                self.assertIn('Previous output failed',http.request.call_args.args[3]['messages'][1]['content'])
                # Conservative crash recovery: consumed repair cannot be repeated,
                # even if its valid answer was lost before the report checkpoint.
                store=Store(store.path)
                with self.assertRaisesRegex(Failure,'LLM_INVALID_EVIDENCE_REFERENCE'):
                    Model(http,'fake',store=store).call('analysis',data,REPORT_SCHEMA,time.monotonic()+20)
                self.assertEqual(http.request.call_count,2)
            finally:del OWNER_CONTEXT.binding
