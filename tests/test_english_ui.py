"""Offline interface-language regressions; incoming alert data is not rewritten."""
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]

class EnglishUITests(unittest.TestCase):
    def test_all_owned_interface_assets_are_english(self):
        class HTML(HTMLParser):
            language = None
            def handle_starttag(self, tag, attrs):
                if tag == 'html': self.language = dict(attrs).get('lang')
        html = (ROOT / 'app/index.html').read_text()
        parsed = HTML(); parsed.feed(html)
        self.assertEqual(parsed.language, 'en')
        for path in (ROOT / 'app').iterdir():
            if path.suffix in ('.html', '.js', '.css', '.py'):
                self.assertFalse(any('\u0400' <= c <= '\u04ff' for c in path.read_text()), path.name)
        self.assertIn('Queue controls', html)
        self.assertIn('Cancellation does not close the GitHub issue', html)
        js = (ROOT / 'app/app.js').read_text()
        self.assertIn("toLocaleString('en-GB'", js)

    def test_render_confirmation_and_errors_are_english_but_data_is_preserved(self):
        script = r'''
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
class Element {
  constructor(){this.children=[];this.textContent='';this.value='pending';}
  appendChild(el){this.children.push(el);return el;}
  replaceChildren(){this.children=[];}
}
const elements=new Map(), get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const ctx=vm.createContext({document:{getElementById:get,createElement:()=>new Element(),hidden:false},
  fetch:()=>new Promise(()=>{}),setInterval:()=>{},AbortSignal,confirm:text=>{ctx.confirmation=text;return false;}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
ctx.fixture={id:'a'.repeat(64),alertname:'Example alert',summary:'Исходный текст',state:'retry',
  stage:2,created:1,available:2,attempts:1,failures:0,review_catalog:2,review_batches:1,can_cancel:true};
ctx.data={counts:{processing:1,queued:2},limits:{parallel_jobs:1,requests_per_minute:3,tokens_per_minute:30000},
  now:100,items:[ctx.fixture],total:1};
vm.runInContext('draw(data)',ctx);
const texts=el=>[el.textContent,...el.children.flatMap(texts)];
const rendered=texts(get('items')).join('\n');
for(const text of ['Waiting to retry','Analyze','Created:','Available at:','Failures:','Cancel','Исходный текст'])assert.ok(rendered.includes(text),text);
assert.ok(get('counts').textContent.includes('Active: 1'));
assert.equal(get('page').textContent,'1–1 of 1');
vm.runInContext('busy=false;cancel(fixture,{disabled:false})',ctx);
assert.ok(ctx.confirmation.startsWith('Cancel pending job "Example alert"?'));
assert.ok(ctx.confirmation.includes('Its history will be preserved.'));
assert.ok(vm.runInContext("errorMessage(new TypeError('localized browser text'))",ctx).startsWith('Unable to reach'));
assert.ok(vm.runInContext("errorMessage({name:'TimeoutError'})",ctx).startsWith('The request timed out'));
ctx.fetch=async()=>({ok:false,redirected:false,status:401,headers:{get:()=> 'application/json'},json:async()=>({code:'AUTH_REQUIRED'})});
(async()=>{try{await vm.runInContext("api('list',{})",ctx);assert.fail('must reject');}
catch(error){assert.ok(error.message.startsWith('Your session has expired.'));}})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        subprocess.run(['node', '-e', script, str(ROOT / 'app/app.js')], check=True, capture_output=True, timeout=10)

if __name__ == '__main__': unittest.main()
