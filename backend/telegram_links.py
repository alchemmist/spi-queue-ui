"""Durable original-card decoration outbox; no GitHub/model dependencies.

Transport: request(method, path, json=None) -> (HTTP status, decoded dict).
It owns credentials/timeouts and MUST NOT log headers or response bodies.
The current legacy bot cannot safely deliver: guarded_protocol defaults False.
Enable only against reviewed immutable episode-key + per-copy acknowledgement API.
"""
import json
import re
import time
import uuid

_URL = re.compile(r'https://github\.com/alchemmist/homelab/issues/([1-9][0-9]*)(?:#issuecomment-([1-9][0-9]*))?\Z')


def primary_url(publication):
    url = publication.get('html_url', '')
    match = _URL.fullmatch(url) if isinstance(url, str) else None
    if not match or int(match[1]) != publication.get('issue_number'):
        raise ValueError('INVALID_PRIMARY_URL')
    if publication.get('kind') == 'issue':
        valid = match[2] is None
    else:
        valid = (publication.get('kind') == 'comment' and match[2] is not None
                 and int(match[2]) == publication.get('id'))
    if not valid or publication.get('role') != 'primary':
        raise ValueError('INVALID_PRIMARY_RECEIPT')
    return url


class Links:
    def __init__(self, store, request, clock=time.time, jitter=lambda: 0.5,
                 guarded_protocol=False):
        self.store, self.request, self.clock = store, request, clock
        self.jitter, self.guarded = jitter, guarded_protocol
        with store.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS spi_card_receipts (
              episode TEXT PRIMARY KEY, receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS spi_links (
              episode TEXT PRIMARY KEY, event_key TEXT NOT NULL, revision INTEGER NOT NULL,
              url TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              available REAL NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0,
              owner TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
              acknowledgement TEXT NOT NULL DEFAULT '{}');
            ''')

    def record_card_receipt(self, episode, receipt, connection=None):
        # Store only identity, never original alert text/credentials.
        safe = {k: receipt[k] for k in ('key', 'episode', 'immutable_key', 'copies') if k in receipt}
        safe['copies'] = [{k: x[k] for k in ('chat_id', 'message_id') if k in x}
                          for x in safe.get('copies', [])]
        if safe.get('episode', episode) != episode:
            raise ValueError('EPISODE_MISMATCH')
        def save(c):
            previous = c.execute('SELECT receipt FROM spi_card_receipts WHERE episode=?', (episode,)).fetchone()
            encoded = json.dumps(safe, sort_keys=True)
            if previous:
                old=json.loads(previous[0])
                if old.get('key')!=safe.get('key') or old.get('episode')!=safe.get('episode'):
                    raise ValueError('RECEIPT_CONFLICT')
                merged={(x['chat_id'],x['message_id'])for x in old.get('copies',[])}
                merged.update((x['chat_id'],x['message_id'])for x in safe.get('copies',[]))
                safe['copies']=[{'chat_id':chat,'message_id':message}for chat,message in sorted(merged)]
                safe['immutable_key']=old.get('immutable_key') is True or safe.get('immutable_key') is True
            encoded=json.dumps(safe,sort_keys=True)
            c.execute('INSERT INTO spi_card_receipts VALUES (?,?) ON CONFLICT(episode) DO UPDATE SET receipt=excluded.receipt',(episode,encoded))
            if not previous or previous[0]!=encoded:
                c.execute("UPDATE spi_links SET state='pending',available=0 WHERE episode=?",(episode,))
        if connection is not None:
            save(connection)
        else:
            with self.store.db() as c: save(c)

    def record_primary_publication(self, event_key, episode, revision, publication, connection=None):
        url = primary_url(publication)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError('INVALID_REVISION')
        def save(c):
            row = c.execute('SELECT * FROM spi_links WHERE episode=?', (episode,)).fetchone()
            if row and revision <= row['revision']:
                if revision == row['revision'] and (url != row['url'] or event_key != row['event_key']):
                    raise ValueError('REVISION_CONFLICT')
                return
            c.execute('''INSERT INTO spi_links(episode,event_key,revision,url) VALUES(?,?,?,?)
              ON CONFLICT(episode) DO UPDATE SET event_key=excluded.event_key,
              revision=excluded.revision,url=excluded.url,state='pending',available=0,
              attempts=0,error=NULL,acknowledgement='{}' ''',
                      (episode, event_key, revision, url))
        if connection is not None:
            save(connection)
        else:
            with self.store.db() as c: save(c)

    def snapshot(self, episode):
        with self.store.db() as c:
            row = c.execute('SELECT * FROM spi_links WHERE episode=?', (episode,)).fetchone()
            return dict(row) if row else None

    def once(self):
        """One bounded attempt. Run on a dedicated thread, never the dispatch loop.

        Reviewed bot contract: immutable timestamp-derived episode keys,
        expected_copies atomic guard, retryable failed edits and explicit updated/
        pending copy sets. Desired-state GET readback is never delivery evidence.
        Serialize newer episode revisions in the caller; local CAS protects state
        but cannot cancel an older in-flight remote edit.
        """
        now, owner = self.clock(), uuid.uuid4().hex
        with self.store.db() as c:
            row = c.execute("SELECT * FROM spi_links WHERE state!='applied' AND available<=? AND lease<=? ORDER BY available,episode LIMIT 1", (now, now)).fetchone()
            if row is None: return False
            row = dict(row)
            c.execute("UPDATE spi_links SET lease=?,owner=?,attempts=attempts+1 WHERE episode=?", (now+120, owner, row['episode']))
            receipt = c.execute('SELECT receipt FROM spi_card_receipts WHERE episode=?', (row['episode'],)).fetchone()
        error, ack, applied = 'WAIT_RECEIPT', {}, False
        try:
            receipt = json.loads(receipt[0]) if receipt else {}
            if receipt.get('key'):
                if not self.guarded:
                    error = 'BOT_GUARD_REQUIRED'
                elif (receipt.get('episode') != row['episode'] or
                      receipt.get('immutable_key') is not True or not receipt.get('copies')):
                    error = 'WAIT_IMMUTABLE_RECEIPT'
                else:
                    # immutable_key is trusted ingress attestation of the reviewed
                    # bot's timestamp-derived episode key, never legacy key inference.
                    expected = {(x['chat_id'], x['message_id']) for x in receipt['copies']}
                    payload = dict(key=receipt['key'], url=row['url'], label='SPI',revision=row['revision'],
                                   expected_copies=receipt['copies'])
                    status, result = self.request('POST', '/v1/cards/links', json=payload)
                    error = 'CARD_NOT_READY' if status in (404, 409) else 'BOT_HTTP_ERROR'
                    if status == 200:
                        updated = {(x.get('chat_id'), x.get('message_id'))
                                   for x in result.get('updated', [])}
                        applied = (result.get('key') == receipt['key'] and expected <= updated
                                   and not result.get('pending', []))
                        ack = {'expected': len(expected), 'observed': len(updated), 'verified': applied}
                        error = None if applied else 'COPIES_PENDING'
        except Exception:
            # Ambiguous acceptance is retryable, never a fabricated receipt.
            error = 'DELIVERY_UNKNOWN'
        delay = min(3600, 2 ** min(row['attempts']+1, 11)) * (0.75 + 0.5*self.jitter())
        with self.store.db() as c:
            c.execute('''UPDATE spi_links SET state=?,error=?,acknowledgement=?,
              available=?,lease=0,owner=NULL WHERE episode=? AND revision=? AND owner=?''',
                      ('applied' if applied else 'pending', error, json.dumps(ack),
                       self.clock()+(21600 if applied else delay), row['episode'], row['revision'], owner))
            # New desired revision keeps the active lease until old request exits.
            # Its completion must not acknowledge the newer URL, only unblock it.
            c.execute("UPDATE spi_links SET lease=0,owner=NULL WHERE episode=? AND owner=? AND revision!=?",
                      (row['episode'], owner, row['revision']))
        return True
