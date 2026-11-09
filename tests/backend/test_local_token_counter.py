import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from core import Failure
from local_token_counter import configured_counter, render, LocalCounter


class CounterContract(unittest.TestCase):
    def body(self):
        return dict(model='qwen3.8-27b', messages=[dict(role='system', content=' s '), dict(role='user', content=' u ')], chat_template_kwargs={'enable_thinking': False}, max_tokens=768)

    def test_render(self):
        self.assertEqual(render(self.body()), '<|im_start|>system\ns<|im_end|>\n<|im_start|>user\nu<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n')

    def test_contract_and_size(self):
        for key, value in [('model', 'other'), ('tools', []), ('max_tokens', 1), ('chat_template_kwargs', {'enable_thinking': True})]:
            body = self.body(); body[key] = value
            with self.assertRaises(Failure): render(body)
        body = self.body(); body['messages'][1]['content'] = 'a' * 524289
        with self.assertRaises(Failure): render(body)

    def test_modes_and_missing_assets(self):
        with patch.dict(os.environ, {'SPI_TOKEN_ACCOUNTING_MODE': 'byte-bound-v1'}):
            self.assertIsNone(configured_counter())
        with patch.dict(os.environ, {'SPI_TOKEN_ACCOUNTING_MODE': 'unknown'}):
            with self.assertRaises(Failure): configured_counter()
        with self.assertRaises(Failure): LocalCounter('/nonexistent/spi-tokenizer')

class ProxyFloor(unittest.TestCase):
    def counter(self):
        from types import SimpleNamespace
        counter = object.__new__(LocalCounter)
        counter._tokenizer = SimpleNamespace(encode=lambda *a, **kw: SimpleNamespace(ids=[1]*10))
        return counter

    def body(self, text, maximum=2500):
        return dict(model='qwen3.8-27b', messages=[dict(role='system', content='s'), dict(role='user', content=text)], chat_template_kwargs={'enable_thinking':False}, max_tokens=maximum, response_format={})

    def test_raw_floor_and_rounding(self):
        for text in [' '*100000+'x', 'a'*60000, '😀\\\"\n'*1000] + ['x'*n for n in range(8)]:
            for maximum in (768,2500):
                body=self.body(text, maximum)
                actual=self.counter().reservation(body)
                self.assertGreaterEqual(actual, (len(text)+1+3)//4+maximum)
                self.assertGreaterEqual(actual,10+2+256+maximum)
                self.assertEqual(actual,max(268,(len(text)+4)//4+256)+maximum)

    def test_oversize_no_bpe_and_contract_not_masked(self):
        from unittest.mock import Mock
        counter=self.counter(); counter._tokenizer=Mock()
        body=self.body(' '*600000)
        self.assertEqual(counter.reservation(body),30001)
        counter._tokenizer.encode.assert_not_called()
        for key,value in [('metadata',{}),('tools',[]),('model','bad'),('chat_template_kwargs',{'enable_thinking':True})]:
            bad=dict(body);bad[key]=value
            with self.assertRaises(Failure):counter.reservation(bad)
