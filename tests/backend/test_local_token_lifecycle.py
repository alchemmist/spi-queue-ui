"""Native-runtime regressions; skipped when the optional pinned runtime is absent."""
import concurrent.futures
import os
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import test_triage
from integrations import Model
from core import Failure
from local_token_counter import LocalCounter, MAX_BYTES

@unittest.skipUnless(Path('/opt/spi-tokenizer/tokenizer.json').exists(), 'optional pinned runtime')
class Lifecycle(unittest.TestCase):
    def test_retained_models_and_request_isolation(self):
        with patch.dict(os.environ, SPI_TOKEN_ACCOUNTING_MODE='local-qwen-v1'):
            models = [Model(Mock(), 'unused') for _ in range(24)]
        self.assertEqual(len({id(m.counter.tokenizer) for m in models}), 1)
        bodies = [dict(model='qwen3.8-27b', messages=[dict(role='system',content='system'),dict(role='user',content=('hello '+str(i))*10)],chat_template_kwargs={'enable_thinking':False},max_tokens=768,response_format={}) for i in range(24)]
        expected = [m.counter.reservation(b) for m,b in zip(models,bodies)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            actual = list(pool.map(lambda pair: pair[0].counter.reservation(pair[1]), zip(models,bodies)))
        self.assertEqual(actual,expected)
        body = bodies[0]
        body['messages'][0]['content']=''
        body['messages'][1]['content']='a'*(MAX_BYTES-128)
        models[0].counter.reservation(body)
        body['messages'][1]['content']+='a'
        self.assertEqual(models[0].counter.reservation(body), 30001)
        for m in models: m.http.request.assert_not_called()

    def test_invalid_native_contract_before_recovery_or_http(self):
        import local_token_counter as lc
        from contextlib import ExitStack
        for change in ('asset','template','library','manifest'):
            with self.subTest(change=change), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ,SPI_TOKEN_ACCOUNTING_MODE='local-qwen-v1'))
                if change in ('asset','template'):
                    pins=dict(lc.PINS);pins['chat_template.jinja' if change=='template' else 'tokenizer.json']='0'*64
                    stack.enter_context(patch.object(lc,'PINS',pins))
                elif change=='library':
                    stack.enter_context(patch('local_token_counter.importlib.metadata.version',return_value='0.0.0'))
                else:
                    stack.enter_context(patch('local_token_counter.json.loads',return_value={}))
                http=Mock();store=Mock()
                with self.assertRaises(Failure):Model(http,'unused',store=store)
                http.request.assert_not_called();store.recover_on_start.assert_not_called();store.reserve_llm.assert_not_called()
        m=Model(Mock(),'unused')
        if m.counter is None:
            with patch.dict(os.environ,SPI_TOKEN_ACCOUNTING_MODE='local-qwen-v1'):m=Model(Mock(),'unused')
        base=dict(model='qwen3.8-27b',messages=[dict(role='system',content='s'),dict(role='user',content='u')],chat_template_kwargs={'enable_thinking':False},max_tokens=768,response_format={})
        for key,value in [('model','wrong'),('tools',[]),('chat_template_kwargs',{'enable_thinking':True}),('messages',[dict(role='assistant',content='x')])]:
            with self.assertRaises(Failure):m.counter.reservation(dict(base,**{key:value}))
        m.http.request.assert_not_called()

    def test_cached_native_does_not_bypass_validation(self):
        LocalCounter()
        with self.assertRaises(Failure): LocalCounter('/nonexistent/spi-tokenizer')
        with patch('local_token_counter.PINS', {'tokenizer.json':'0'*64}):
            with self.assertRaises(Failure): LocalCounter()
