"""Small operator DTO and atomic cancellation. No execution or retry controls."""
import hmac
import math
import re
import time
from core import Failure

MAX_CANCEL_BATCH = 4000

STATES = ('queued', 'retry', 'processing', 'dead', 'complete', 'cancelled', 'dry_run')
# Fixed SQL identifiers only; no client text is interpolated into SQL.
SORTS = {'created': ('created', 'id'),
         'alert': ('sort_alert', 'created', 'id'),
         'state': ('sort_state', 'stage', 'created', 'id'),
         'progress': ('sort_progress', 'attempts', 'failures', 'created', 'id')}

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
        sort, direction, after = data.get('sort', 'created'), data.get('direction', 'asc'), data.get('after')
        if not isinstance(sort, str) or sort not in SORTS or direction not in ('asc', 'desc'):
            raise Failure('INVALID_QUEUE_QUERY')
        columns = SORTS[sort]
        values = []
        if after is not None:
            if (not isinstance(after, dict) or set(after) != {'sort', 'direction', 'values'}
                    or after['sort'] != sort or after['direction'] != direction or offset
                    or not isinstance(after['values'], list) or len(after['values']) != len(columns)):
                raise Failure('INVALID_QUEUE_QUERY')
            values = after['values']
            for column, value in zip(columns, values):
                if column in ('id', 'sort_alert'):
                    if not isinstance(value, str) or len(value) > 200 or (column == 'id' and not re.fullmatch('[0-9a-f]{64}', value)):
                        raise Failure('INVALID_QUEUE_QUERY')
                elif type(value) not in (int, float) or not math.isfinite(value):
                    raise Failure('INVALID_QUEUE_QUERY')
        selected = ('processing', 'queued', 'retry') if state == 'pending' else (state,)
        now = time.time()
        with self.store.db() as c:
            counts = {r['state']: r['n'] for r in c.execute('SELECT state,count(*) n FROM jobs GROUP BY state')}
            where = 'j.state IN (' + ','.join('?' for _ in selected) + ')'
            total = c.execute('SELECT count(*) FROM jobs j WHERE ' + where, selected).fetchone()[0]
            query = '''SELECT * FROM (SELECT j.id,j.state,j.stage,j.created,j.updated,j.available,j.attempts,j.error,j.issue,
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
                o.phase IN ('ready','waiting','running','complete') AS owned,
                coalesce(substr(json_extract(j.event,'$.labels.alertname'),1,200),'') COLLATE NOCASE AS sort_alert,
                CASE j.state WHEN 'queued' THEN 0 WHEN 'retry' THEN 1 WHEN 'processing' THEN 2
                  WHEN 'dead' THEN 3 WHEN 'complete' THEN 4 WHEN 'cancelled' THEN 5 ELSE 6 END AS sort_state,
                coalesce(json_array_length(json_extract(j.result,'$.match_progress_v3.reviews')),0) AS sort_progress
                FROM jobs j LEFT JOIN job_failure_budget b ON b.job_id=j.id
                LEFT JOIN workflow_wait_owner o ON o.job_id=j.id WHERE ''' + where + ') AS q'
            if after is not None:
                query += ' WHERE (' + ','.join(columns) + ') ' + ('>' if direction == 'asc' else '<') + ' (' + ','.join('?' for _ in columns) + ')'
            query += ' ORDER BY ' + ','.join(column + ' ' + direction for column in columns) + ' LIMIT ? OFFSET ?'
            rows = c.execute(query, (*selected, *values, limit + 1, offset)).fetchall()
            page = rows[:limit]
            cursor = ({'sort':sort, 'direction':direction, 'values':[page[-1][column] for column in columns]}
                      if len(rows) > limit else None)
            items = []
            for r in page:
                item = dict(r)
                item['can_cancel'] = (r['state'] in ('queued', 'retry') and not r['owned'] and not r['has_intent'] and not r['has_primary'] and not r['issue'])
                for key in ('owned', 'has_intent', 'has_primary', 'sort_alert', 'sort_state', 'sort_progress'):
                    del item[key]
                items.append(item)
        return {'now': now, 'counts': counts, 'total': total, 'offset': offset, 'limit': limit,
                'items': items, 'sort': sort, 'direction': direction, 'next_cursor': cursor, 'limits': {'parallel_jobs': 1, 'requests_per_minute': 3,
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


    def cancel_batch(self, data):
        """One fenced transaction, one wake. Never cancel a live owner/publication.

        The temporary tables are connection-local, memory-backed and bounded.
        Audits and job updates commit together or all roll back on any failure.
        """
        requests = data.get('items')
        if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_CANCEL_BATCH or set(data) != {'items'}:
            raise Failure('INVALID_QUEUE_CANCEL_BATCH')
        values = []
        seen = set()
        for item in requests:
            if not isinstance(item, dict) or set(item) != {'job_id', 'expected_updated'}:
                raise Failure('INVALID_QUEUE_CANCEL_BATCH')
            key, expected = item['job_id'], item['expected_updated']
            try:valid_version = type(expected) in (int, float) and math.isfinite(expected)
            except OverflowError:valid_version = False
            if (not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key) or key in seen
                    or not valid_version):
                raise Failure('INVALID_QUEUE_CANCEL_BATCH')
            seen.add(key); values.append((key, expected))
        cancelled, already, skipped = [], [], []
        with self.store.db() as c:
            c.execute('CREATE TEMP TABLE cancel_batch_input(id TEXT PRIMARY KEY,expected REAL NOT NULL)')
            c.executemany('INSERT INTO cancel_batch_input VALUES(?,?)', values)
            rows = c.execute("""SELECT i.id,i.expected,j.id AS found,j.state,j.updated,j.intent,j.issue,
                json_extract(j.result,'$.primary_publication') AS primary_receipt,
                EXISTS(SELECT 1 FROM workflow_wait_owner o WHERE o.job_id=j.id
                  AND o.phase IN ('ready','waiting','running','complete')) AS owned,
                EXISTS(SELECT 1 FROM queue_operator_actions a WHERE a.job_id=j.id AND a.action='cancel') AS audited
                FROM cancel_batch_input i LEFT JOIN jobs j ON j.id=i.id""").fetchall()
            for row in rows:
                code = None
                if row['found'] is None:code = 'JOB_NOT_FOUND'
                elif row['state'] == 'cancelled' and row['audited']:
                    already.append(row['id']); continue
                elif row['updated'] != row['expected']:code = 'QUEUE_ITEM_CHANGED'
                elif (row['state'] not in ('queued', 'retry') or row['owned'] or row['intent'] is not None
                      or row['primary_receipt'] is not None or row['issue']):code = 'QUEUE_ITEM_NOT_CANCELLABLE'
                if code:skipped.append({'job_id':row['id'], 'code':code})
                else:cancelled.append(row['id'])
            c.execute('CREATE TEMP TABLE cancel_batch_eligible(id TEXT PRIMARY KEY)')
            c.executemany('INSERT INTO cancel_batch_eligible VALUES(?)', ((key,) for key in cancelled))
            now = time.time()
            c.execute("""INSERT INTO queue_operator_actions(job_id,action,at,previous_state,previous_error,previous_updated,source)
                SELECT j.id,'cancel',?,j.state,j.error,j.updated,'private-sso-queue-ui'
                FROM jobs j JOIN cancel_batch_eligible e ON e.id=j.id""", (now,))
            c.execute("""UPDATE jobs SET state='cancelled',lease=0,updated=?,error='OPERATOR_CANCELLED'
                WHERE id IN (SELECT id FROM cancel_batch_eligible)""", (now,))
            counts = {r['state']:r['n'] for r in c.execute('SELECT state,count(*) n FROM jobs GROUP BY state')}
        return {'cancelled':cancelled, 'already_cancelled':already, 'skipped':skipped, 'counts':counts, 'now':now}
