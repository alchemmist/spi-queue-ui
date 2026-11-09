"""Small operator DTO and atomic cancellation. No execution or retry controls."""
import hmac
import math
import re
import time
from core import Failure

STATES = ('queued', 'retry', 'processing', 'dead', 'complete', 'cancelled', 'dry_run')

def authorized(headers, token):
    values = headers.get_all('X-Queue-Token', [])
    return (len(token) >= 32 and len(values) == 1 and len(values[0]) <= 128
            and hmac.compare_digest(values[0].encode('utf-8'), token.encode('utf-8')))

class QueueAdmin:
    def __init__(self, store):
        self.store = store
        with store.db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS queue_operator_actions (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                action TEXT NOT NULL, at REAL NOT NULL,
                previous_state TEXT NOT NULL, previous_error TEXT, previous_updated REAL NOT NULL,
                source TEXT NOT NULL CHECK(source='private-sso-queue-ui'))''')

    def list(self, data):
        state = data.get('state', 'pending')
        offset = data.get('offset', 0)
        limit = data.get('limit', 25)
        if state not in ('pending', *STATES) or type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or not 1 <= limit <= 100:
            raise Failure('INVALID_QUEUE_QUERY')
        selected = ('processing', 'queued', 'retry') if state == 'pending' else (state,)
        now = time.time()
        with self.store.db() as c:
            counts = {r['state']: r['n'] for r in c.execute('SELECT state,count(*) n FROM jobs GROUP BY state')}
            where = 'j.state IN (' + ','.join('?' for _ in selected) + ')'
            total = c.execute('SELECT count(*) FROM jobs j WHERE ' + where, selected).fetchone()[0]
            rows = c.execute('''SELECT j.id,j.state,j.stage,j.created,j.updated,j.available,j.attempts,j.error,j.issue,
                j.intent IS NOT NULL AS has_intent,
                json_extract(j.result,'$.primary_publication') IS NOT NULL AS has_primary,
                substr(json_extract(j.event,'$.labels.alertname'),1,200) AS alertname,
                substr(json_extract(j.event,'$.labels.instance'),1,200) AS instance,
                substr(json_extract(j.event,'$.labels.namespace'),1,120) AS namespace,
                substr(json_extract(j.event,'$.labels.severity'),1,80) AS severity,
                json_extract(j.event,'$.status') AS alert_status,
                substr(json_extract(j.event,'$.annotations.summary'),1,400) AS summary,
                coalesce(b.failures,0) AS failures,
                o.execution_id,o.phase AS owner_phase,o.stage AS owner_stage,o.due,o.deadline,
                json_array_length(json_extract(j.result,'$.match_progress_v3.reviews')) AS review_batches,
                json_array_length(json_extract(j.result,'$.match_progress_v3.snapshot')) AS review_catalog,
                o.phase IN ('ready','waiting','running','complete') AS owned
                FROM jobs j LEFT JOIN job_failure_budget b ON b.job_id=j.id
                LEFT JOIN workflow_wait_owner o ON o.job_id=j.id WHERE ''' + where + '''
                ORDER BY CASE WHEN j.state='processing' THEN 0 ELSE 1 END,j.stage DESC,j.created,j.id LIMIT ? OFFSET ?''',
                (*selected, limit, offset)).fetchall()
            items = []
            for r in rows:
                item = dict(r)
                item['can_cancel'] = (r['state'] in ('queued', 'retry') and not r['owned'] and not r['has_intent'] and not r['has_primary'] and not r['issue'])
                for key in ('owned', 'has_intent', 'has_primary'):
                    del item[key]
                items.append(item)
        return {'now': now, 'counts': counts, 'total': total, 'offset': offset, 'limit': limit,
                'items': items, 'limits': {'parallel_jobs': 1, 'requests_per_minute': 3,
                'tokens_per_minute': self.store.minute_token_limit, 'request_tokens': 30000}}

    def cancel(self, data):
        key = data.get('job_id')
        expected = data.get('expected_updated')
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key) or type(expected) not in (int, float) or not math.isfinite(expected):
            raise Failure('INVALID_QUEUE_CANCEL')
        with self.store.db() as c:
            row = c.execute('''SELECT j.state,j.updated,j.intent,j.issue,j.error,
                json_extract(j.result,'$.primary_publication') AS primary_receipt,
                EXISTS(SELECT 1 FROM workflow_wait_owner o WHERE o.job_id=j.id
                  AND o.phase IN ('ready','waiting','running','complete')) AS owned
                FROM jobs j WHERE j.id=?''', (key,)).fetchone()
            if row is None:
                raise Failure('JOB_NOT_FOUND')
            if row['state'] == 'cancelled' and c.execute("SELECT 1 FROM queue_operator_actions WHERE job_id=? AND action='cancel'", (key,)).fetchone():
                return {'status': 'cancelled', 'already_applied': True}
            if row['updated'] != expected:
                raise Failure('QUEUE_ITEM_CHANGED')
            if row['state'] not in ('queued', 'retry') or row['owned'] or row['intent'] is not None or row['primary_receipt'] is not None or row['issue']:
                raise Failure('QUEUE_ITEM_NOT_CANCELLABLE')
            now = time.time()
            c.execute("UPDATE jobs SET state='cancelled',lease=0,updated=?,error='OPERATOR_CANCELLED' WHERE id=?", (now, key))
            c.execute("INSERT INTO queue_operator_actions(job_id,action,at,previous_state,previous_error,previous_updated,source) VALUES(?,'cancel',?,?,?,?,'private-sso-queue-ui')", (key, now, row['state'], row['error'], row['updated']))
        return {'status': 'cancelled', 'already_applied': False}
