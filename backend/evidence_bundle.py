"""Bounded, redacted evidence persisted as ordinary issue comments; no Contents grant."""
import hashlib
import json
import re
import os
from core import Failure, SECRET_KEY, SECRET_VALUE, ASSIGNMENT
from grafana_links import repair_markdown_links

MAX_ARTIFACT=90000
MAX_BUNDLE=360000
PART_SIZE=40000

def encoded(value):
    return json.dumps(value,ensure_ascii=True,sort_keys=True,indent=2).replace('`','\\u0060')

def redact(value,known=()):
    if isinstance(value,dict):
        return {str(k):'[REDACTED]' if SECRET_KEY.search(str(k)) or re.fullmatch(r'(?i)(token|credential|credentials|client_secret|set-cookie)',str(k)) else redact(v,known) for k,v in value.items()}
    if isinstance(value,list):return [redact(v,known) for v in value]
    if not isinstance(value,str):return value
    for secret in sorted((x for x in known if isinstance(x,str) and len(x)>=8),key=len,reverse=True):value=value.replace(secret,'[REDACTED]')
    value=SECRET_VALUE.sub('[REDACTED]',value)
    value=re.sub(r'(?im)\b((?:set-cookie|cookie|authorization)\s*[:=]\s*)[^\r\n]+',r'\1[REDACTED]',value)
    value=re.sub(r'''(?i)((?:["']?(?:password|passwd|token|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)["']?)\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*')''',r'\1"[REDACTED]"',value)
    value=re.sub(r'(?im)(\b(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\r\n,;&]+',r'\1[REDACTED]',value)
    value=ASSIGNMENT.sub(r'\1[REDACTED]',value)
    value=re.sub(r'(\b[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@',r'\1[REDACTED]@',value,flags=re.I)
    return re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b','[REDACTED_EMAIL]',value)

def prepare_bundle(event,evidence,artifacts,links=(),known=()):
    records=[];parts=[];remaining=MAX_BUNDLE
    for n,item in enumerate(artifacts):
        safe=redact(item,known);content=encoded(safe.get('content'));original=len(content)
        take=min(MAX_ARTIFACT,remaining);stored=content[:take];remaining-=len(stored)
        aid='E'+str(n+1).zfill(3);sha=hashlib.sha256(stored.encode()).hexdigest()
        row={'id':aid,'name':re.sub(r'[^A-Za-z0-9_.-]','_',str(safe.get('name','evidence.json')))[:100],
             'sha256':sha,'source':safe.get('source',{}),'stored_chars':len(stored),'sanitized_chars':original,
             'truncated':len(stored)<original,'redaction_applied':encoded(item)!=encoded(safe)}
        records.append(row)
        for offset in range(0,len(stored),PART_SIZE):
            parts.append({'id':aid,'name':row['name'],'part':offset//PART_SIZE+1,'total':(len(stored)+PART_SIZE-1)//PART_SIZE,
                          'sha256':sha,'text':stored[offset:offset+PART_SIZE]})
    manifest={'format':'socrat-evidence-v2','event_key':event['event_key'],'status':event['status'],
              'incident_start':event['startsAt'],'incident_end':event.get('endsAt'),'window':evidence.get('window'),
              'artifacts':records,'missing':redact(evidence.get('missing',[]),known),'links':redact(list(links),known),
              'limits':{'artifact_chars':MAX_ARTIFACT,'bundle_chars':MAX_BUNDLE,'comment_payload_chars':PART_SIZE},
              'redaction_policy':'Known credentials, credential fields/patterns, cookies, PEM private keys, URL passwords and emails removed. Internal topology retained. Original data is bounded, not an exhaustive dump.'}
    manifest_text=encoded(manifest)
    # Manifest itself is a first-class, partitioned artifact, never silently clipped.
    manifest_parts=[{'id':'MANIFEST','name':'manifest.json','part':i//PART_SIZE+1,'total':(len(manifest_text)+PART_SIZE-1)//PART_SIZE,
                     'sha256':hashlib.sha256(manifest_text.encode()).hexdigest(),'text':manifest_text[i:i+PART_SIZE]}for i in range(0,len(manifest_text),PART_SIZE)]
    allparts=manifest_parts+parts
    for part in allparts:
        identity=hashlib.sha256(encoded(part).encode()).hexdigest()[:20]
        part['marker']='<!-- socrat-spi-artifact:'+event['event_key']+':'+identity+' -->'
    excerpts=[];budget=24000;seen_ids=set()
    base_names={'alert-event.json','rule-definitions.json','metric-samples.json','collection-context.json'}
    for part in parts:
        if budget<=0:break
        if part['id'] in seen_ids or part['name'] in base_names:continue
        seen_ids.add(part['id'])
        text=part['text'][:min(4000,budget)];budget-=len(text)
        excerpts.append({'id':part['id'],'part':part['part'],'excerpt':text,'excerpt_truncated':len(text)<len(part['text'])})
    return {'manifest':manifest,'parts':allparts,'model_context':{'index':records,'excerpts':excerpts,
        'limitation':'Excerpts are bounded. Use evidence IDs in facts. Not every stored byte was reviewed by the model.'}}

def index_markdown(bundle):
    if not bundle:return ''
    m=bundle['manifest'];lines=['','## Reproducible evidence','Incident: '+m['incident_start']+' to '+(m.get('incident_end') or 'collection time')+'.',
      'Text artifacts and checksums follow in linked comments. Parts concatenate in order; SHA-256 covers the stored ASCII JSON text, without code fences. Truncation/redaction and failed reads are recorded in MANIFEST.',
      'Grafana Explore links request sign-in and require Explore permission (Editor/Admin here); anonymous dashboard access is not sufficient.']
    link_budget=24000
    for link in m.get('links',[])[:18]:
        # Only generated Grafana origin links, never alert-supplied arbitrary URLs.
        if link.get('url','').startswith('https://grafana.socrat.alchemmist.xyz/explore?'):
            if len(link['url'])>link_budget:continue
            link_budget-=len(link['url'])
            lines.append('- ['+link['title'].replace('[','').replace(']','')+']('+link['url']+')')
    lines+=['- '+x['id']+': `'+x['name']+'`'+(' (TRUNCATED)' if x['truncated'] else '') for x in m['artifacts']]
    # Also fix bundles collected/saved before the generator login correction.
    return repair_markdown_links('\n'.join(lines))

def index_marker(bundle):
    return '<!-- socrat-spi-evidence-index:'+bundle['manifest']['event_key']+':'+hashlib.sha256(encoded(bundle['manifest']).encode()).hexdigest()[:20]+' -->'

def trusted_comment(comment):
    user=comment.get('user',{});app=comment.get('performed_via_github_app')or{}
    if user.get('type')!='Bot':return False
    if app.get('id')is not None:return app['id']==int(os.environ.get('GITHUB_APP_ID','5569869'))
    # Issue responses may omit performed_via_github_app; bot user IDs are immutable too.
    return user.get('id')==int(os.environ.get('GITHUB_BOT_USER_ID','351189559'))

def publish_parts(github,number,bundle,progress,save):
    """Persist before each POST; an unobservable ambiguous write requires review."""
    if not bundle:return
    comments=[c for c in github.comments(number) if trusted_comment(c)]
    for part in bundle['parts']:
        marker=part['marker'];found=next((c for c in comments if marker in (c.get('body') or '')),None)
        if marker in progress.get('done',{}):continue
        if found:
            progress.setdefault('done',{})[marker]=found.get('html_url','');progress.pop('pending',None);save();continue
        if progress.get('pending')==marker:raise Failure('WRITE_NEEDS_REVIEW')
        progress['pending']=marker;save()
        # Four backticks cannot be closed by ordinary triple-backtick stack/log text.
        fence='`'*(max([len(x)for x in re.findall(r'`+',part['text'])]+[3])+1)
        body='## Evidence '+part['id']+' — '+part['name']+' (part '+str(part['part'])+'/'+str(part['total'])+')\n\nSHA-256: `'+part['sha256']+'`\n\n'+fence+'text\n'+part['text']+'\n'+fence+'\n\n'+marker
        try:response=github.request('POST','/issues/'+str(number)+'/comments',{'body':body})
        except Failure as exc:
            if exc.code.startswith('HTTP_4') and exc.code!='HTTP_408':progress.pop('pending',None);save()
            raise
        progress.setdefault('done',{})[marker]=response.get('html_url','');progress.pop('pending',None);save()
    # A deterministic index comment provides direct navigation without editing human text.
    links=progress.get('done',{});marker=index_marker(bundle)
    if any(marker in (c.get('body')or'')for c in comments) or progress.get('index_done'):return
    if progress.get('index_pending'):raise Failure('WRITE_NEEDS_REVIEW')
    lines=['## Evidence artifact index', 'Event: `'+bundle['manifest']['event_key']+'`']
    for p in bundle['parts']:
        url=links.get(p['marker']);label=p['id']+' / '+p['name']+' / '+str(p['part'])
        lines.append('- ['+label+']('+url+')' if url else '- '+label)
    progress['index_pending']=True;save()
    try:github.request('POST','/issues/'+str(number)+'/comments',{'body':index_markdown(bundle)+'\n\n'+'\n'.join(lines)+'\n\n'+marker})
    except Failure as exc:
        if exc.code.startswith('HTTP_4') and exc.code!='HTTP_408':progress.pop('index_pending',None);save()
        raise
    progress['index_done']=True;progress.pop('index_pending',None);save()


def storage_objects(bundle):
    """Reconstitute exactly the persisted ASCII bytes; reject damaged bundles."""
    if not bundle:return []
    manifest=bundle['manifest'];text=encoded(manifest).encode('ascii')
    sha=hashlib.sha256(text).hexdigest()
    prefix='spi/v1/'+manifest['event_key']+'/'+sha+'/'
    objects=[(prefix+'manifest.json',text)]
    for row in manifest['artifacts']:
        parts=sorted((p for p in bundle['parts'] if p['id']==row['id']),key=lambda p:p['part'])
        raw=''.join(p['text'] for p in parts).encode('ascii')
        if len(raw)!=row['stored_chars'] or hashlib.sha256(raw).hexdigest()!=row['sha256']:
            raise Failure('STORAGE_INTEGRITY')
        objects.append((prefix+row['id']+'-'+row['name'],raw))
    return objects


def storage_markdown(objects):
    if not objects:return ''
    origin=os.environ.get('SPI_EVIDENCE_PUBLIC_ORIGIN','https://spi-evidence.socrat.alchemmist.xyz')
    if origin not in ('https://rustfs.socrat.alchemmist.xyz','https://spi-evidence.socrat.alchemmist.xyz'):
        raise Failure('STORAGE_PUBLIC_ORIGIN')
    return '\n\n## Reproducible evidence\nSanitized, bounded evidence; truncation and missing reads are recorded in the manifest. Not every byte was reviewed by the model. Sign-in required.\n'+ '\n'.join(
        '- ['+('MANIFEST' if i==0 else key.rsplit('/',1)[1])+']('+origin+'/'+key+')'
        for i,(key,raw) in enumerate(objects))
