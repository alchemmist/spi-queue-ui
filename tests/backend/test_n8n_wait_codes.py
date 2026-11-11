"""Run the actual exported consumer against every producer wait code, offline."""
import ast
import inspect
import json
from pathlib import Path
import re
import subprocess
import textwrap
import unittest

from durable_wait import WaitOwners

ROOT = Path(__file__).resolve().parents[2]


def producer_codes():
    tree = ast.parse(textwrap.dedent(inspect.getsource(WaitOwners.finish)))
    tests = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)
             and isinstance(n.left, ast.Name) and n.left.id == 'code'
             and any(isinstance(op, ast.NotIn) for op in n.ops)]
    assert len(tests) == 1
    return set(ast.literal_eval(tests[0].comparators[0])) | {'WORKER_ADMISSION_DEFERRED'}


class WaitCodeContract(unittest.TestCase):
    def test_export_accepts_exactly_the_backend_safe_wait_codes(self):
        workflow = json.loads((ROOT/'integrations/n8n/triage-workflow.json').read_text())
        code = next(n['parameters']['jsCode'] for n in workflow['nodes'] if n['name'] == 'Select Claimed Job')
        match = re.search(r"if\(x.status==='deferred' && !(\[[^\n]+?\])\.includes\(x.code\)\)", code)
        self.assertIsNotNone(match)
        self.assertEqual(set(ast.literal_eval(match[1])), producer_codes())

    def test_real_javascript_retains_fences_deadlines_and_unsafe_failure_rejection(self):
        workflow = json.loads((ROOT/'integrations/n8n/triage-workflow.json').read_text())
        code = next(n['parameters']['jsCode'] for n in workflow['nodes'] if n['name'] == 'Select Claimed Job')
        script = r'''
const fs=require('fs'), assert=require('assert');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const select=new Function('items','$execution',input.code);
Date.now=()=>1700000000000;
const base={contract:1,status:'deferred',job_id:'a'.repeat(64),execution_id:'16795',
 workflow_revision:'durable-v1',fence:'b'.repeat(64),stage:'match',
 deadline:1700021600,due_at:1700000066};
function run(x){return select([{json:x}],{id:'16795'});}
for(const code of input.codes){
 for(const stage of ['match','collect','analyze','publish']){
  const x={...base,code,stage};
  if(code==='STORAGE_TRANSIENT' && stage!=='publish') assert.throws(()=>run(x),/INVALID_SPI_WAIT_CODE/);
  else assert.deepStrictEqual(run(x),[{json:x}]);
 }
}
for(const code of ['WRITE_NEEDS_REVIEW','LLM_UPSTREAM_TIMEOUT','UPSTREAM_NETWORK_ERROR','INVALID_MODEL_SCHEMA','UNKNOWN'])
 assert.throws(()=>run({...base,code}),/INVALID_SPI_WAIT_CODE/);
const valid={...base,code:'MATCH_PROGRESS_DEFERRED'};
assert.throws(()=>run({...valid,execution_id:'16796'}),/INVALID_SPI_WAIT_ENVELOPE/);
assert.throws(()=>run({...valid,fence:'bad'}),/INVALID_SPI_WAIT_ENVELOPE/);
assert.throws(()=>run({...valid,workflow_revision:'other'}),/INVALID_SPI_WAIT_ENVELOPE/);
assert.throws(()=>run({...valid,due_at:1700000000}),/INVALID_SPI_WAIT_DUE/);
assert.throws(()=>run({...valid,due_at:valid.deadline}),/INVALID_SPI_WAIT_DUE/);
assert.throws(()=>run({...valid,status:'ready',deadline:1699999999}),/SPI_EXECUTION_DEADLINE/);
assert.deepStrictEqual(run({contract:1,status:'empty',job_id:''}),[]);
assert.equal(run({...base,status:'ready'}).length,1);
assert.equal(run({...base,status:'complete',stage:'publish',outcome:'complete'}).length,1);
assert.throws(()=>run({...base,status:'complete',stage:'match',outcome:'complete'}),/INVALID_SPI_TERMINAL_OUTCOME/);
console.log('all producer codes and negative contract guards verified');
'''
        result = subprocess.run(['node','-e',script], input=json.dumps({'code':code,'codes':sorted(producer_codes())}),
                                text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
