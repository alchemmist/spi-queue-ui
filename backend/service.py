"""Authenticated private stage API. Durable state is independent of n8n executions."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from storage_runtime import load_storage
from dispatcher import Dispatcher
from core import Failure, OWNER_CONTEXT, PATIENT_TRANSIENT, Store, digest, normalize, render
from integrations import Evidence, GitHub, HTTP, Model, REQUEST_DEADLINE
from evidence_bundle import index_markdown, publish_parts, storage_objects, storage_markdown
from s3_evidence import StorageTransient, StoragePermanent, StorageConflict
from grafana_links import repair_markdown_links
from telegram_delivery import Delivery
from telegram_links import primary_url
from card_correlation import record_receipt


def log_event(event, level='info', **fields):
    # Only operational counters and bounded machine codes, never evidence or IDs.
    allowed = {'stage', 'code', 'phase', 'recovered_jobs', 'oldest_pending_seconds', 'dead_jobs', 'telegram_links_pending'}
    record = {k: v for k, v in fields.items() if k in allowed}
    for key in ('stage', 'phase', 'code'):
        if key in record:
            value = record[key]
            valid = (isinstance(value, str) and bool(re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', value))) if key == 'code' else value in {'match', 'collect', 'analyze', 'publish', 'sign', 'installation', 'mint', 'publish_secret', 'bootstrap'}
            if not valid:
                record[key] = 'REDACTED'
    record.update(ts=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                  level=level, service='spi-triage', component='spi-triage', event=event, msg=event)
    print(json.dumps(record, separators=(',', ':')), flush=True)

class Engine:
    def __init__(self,store,github,model,evidence,write=False,delivery=None,storage=None,storage_backend='legacy'):
        self.store=store;self.github=github;self.model=model;self.evidence=evidence;self.write=write
        if storage_backend not in {'legacy','s3'}:raise ValueError('Invalid storage backend')
        self.storage=storage;self.storage_backend=storage_backend
        self.lock=threading.Lock();self.delivery=delivery
    def claim(self, data=None):
        if data is not None:
            return self.store.owners.claim(data.get('execution_id'),data.get('workflow_revision'),time.time())
        # A socket call may outlive the durable lease. Never reclaim while this
        # process still owns a stage, including its final save/failure handling.
        if not self.lock.acquire(blocking=False):return {'job_id':''}
        try:return self.store.claim()
        finally:self.lock.release()
    def run(self,name,key):
        with self.lock:
            with self.store.db() as c:
                if c.execute("SELECT 1 FROM workflow_wait_owner WHERE job_id=? AND phase IN ('ready','waiting','running','complete')",(key,)).fetchone():
                    raise Failure('WAIT_OWNER_CONFLICT')
            REQUEST_DEADLINE.at=time.monotonic()+1800
            try:return self._run(name,key)
            except Failure as e:
                if not (self.store.get(key)['state']=='dead' and self.store.get(key)['error']=='WRITE_NEEDS_REVIEW'):
                    self.store.fail(key,e.code,retry_after=e.retry_after,rate_metadata=e.rate_metadata)
                current=self.store.get(key)
                patient=e.code in PATIENT_TRANSIENT or (e.code=='OPEN_SPI_CHANGED' and current['stage']==0 and not current['intent'])
                log_event('stage_deferred' if patient else 'stage_failed', 'warning' if patient else 'error', stage=name, code=e.code)
                raise
            except Exception:
                self.store.fail(key,'INTERNAL_ERROR')
                log_event('stage_failed', 'error', code='INTERNAL_ERROR')
                raise Failure('INTERNAL_ERROR') from None
            finally:
                del REQUEST_DEADLINE.at
    def run_v1(self, name, data):
        # Admission and typed GitHub read timeouts use durable HTTP deferral.
        # Legacy run() retains its 422 semantics; unsafe writes stay fatal.
        if not self.lock.acquire(blocking=False):
            result=self.store.owners.capacity_defer(name,data,time.time())
            if result['status']=='deferred':log_event('stage_deferred','warning',stage=name,code=result['code'])
            return result
        try:
            priority=self.store.owners.capacity_defer(name,data,time.time(),priority_only=True)
            if priority is not None:
                if priority['status']=='deferred':log_event('stage_deferred','warning',stage=name,code=priority['code'])
                return priority
            cached,owner=self.store.owners.begin(name,data,time.time())
            if cached is not None:return cached
            OWNER_CONTEXT.binding=(owner['job_id'],owner['fence'])
            REQUEST_DEADLINE.at=time.monotonic()+min(1800,max(0,owner['deadline']-time.time()))
            try:
                self._run(name,owner['job_id'])
                return self.store.owners.finish(owner,False,time.time())
            except Failure as exc:
                if exc.code=='OPEN_SPI_CHANGED':
                    # Store reset and owner/fence/receipt changed atomically.
                    # This is a normal bounded stage rewind, not a patient error
                    # or a new execution/claim; real/ambiguous failures stay fatal.
                    restarted=self.store.owners.snapshot_response(owner,time.time())
                    if restarted is not None:
                        log_event('snapshot_revalidated',stage='match',code=exc.code)
                        return restarted
                if exc.code in {'LLM_ADMISSION_DEFERRED','GITHUB_READ_TIMEOUT'}:
                    try:
                        result=self.store.owners.finish(owner,True,time.time(),exc.retry_after,code=exc.code)
                        log_event('stage_deferred','warning',stage=name,code=exc.code)
                        return result
                    except Failure as terminal:
                        exc=terminal
                self.store.fail(owner['job_id'],exc.code,retry_after=exc.retry_after,rate_metadata=exc.rate_metadata,on_fail=self.store.owners.failure_callback(owner))
                log_event('stage_failed','error',stage=name,code=exc.code)
                raise exc
            except Exception:
                self.store.fail(owner['job_id'],'INTERNAL_ERROR',on_fail=self.store.owners.failure_callback(owner))
                raise Failure('INTERNAL_ERROR') from None
            finally:
                del REQUEST_DEADLINE.at
                del OWNER_CONTEXT.binding
        finally:
            self.lock.release()

    def _run(self,name,key):
        stages={'match':1,'collect':2,'analyze':3,'publish':4}
        if name not in stages:raise Failure('INVALID_STAGE')
        job=self.store.get(key);wanted=stages[name]
        if job['state']=='dead' and job['error']=='WRITE_NEEDS_REVIEW':raise Failure('WRITE_NEEDS_REVIEW')
        if job['stage']>=wanted:return {'job_id':key,'status':job['state'],'stage':job['stage']}
        if job['stage']!=wanted-1 or job['state']!='processing':raise Failure('INVALID_STAGE_ORDER')
        r=job['result'];event=job['event'];resolved=event['status']=='resolved'
        if not job['intent']:self.store.snapshot_guard(key)
        if name=='match':
            if resolved:
                r['resolved_issue']=self.store.episode_issue(event['episode'])
                if self.delivery and not r['resolved_issue']:r['analyze_recovery']=True
            if not resolved or r.get('analyze_recovery'):
                issues=self.github.snapshot();r['open_signature']=digest([(x['number'],x['updated_at']) for x in issues])
                def checkpoint(progress):
                    r['match_progress_v3']=progress
                    def retain(c):
                        c.execute('INSERT OR REPLACE INTO match_review_v3 VALUES(?,?)',(key,json.dumps(progress)))
                    self.store.save(key,0,r,on_save=retain)
                r['match']=self.model.match(event,issues,progress=self.store.match_checkpoint(key) or r.get('match_progress_v3'),on_progress=checkpoint)
        elif name=='collect':
            if not resolved or r.get('resolved_issue') or r.get('analyze_recovery'):r['evidence']=self.evidence.collect(event)
        elif name=='analyze':
            if not resolved or r.get('analyze_recovery'):
                if not job['intent'] and self.github.open_signature()!=r['open_signature']:
                    self.invalidate_snapshot(key,r,job)
                r['report']=self.model.analyze(event,r['match'],r['evidence']);render(r['report'],event)
        elif name=='publish':
            if not self.write:
                self.store.save(key,4,r,state='dry_run');return {'job_id':key,'status':'dry_run','issue_number':0}
            if 'publication_backend' not in r:
                r['publication_backend']='legacy' if job['intent'] or 'artifact_publication' in r else self.storage_backend
                self.store.save(key,3,r,intent=job['intent'])
            if r['publication_backend'] not in {'legacy','s3'}:raise Failure('STORAGE_INTEGRITY')
            marker='<!-- socrat-spi:'+key+' -->'
            # Reconcile BEFORE checking open candidates: the previous write may itself
            # have changed that snapshot, or a human may have closed the created issue.
            if job['intent']:
                if r['publication_backend']=='s3':self.upload_evidence(key,r,intent=job['intent'])
                intent=json.loads(job['intent'])
                number=intent['number'] if intent.get('primary_done') else self.github.reconcile(marker,intent['number'])
                if not number:raise Failure('WRITE_NEEDS_REVIEW')
                if self.delivery and not r.get('primary_publication'):
                    self.save_primary(key,r,event,self.github.primary_reference(marker,number))
                self.finish_evidence(key,r,number)
                return {'job_id':key,'status':'reconciled','issue_number':number}
            # Without a trusted primary intent the artifact target cannot be
            # inferred from a fresh match. Retain all legacy receipts for review.
            if 'artifact_publication' in r:
                raise Failure('WRITE_NEEDS_REVIEW')
            if resolved and not r.get('analyze_recovery'):
                number=r['resolved_issue']
                existing=self.github.request('GET','/issues/'+str(number)) if number else None
                if not number or existing.get('state')!='open':
                    self.store.save(key,4,r,state='complete',issue=number or 0);return {'job_id':key,'status':'resolved_without_open_spi'}
                title='';body='## Incident resolved\n\nObserved recovery at '+(event['endsAt'] or 'an unspecified time')+'.\n\nResolution does not establish root-cause remediation; this issue remains open.\n\n'+marker
            else:
                issues=self.github.snapshot()
                if digest([(x['number'],x['updated_at']) for x in issues])!=r['open_signature']:
                    self.invalidate_snapshot(key,r,job)
                title,body=render(r['report'],event);number=r['report']['issue_number']
                if number not in {0,*[x['number']for x in r['match']['candidates']]}:raise Failure('INVALID_MATCH_TARGET')
            if r['publication_backend']=='s3':
                objects=self.upload_evidence(key,r)
                if not resolved or r.get('analyze_recovery'):
                    issues=self.github.snapshot()
                    if digest([(x['number'],x['updated_at']) for x in issues])!=r['open_signature']:
                        self.invalidate_snapshot(key,r,job)
                if resolved and not r.get('analyze_recovery'):
                    existing=self.github.request('GET','/issues/'+str(number))
                    if existing.get('state')!='open':
                        self.store.save(key,4,r,state='complete',issue=number)
                        return {'job_id':key,'status':'resolved_without_open_spi'}
                body=repair_markdown_links(body+storage_markdown(objects))
            else:body=repair_markdown_links(body+index_markdown(r.get('evidence',{}).get('bundle')))
            self.store.save(key,3,r,intent=json.dumps({'number':number}))
            try:
                if number:
                    posted=self.github.request('POST','/issues/'+str(number)+'/comments',{'body':body})
                    kind='comment'
                else:
                    posted=self.github.request('POST','/issues',{'title':title,'body':body});number=posted['number'];kind='issue'
            except Failure as e:
                # 4xx are definitive rejections. Network errors/5xx can hide a
                # successful write: keep intent and never blindly repeat the POST.
                if e.code.startswith('HTTP_4') and e.code!='HTTP_408':
                    self.store.save(key,3,r,intent=None)
                raise
            if self.delivery:
                publication={'kind':kind,'issue_number':number,'id':posted.get('id'),'html_url':posted.get('html_url'),'role':'primary'}
                self.save_primary(key,r,event,publication)
            self.finish_evidence(key,r,number)
            log_event('publication_ok')
            return {'job_id':key,'status':'published','issue_number':number}
        self.store.save(key,wanted,r)
        return {'job_id':key,'status':'processing','stage':wanted}


    def invalidate_snapshot(self,key,result,job):
        if job['intent'] or 'artifact_publication' in result:
            raise Failure('WRITE_NEEDS_REVIEW')
        retained=dict(result)
        # Uploaded immutable objects keep their original evidence binding forever.
        if 'storage_publication' in retained:
            audit=list(retained.get('snapshot_audit',[]))
            audit.append({k:retained.get(k) for k in ('evidence','storage_publication','open_signature')})
            retained['snapshot_audit']=audit
            del retained['storage_publication']
        for field in ('match','report','evidence','open_signature'):
            retained.pop(field,None)
        self.store.replace_snapshot(key,self.store.get(key),retained,'report' in result or 'storage_publication' in result)
        raise Failure('OPEN_SPI_CHANGED',retry_after=15)

    def upload_evidence(self,key,result,intent=None):
        objects=storage_objects(result.get('evidence',{}).get('bundle'))
        binding=[{'bucket':'spi-evidence','key':k,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'verified':True} for k,raw in objects]
        progress=result.setdefault('storage_publication',{'binding':binding,'verified':[]})
        if progress.get('binding')!=binding or progress.get('verified')!=binding[:len(progress.get('verified',[]))]:
            raise Failure('STORAGE_INTEGRITY')
        def save():
            fields={} if intent is None else {'intent':intent}
            self.store.save(key,3,result,**fields)
        save()
        for i,(object_key,raw) in enumerate(objects):
            if i<len(progress['verified']):continue
            if self.storage is None:raise Failure('STORAGE_CONFIG')
            try:verified=self.storage.ensure_object(object_key,raw)
            except StorageTransient as exc:raise Failure('STORAGE_TRANSIENT',exc.retry_after) from None
            except StorageConflict:raise Failure('STORAGE_CONFLICT') from None
            except StoragePermanent:raise Failure('STORAGE_PERMANENT') from None
            if verified!=binding[i]:raise Failure('STORAGE_INTEGRITY')
            progress['verified'].append(verified);save()
        return objects

    def save_primary(self,key,result,event,publication):
        try:primary_url(publication)
        except ValueError:raise Failure('INVALID_PRIMARY_REFERENCE')from None
        result['primary_publication']=publication
        def queue(c):self.delivery.links.record_primary_publication(key,event['episode'],2 if event['status']=='resolved'else 1,publication,connection=c)
        self.store.save(key,3,result,intent=json.dumps({'number':publication['issue_number'],'primary_done':True}),on_save=queue)
        self.delivery.signal()

    def finish_evidence(self,key,result,number):
        intent=json.dumps({'number':number,'primary_done':True})
        progress=result.setdefault('artifact_publication',{})
        def save():self.store.save(key,3,result,intent=intent)
        save()
        if result.get('publication_backend','legacy')=='legacy':
            publish_parts(self.github,number,result.get('evidence',{}).get('bundle'),progress,save)
        else:self.upload_evidence(key,result,intent=intent)
        self.store.save(key,4,result,state='complete',issue=number,intent=None)


def read_request_body(handler, timeout=10):
    """Bound partial/trickling bodies by a total elapsed-time deadline."""
    try:length=int(handler.headers.get('Content-Length','0'))
    except (ValueError,TypeError):raise Failure('INVALID_BODY_SIZE') from None
    if not 0<length<=512_000:raise Failure('INVALID_BODY_SIZE')
    deadline=time.monotonic()+timeout
    previous=handler.connection.gettimeout()
    chunks=[]
    try:
        while length:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            handler.connection.settimeout(remaining)
            chunk=handler.rfile.read1(length)
            if not chunk:raise Failure('INCOMPLETE_BODY')
            chunks.append(chunk);length-=len(chunk)
        data=json.loads(b''.join(chunks))
        if not isinstance(data,dict):raise Failure('INVALID_REQUEST')
        return data
    except TimeoutError:raise Failure('REQUEST_BODY_TIMEOUT') from None
    finally:handler.connection.settimeout(previous)


def serve():
    os.umask(0o077)
    # Fail closed before store recovery, claims or dispatch.
    storage,storage_backend=load_storage()
    model=Model(HTTP(),os.environ['LITELLM_KEY'])
    http=HTTP();store=Store(os.environ.get('SPI_DATABASE','/data/spi.sqlite'),minute_token_limit=int(os.environ.get('SPI_LLM_MINUTE_TOKENS','30000')))
    model.store=store
    queue_admin = None
    queue_token = ''
    if os.environ.get('SPI_QUEUE_UI_ENABLED') == 'true':
        from pathlib import Path
        queue_token = Path('/queue-control/token').read_text().strip()
        if len(queue_token) < 32:raise RuntimeError('QUEUE_ADMIN_TOKEN_REQUIRED')
        from queue_admin import QueueAdmin, authorized as queue_authorized
        queue_admin = QueueAdmin(store)
    recovered=store.recover_on_start()
    log_event('worker_started', recovered_jobs=recovered)
    delivery=Delivery(store,os.environ.get('BOT_INTERNAL_TOKEN',''),enabled=os.environ.get('SPI_TELEGRAM_LINKS_ENABLED','false')=='true')
    engine=Engine(store,GitHub(http,os.environ.get('GITHUB_TOKEN_FILE','/github/token')),
                  model,Evidence(http,os.environ['GRAFANA_TOKEN'],os.environ['GRAFANA_URL'],github=GitHub(http,os.environ.get('GITHUB_TOKEN_FILE','/github/token'))),
                  write=os.environ.get('WRITE_ENABLED')=='true',delivery=delivery,
                  storage=storage,storage_backend=storage_backend)
    delivery.github=engine.github
    dispatcher=Dispatcher(store,os.environ['WORKER_TOKEN'],enabled=os.environ.get('SPI_DISPATCH_ENABLED','false')=='true',busy=engine.lock.locked)
    dispatcher.start();delivery.start()
    def heartbeat():
        while True:
            try:
                h=store.health()
                with store.db()as c:pending=c.execute("SELECT count(*)FROM spi_links WHERE state!='applied'").fetchone()[0]
                log_event('queue_health',oldest_pending_seconds=h['oldest_pending_seconds'],dead_jobs=h['counts'].get('dead',0),telegram_links_pending=pending)
            except Exception:log_event('health_read_failed','error')
            time.sleep(60)
    threading.Thread(target=heartbeat,name='spi-health',daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,body,content_type='application/json'):
            raw=body.encode() if isinstance(body,str) else json.dumps(body).encode()
            self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(raw)));self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_GET(self):
            if self.path=='/healthz':return self.send(200,{'status':'ok'})
            if self.path=='/metrics':
                h=store.health();lines=['# TYPE socrat_spi_jobs gauge']
                for state in ['queued','retry','processing','complete','dry_run','dead']:
                    lines.append('socrat_spi_jobs{state="'+state+'"} '+str(h['counts'].get(state,0)))
                lines+=['# TYPE socrat_spi_oldest_pending_seconds gauge','socrat_spi_oldest_pending_seconds '+str(h['oldest_pending_seconds'])]
                with store.db()as c:
                    pending=c.execute("SELECT count(*)FROM spi_links WHERE state!='applied'").fetchone()[0]
                lines+=['socrat_spi_telegram_links_pending '+str(pending),'socrat_spi_telegram_links_enabled '+str(int(delivery.enabled))]
                d=store.dispatch_status()
                lines+=['socrat_spi_dispatch_enabled '+str(int(dispatcher.enabled)), 'socrat_spi_dispatch_attempts '+str(d['attempts'])]
                return self.send(200,'\n'.join(lines)+'\n','text/plain; version=0.0.4')
            return self.send(404,{'code':'NOT_FOUND'})
        def do_POST(self):
            if self.path in ('/operator/queue/list', '/operator/queue/cancel'):
                if queue_admin is None or not queue_authorized(self.headers, queue_token):
                    return self.send(403, {'code':'FORBIDDEN'})
                try:
                    data = read_request_body(self)
                    if self.path.endswith('/list'):
                        return self.send(200, queue_admin.list(data))
                    result = queue_admin.cancel(data)
                    dispatcher.signal()
                    return self.send(200, result)
                except Failure as e:
                    return self.send(409 if e.code in ('QUEUE_ITEM_CHANGED','QUEUE_ITEM_NOT_CANCELLABLE') else 400, {'code':e.code})
                except (ValueError, TypeError, AttributeError):
                    return self.send(400, {'code':'INVALID_REQUEST'})
                except Exception:
                    return self.send(500, {'code':'INTERNAL_ERROR'})
            if not hmac.compare_digest(self.headers.get('X-Worker-Token',''),os.environ['WORKER_TOKEN']):return self.send(403,{'code':'FORBIDDEN'})
            try:
                data=read_request_body(self)
                contract=self.headers.get('X-SPI-Workflow-Contract')
                if contract not in (None,'1'):raise Failure('INVALID_WORKFLOW_CONTRACT')
                opted=contract=='1'
                if opted and os.environ.get('SPI_DURABLE_WAIT_ENABLED','false')!='true':raise Failure('WORKFLOW_CONTRACT_DISABLED')
                if self.path=='/enqueue':result=store.enqueue(normalize(data))
                elif self.path=='/enqueue-group':
                    alerts=data.get('alerts')
                    if not isinstance(alerts,list) or not 0<len(alerts)<=200:raise Failure('INVALID_ALERT_GROUP')
                    events=[normalize({'source':data.get('externalURL','socrat-alerts'),'alert':a})for a in alerts]
                    result={'accepted':len(store.enqueue_many(events)),'events':[{'event_key':e['event_key'],'episode':e['episode']}for e in events]}
                elif self.path=='/telegram-receipt':
                    result=record_receipt(store,data)
                    receipt=data.get('receipt')
                    if isinstance(receipt,dict) and not receipt.get('error'):
                        delivery.receipt({'episode':result['episode']},receipt)
                elif self.path=='/claim':
                    result=engine.claim(data if opted else None);h=store.health()
                    log_event('queue_health', oldest_pending_seconds=h['oldest_pending_seconds'], dead_jobs=h['counts'].get('dead',0))
                elif self.path in ['/match','/collect','/analyze','/publish']:
                    key=data.get('job_id','')
                    if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef'for c in key):raise Failure('INVALID_JOB_ID')
                    result=engine.run_v1(self.path[1:],data) if opted else engine.run(self.path[1:],key)
                else:return self.send(404,{'code':'NOT_FOUND'})
                return self.send(202 if opted and result.get('status')=='deferred' else 200,result)
            except Failure as e:
                self.close_connection=True
                return self.send(422,{'code':e.code})
            except (ValueError,TypeError,AttributeError):return self.send(400,{'code':'INVALID_REQUEST'})
            except Exception:return self.send(500,{'code':'INTERNAL_ERROR'})
            finally:dispatcher.signal()
    ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()

if __name__=='__main__':serve()
