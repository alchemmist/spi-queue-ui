"""Durable opt-in v1 stage admission and execution ownership.

Legacy routes never return patient success. Mutations use Store's immediate
transactions; expired running owners require review unless their stage checkpoint
already committed. Waiting owners have no in-flight side effect to replay.
"""
import math
import re
import secrets
from core import Failure

ABSOLUTE_SECONDS = 21600
INFLIGHT_SECONDS = 1980
WAIT_GRACE_SECONDS = 180
MIN_DURABLE_WAIT_SECONDS = 66
REVISION = 'durable-v1'
STAGES = ('match', 'collect', 'analyze', 'publish')


class WaitOwners:
    def __init__(self, store):
        self.store = store
        with store.db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS workflow_wait_owner (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                execution_id TEXT UNIQUE NOT NULL, revision TEXT NOT NULL,
                fence TEXT NOT NULL, stage TEXT NOT NULL,
                phase TEXT NOT NULL, due REAL NOT NULL, expires REAL NOT NULL,
                deadline REAL NOT NULL, request_fence TEXT, response TEXT, request_stage TEXT)''')

            c.execute('CREATE TABLE IF NOT EXISTS workflow_execution_tombstone (execution_id TEXT PRIMARY KEY)')
            c.execute('CREATE TABLE IF NOT EXISTS workflow_empty_claim (execution_id TEXT PRIMARY KEY,revision TEXT NOT NULL)')

    @staticmethod
    def _time(now):
        if type(now) not in (float, int) or not math.isfinite(now):
            raise Failure('INVALID_WAIT_TIME')

    def bind(self, job_id, execution_id, revision, stage, now):
        self._time(now)
        if (stage not in STAGES or not isinstance(execution_id, str)
                or not execution_id.isdecimal() or len(execution_id) > 32
                or not isinstance(revision, str) or not 1 <= len(revision) <= 128):
            raise Failure('INVALID_WAIT_OWNER')
        with self.store.db() as c:
            row = c.execute('SELECT * FROM workflow_wait_owner WHERE execution_id=? OR job_id=?',
                            (execution_id, job_id)).fetchone()
            if row:
                if (row['job_id'], row['execution_id'], row['revision']) != (job_id, execution_id, revision):
                    raise Failure('WAIT_OWNER_CONFLICT')
                if row['expires'] <= now:
                    raise Failure('WAIT_OWNER_EXPIRED')
                return dict(row)
            job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['state'] != 'processing' or job['stage'] != STAGES.index(stage):
                raise Failure('INVALID_STAGE_ORDER')
            c.execute('INSERT INTO workflow_wait_owner(job_id,execution_id,revision,fence,stage,phase,due,expires,deadline) VALUES(?,?,?,?,?,?,?,?,?)',
                      (job_id, execution_id, revision, secrets.token_hex(32), stage,
                       'running', now, now + INFLIGHT_SECONDS, now + ABSOLUTE_SECONDS))
            return dict(c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?', (job_id,)).fetchone())

    def _checked(self, c, owner, now):
        row = c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?', (owner.get('job_id'),)).fetchone()
        if not row or any(row[k] != owner.get(k) for k in ('execution_id', 'revision', 'fence', 'stage')):
            raise Failure('STALE_WAIT_OWNER')
        if now >= min(row['expires'], row['deadline']):
            raise Failure('WAIT_OWNER_EXPIRED')
        job = c.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
        if not job or job['stage'] != STAGES.index(row['stage']) or job['state'] != 'processing':
            raise Failure('INVALID_STAGE_ORDER')
        return row

    def defer(self, owner, code, retry_after, now):
        self._time(now)
        if code != 'LLM_ADMISSION_DEFERRED':
            raise Failure('UNSAFE_WAIT_ERROR')
        if type(retry_after) not in (int, float) or not math.isfinite(retry_after) or not 0 <= retry_after <= 86400:
            raise Failure('INVALID_WAIT_TIME')
        with self.store.db() as c:
            row = self._checked(c, owner, now)
            if row['phase'] == 'waiting':
                return dict(row)  # Lost HTTP response cannot extend the wait.
            due = now + max(MIN_DURABLE_WAIT_SECONDS, retry_after + 2)
            if due + WAIT_GRACE_SECONDS >= row['deadline']:
                raise Failure('WAIT_DEADLINE_EXCEEDED')
            c.execute("UPDATE workflow_wait_owner SET phase='waiting',due=?,expires=? WHERE job_id=?",
                      (due, due + WAIT_GRACE_SECONDS, row['job_id']))
            return dict(c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?', (row['job_id'],)).fetchone())

    def resume(self, owner, now):
        self._time(now)
        with self.store.db() as c:
            row = self._checked(c, owner, now)
            if row['phase'] != 'waiting':
                # A duplicated resume must not authorize another model request.
                raise Failure('WAIT_RESUME_ALREADY_CONSUMED')
            if now < row['due']:
                raise Failure('WAIT_NOT_DUE')
            # Rotate on consuming wake; late duplicates carry an obsolete fence.
            c.execute("UPDATE workflow_wait_owner SET phase='running',fence=?,expires=? WHERE job_id=?",
                      (secrets.token_hex(32), min(now + INFLIGHT_SECONDS, row['deadline']), row['job_id']))
            return dict(c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?', (row['job_id'],)).fetchone())

    @staticmethod
    def validate_identity(execution_id, revision):
        if (not isinstance(execution_id, str) or not re.fullmatch(r'[1-9][0-9]{0,31}',execution_id)
                or revision != REVISION):
            raise Failure('INVALID_WAIT_OWNER')

    def claim(self, execution_id, revision, now):
        self.validate_identity(execution_id, revision)
        with self.store.db() as c:
            self.store._reconcile_owners(c, now)
            empty=c.execute('SELECT revision FROM workflow_empty_claim WHERE execution_id=?',(execution_id,)).fetchone()
            if empty:
                if empty['revision']!=revision:raise Failure('STALE_WAIT_OWNER')
                return {'status':'empty','job_id':'','contract':1}
            if c.execute('SELECT 1 FROM workflow_execution_tombstone WHERE execution_id=?',(execution_id,)).fetchone():
                raise Failure('STALE_WAIT_OWNER')
            old = c.execute('SELECT * FROM workflow_wait_owner WHERE execution_id=?', (execution_id,)).fetchone()
            if old:
                if old['revision'] != revision or old['phase'] in ('revoked', 'failed'):
                    raise Failure('STALE_WAIT_OWNER')
                if old['phase']=='running':raise Failure('WAIT_REQUEST_IN_FLIGHT')
                if old['phase']=='complete':
                    import json
                    return json.loads(old['response'])
                return self.envelope(old,'deferred' if old['phase']=='waiting' else 'ready')
            # A fresh callback acknowledges its wake just like legacy claim().
            # Replayed execution/empty claims above must not clear a newer wake.
            c.execute('DELETE FROM dispatch WHERE singleton=1')
            if c.execute("SELECT 1 FROM jobs WHERE state='processing' AND lease>?", (now,)).fetchone():
                c.execute('INSERT INTO workflow_empty_claim VALUES(?,?)',(execution_id,revision))
                return {'status': 'empty', 'job_id': '', 'contract': 1}
            row, _ = self.store._schedule(c, now)
            if not row:
                c.execute('INSERT INTO workflow_empty_claim VALUES(?,?)',(execution_id,revision))
                return {'status': 'empty', 'job_id': '', 'contract': 1}
            c.execute("UPDATE jobs SET state='processing',lease=?,attempts=attempts+1,updated=? WHERE id=?",
                      (now+WAIT_GRACE_SECONDS, now, row['id']))
            c.execute("DELETE FROM workflow_wait_owner WHERE job_id=? AND phase IN ('failed','revoked')",(row['id'],))
            stage = STAGES[row['stage']]
            c.execute('INSERT INTO workflow_wait_owner(job_id,execution_id,revision,fence,stage,phase,due,expires,deadline) VALUES(?,?,?,?,?,?,?,?,?)',
                      (row['id'], execution_id, revision, secrets.token_hex(32), stage, 'ready', now,
                       now+WAIT_GRACE_SECONDS, now+ABSOLUTE_SECONDS))
            return self.envelope(c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?', (row['id'],)).fetchone())

    @staticmethod
    def envelope(row, status='ready'):
        return {'contract': 1, 'status': status, 'job_id': row['job_id'],
                'execution_id': row['execution_id'], 'workflow_revision': row['revision'],
                'fence': row['fence'], 'stage': row['stage'],
                'due_at': row['due'], 'deadline': row['deadline']}

    def begin(self, name, data, now):
        import json
        self.validate_identity(data.get('execution_id'), data.get('workflow_revision'))
        with self.store.db() as c:
            row = c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?', (data.get('job_id'),)).fetchone()
            if not row or any(row[k] != data.get(v) for k,v in
                    (('execution_id','execution_id'),('revision','workflow_revision'))):
                raise Failure('STALE_WAIT_OWNER')
            if now >= min(row['expires'],row['deadline']):raise Failure('WAIT_OWNER_EXPIRED')
            # Replay an already committed HTTP response, never another model call.
            if row['request_fence'] == data.get('fence') and row['response']:
                if row['request_stage']!=name or data.get('stage')!=name:raise Failure('STALE_WAIT_OWNER')
                return json.loads(row['response']), None
            if (row['fence'] != data.get('fence') or row['stage'] != name
                    or data.get('stage') != name or row['phase'] not in ('ready','waiting')):
                raise Failure('STALE_WAIT_OWNER')
            if now >= min(row['expires'],row['deadline']):
                raise Failure('WAIT_OWNER_EXPIRED')
            if row['phase']=='waiting' and now < row['due']:
                raise Failure('WAIT_NOT_DUE')
            job = c.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
            if not job or job['stage'] != STAGES.index(name) or job['state'] != 'processing':
                raise Failure('INVALID_STAGE_ORDER')
            c.execute("UPDATE workflow_wait_owner SET phase='running',request_fence=?,request_stage=?,response=NULL,expires=? WHERE job_id=?",
                      (row['fence'],name, min(now+INFLIGHT_SECONDS,row['deadline']),row['job_id']))
            c.execute('UPDATE jobs SET lease=? WHERE id=?', (min(now+INFLIGHT_SECONDS,row['deadline']),row['job_id']))
            return None, dict(row)

    def finish(self, owner, deferred, now, retry_after=None, code="LLM_ADMISSION_DEFERRED"):
        if deferred and code not in {"LLM_ADMISSION_DEFERRED", "GITHUB_READ_TIMEOUT", "GRAPH_CAPTURE_DEFERRED"}:
            raise Failure("UNSAFE_WAIT_ERROR")
        import json
        with self.store.db() as c:
            row = c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?', (owner['job_id'],)).fetchone()
            if not row or row['phase']!='running' or row['fence']!=owner['fence']:
                raise Failure('STALE_WAIT_OWNER')
            job = c.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
            if deferred:
                if job['stage'] != STAGES.index(row['stage']):
                    raise Failure('INVALID_STAGE_ORDER')
                delay = Failure(code, retry_after).retry_after
                due = now+max(MIN_DURABLE_WAIT_SECONDS,(delay or 61)+2)
                if due+WAIT_GRACE_SECONDS >= row['deadline']:
                    raise Failure('WAIT_DEADLINE_EXCEEDED')
                phase='waiting'; stage=row['stage']; expires=due+WAIT_GRACE_SECONDS
                c.execute("UPDATE jobs SET lease=0,available=?,updated=?,error=? WHERE id=?",(due,now,code,row['job_id']))
            else:
                if job['stage'] != STAGES.index(row['stage'])+1:
                    raise Failure('INVALID_STAGE_ORDER')
                phase='complete' if job['stage']==4 else 'ready'
                stage=STAGES[min(job['stage'],3)]; due=now
                expires=min(now+WAIT_GRACE_SECONDS,row['deadline'])
                if phase=='ready':c.execute('UPDATE jobs SET lease=?,updated=? WHERE id=?',(expires,now,row['job_id']))
            fence=secrets.token_hex(32)
            response=self.envelope(dict(row)|{'fence':fence,'stage':stage,'due':due},
                                   'deferred' if deferred else ('complete' if phase=='complete' else 'ready'))
            if deferred:response['code']=code
            if phase=='complete': response['outcome']=job['state']
            c.execute('UPDATE workflow_wait_owner SET phase=?,stage=?,fence=?,due=?,expires=?,response=? WHERE job_id=?',
                      (phase,stage,fence,due,expires,json.dumps(response),row['job_id']))
            return response

    def snapshot_replaced(self,c,key,now):
        """Commit a safe rewind receipt with Store.replace_snapshot's job reset.

        Legacy calls have no running v1 owner. Never mint a new claim, deadline,
        budget, or authority to replay an ambiguous publication.
        """
        import json
        from core import OWNER_CONTEXT
        row=c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?',(key,)).fetchone()
        if not row or row['phase'] not in ('ready','waiting','running'):return
        if (row['phase']!='running' or getattr(OWNER_CONTEXT,'binding',None)!=(key,row['fence'])
                or row['stage'] not in ('collect','analyze','publish')
                or row['request_stage']!=row['stage'] or row['request_fence']!=row['fence']):
            raise Failure('STALE_WAIT_OWNER')
        if now>=min(row['expires'],row['deadline']):raise Failure('WAIT_OWNER_EXPIRED')
        job=c.execute('SELECT stage,state,intent FROM jobs WHERE id=?',(key,)).fetchone()
        if job['stage']!=0 or job['state']!='processing' or job['intent']:
            raise Failure('INVALID_STAGE_ORDER')
        fence=secrets.token_hex(32);expires=min(now+WAIT_GRACE_SECONDS,row['deadline'])
        response=self.envelope(dict(row)|{'fence':fence,'stage':'match','due':now},'ready')
        c.execute("UPDATE workflow_wait_owner SET phase='ready',stage='match',fence=?,due=?,expires=?,response=? WHERE job_id=?",
                  (fence,now,expires,json.dumps(response),key))
        c.execute('UPDATE jobs SET lease=?,updated=? WHERE id=?',(expires,now,key))

    def snapshot_response(self,owner,now):
        """Only the exact committed old request may receive its rewind receipt."""
        import json
        with self.store.db() as c:
            row=c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?',(owner['job_id'],)).fetchone()
            if (not row or row['phase']!='ready' or row['stage']!='match'
                    or row['execution_id']!=owner['execution_id'] or row['revision']!=owner['revision']
                    or row['request_fence']!=owner['fence'] or row['request_stage']!=owner['stage']
                    or not row['response'] or now>=min(row['expires'],row['deadline'])):return None
            job=c.execute('SELECT stage,state,intent FROM jobs WHERE id=?',(owner['job_id'],)).fetchone()
            if job['stage']!=0 or job['state']!='processing' or job['intent']:return None
            response=json.loads(row['response'])
            if response.get('status')!='ready' or response.get('stage')!='match' or response.get('fence')!=row['fence']:
                raise Failure('STALE_WAIT_OWNER')
            return response

    def failure_callback(self,owner):
        def release(c):
            c.execute('INSERT OR IGNORE INTO workflow_execution_tombstone VALUES(?)',(owner['execution_id'],))
            c.execute("UPDATE workflow_wait_owner SET phase='failed',expires=0 WHERE job_id=? AND fence=?",(owner['job_id'],owner['fence']))
        return release

    def abandon(self, owner):
        # Keep execution tombstone so repeated claim cannot accidentally take a new job.
        with self.store.db() as c:
            c.execute('INSERT OR IGNORE INTO workflow_execution_tombstone VALUES(?)',(owner['execution_id'],))
            c.execute("UPDATE workflow_wait_owner SET phase='failed',expires=0 WHERE job_id=? AND fence=?",
                      (owner['job_id'],owner['fence']))

    def capacity_defer(self,name,data,now,priority_only=False):
        # Called only when Engine's nonblocking acquisition proved capacity busy.
        # No model/publication is started; renewal and replay receipt commit together.
        import json
        self.validate_identity(data.get('execution_id'),data.get('workflow_revision'))
        with self.store.db() as c:
            row=c.execute('SELECT * FROM workflow_wait_owner WHERE job_id=?',(data.get('job_id'),)).fetchone()
            if not row or row['execution_id']!=data['execution_id'] or row['revision']!=data['workflow_revision']:
                raise Failure('STALE_WAIT_OWNER')
            if now>=min(row['expires'],row['deadline']):raise Failure('WAIT_OWNER_EXPIRED')
            if row['request_fence']==data.get('fence') and row['response']:
                if row['request_stage']!=name or data.get('stage')!=name:raise Failure('STALE_WAIT_OWNER')
                return json.loads(row['response'])
            if (row['phase'] not in ('ready','waiting') or row['stage']!=name
                    or data.get('stage')!=name or row['fence']!=data.get('fence')):
                raise Failure('STALE_WAIT_OWNER')
            if row['phase']=='waiting' and now<row['due']:raise Failure('WAIT_NOT_DUE')
            job=c.execute('SELECT * FROM jobs WHERE id=?',(row['job_id'],)).fetchone()
            if not job or job['stage']!=STAGES.index(name) or job['state']!='processing':raise Failure('INVALID_STAGE_ORDER')
            self.store._reconcile_owners(c,now)
            if priority_only and not self.store._owned_priority_until(c,STAGES.index(name),now,row['job_id']):return None
            due=now+MIN_DURABLE_WAIT_SECONDS+1+secrets.randbelow(3)
            if due+WAIT_GRACE_SECONDS>=row['deadline']:raise Failure('WAIT_DEADLINE_EXCEEDED')
            response=self.envelope(dict(row)|{'fence':secrets.token_hex(32),'due':due},'deferred')
            response['code']='WORKER_ADMISSION_DEFERRED'
            c.execute("UPDATE workflow_wait_owner SET phase='waiting',fence=?,request_fence=?,request_stage=?,due=?,expires=?,response=? WHERE job_id=?",
                      (response['fence'],row['fence'],name,due,due+WAIT_GRACE_SECONDS,json.dumps(response),row['job_id']))
            c.execute("UPDATE jobs SET lease=0,available=?,updated=?,error='WORKER_ADMISSION_DEFERRED' WHERE id=?",(due,now,row['job_id']))
            return response
