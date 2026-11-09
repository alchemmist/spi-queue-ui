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
