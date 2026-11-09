"""Semantic read patience; no network, credentials or inference."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import test_triage as f
from core import Failure, Store, PATIENT_TRANSIENT
from integrations import GitHub


class GitHubReadTimeoutTests(unittest.TestCase):
    def test_only_known_semantic_get_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            token=Path(tmp)/'token';token.write_text('test-only')
            for method in ('GET','POST','PATCH','PUT','DELETE'):
                for code in ('UPSTREAM_TIMEOUT','HTTP_401','HTTP_403','HTTP_429','HTTP_500',
                             'INVALID_UPSTREAM_JSON','UPSTREAM_NETWORK_ERROR','REQUEST_BUDGET_EXCEEDED',
                             'INVALID_GITHUB_PAGE','INVALID_PRIMARY_REFERENCE'):
                    with self.subTest(method=method,code=code):
                        original=Failure(code)
                        http=Mock();http.request.side_effect=original
                        github=GitHub(http,str(token))
                        with self.assertRaises(Failure) as caught:github.request(method,'/issues')
                        if method=='GET' and code=='UPSTREAM_TIMEOUT':
                            self.assertEqual(caught.exception.code,'GITHUB_READ_TIMEOUT')
                            self.assertEqual(caught.exception.retry_after,60)
                        else:self.assertIs(caught.exception,original)
            http.request.side_effect=TimeoutError('not a typed Failure')
            with self.assertRaises(TimeoutError):github.request('GET','/issues')

    def test_durable_patient_restart_and_later_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'jobs.sqlite');key=store.enqueue(f.event())['job_id'];store.claim()
            result={'match_progress_v3':{'proof':'retained'},'report':f.report()}
            store.save(key,2,result)
            with store.db() as c:
                c.execute('INSERT INTO snapshot_budget VALUES(?,?,?)',(key,2,'{}'))
                c.execute('INSERT INTO match_review_v3 VALUES(?,?)',(key,'{}'))
                c.execute('INSERT INTO job_failure_budget VALUES(?,?)',(key,3))
                c.execute('INSERT INTO llm_admission(tokens,expires,active) VALUES(?,?,?)',(123,9999999999,0))
                c.execute('UPDATE jobs SET attempts=100 WHERE id=?',(key,))
            tables=('snapshot_budget','match_review_v3','job_failure_budget','llm_admission')
            def preserved():
                with store.db() as c:return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
            before=preserved()
            token=Path(tmp)/'token';token.write_text('test-only')
            http=Mock();http.request.side_effect=Failure('UPSTREAM_TIMEOUT')
            github=GitHub(http,str(token))
            for _ in range(3):
                old=copy.deepcopy(store.get(key))
                with self.assertRaises(Failure) as caught:github.open_signature()
                error=caught.exception
                store.fail(key,error.code,error.retry_after,error.rate_metadata)
                store=Store(store.path)
                row=store.get(key)
                self.assertEqual(row['result'],old['result'])
                self.assertEqual(row['attempts'],old['attempts'])
                self.assertEqual(row['error'],'GITHUB_READ_TIMEOUT')
                self.assertEqual(row['state'],'retry')
                self.assertTrue(61<=row['available']-row['updated']<=63)
                self.assertEqual(preserved(),before)
                self.assertFalse(store.claim().get('job_id'))
                with store.db() as c:c.execute('UPDATE jobs SET available=0 WHERE id=?',(key,))
                self.assertEqual(store.claim()['job_id'],key)
            http.request.side_effect=None;http.request.return_value=[]
            self.assertEqual(github.snapshot(),[])
            model=Mock();model.analyze.return_value=f.report()
            model.analyze(f.event(),store.get(key)['result'])
            model.analyze.assert_called_once()
            self.assertEqual(preserved(),before)

    def test_write_timeout_is_finite_and_intent_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'jobs.sqlite');key=store.enqueue(f.event())['job_id'];store.claim()
            with store.db() as c:c.execute('UPDATE jobs SET intent=? WHERE id=?',('durable-intent',key))
            for _ in range(8):store.fail(key,'UPSTREAM_TIMEOUT')
            self.assertNotIn('UPSTREAM_TIMEOUT',PATIENT_TRANSIENT)
            self.assertEqual(store.get(key)['state'],'dead')
            self.assertEqual(store.get(key)['intent'],'durable-intent')


class EngineReadRecoveryTests(unittest.TestCase):
    setUp=f.EngineTests.setUp
    tearDown=f.EngineTests.tearDown

    def test_actual_analyze_resumes_after_read_deadline(self):
        from integrations import REQUEST_DEADLINE
        import time
        key=self.store.enqueue(f.event())['job_id'];self.store.claim()
        self.engine.run('match',key);self.engine.run('collect',key)
        old=copy.deepcopy(self.store.get(key)['result'])
        token=Path(self.tmp.name)/'token';token.write_text('test-only')
        http=Mock();http.request.side_effect=Failure('UPSTREAM_TIMEOUT')
        self.engine.github=GitHub(http,str(token))
        REQUEST_DEADLINE.at=time.monotonic()+1
        try:
            with self.assertRaises(Failure) as caught:self.engine.run('analyze',key)
            self.assertEqual(caught.exception.code,'GITHUB_READ_TIMEOUT')
            self.store.fail(key,caught.exception.code,caught.exception.retry_after)
        finally:
            if hasattr(REQUEST_DEADLINE,"at"):del REQUEST_DEADLINE.at
        self.assertEqual(self.store.get(key)['result'],old)
        self.model.analyze.assert_not_called()
        # Another job can acquire while the read patiently waits.
        other=self.store.enqueue(f.event(start='2026-11-03T15:00:00Z'))['job_id']
        self.assertEqual(self.store.claim()['job_id'],other)
        self.store.fail(other,'HTTP_403')
        with self.store.db() as c:c.execute('UPDATE jobs SET available=0 WHERE id=?',(key,))
        self.assertEqual(self.store.claim()['job_id'],key)
        http.request.side_effect=None;http.request.return_value=[]
        self.engine.run('analyze',key)
        self.model.analyze.assert_called_once()
        self.assertEqual(self.store.get(key)['stage'],3)
