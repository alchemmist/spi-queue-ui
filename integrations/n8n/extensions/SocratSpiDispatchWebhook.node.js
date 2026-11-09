'use strict';
const {createHash,timingSafeEqual}=require('node:crypto');
function same(a,b){return timingSafeEqual(createHash('sha256').update(a).digest(),createHash('sha256').update(b).digest());}
class SocratSpiDispatchWebhook {
 constructor(){this.description={displayName:'Socrat SPI Dispatch Webhook',name:'socratSpiDispatchWebhook',group:['trigger'],version:1,
  description:'Authenticate an SPI wake without exposing request headers or payload in execution data',defaults:{name:'Dispatch Triage Job'},inputs:[],outputs:['main'],
  credentials:[{name:'httpHeaderAuth',required:true}],
  webhooks:[{name:'default',httpMethod:'POST',isFullPath:true,path:'={{$parameter["path"]}}',responseMode:'onReceived',responseCode:202}],
  properties:[{displayName:'Path',name:'path',type:'string',default:'spi-incident-triage-dispatch'}]};}
 async webhook(context){
  context=context||this;
  const req=context.getRequestObject(),res=context.getResponseObject();
  const credential=await context.getCredentials('httpHeaderAuth');
  if(credential.name!=='X-Worker-Token'||typeof credential.value!=='string'||!credential.value)throw new Error('SPI dispatch credential is not configured');
  const provided=req.headers['x-worker-token'];
  if(typeof provided!=='string'||!same(provided,credential.value)){
   res.writeHead(401);res.end('Unauthorized');return {noWebhookResponse:true};
  }
  // No request headers, cookies, URL parameters or body enter the execution.
  return {workflowData:[[{json:{dispatch:true}}]],webhookResponse:{status:'accepted'}};
 }
}
module.exports={SocratSpiDispatchWebhook};
