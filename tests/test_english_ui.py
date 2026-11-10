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
        html = (ROOT / 'frontend/index.html').read_text()
        parsed = HTML(); parsed.feed(html)
        self.assertEqual(parsed.language, 'en')
        for path in (ROOT / 'frontend').iterdir():
            if path.suffix in ('.html', '.js', '.css', '.py'):
                self.assertFalse(any('\u0400' <= c <= '\u04ff' for c in path.read_text()), path.name)
        self.assertIn('Queue controls', html)
        self.assertIn('Cancellation does not close the GitHub issue', html)
        js = (ROOT / 'frontend/app.js').read_text()
        self.assertIn("toLocaleString('en-GB'", js)

    def test_render_confirmation_and_errors_are_english_but_data_is_preserved(self):
        script = r'''
const assert=require('node:assert/strict'), path=require('node:path');
const {createHarness,texts}=require(path.join(path.dirname(process.argv[1]),'../tests/ui_harness.js'));
const {context:ctx,get,run}=createHarness(process.argv[1]);
run('stopLoading()');
ctx.fixture={id:'a'.repeat(64),alertname:'Example alert',summary:'Исходный текст',state:'retry',
  stage:2,created:1,available:2,attempts:1,failures:0,review_catalog:2,review_batches:1,can_cancel:true};
ctx.data={counts:{processing:1,queued:2},limits:{parallel_jobs:1,requests_per_minute:3,tokens_per_minute:30000},
  now:100,items:[ctx.fixture],total:1};
run('items=[fixture]; draw(data)');
const rendered=texts(get('items')).join('\n');
for(const text of ['Waiting to retry','Analyze','Created:','Available at:','Failures:','Cancel','Исходный текст'])assert.ok(rendered.includes(text),text);
assert.ok(get('counts').textContent.includes('Active: 1'));
assert.ok(rendered.includes('a'.repeat(64)));
assert.equal(get('items').querySelectorAll('details').length,0);
run('busy=false;cancel(fixture,{disabled:false})');
assert.ok(ctx.confirmation.startsWith('Cancel pending job "Example alert"?'));
assert.ok(ctx.confirmation.includes('Its history will be preserved.'));
assert.ok(run("errorMessage(new TypeError('localized browser text'))").startsWith('Unable to reach'));
assert.ok(run("errorMessage({name:'TimeoutError'})").startsWith('The request timed out'));
ctx.fetch=async()=>({ok:false,redirected:false,status:401,headers:{get:()=> 'application/json'},json:async()=>({code:'AUTH_REQUIRED'})});
(async()=>{try{await run("api('list',{})");assert.fail('must reject');}
catch(error){assert.ok(error.message.startsWith('Your session has expired.'));}})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        subprocess.run(['node', '-e', script, str(ROOT / 'frontend/app.js')], check=True, capture_output=True, timeout=10)

    def test_selection_toolbar_does_not_participate_in_table_layout(self):
        css = (ROOT / 'frontend/style.css').read_text()
        bar = css.split('#bulkbar {', 1)[1].split('}', 1)[0]
        self.assertIn('position: fixed', bar)
        self.assertIn('bottom: 1rem', bar)
        self.assertNotIn('position: sticky', bar)
        self.assertIn('.selecting #bulkbar { pointer-events: none; }', css)
        self.assertIn('padding-bottom: 8rem', css)

    def test_sort_scroll_selection_and_bulk_cancellation_contract(self):
        result = subprocess.run(['node', str(ROOT / 'tests/ui_interactions.js'), str(ROOT / 'frontend/app.js')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__': unittest.main()
