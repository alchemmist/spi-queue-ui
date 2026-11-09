'use strict';
const {logEvent}=require('./event-log');
const {createHash,timingSafeEqual}=require('node:crypto');
const {identity}=require('./SpiEventIdentity');
const sensitive=/authorization|cookie|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|x[_-]?token|private[_-]?key/i;
function sanitize(value,secrets){
 if(typeof value==='string'){
  let s=value;for(const secret of secrets)if(secret)s=s.split(secret).join('[REDACTED]');
  return s.replace(/Bearer\s+\S+/gi,'[REDACTED]').replace(/(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g,'[REDACTED]').replace(/((?:password|secret|token|api[_-]?key|cookie|authorization)\s*[=:]\s*)[^\s,;&]+/gi,'$1[REDACTED]');
 }
 if(Array.isArray(value))return value.map(v=>sanitize(v,secrets));
 if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,sensitive.test(k)?'[REDACTED]':sanitize(v,secrets)]));
 return value;
}
function same(a,b){return timingSafeEqual(createHash('sha256').update(a).digest(),createHash('sha256').update(b).digest());}
class SocratAlertWebhook {
 constructor(){this.description={displayName:'Socrat Alert Webhook',name:'socratAlertWebhook',group:['trigger'],version:1,
  description:'Authenticate alerts, durably enqueue triage and emit sanitized Telegram data without request headers',defaults:{name:'Alertmanager Webhook'},inputs:[],outputs:['main'],
  credentials:[{name:'socratAlertIngress',required:true},{name:'httpHeaderAuth',required:true}],
  webhooks:[{name:'default',httpMethod:'POST',isFullPath:true,path:'={{$parameter["path"]}}',responseMode:'onReceived',responseCode:200}],
  properties:[{displayName:'Path',name:'path',type:'string',default:'socrat-alerts'},
   {displayName:'Require Authorization',name:'requireAuth',type:'boolean',default:true,description:'Disable only during controlled notifier migration; unauthenticated calls never enqueue triage'}]};}
 async webhook(context){
  context=context||this; // Custom-node loader in n8n 1.123 binds the webhook context as this.
  const req=context.getRequestObject(),res=context.getResponseObject();
  const ingress=await context.getCredentials('socratAlertIngress'),worker=await context.getCredentials('httpHeaderAuth');
  if(!ingress.token||worker.name!=='X-Worker-Token'||!worker.value)throw new Error('Socrat alert credentials are not configured');
  const auth=typeof req.headers.authorization==='string'?req.headers.authorization:'';
  const authenticated=same(auth,'Bearer '+ingress.token);
  if(!authenticated&&context.getNodeParameter('requireAuth',true)){
   res.writeHead(401);res.end('Unauthorized');return{noWebhookResponse:true};
  }
  const body=req.body;
  if(!body||!Array.isArray(body.alerts)||!body.alerts.length||body.alerts.length>200||JSON.stringify(body).length>512000){
   res.writeHead(400);res.end('Invalid alert group');return{noWebhookResponse:true};
  }
  const safe={externalURL:String(body.externalURL||'socrat-alerts'),alerts:body.alerts.map(a=>Object.fromEntries(['status','fingerprint','labels','annotations','startsAt','endsAt','generatorURL'].filter(k=>a[k]!==undefined).map(k=>[k,a[k]])))};
  const payload=sanitize(safe,[String(ingress.token),String(worker.value)]);
  let queued=false,triageEvents=[];
  if(authenticated){
   try{triageEvents=payload.alerts.map(alert=>identity(payload.externalURL,alert));}
   catch{res.writeHead(400);res.end('Invalid alert identity');return {noWebhookResponse:true};}
  }
  if(authenticated){
   try{
    const r=await fetch('http://spi-triage.automation.svc:8080/enqueue-group',{method:'POST',headers:{'Content-Type':'application/json','X-Worker-Token':worker.value},body:JSON.stringify(payload),signal:AbortSignal.timeout(8000)});
    const acknowledgement=await r.json();
    if(!r.ok||acknowledgement.accepted!==payload.alerts.length)throw new Error('Queue unavailable');
    if(Array.isArray(acknowledgement.events))triageEvents=acknowledgement.events;queued=true;
   }catch{
    // Upstream retries recover the durable handoff; Telegram still runs now.
    res.writeHead(503);res.end('Triage queue unavailable; retry delivery');
    logEvent('spi-triage','handoff_failed');
   }
  }
  const result={workflowData:[[{json:{body:payload,triageQueued:queued,triageEvents}}]]};
  if(authenticated&&!queued)return{...result,noWebhookResponse:true};
  return{...result,webhookResponse:{status:queued?'accepted':'legacy_telegram_only'}};
 }
}
module.exports={SocratAlertWebhook,sanitize};
