"""Fake offset pagination races; no network or inference."""
import copy
import unittest
from unittest.mock import Mock
import test_triage as f
from integrations import GitHub,Model
from core import Failure,digest

class SnapshotConsistencyTests(unittest.TestCase):
    def rows(self,n=61):
        return [dict(number=i,title='[SPI] fake',updated_at='first',body='',comments=0) for i in range(1,n+1)]
    def transport(self,mutation,descending=False,n=61):
        g=GitHub(Mock(),'unused'); rows=self.rows(n); calls=[]
        def request(method,path,body=None):
            import urllib.parse
            q=urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            page=int(q['page'][0]); calls.append(path)
            ordered=sorted(rows,key=lambda x:x['number'],reverse=descending)
            result=copy.deepcopy(ordered[(page-1)*20:page*20])
            if len(calls)==1:mutation(rows)
            return result
        g.request=request
        return g,calls
    def test_old_desc_insert_reproduces_actual_current_duplicate_with_valid_proof(self):
        original=self.rows();m=Model(Mock(),'fake');saved=[]
        m.call=Mock(return_value=dict(issue_number=0,reason='different',confidence='low'))
        m.match(f.event(),original,on_progress=lambda p:saved.append(copy.deepcopy(p)))
        proof=copy.deepcopy(saved[-1]);m.call.reset_mock()
        g,calls=self.transport(lambda r:r.append(dict(r[-1],number=62)),descending=True)
        current=list(g.pages('/issues?state=open&sort=created&direction=desc'))
        self.assertEqual(len(current),62);self.assertEqual(len({x['number'] for x in current}),61)
        # Execute the exact clean pre-fix matcher, not an approximation.
        import subprocess,types
        baseline=types.ModuleType('baseline_integrations')
        exec(__import__('pathlib').Path(__file__).resolve().parents[1].joinpath('fixtures/pre_snapshot_integrations.py').read_text(),baseline.__dict__)
        old=baseline.Model(Mock(),'fake');old.call=Mock()
        with self.assertRaises(Failure) as previous:old.match(f.event(),current,proof)
        self.assertEqual(previous.exception.code,'INVALID_MATCH_PROGRESS');old.call.assert_not_called()
        with self.assertLogs('integrations',level='WARNING') as logs:
            with self.assertRaises(Failure) as e:m.match(f.event(),current,proof)
        self.assertEqual(e.exception.code,'GITHUB_SNAPSHOT_UNSTABLE')
        self.assertIn('CURRENT_DUPLICATE',''.join(logs.output));m.call.assert_not_called()
        self.assertEqual(proof,saved[-1])
    def test_insert_close_reopen_reorder_coherent_or_visible_instability(self):
        for n in (61,62):
            mutations=[lambda r:r.append(dict(r[-1],number=100)),lambda r:r.pop(0),
                       lambda r:r.insert(0,dict(r[0],number=100)),lambda r:r.reverse()]
            for mutation in mutations:
                with self.subTest(n=n,mutation=mutation):
                    g,calls=self.transport(mutation,n=n)
                    try:
                        snapshot=g.snapshot()
                    except Failure as e:self.assertEqual(e.code,'GITHUB_SNAPSHOT_UNSTABLE')
                    else:
                        expected=g.issues()
                        self.assertEqual([x['number'] for x in snapshot],[x['number'] for x in expected])
                        self.assertEqual(g.open_signature(),digest([(x['number'],x['updated_at']) for x in snapshot]))
                    self.assertLessEqual(len(calls),20)
    def test_comment_window_update_defers_no_model(self):
        g=GitHub(Mock(),'unused');first=self.rows();second=copy.deepcopy(first);second[0]['updated_at']='changed'
        g.issues=Mock(side_effect=[first,second]);g.comments=Mock()
        with self.assertRaises(Failure) as e:g.snapshot()
        self.assertEqual(e.exception.code,'GITHUB_SNAPSHOT_UNSTABLE');self.assertEqual(g.issues.call_count,2)
    def test_bad_schema_finite(self):
        g=GitHub(Mock(),'unused');g.request=Mock(return_value=[{'title':'[SPI] bad','number':'secret'}])
        with self.assertRaises(Failure) as e:g.snapshot()
        self.assertEqual(e.exception.code,'INVALID_GITHUB_PAGE');self.assertEqual(g.request.call_count,1)

class PatientBudgetTests(unittest.TestCase):
    setUp=f.EngineTests.setUp
    tearDown=f.EngineTests.tearDown
    def test_engine_unstable_snapshot_zero_model(self):
        key=self.store.enqueue(f.event())['job_id'];self.store.claim()
        self.github.snapshot.side_effect=Failure('GITHUB_SNAPSHOT_UNSTABLE',retry_after=15)
        with self.assertRaises(Failure):self.engine.run('match',key)
        self.model.match.assert_not_called()
        with self.store.db() as c:
            self.assertEqual(c.execute('select count(*) from job_failure_budget where job_id=?',(key,)).fetchone()[0],0)
    def test_snapshot_deferral_preserves_proof_and_failure_budget(self):
        key=self.store.enqueue(f.event())['job_id'];self.store.claim()
        self.store.save(key,0,{'match_progress_v3':{'proof':'retained'}})
        for _ in range(3):
            self.store.fail(key,'GITHUB_SNAPSHOT_UNSTABLE',retry_after=15)
            row=self.store.get(key)
            self.assertEqual(row['result']['match_progress_v3'],{'proof':'retained'})
            self.assertTrue(15<=row['available']-row['updated']<=18)
            with self.store.db() as c:
                self.assertEqual(c.execute('select count(*) from job_failure_budget where job_id=?',(key,)).fetchone()[0],0)
                c.execute('update jobs set available=0 where id=?',(key,))
            self.store.claim()
