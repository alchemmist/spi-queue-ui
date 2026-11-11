"""Offline regression for17941: bounded generation, never partial-report salvage."""
import copy
import json
import time
import unittest
from unittest.mock import Mock,patch
from core import REPORT_SCHEMA,MATCH_SCHEMA,Failure,validate
from integrations import Model
from report_bounds import guided_report_schema,TEXT_LIMITS,ARRAY_LIMITS
import test_triage as f


def maximal(schema):
    out={}
    for key,p in schema['properties'].items():
        if 'enum'in p:out[key]=max(p['enum'],key=lambda x:len(str(x)))
        elif p['type']=='integer':out[key]=p['maximum']
        elif p['type']=='array':out[key]=['!'*p['items']['maxLength']]*p['maxItems']
        else:out[key]='!'*p['maxLength']
    return out


@patch.dict('os.environ',{'SPI_TOKEN_ACCOUNTING_MODE':'byte-bound-v1'})
class ReportBoundsTests(unittest.TestCase):
    def model(self):return Model(Mock(),'test-only',store=Mock())

    def test_every_variable_prose_field_is_bounded_before_generation(self):
        before=copy.deepcopy(REPORT_SCHEMA);m=self.model()
        body=m.payload('analysis',{},REPORT_SCHEMA);schema=body['response_format']['json_schema']['schema']
        for key,limit in TEXT_LIMITS.items():self.assertEqual(schema['properties'][key]['maxLength'],limit)
        for key,(count,length)in ARRAY_LIMITS.items():
            self.assertEqual(schema['properties'][key]['maxItems'],count)
            self.assertEqual(schema['properties'][key]['items']['maxLength'],length)
        self.assertEqual(body['max_tokens'],2500)
        self.assertEqual(body['chat_template_kwargs'],{'enable_thinking':False})
        # This decoder ignores maxLength with pattern, and crashes with
        # multiple bounded pattern fields. Prose must use maxLength alone.
        self.assertNotIn('pattern',schema['properties']['summary'])
        self.assertIn('{1,120}',schema['properties']['facts']['items']['pattern'])
        self.assertEqual(REPORT_SCHEMA,before)
        self.assertEqual(m.payload('match',{},MATCH_SCHEMA)['max_tokens'],768)
        m.http.request.assert_not_called()

    def test_worst_canonical_completion_fits_without_any_bpe_merges(self):
        schema=guided_report_schema(issue_numbers=[2147483647]);value=maximal(schema);validate(value,schema)
        raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('ascii')
        # Conservative byte-level upper bound: output BPE merges can only help.
        self.assertLessEqual(len(raw),2200)
        self.assertLess(len(raw),self.model().payload('x',{},REPORT_SCHEMA)['max_tokens'])

    def test_fact_reference_and_prose_bounds_are_both_required(self):
        schema=guided_report_schema(['E007']);value=f.report();value['facts']=['Observation [E007].']
        validate(value,schema)
        for fact in ('Observation [E999].','Uncited observation.','x'*121+' [E007].'):
            value['facts']=[fact]
            with self.assertRaises(Failure):validate(value,schema)
        value['facts']=[];validate(value,guided_report_schema([]))

    def test_fact_escaping_and_unbounded_list_loopholes_fail_validation(self):
        schema=guided_report_schema()
        for text in ('line\nline','a"b','a\\b','a\tb','a\u2028b'):
            value=f.report();value['facts']=[text]
            with self.assertRaises(Failure):validate(value,schema)
        for field,(maximum,_)in ARRAY_LIMITS.items():
            value=f.report();value[field]=['safe']*(maximum+1)
            with self.assertRaises(Failure):validate(value,schema)
        value=f.report();value['issue_number']=2147483648
        with self.assertRaises(Failure):validate(value,schema)

    def test_upstream_ignoring_schema_is_not_published_or_regenerated(self):
        m=self.model();value=f.report();value['summary']='x'*161
        m.http.request.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}
        with self.assertRaisesRegex(Failure,'LLM_INVALID_MODEL_SCHEMA'):
            m.call('analysis',{},REPORT_SCHEMA,time.monotonic()+30)
        self.assertEqual(m.http.request.call_count,1)
        m.store.finish_llm.assert_called_once()

    def test_length_finish_still_fails_even_when_content_parses(self):
        m=self.model();m.http.request.return_value={'choices':[{'finish_reason':'length','message':{'content':json.dumps(f.report())}}]}
        with self.assertRaisesRegex(Failure,'LLM_MODEL_OUTPUT_TRUNCATED'):
            m.call('analysis',{},REPORT_SCHEMA,time.monotonic()+30)
        self.assertEqual(m.http.request.call_count,1)
        m.store.finish_llm.assert_called_once()

    def test_history_is_not_reinterpreted_under_generation_limits(self):
        value=f.report();value['summary']='x'*1000;value['facts']=['Historical fact.']*20
        validate(value,REPORT_SCHEMA)
        with self.assertRaises(Failure):validate(value,guided_report_schema())

    def test_complete_short_report_uses_one_existing_paid_request(self):
        m=self.model();value=f.report()
        m.http.request.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}
        self.assertEqual(m.call('analysis',{},REPORT_SCHEMA,time.monotonic()+30),value)
        self.assertEqual(m.http.request.call_count,1);m.store.reserve_llm.assert_called_once()

    def test_input_evidence_and_global_schema_are_not_shortened(self):
        m=self.model();data={'evidence':{'logs':'private diagnostic '*1000}}
        before=copy.deepcopy(data);body=m.payload('analysis',data,REPORT_SCHEMA)
        self.assertEqual(data,before)
        self.assertIn('private diagnostic '*1000,body['messages'][1]['content'])


@patch.dict('os.environ',{'SPI_TOKEN_ACCOUNTING_MODE':'byte-bound-v1'})
class ReportCompletenessTests(unittest.TestCase):
    def test_cap_fragment_is_rejected_without_an_extra_paid_regeneration(self):
        m=Model(Mock(),'test-only',store=Mock());value=f.report()
        value['summary']='No application-level errors, pan'
        m.http.request.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}
        with self.assertRaisesRegex(Failure,'LLM_INVALID_MODEL_SCHEMA'):
            m.call('analysis',{},REPORT_SCHEMA,time.monotonic()+30)
        self.assertEqual(m.http.request.call_count,1);m.store.finish_llm.assert_called_once()

    def test_ellipsis_or_open_parenthetical_is_not_a_complete_generated_report(self):
        from report_bounds import validate_generated_report
        for text in ('The pod itself (','Delivery failed...', 'Delivery failed'):
            value=f.report();value['impact']=text
            with self.assertRaises(Failure):validate_generated_report(value,guided_report_schema())

    def test_prompt_contains_real_headroom_not_an_invisible_schema_reference(self):
        m=Model(Mock(),'test-only',store=Mock());captured={}
        def call(task,data,schema,deadline):captured['task']=task;return f.report()
        m.call=call;m.analyze(f.event(),{'candidates':[]},{})
        self.assertIn('at most 8 words',captured['task'])
        self.assertIn('NOT a target to fill',captured['task'])
        self.assertIn('COMPLETE sentence',captured['task'])
