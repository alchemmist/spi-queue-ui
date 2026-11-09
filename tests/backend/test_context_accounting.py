"""Validated byte-bound contract; fixtures contain no production evidence."""
import copy,json,time,unittest
from unittest.mock import Mock
import test_triage as f
from core import Failure,MATCH_SCHEMA,REPORT_SCHEMA
from integrations import Model

class ContextAccountingTests(unittest.TestCase):
 def test_contract_and_schema_completion(self):
  m=Model(Mock(),'unused');b=m.payload('x',{'text':'é😀"\\\n'*100},MATCH_SCHEMA)
  n=m.reservation(b);b['max_tokens']+=123;self.assertEqual(m.reservation(b),n+123)
  b['response_format']['extra']='x'*123;self.assertGreater(m.reservation(b),n+246)
  for change in ({'model':'other'},{'tools':[]},{'chat_template_kwargs':{'enable_thinking':True}},{'messages':[]}):
   with self.assertRaises(Failure) as e:m.reservation({**b,**change})
   self.assertEqual(e.exception.code,'LLM_ACCOUNTING_CONTRACT_MISMATCH')
  m.http.request.assert_not_called()
 def test_excerpts_identity_reference_metadata_and_no_mutation(self):
  x={'candidates':[{'number':n,'body':'😀'*8000} for n in range(120)],'index':[{'id':'E001','path':'original/reference','excerpt':'x'*40000}], 'missing':[{'reason':'not collected'}], 'truncated':True}
  old=copy.deepcopy(x);v=Model.bounded_context(x)
  self.assertEqual(x,old);self.assertEqual([c['number'] for c in v['candidates']],list(range(120)))
  self.assertEqual(v['index'][0]['id'],'E001');self.assertEqual(v['index'][0]['path'],'original/reference')
  self.assertEqual(v['missing'],x['missing']);self.assertTrue(v['truncated'])
  for c in v['candidates']:
   self.assertTrue(c['body']['truncated']);self.assertGreater(c['body']['unreviewed_bytes'],0)
 def test_matching_event_bounded_and_all_identities(self):
  m=Model(Mock(),'unused');event=f.event();event['description']='x'*1000000
  seen=[]
  def call(task,data,schema,deadline):
   self.assertLessEqual(m.reservation(m.payload(task,data,schema)),30000)
   self.assertTrue(data['alert']['description']['truncated'])
   seen.extend(x['number'] for x in data['open_issues'])
   return {'issue_number':0,'reason':'No same problem.','confidence':'low'}
  m.call=call;m.match(event,[{'number':i,'body':'x'*6000} for i in range(120)])
  self.assertEqual(seen,list(range(120)))
 def test_large_analysis_preserves_candidates_and_artifact_index(self):
  m=Model(Mock(),'unused');event=f.event();event['description']='x'*80000
  match={'candidates':[{'number':n,'body':'report '*5000} for n in range(3)],'reviewed_numbers':list(range(120))}
  evidence={'bundle':{'model_context':{'index':[{'id':'E001','path':'artifact/reference'}],'excerpt':'evidence '*5000}},'missing':['logs not collected']}
  original=copy.deepcopy((event,match,evidence));captured={}
  class Stop(Exception):pass
  def call(task,data,schema,deadline):
   captured.update(data)
   self.assertLessEqual(m.budget_reservation(m.payload(task,data,schema)),30000)
   raise Stop()
  m.call=call
  with self.assertRaises(Stop):m.analyze(event,match,evidence)
  self.assertEqual((event,match,evidence),original)
  self.assertEqual([x['number'] for x in captured['preliminary_match']['candidates']],[0,1,2])
  self.assertEqual(captured['preliminary_match']['reviewed_numbers'],list(range(120)))
  self.assertEqual(captured['evidence']['artifact_context']['index'][0]['id'],'E001')
  self.assertTrue(captured['alert']['description']['truncated'])
 def test_irreducible_metadata_visible_without_transport(self):
  m=Model(Mock(),'unused');event=f.event();event['path']=['x'*120000]*5
  with self.assertRaises(Failure) as e:m.analyze(event,{'candidates':[]},{})
  self.assertEqual(e.exception.code,'LLM_CONTEXT_METADATA_TOO_LARGE');m.http.request.assert_not_called()

 def test_local_120_long_report_analysis(self):
  if __import__('os').environ.get('SPI_TOKEN_ACCOUNTING_MODE')!='local-qwen-v1':return
  m=Model(Mock(),'unused')
  match={'candidates':[{'number':n,'body':'report '*5000} for n in range(120)],'reviewed_numbers':list(range(120))}
  evidence={'bundle':{'model_context':{'index':[{'id':'E001','path':'artifact/reference'}],'excerpt':'evidence '*5000}}}
  original=copy.deepcopy((match,evidence));captured={}
  class Stop(Exception):pass
  def call(task,data,schema,deadline):
   captured.update(data)
   self.assertLessEqual(m.counter.reservation(m.payload(task,data,schema)),30000)
   raise Stop()
  m.call=call
  with self.assertRaises(Stop):m.analyze(f.event(),match,evidence)
  self.assertEqual((match,evidence),original)
  self.assertEqual([c['number'] for c in captured['preliminary_match']['candidates']],list(range(120)))
  self.assertEqual(captured['evidence']['artifact_context']['index'][0]['id'],'E001')
