"""Independent durable-link retry loop; only the native internal bot API is called."""
import json,threading,time,urllib.request,urllib.error
from telegram_links import Links

BASE='http://socrat-bot.socrat-bot.svc:8080'
class Delivery:
    def __init__(self,store,token,enabled=False):
        if enabled and not token:raise ValueError("BOT_LINK_TOKEN_REQUIRED")
        self.store,self.token,self.enabled=store,token,enabled
        self.github=None
        self.links=Links(store,self.request,guarded_protocol=enabled)
        self.wake=threading.Event();self.discovery_wake=threading.Event();self.stopped=threading.Event()
        with store.db()as c:
            c.execute("CREATE TABLE IF NOT EXISTS spi_card_discovery(episode TEXT PRIMARY KEY,available REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,error TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS spi_primary_backfill(job_id TEXT PRIMARY KEY,available REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,error TEXT)")
    def request(self,method,path,json=None):
        if not path.startswith('/v1/cards') or '..' in path or '://' in path:raise ValueError('INVALID_BOT_PATH')
        import json as codec
        body=codec.dumps(json).encode()if json is not None else None
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*a,**kw):return None
        req=urllib.request.Request(BASE+path,data=body,method=method,headers={'X-Internal-Token':self.token,'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=30)as r:
                raw=r.read(2_000_001)
                if len(raw)>2_000_000:raise ValueError('BOT_RESPONSE_LIMIT')
                return r.status,codec.loads(raw)
        except urllib.error.HTTPError as e:return e.code,{}
    def signal(self):self.wake.set();self.discovery_wake.set()
    def receipt(self,event,receipt):
        if (receipt.get('episode')!=event['episode'] or receipt.get('immutable_key') is not True
            or not str(receipt.get('key','')).endswith('/spi/'+event['episode'])):return False
        self.links.record_card_receipt(event['episode'],receipt);self.signal();return True
    def discover(self):
        # Separate loop, per-episode persisted backoff, at most one request/tick.
        with self.store.db()as c:
            row=c.execute("SELECT l.episode,coalesce(d.attempts,0) attempts FROM (SELECT episode FROM spi_links UNION SELECT episode FROM jobs WHERE state='complete' AND issue>0) l LEFT JOIN spi_card_receipts r ON r.episode=l.episode LEFT JOIN spi_card_discovery d ON d.episode=l.episode WHERE r.episode IS NULL AND coalesce(d.available,0)<=? ORDER BY coalesce(d.available,0),l.episode LIMIT 1",(time.time(),)).fetchone()
        if row is None:return False
        episode=row['episode'];found=False
        try:
            status,result=self.request('GET','/v1/cards?status=all&episode='+episode)
            cards=result.get('cards',[])if status==200 else []
            if len(cards)==1:
                card=cards[0];copies=[{'chat_id':x['chat_id'],'message_id':x['message_id']}for x in card.get('copies',[])if type(x.get('message_id'))is int and x['message_id']>0]
                if copies:found=self.receipt({'episode':episode},{**card,'copies':copies})
        except Exception:pass  # Never block unrelated eligible link deliveries.
        with self.store.db()as c:
            c.execute("INSERT INTO spi_card_discovery VALUES(?,?,?,?) ON CONFLICT(episode) DO UPDATE SET available=excluded.available,attempts=excluded.attempts,error=excluded.error",(episode,time.time()+min(3600,30*2**min(row['attempts'],7)),row['attempts']+1,None if found else 'CARD_DISCOVERY_PENDING'))
        return True
    def backfill(self):
        # Repeated real alerts may reference pre-migration completed jobs. Recover
        # ONLY their trusted primary permalink; never rerun model/publication.
        if self.github is None:return False
        with self.store.db()as c:
            row=c.execute("SELECT j.*,coalesce(b.attempts,0) backfill_attempts FROM jobs j JOIN spi_card_receipts r ON r.episode=j.episode LEFT JOIN spi_links l ON l.episode=j.episode LEFT JOIN spi_primary_backfill b ON b.job_id=j.id WHERE j.state='complete' AND j.issue>0 AND coalesce(l.revision,0)<CASE json_extract(j.event,'$.status') WHEN 'resolved' THEN 2 ELSE 1 END AND coalesce(b.available,0)<=? ORDER BY coalesce(b.available,0),j.created DESC LIMIT 1",(time.time(),)).fetchone()
        if row is None:return False
        found=False
        try:
            publication=self.github.primary_reference('<!-- socrat-spi:'+row['id']+' -->',row['issue'])
            event=json.loads(row['event']);result=json.loads(row['result']);result['primary_publication']=publication
            def queue(c):self.links.record_primary_publication(row['id'],row['episode'],2 if event['status']=='resolved'else 1,publication,connection=c)
            self.store.save(row['id'],row['stage'],result,state='complete',on_save=queue)
            found=True;self.signal()
        except Exception:pass
        with self.store.db()as c:c.execute("INSERT INTO spi_primary_backfill VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET available=excluded.available,attempts=excluded.attempts,error=excluded.error",(row['id'],time.time()+min(3600,30*2**min(row['backfill_attempts'],7)),row['backfill_attempts']+1,None if found else 'TARGET_RECOVERY_PENDING'))
        return True
    def _discover_run(self):
        while not self.stopped.is_set():
            self.discovery_wake.clear()
            try:
                worked=self.backfill()
                worked=self.discover() or worked
            except Exception:worked=False
            self.discovery_wake.wait(1 if worked else 30)
    def _run(self):
        while not self.stopped.is_set():
            self.wake.clear()
            try:
                with self.store.db()as c:c.execute("UPDATE spi_links SET state='pending' WHERE state='applied' AND available<=?",(time.time(),))
                if self.links.once():continue
                with self.store.db()as c:
                    due=c.execute("SELECT min(max(available,lease)) FROM spi_links WHERE state!='applied'").fetchone()[0]
                timeout=60 if due is None else min(60,max(.1,due-time.time()))
            except Exception:
                print(json.dumps({'component':'spi-triage','event':'telegram_link_retry_pending'}),flush=True);timeout=30
            self.wake.wait(timeout)
    def start(self):
        if self.enabled:
            threading.Thread(target=self._run,name='spi-telegram-links',daemon=True).start()
            threading.Thread(target=self._discover_run,name='spi-telegram-discovery',daemon=True).start()
