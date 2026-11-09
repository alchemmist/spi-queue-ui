import importlib.util
from pathlib import Path
import tempfile
import unittest

FILES = Path(__file__).resolve().parents[2] / 'backend'
def load(name):
    spec = importlib.util.spec_from_file_location(name, FILES / (name+'.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
core, links = load('core'), load('telegram_links')

class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = core.Store(Path(self.tmp.name)/'db')
        self.now = 1000
        self.calls = []
        self.item = dict(link_protocol=1, episode='e', generation=1, copies=[
            dict(chat_id=1,message_id=2,delivery_state='applied',applied_revision=1,
                 applied_url='https://github.com/alchemmist/homelab/issues/42#issuecomment-7')])
        self.status = 200
        self.outbox = links.Links(self.store,self.http,clock=lambda:self.now,guarded_protocol=True)
    def http(self, method, path, **kwargs):
        self.calls.append((method,path,kwargs))
        return self.status, self.item if method == 'GET' else dict(key='Alert/pod/ns/name',updated=[dict(chat_id=1,message_id=2)],pending=[])
    def publication(self, **changes):
        return dict(dict(kind='comment', role='primary',issue_number=42,id=7,
                         html_url='https://github.com/alchemmist/homelab/issues/42#issuecomment-7'), **changes)
    def enqueue(self):
        self.outbox.record_primary_publication('event','e',1,self.publication())
    def receipt(self):
        self.outbox.record_card_receipt('e',dict(key='Alert/pod/ns/name',episode='e',immutable_key=True,copies=[dict(chat_id=1,message_id=2)]))
    def retry(self):
        self.now += 4000; self.outbox.once()
    def test_duplicate_and_all_copy_ack(self):
        self.enqueue(); self.enqueue(); self.receipt(); self.outbox.once()
        self.assertEqual(self.outbox.snapshot('e')['state'],'applied')
        self.assertFalse(self.outbox.once())
        self.assertEqual(len(self.calls),1)
    def test_publication_before_card(self):
        self.enqueue(); self.outbox.once()
        self.assertEqual(self.outbox.snapshot('e')['error'],'WAIT_RECEIPT')
        self.receipt(); self.outbox.once()
        self.assertEqual(self.outbox.snapshot('e')['state'],'applied')
    def test_races_and_restart(self):
        self.enqueue(); self.receipt()
        for status in (404,409,500):
            self.status=status; self.retry()
            self.assertEqual(self.outbox.snapshot('e')['state'],'pending')
        self.outbox=links.Links(self.store,self.http,clock=lambda:self.now,guarded_protocol=True)
        self.status=200; self.retry()
        self.assertEqual(self.outbox.snapshot('e')['state'],'applied')
    def test_partial_readback_not_desired_state(self):
        self.enqueue(); self.receipt()
        self.outbox.request=lambda *a,**kw: (200,dict(key='Alert/pod/ns/name',spi_url=self.publication()['html_url'],updated=[],pending=[dict(chat_id=1,message_id=2)]))
        self.outbox.once()
        self.assertEqual(self.outbox.snapshot('e')['error'],'COPIES_PENDING')
        self.outbox.request=self.http; self.retry()
        self.assertEqual(self.outbox.snapshot('e')['state'],'applied')
    def test_timeout_secret_safe(self):
        self.enqueue(); self.receipt()
        def fail(*a,**kw): raise TimeoutError('X-Internal-Token: secret-value')
        self.outbox.request=fail; self.outbox.once()
        self.assertEqual(self.outbox.snapshot('e')['error'],'DELIVERY_UNKNOWN')
        self.assertNotIn('secret-value',str(self.outbox.snapshot('e')))
    def test_lease_recovery(self):
        self.enqueue(); self.receipt()
        with self.store.db() as c:
            c.execute("UPDATE spi_links SET owner='crashed',lease=1100")
        self.assertFalse(self.outbox.once()); self.retry()
        self.assertEqual(self.outbox.snapshot('e')['state'],'applied')
    def test_episode_and_stale_revision(self):
        self.enqueue()
        self.outbox.record_card_receipt('e',dict(key='legacy',copies=[dict(chat_id=1,message_id=2)]))
        self.outbox.once()
        self.assertEqual(self.outbox.snapshot('e')['error'],'WAIT_IMMUTABLE_RECEIPT')
        self.assertFalse(any(c[0]=='POST' for c in self.calls))
        self.outbox.record_primary_publication('resolved','e',2,self.publication(id=8,html_url='https://github.com/alchemmist/homelab/issues/42#issuecomment-8'))
        self.enqueue()
        self.assertEqual(self.outbox.snapshot('e')['revision'],2)
    def test_out_of_order_completion_cannot_ack_new_revision(self):
        self.enqueue(); self.receipt()
        original=self.http
        def racing(method,path,**kwargs):
            if method=='POST':
                self.outbox.record_primary_publication('resolved','e',2,self.publication(id=8,html_url='https://github.com/alchemmist/homelab/issues/42#issuecomment-8'))
            return original(method,path,**kwargs)
        self.outbox.request=racing; self.outbox.once()
        self.assertEqual(self.outbox.snapshot('e')['state'],'pending')
        self.assertEqual(self.outbox.snapshot('e')['revision'],2)
    def test_legacy_bot_blocked(self):
        self.enqueue(); self.receipt(); self.outbox.guarded=False; self.outbox.once()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.outbox.snapshot('e')['error'],'BOT_GUARD_REQUIRED')
    def test_urls(self):
        self.assertEqual(links.primary_url(self.publication(kind='issue',html_url='https://github.com/alchemmist/homelab/issues/42')), 'https://github.com/alchemmist/homelab/issues/42')
        for changes in [dict(role='artifact'),dict(html_url='https://github.com/alchemmist/homelab/issues/42'),dict(html_url='https://evil/issues/42#issuecomment-7'),dict(id=8),dict(html_url=self.publication()['html_url']+'?token=x')]:
            with self.assertRaises(ValueError): links.primary_url(self.publication(**changes))
    def test_atomic_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.store.db() as c:
                self.outbox.record_primary_publication('event','e',1,self.publication(),connection=c)
                raise RuntimeError()
        self.assertIsNone(self.outbox.snapshot('e'))

if __name__ == '__main__': unittest.main()
