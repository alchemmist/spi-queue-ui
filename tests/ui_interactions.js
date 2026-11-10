'use strict';
const assert = require('node:assert/strict');
const {createHarness} = require('./ui_harness.js');
const source = process.argv[2];
const item = (n, fields={}) => ({id:n.toString(16).padStart(64,'0'), alertname:'Alert '+n,state:'queued',stage:0,
  created:n,updated:n,available:n,attempts:0,failures:0,review_catalog:null,can_cancel:true,...fields});
const data = (items, next_cursor=null) => ({counts:{queued:items.length}, limits:{parallel_jobs:1,requests_per_minute:3,tokens_per_minute:30000},now:100,items,total:items.length,next_cursor});
const response = (body,status=200) => ({ok:status===200,status,redirected:false,headers:{get:()=> 'application/json'},json:async()=>body});
function setup(list=[item(1),item(2),item(3),item(4)]) {
  const h = createHarness(source);h.context.fixture=list;h.context.data=data(list);
  h.run('stopLoading();items=[...fixture];total=items.length;cursor=null;draw(data);loadedStatus()');
  return h;
}
const pointer = (target,y) => ({target,button:0,buttons:1,isPrimary:true,pointerId:1,clientX:100,clientY:y,preventDefault:()=>{}});
(async()=>{
  {
    const h=setup();
    assert.equal(h.run('sort'), 'created');assert.equal(h.run('direction'), 'asc');
    assert.equal(h.get('sort-created').parentElement.getAttribute('aria-sort'),'ascending');
    assert.ok(h.get('loaded').textContent.includes('4 of 4'));
    assert.equal(h.get('items').querySelectorAll('input').length,0);
    const first=h.get('items').children[0], third=h.get('items').children[2];
    h.get('items').onpointerdown(pointer(first,100));
    h.context.document.elementFromPoint=()=>third;
    h.events.pointermove(pointer(third,300));h.events.pointerup(pointer(third,300));
    assert.equal(h.run('selected.size'),3);
    assert.equal(first.getAttribute('aria-selected'),'true');assert.ok(first.classList.contains('selected'));
    assert.equal(h.run('gesture'),null);assert.ok(!h.context.document.body.classList.contains('selecting'));
    h.run('skipClick=false');
    h.get('items').onpointerdown(pointer(third,300));h.events.pointerup(pointer(third,300));
    assert.equal(h.run('selected.size'),2);
    const fourth=h.get('items').children[3];h.run('skipClick=false');
    h.get('items').onpointerdown(pointer(fourth,400));h.events.pointerup(pointer(fourth,400));
    assert.equal(h.run('selected.size'),3);
    const link={closest:selector=>selector.includes('button')?{}:null};
    h.get('items').onpointerdown(pointer(link,10));assert.equal(h.run('gesture'),null);
    h.get('items').onclick({target:link});assert.equal(h.run('selected.size'),3);
    h.get('clear-selection').onclick();assert.equal(h.run('selected.size'),0);
    h.get('items').onkeydown({target:first,key:' ',preventDefault:()=>{}});assert.equal(h.run('selected.size'),1);
    h.get('items').onkeydown({target:first,key:'Escape',preventDefault:()=>{}});assert.equal(h.run('selected.size'),0);
    h.get('items').onpointerdown(pointer(first,100));h.events.pointermove(pointer(third,300));
    h.events.pointercancel();assert.equal(h.run('selected.size'),0);
  }
  {
    const h=setup([item(1)]), posted=[];
    const after={sort:'created',direction:'asc',values:[1,item(1).id]};
    h.run('cursor={}');h.context.fetch=async(url,options)=>{posted.push(JSON.parse(options.body));return response({...data([item(1),item(2)],after),total:3});};
    await h.run('refresh()');assert.equal(h.run('items.length'),2);
    h.get('select-all').checked=true;h.get('select-all').onchange();
    assert.equal(h.run('selected.size'),2);assert.equal(h.get('cancel-selected').disabled,true);
    h.context.fetch=async(url,options)=>{posted.push(JSON.parse(options.body));return response(data([item(2),item(3)]));};
    await h.run('loadMore()');assert.equal(h.run('items.length'),3);assert.equal(h.run('selected.size'),3);
    assert.equal(h.get('cancel-selected').disabled,false);
    assert.deepEqual(posted[1].after,after);assert.equal(posted[0].sort,'created');assert.equal(posted[0].direction,'asc');
    h.get('select-all').checked=false;h.get('select-all').onchange();assert.equal(h.run('selected.size'),0);
    h.context.fetch=async(url,options)=>{posted.push(JSON.parse(options.body));return response(data([item(3)]));};
    h.get('sort-created').onclick();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(posted.at(-1).direction,'desc');assert.ok(!('after' in posted.at(-1)));
    h.get('sort-alert').onclick();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(posted.at(-1).sort,'alert');assert.equal(posted.at(-1).direction,'asc');
  }
  {
    const jobs=Array.from({length:400},(_,n)=>item(n+1));
    const h=setup([...jobs,item(999,{can_cancel:false,state:'processing'})]),requests=[];
    h.context.confirm=()=>true;h.run('for(const item of items)selected.set(item.id,{...item});selectionUI()');
    h.context.fetch=async(url,options)=>{
      const body=JSON.parse(options.body);requests.push([url,body]);assert.equal(url,'/api/cancel-batch');
      assert.equal(body.items.length,400);assert.equal(body.items[0].expected_updated,1);
      h.context.newArrival=item(9000);h.run('items.push(newArrival);fixture[0].updated=999');
      return response({cancelled:jobs.map(x=>x.id),already_cancelled:[],skipped:[],counts:{queued:1,processing:1,cancelled:400},now:123});
    };
    await h.run('cancelSelected()');
    assert.equal(requests.length,1);assert.equal(h.run('items.length'),2);
    assert.equal(h.run('items[1].id'),item(9000).id);assert.equal(h.run('selected.has(items[1].id)'),false);
    assert.ok(!requests[0][1].items.some(x=>x.job_id===item(9000).id));
    assert.equal(h.run('selected.size'),1);assert.ok(h.get('status').textContent.includes('400 cancelled · 1 changed/protected'));
    assert.ok(h.get('counts').textContent.includes('Cancelled: 400'));assert.equal(h.get('items').children.length,2);
  }
  {
    const h=setup(),requests=[];h.context.confirm=()=>true;
    h.run('for(const item of items)selected.set(item.id,{...item})');
    h.context.fetch=async(url,options)=>{
      requests.push([url,JSON.parse(options.body)]);
      return response({cancelled:[item(1).id],already_cancelled:[],skipped:[2,3,4].map(n=>({job_id:item(n).id,code:'QUEUE_ITEM_CHANGED'})),counts:{queued:3,cancelled:1},now:123});
    };
    await h.run('cancelSelected()');assert.equal(requests.length,1);assert.equal(h.run('items.length'),3);
    assert.equal(h.run('selected.size'),0);assert.equal(h.run('items.every(x=>!x.can_cancel)'),true);
    assert.ok(h.get('status').textContent.includes('1 cancelled · 3 changed/protected'));
  }
  for(const scenario of ['auth','lost-reply','malformed']) {
    const h=setup(),requests=[];h.context.confirm=()=>true;
    h.run('for(const item of items)selected.set(item.id,{...item})');
    h.context.fetch=async(url,options)=>{
      requests.push([url,JSON.parse(options.body)]);
      if(scenario==='auth')return response({code:'AUTH_REQUIRED'},401);
      if(scenario==='lost-reply')throw new TypeError('offline');
      return response({cancelled:[item(1).id],already_cancelled:[],skipped:[],counts:{},now:123});
    };
    await h.run('cancelSelected()');assert.equal(requests.length,1);
    assert.equal(h.run('items.length'),4);assert.equal(h.run('selected.size'),4);
    assert.ok(h.get('status').textContent.includes('Cancellation not confirmed'));
    assert.equal(h.run('busy'),false);assert.equal(h.run('loadFailed'),true);
    await h.run('loadMore()');h.events.interval();assert.equal(requests.length,1);
  }
  {
    const h=setup(),posted=[];let resolve;
    h.context.fetch=()=>new Promise(r=>{resolve=r;});const old=h.run('refresh()');
    h.context.fetch=async(url,options)=>{posted.push(JSON.parse(options.body));return response(data([item(8)]));};
    h.get('sort-state').onclick();await new Promise(r=>setImmediate(r));
    resolve(response(data([item(1)])));await old;
    assert.equal(h.run('items[0].id'),item(8).id);assert.equal(h.run('sort'),'state');
    h.run('cursor={}');h.context.fetch=async()=>{posted.push('failure');throw new TypeError('offline');};
    await h.run('loadMore()');const count=posted.length;
    await h.run('loadMore()');h.events.visibilitychange();h.events.interval();assert.equal(posted.length,count);
    assert.equal(h.get('retry-load').hidden,false);
  }
  console.log('UI interactions: sorting, cursor/dedup, pointer/keyboard selection, frozen bulk scope, partial failure, auth stop, stale response and loading failure passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
