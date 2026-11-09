"""SPI-only synthetic payloads and deterministic ledger clocks; no live inference."""
import json,tempfile,time,unittest,os
from unittest.mock import patch
from unittest.mock import Mock
import test_triage as f
from core import Store,Failure,MATCH_SCHEMA,REPORT_SCHEMA
from integrations import Model

GOOD={'issue_number':0,'reason':'Distinct cause; no supporting overlap.','confidence':'low'}
class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name+'/db')
    def tearDown(self):self.tmp.cleanup()
    def test_restart_parallel_and_window(self):
        r=self.store.reserve_llm(15000,now=100)
        with self.assertRaises(Failure):Store(self.store.path).reserve_llm(1,now=101)
        self.store.finish_llm(r,now=110)
        s=Store(self.store.path);r=s.reserve_llm(15000,now=111);s.finish_llm(r,now=111)
        with self.assertRaises(Failure) as e:s.reserve_llm(1,now=170.999)
        self.assertEqual(e.exception.code,'LLM_ADMISSION_DEFERRED')
        s.reserve_llm(1,now=171)
    def test_rpm_and_crash_expiry(self):
        for t in (1,2,3):
            r=self.store.reserve_llm(1,now=t);self.store.finish_llm(r,now=t)
        with self.assertRaises(Failure):self.store.reserve_llm(1,now=4)
        r=self.store.reserve_llm(1,timeout=10,now=64)
        with self.assertRaises(Failure):Store(self.store.path).reserve_llm(1,now=134.99)
        Store(self.store.path).reserve_llm(1,now=135)
    def test_no_usage_refund_and_impossible(self):
        r=self.store.reserve_llm(30000,now=1);self.store.finish_llm(r,actual=1,now=2)
        with self.assertRaises(Failure):self.store.reserve_llm(1,now=3)
        with self.assertRaises(Failure) as e:self.store.reserve_llm(30001,now=100)
        self.assertEqual(e.exception.code,'LLM_REQUEST_TOO_LARGE')
    @patch.dict(os.environ, {'SPI_TOKEN_ACCOUNTING_MODE':'byte-bound-v1'})
    def test_serialized_schema_output_and_analysis(self):
        m=Model(Mock(),'fake',store=self.store)
        for schema,output in ((MATCH_SCHEMA,768),(REPORT_SCHEMA,2500)):
            body=m.payload('task',{'text':'😀"\\'*100},schema)
            import unicodedata
            expected=sum(len(unicodedata.normalize('NFC',x['content']).encode()) for x in body['messages'])
            expected+=len(json.dumps(body['response_format'],ensure_ascii=False).encode())+256+output
            self.assertEqual(m.reservation(body),expected)
            self.assertLess(m.reservation(body),len(json.dumps(body).encode())+2048+output)
            self.assertGreater(m.reservation(body),len(body['messages'][1]['content']))
        with self.assertRaises(Failure) as e:m.call('test',{'huge':['x'*12000]*5},REPORT_SCHEMA,time.monotonic()+20)
        self.assertEqual(e.exception.code,'LLM_REQUEST_TOO_LARGE');m.http.request.assert_not_called()
    def test_local_compressible_and_incompressible_before_http(self):
        if os.environ.get('SPI_TOKEN_ACCOUNTING_MODE') != 'local-qwen-v1':
            return  # Local validation image exercises native path; host remains optional.
        h=Mock();h.request.return_value={'choices':[{'message':{'content':json.dumps(GOOD)}}]}
        m=Model(h,'fake',store=self.store)
        self.assertEqual(m.call('test',{'huge':['x'*12000]*5},MATCH_SCHEMA,time.monotonic()+20),GOOD)
        h.request.assert_called_once();h.reset_mock()
        import random,string
        rng=random.Random(419)
        noise=''.join(rng.choice(string.ascii_letters+string.digits) for _ in range(60000))
        with self.assertRaises(Failure) as e:
            m.call('test',{'huge':noise},MATCH_SCHEMA,time.monotonic()+20)
        self.assertEqual(e.exception.code,'LLM_REQUEST_TOO_LARGE');h.request.assert_not_called()

    def test_local_computed_cost_durable_clock(self):
        if os.environ.get('SPI_TOKEN_ACCOUNTING_MODE') != 'local-qwen-v1':
            return
        m=Model(Mock(),'fake',store=self.store)
        bodies=[m.payload('test',{'text':'x'*16000+str(i)},MATCH_SCHEMA) for i in range(3)]
        costs=[m.counter.reservation(b) for b in bodies]
        with patch.dict(os.environ, {'SPI_TOKEN_ACCOUNTING_MODE':'byte-bound-v1'}):
            legacy=Model(Mock(),'fake')
        self.assertLessEqual(sum(costs),30000)
        self.assertGreater(sum(legacy.reservation(b) for b in bodies),30000)
        s=self.store
        for i,cost in enumerate(costs):
            now=100+i*2
            r=s.reserve_llm(cost,now=now)
            with self.assertRaises(Failure) as e:Store(s.path).reserve_llm(1,now=now+.1)
            self.assertEqual(e.exception.code,'LLM_ADMISSION_DEFERRED')
            s.finish_llm(r,actual=1,now=now+1)
            s=Store(s.path)
        with s.db() as db:
            self.assertEqual([r[0] for r in db.execute('select tokens from llm_admission order by id')],costs)
        with self.assertRaises(Failure):s.reserve_llm(1,now=161.999)
        r=s.reserve_llm(costs[0],now=162)
        s.finish_llm(r,actual=30000,now=163)
        with Store(s.path).db() as db:
            self.assertEqual(db.execute('select tokens from llm_admission where id=?',(r,)).fetchone()[0],30000)
        with self.assertRaises(Failure):Store(s.path).reserve_llm(1,now=223.999)
        Store(s.path).reserve_llm(1,now=224)

    def test_actual_call_uses_shared_admission(self):
        h=Mock();h.request.return_value={'choices':[{'message':{'content':json.dumps(GOOD)}}],'usage':{'total_tokens':50}}
        m=Model(h,'fake',store=self.store)
        for _ in range(3):self.assertEqual(m.call('test',{},MATCH_SCHEMA,time.monotonic()+20),GOOD)
        with self.assertRaises(Failure):Model(h,'fake',store=Store(self.store.path)).call('test',{},MATCH_SCHEMA,time.monotonic()+20)
        self.assertEqual(h.request.call_count,3)
    def test_all120_and_oversized_single_excerpt(self):
        issues=[{'number':i,'body':'a'*6000,'recent_comments':['b'*1200]*3}for i in range(120)]
        m=Model(Mock(),'fake',store=self.store);seen=[];saved=[]
        def call(task,data,schema,deadline):
            self.assertLessEqual(m.reservation(m.payload(task,data,schema)),30000)
            self.assertLessEqual(len(data['open_issues']),12)
            seen.extend(x['number'] for x in data['open_issues']);return dict(GOOD)
        m.call=call
        result=m.match(f.event(),issues,on_progress=saved.append)
        self.assertEqual(seen,list(range(120)));self.assertEqual(result['reviewed_numbers'],seen)
        self.assertGreater(len(saved),10)
        seen.clear();m.match(f.event(),issues,progress=saved[-1]);self.assertEqual(seen,[])
    def test_legacy_partition_not_reinterpreted(self):
        issues=[{'number':i} for i in range(25)]
        from core import digest
        progress={'signature':digest(issues),'decisions':[GOOD]}
        m=Model(Mock(),'fake',store=self.store);m.call=Mock(return_value=GOOD)
        saved=[];m.match(f.event(),issues,progress=progress,on_progress=saved.append)
        self.assertEqual(saved[-1]['reviews'][0]['ids'],list(range(12)))
        self.assertEqual(m.call.call_count,3) # Unbound legacy proof must be reviewed again.
    def test_reset_delay_not_claim_backoff(self):
        key=self.store.enqueue(f.event())['job_id']
        for _ in range(20):self.store.fail(key,'LLM_ADMISSION_DEFERRED',retry_after=60)
        job=self.store.get(key)
        self.assertEqual(job['state'],'retry');self.assertTrue(61<=job['available']-job['updated']<=63)
        self.store.fail(key,'LLM_HTTP_401')
        with self.store.db() as c:self.assertEqual(c.execute('select failures from job_failure_budget').fetchone()[0],1)
    def test_final_comparison_resume_and_budget(self):
        issues=[{'number':i,'body':'x'*6000} for i in range(1,31)]
        m=Model(Mock(),'fake',store=self.store);saved=[];calls=[]
        def choose(task,data,schema,deadline):
            self.assertLessEqual(m.reservation(m.payload(task,data,schema)),30000)
            ids=[x['number'] for x in data['open_issues']];calls.append(ids)
            if len(ids)==2 and not any('selection' in s for s in saved):
                return {**GOOD,'issue_number':ids[0]}
            if len(ids)==2:raise Failure('LLM_ADMISSION_DEFERRED',retry_after=61)
            return {**GOOD,'issue_number':ids[0]}
        m.call=choose
        with self.assertRaises(Failure):m.match(f.event(),issues,on_progress=saved.append)
        self.assertIn('selection',saved[-1]);completed=len(calls)
        m.call=Mock(side_effect=lambda t,d,s,dl:{**GOOD,'issue_number':d['open_issues'][0]['number']})
        result=m.match(f.event(),issues,progress=saved[-1])
        self.assertEqual(result['reviewed_numbers'],list(range(1,31)))
        self.assertEqual(m.call.call_count,1)
    def test_invalid_output_finite_not_hidden_by_admission(self):
        h=Mock();h.request.return_value={'choices':[{'message':{'content':'bad JSON'}}]}
        m=Model(h,'fake',store=self.store)
        with self.assertRaises(Failure) as e:m.call('test',{},MATCH_SCHEMA,time.monotonic()+30)
        self.assertEqual(e.exception.code,'LLM_INVALID_MODEL_SCHEMA')
        from core import LLM_TRANSIENT
        self.assertNotIn(e.exception.code,LLM_TRANSIENT);self.assertEqual(h.request.call_count,1)
    def test_numeric_rate_headers_allowlist_and_durable_metadata(self):
        import urllib.error
        from integrations import HTTP
        h=HTTP();h.opener=Mock()
        h.opener.open.side_effect=urllib.error.HTTPError('http://internal',429,'body not retained',{
            'x-ratelimit-remaining-tokens':'4155','x-ratelimit-reset-tokens':'60',
            'x-ratelimit-remaining-requests':'NaN','X-Unknown':'private'},None)
        with self.assertRaises(Failure) as e:h.request('POST','http://internal')
        self.assertEqual(e.exception.retry_after,60)
        self.assertEqual(e.exception.rate_metadata,{'tokens_remaining':4155.,'tokens_reset':60.,'retry_after':60})
        key=self.store.enqueue(f.event())['job_id']
        self.store.fail(key,'LLM_HTTP_429',retry_after=e.exception.retry_after,
                        rate_metadata={**e.exception.rate_metadata,'body':'not allowed'})
        with self.store.db() as c:
            data=json.loads(c.execute('select metadata from llm_rate_metadata').fetchone()[0])
        self.assertNotIn('body',data);self.assertEqual(data['tokens_remaining'],4155)

    def test_local_proxy_modeled_precheck_and_settlement(self):
        if os.environ.get('SPI_TOKEN_ACCOUNTING_MODE') != 'local-qwen-v1':return
        m=Model(Mock(),'fake',store=self.store)
        # Actual installed 1.105.1 text estimator and reconciliation semantics:
        # pre-call checks current + raw chars//4 + explicit cap > limit;
        # success settles actual-reserved, errors without usage refund fully.
        proxy_current=0
        for i in range(3):
            body=m.payload('test',{'text':'x'*16000+str(i)},MATCH_SCHEMA)
            cost=m.counter.reservation(body)
            proxy=sum(len(x['content']) for x in body['messages'])//4+body['max_tokens']
            self.assertGreaterEqual(cost,proxy)
            self.assertLessEqual(proxy_current+proxy,30000)
            r=self.store.reserve_llm(cost,now=100+i*2)
            proxy_current+=proxy
            actual=proxy if i==0 else 1
            proxy_current+=actual-proxy
            self.store.finish_llm(r,actual=actual,now=101+i*2)
        # Floor-dominant raw whitespace survives trimming: two such requests
        # cannot overlap the local minute even when proxy refunds to 1.
        body=m.payload('test',{'text':' '*100000+'x'},REPORT_SCHEMA)
        cost=m.counter.reservation(body)
        proxy=sum(len(x['content']) for x in body['messages'])//4+2500
        self.assertGreaterEqual(cost,proxy)
        r=self.store.reserve_llm(cost,now=200)
        self.store.finish_llm(r,actual=1,now=201)
        with self.assertRaises(Failure):self.store.reserve_llm(cost,now=202)
        self.store.reserve_llm(cost,now=262)

    def test_local_raw_bound_before_http_and_bpe(self):
        if os.environ.get('SPI_TOKEN_ACCOUNTING_MODE') != 'local-qwen-v1':return
        m=Model(Mock(),'fake',store=self.store)
        with patch.object(m.counter,'_tokenizer') as native:
            with self.assertRaises(Failure) as e:
                m.call('test',{'text':' '*600000},REPORT_SCHEMA,time.monotonic()+20)
            self.assertEqual(e.exception.code,'LLM_REQUEST_TOO_LARGE')
            native.encode.assert_not_called()
        m.http.request.assert_not_called()
