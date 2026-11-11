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
    records=[];parts=[];remaining=MAX_BUNDLE;skipped=[]
    for n,item in enumerate(artifacts):
        safe=redact(item,known);content=encoded(safe.get('content'));original=len(content)
        take=min(MAX_ARTIFACT,remaining//max(1,len(artifacts)-n))
        if take<4:
            skipped.append({'source':str(safe.get('name','artifact'))[:100],'reason':'ARTIFACT_BUNDLE_BUDGET'})
            continue
        stored=content[:take]
        if len(stored)<original:
            # Never call a chopped JSON string a JSON snapshot. Retain a valid
            # envelope with original digest/size and explicitly bounded excerpt.
            base={'representation':'bounded_json_excerpt','truncated':True,
                  'sanitized_chars':original,'sanitized_sha256':hashlib.sha256(content.encode()).hexdigest(),
                  'limitation':'Full sanitized document exceeded the artifact/bundle budget. Excerpt is text, not a complete upstream response.'}
            lo,hi=0,len(stored)
            while lo<hi:
                mid=(lo+hi+1)//2
                if len(encoded({**base,'excerpt':content[:mid]}))<=take:lo=mid
                else:hi=mid-1
            stored=encoded({**base,'excerpt':content[:lo]})
            if len(stored)>take:stored='null' if take>=4 else ''
        remaining-=len(stored)
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
              'artifacts':records,'missing':redact(evidence.get('missing',[])+skipped,known),'links':redact(list(links),known),
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
    if bundle.get('binary_refs'):raise Failure('GRAPH_REQUIRES_S3')
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
        if row['id'] in bundle.get('binary_refs',{}):
            from graph_evidence import read_artifact
            try:raw=read_artifact(bundle['binary_refs'][row['id']])
            except (ValueError,OSError):raise Failure('STORAGE_INTEGRITY') from None
        else:
            parts=sorted((p for p in bundle['parts'] if p['id']==row['id']),key=lambda p:p['part'])
            raw=''.join(p['text'] for p in parts).encode('ascii')
        size=row.get('stored_bytes') if manifest.get('format')=='socrat-evidence-v3' else row['stored_chars']
        if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=row['sha256']:
            raise Failure('STORAGE_INTEGRITY')
        objects.append((prefix+row['id']+'-'+row['name'],raw))
    return objects


def storage_markdown(objects):
    if not objects:return ''
    origin=os.environ.get('SPI_EVIDENCE_PUBLIC_ORIGIN','https://spi-evidence.socrat.alchemmist.xyz')
    if origin not in ('https://rustfs.socrat.alchemmist.xyz','https://spi-evidence.socrat.alchemmist.xyz'):
        raise Failure('STORAGE_PUBLIC_ORIGIN')
    text= '\n\n## Reproducible evidence\nSanitized, bounded evidence; truncation and missing reads are recorded in the manifest. Not every byte was reviewed by the model. Sign-in required.\n'+ '\n'.join(
        '- ['+('MANIFEST' if i==0 else key.rsplit('/',1)[1])+']('+origin+'/'+key+')'
        for i,(key,raw) in enumerate(objects))

    try:manifest=json.loads(objects[0][1].decode('ascii'))
    except (ValueError,UnicodeError):return text
    text+=navigation_markdown(manifest)
    keys={key.rsplit('/',1)[1]:key for key,raw in objects}
    graphs=[];graph_link_budget=12000
    for row in manifest.get('artifacts',[]):
        if row.get('mime_type')!='image/png' or row.get('source',{}).get('kind')!='grafana_panel_png':continue
        source=row['source'];name=row['id']+'-'+row['name']
        if name not in keys:raise Failure('STORAGE_INTEGRITY')
        title=' '.join(str(source.get('title',row['name'])).split())[:160]
        title=title.replace('\\','\\\\').replace('[','\\[').replace(']','\\]').replace('<','&lt;').replace('>','&gt;')
        from datetime import datetime,timezone
        try:
            start=datetime.fromtimestamp(source['from_ms']/1000,timezone.utc).isoformat()
            end=datetime.fromtimestamp(source['to_ms']/1000,timezone.utc).isoformat()
        except (KeyError,TypeError,ValueError,OverflowError):raise Failure('STORAGE_INTEGRITY') from None
        dashboard=grafana_panel_url(source)
        link=' [Open Grafana panel]('+dashboard+')' if len(dashboard)<=graph_link_budget else ' Panel URL omitted by publication budget; see manifest.'
        if len(dashboard)<=graph_link_budget:graph_link_budget-=len(dashboard)
        graphs.append('- ['+title+' (PNG)]('+origin+'/'+keys[name]+') — '+start+' to '+end+'.'+link)
    if graphs:text+='\n\n## Graphs\nPrivate PNG evidence; sign in through the existing Evidence/Authentik flow. Images are for human review, not model input.\n'+'\n'.join(graphs)
    return text


def navigation_markdown(manifest):
    """Human navigation is deterministic metadata, never model-generated prose."""
    from urllib.parse import urlsplit
    lines=['','## Grafana and Loki navigation'];budget=12000;seen=set();omitted=0
    for link in manifest.get('links',[])[:18]:
        url=link.get('url','')
        if not isinstance(url,str)or any(ord(c)<33 or c in '<>()[]\\'for c in url):
            omitted+=1;continue
        try:p=urlsplit(url)
        except ValueError:omitted+=1;continue
        if p.scheme!='https' or p.netloc!='grafana.socrat.alchemmist.xyz' or (p.path!='/explore'and re.fullmatch(r'/d/[A-Za-z0-9_-]{1,64}/_',p.path)is None) or p.fragment:
            omitted+=1;continue
        if url in seen:continue
        if len(url)>budget:omitted+=1;continue
        seen.add(url);budget-=len(url)
        title=re.sub(r'[\\`\[\]<>\r\n]',' ',str(link.get('title','Grafana Explore')))[:160]
        lines.append('- ['+title+']('+url+')')
    if omitted:lines.append('- '+str(omitted)+' navigation links omitted by validation/publication budget; see manifest.')
    if not seen:lines.append('- No validated Explore link was collected; this is not evidence of health.')
    gaps=manifest.get('missing',[])
    if gaps:
        lines+=['','## Evidence gaps','Missing/failed/empty reads are not proof of recovery. See the manifest for query/window details.']
        for item in gaps[:24]:
            source=re.sub(r'[^A-Za-z0-9_.-]','_',str(item.get('source','unknown')))[:80]
            reason=re.sub(r'[^A-Za-z0-9_.-]','_',str(item.get('reason','unknown')))[:100]
            lines.append('- `'+source+'`: `'+reason+'`')
        if len(gaps)>24:lines.append('- Additional gaps are listed in the manifest.')
    return repair_markdown_links('\n'.join(lines))


def grafana_panel_url(source):
    from urllib.parse import urlencode,quote
    uid=source.get('dashboard_uid');panel=source.get('panel_id')
    if not isinstance(uid,str)or re.fullmatch(r'[A-Za-z0-9_-]{1,64}',uid)is None or type(panel)is not int or panel<1:
        raise Failure('STORAGE_INTEGRITY')
    lo,hi=source.get('from_ms'),source.get('to_ms')
    if type(lo)is not int or type(hi)is not int or not 0<=lo<hi:raise Failure('STORAGE_INTEGRITY')
    params=[('orgId','1'),('from',str(lo)),('to',str(hi)),('viewPanel',str(panel)),('timezone','utc'),('forceLogin','true')]
    for k,v in sorted(source.get('variables',{}).items()):
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',k)is None or SECRET_KEY.search(k):raise Failure('STORAGE_INTEGRITY')
        for value in(v if isinstance(v,list)else[v]):params.append(('var-'+k,str(value)))
    return 'https://grafana.socrat.alchemmist.xyz/d/'+quote(uid,safe='')+'/_?'+urlencode(params)
