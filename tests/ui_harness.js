'use strict';
// Tiny deterministic DOM fixture for the actual browser script. No dependencies,
// network, live cookies or production mutation. Real pointer layout tested separately.
const fs = require('node:fs'), vm = require('node:vm');
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.textContent = ''; this.value = 'pending';
    this.dataset = {}; this.attributes = {}; this.parentElement = null;
    const classes = new Set();
    this.classList = {add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),
      toggle:(c,on)=>{if(on)classes.add(c);else classes.delete(c);}};
  }
  appendChild(el) {
    if (el.fragment) { for (const child of el.children) this.appendChild(child); el.children = []; }
    else { this.children.push(el); el.parentElement = this; }
    return el;
  }
  replaceChildren() { this.children = []; this.textContent = ''; }
  setAttribute(key,value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  closest(selector) {
    if (selector === 'tr[data-job-id]' && this.tagName === 'TR' && this.dataset.jobId) return this;
    if (selector.includes('button') && ['A','BUTTON','INPUT','SELECT','TEXTAREA'].includes(this.tagName)) return this;
    return this.parentElement?.closest(selector) || null;
  }
  querySelectorAll(selector) { return this.children.flatMap(el => [...(el.tagName === selector.toUpperCase() ? [el] : []), ...el.querySelectorAll(selector)]); }
  setPointerCapture(id) { this.pointer = id; }
  hasPointerCapture(id) { return this.pointer === id; }
  releasePointerCapture() { this.pointer = null; }
  focus() { this.focused = true; }
}
function createHarness(source) {
  const elements = new Map(), events = {}, timers = new Map(); let timer = 0;
  const get = id => {
    if (!elements.has(id)) {
      const element = new Element(id === 'items' ? 'tbody' : id.startsWith('sort-') ? 'button' : 'div');
      if (id.startsWith('sort-')) new Element('th').appendChild(element);
      elements.set(id, element);
    }
    return elements.get(id);
  };
  const window = {scrollY:0,addEventListener:(key,fn)=>{events[key]=fn;},getSelection:()=>({isCollapsed:true,removeAllRanges:()=>{}}),scrollBy:(_,dy)=>{window.scrollY+=dy;},location:{assign:()=>{}}};
  const document = {getElementById:get,createElement:tag=>new Element(tag),
    createDocumentFragment:()=>Object.assign(new Element(),{fragment:true}),hidden:false,body:new Element('body'),
    addEventListener:(key,fn)=>{events[key]=fn;},elementFromPoint:()=>null};
  const context = vm.createContext({document,window,innerHeight:900,AbortSignal,AbortController,TypeError,SyntaxError,
    fetch:()=>new Promise(()=>{}),confirm:text=>{context.confirmation=text;return false;},
    setInterval:fn=>{events.interval=fn;},setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id),
    requestAnimationFrame:fn=>{timers.set(++timer,fn);return timer;},cancelAnimationFrame:id=>timers.delete(id)});
  vm.runInContext(fs.readFileSync(source,'utf8'),context);
  return {context,get,events,timers,run:code=>vm.runInContext(code,context)};
}
const texts = el => [el.textContent, ...el.children.flatMap(texts)];
module.exports = {createHarness,Element,texts};
