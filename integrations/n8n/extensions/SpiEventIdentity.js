'use strict';
const {createHash}=require('node:crypto');
function serialize(v){
 if(Array.isArray(v))return '['+v.map(serialize).join(',')+']';
 if(v&&typeof v==='object'){
  const cmp=(a,b)=>{const x=Array.from(a,c=>c.codePointAt(0)),y=Array.from(b,c=>c.codePointAt(0));for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i];return x.length-y.length;};
  return '{'+Object.keys(v).sort(cmp).map(k=>JSON.stringify(k)+':'+serialize(v[k])).join(',')+'}';
 }
 return JSON.stringify(v);
}
function digest(v){const text=serialize(v).replace(/[^\x00-\x7F]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));return createHash('sha256').update(text).digest('hex');}
function identity(source,alert){
 const raw=String(alert.startsAt||'');const date=new Date(raw);
 if(!Number.isFinite(date.getTime()))throw new Error('Invalid alert timestamp');
 const match=raw.match(/T\d\d:\d\d:\d\d(?:[.,](\d+))?/);const fraction=(match&&match[1]||'').slice(0,6).padEnd(6,'0');
 const started=date.toISOString().slice(0,19)+(Number(fraction)?'.'+fraction:'')+'Z';
 const origin=Array.from(String(source)).slice(0,256).join('');
 const episode=digest([origin,alert.fingerprint||digest(alert.labels||{}),started]);
 return {episode,event_key:digest([episode,alert.status])};
}
module.exports={identity};
