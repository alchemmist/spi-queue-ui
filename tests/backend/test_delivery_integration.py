import unittest,tempfile,json
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
import test_triage as fixture
from core import Store,Failure
from telegram_links import Links
from telegram_delivery import Delivery
from integrations import GitHub

class DeliveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture.EngineTests.setUp(self)
        self.links=Links(self.store,Mock(),guarded_protocol=True)
        self.engine.delivery=SimpleNamespace(links=self.links,signal=Mock())
        self.engine.write=True
    def tearDown(self):fixture.EngineTests.tearDown(self)
    def process(self,*a,**kw):return fixture.EngineTests.process(self,*a,**kw)
    def test_new_issue_is_durably_linked_before_artifacts(self):
        self.github.request.return_value={'number':7,'id':70,'html_url':'https://github.com/alchemmist/homelab/issues/7'}
        key=self.process();r=self.store.get(key)
        self.assertEqual(r['result']['primary_publication']['kind'],'issue')
        self.assertEqual(self.links.snapshot(r['episode'])['url'],self.github.request.return_value['html_url'])
    def test_matched_issue_points_to_exact_primary_comment_not_root(self):
        self.model.match.return_value={'candidates':[{'number':7}]};self.model.analyze.return_value=fixture.report(7)
        url='https://github.com/alchemmist/homelab/issues/7#issuecomment-777'
        self.github.request.return_value={'id':777,'html_url':url}
        key=self.process();self.assertEqual(self.links.snapshot(self.store.get(key)['episode'])['url'],url)
        self.engine.run('publish',key);self.assertEqual(self.github.request.call_count,1)
    def test_ambiguous_comment_reconciles_exact_url_without_second_write(self):
        key=self.process(through=3);self.github.request.side_effect=Failure('UPSTREAM_NETWORK_ERROR')
        with self.assertRaises(Failure):self.engine.run('publish',key)
        url='https://github.com/alchemmist/homelab/issues/7#issuecomment-778'
        self.github.reconcile.return_value=7;self.github.primary_reference.return_value={'kind':'comment','role':'primary','issue_number':7,'id':778,'html_url':url}
        with self.store.db()as c:c.execute('UPDATE jobs SET available=0,lease=0')
        self.store.claim();self.engine.run('publish',key)
        self.assertEqual(self.github.request.call_count,1);self.assertEqual(self.links.snapshot(self.store.get(key)['episode'])['url'],url)
    def test_save_primary_rolls_back_state_when_outbox_write_fails(self):
        key=self.process(through=3);before=self.store.get(key)['result'];self.links.record_primary_publication=Mock(side_effect=RuntimeError('db fault'))
        with self.assertRaises(RuntimeError):self.engine.save_primary(key,dict(before),fixture.event(),{'kind':'issue','role':'primary','issue_number':7,'id':70,'html_url':'https://github.com/alchemmist/homelab/issues/7'})
        self.assertEqual(self.store.get(key)['result'],before)
    def test_orphan_resolved_is_analyzed_instead_of_dropped_when_links_required(self):
        self.github.request.return_value={'number':7,'id':70,'html_url':'https://github.com/alchemmist/homelab/issues/7'}
        key=self.process(fixture.event('resolved'));self.model.analyze.assert_called_once()
        self.assertEqual(self.links.snapshot(self.store.get(key)['episode'])['revision'],2)
    def test_lost_receipt_callback_recovers_by_immutable_episode(self):
        delivery=Delivery(self.store,'test',enabled=True);e=fixture.event();episode=e['episode'];key='Test/spi/'+episode
        delivery.links.record_primary_publication(e['event_key'],episode,1,{'kind':'issue','role':'primary','issue_number':7,'id':70,'html_url':'https://github.com/alchemmist/homelab/issues/7'})
        delivery.request=Mock(return_value=(200,{'cards':[{'key':key,'episode':episode,'immutable_key':True,'copies':[{'chat_id':1,'message_id':3,'text_stored':True}]}]}))
        delivery.discover()
        with self.store.db()as c:self.assertTrue(c.execute('SELECT receipt FROM spi_card_receipts WHERE episode=?',(episode,)).fetchone())
    def test_new_recipient_receipt_requeues_applied_target(self):
        e=fixture.event();self.links.record_primary_publication(e['event_key'],e['episode'],1,{'kind':'issue','role':'primary','issue_number':7,'id':70,'html_url':'https://github.com/alchemmist/homelab/issues/7'})
        base={'key':'Test/spi/'+e['episode'],'episode':e['episode'],'immutable_key':True,'copies':[{'chat_id':1,'message_id':3}]}
        self.links.record_card_receipt(e['episode'],base)
        with self.store.db()as c:c.execute("UPDATE spi_links SET state='applied'")
        self.links.record_card_receipt(e['episode'],{**base,'copies':[{'chat_id':2,'message_id':4}]})
        self.assertEqual(self.links.snapshot(e['episode'])['state'],'pending')
    def test_closed_issue_resolution_preserves_exact_existing_comment(self):
        self.model.match.return_value={'candidates':[{'number':7}]};self.model.analyze.return_value=fixture.report(7)
        url='https://github.com/alchemmist/homelab/issues/7#issuecomment-777'
        self.github.request.return_value={'id':777,'html_url':url};key=self.process()
        self.github.request.return_value={'id':70,'number':7,'state':'closed','html_url':'https://github.com/alchemmist/homelab/issues/7'}
        self.process(fixture.event('resolved'))
        row=self.links.snapshot(self.store.get(key)['episode']);self.assertEqual(row['url'],url);self.assertEqual(row['revision'],1)
    def test_discovery_failure_does_not_block_ready_link(self):
        d=Delivery(self.store,'test',enabled=True);e=fixture.event();other=fixture.event(start='2026-11-03T14:00:00Z')
        pub={'kind':'issue','role':'primary','issue_number':7,'id':70,'html_url':'https://github.com/alchemmist/homelab/issues/7'}
        for event in [e,other]:d.links.record_primary_publication(event['event_key'],event['episode'],1,pub)
        key='Test/spi/'+other['episode'];d.receipt(other,{'key':key,'episode':other['episode'],'immutable_key':True,'copies':[{'chat_id':1,'message_id':2}]})
        d.request=Mock(side_effect=TimeoutError('private-token'));d.discover()
        d.links.request=Mock(return_value=(200,{'key':key,'updated':[{'chat_id':1,'message_id':2}],'pending':[]}))
        # Missing receipt backs off independently; ready episode is then eligible.
        for _ in range(2):d.links.once()
        self.assertEqual(d.links.snapshot(other['episode'])['state'],'applied')
    def test_fallback_ingress_identity_matches_python_including_unicode_and_microseconds(self):
        import subprocess
        for start in ['2026-11-03T13:00:00Z','2026-11-03T13:00:00.123456Z','2026-11-03T16:00:00+03:00']:
            raw={'status':'firing','startsAt':start,'labels':{'alertname':'Test','unicode':'café 🚀','2':'b','10':'a','🚀':'v','\ue000':'w'},'annotations':{}}
            value=json.loads(subprocess.check_output(['node','-e',"const {identity}=require('./integrations/n8n/extensions/SpiEventIdentity');const x=JSON.parse(process.argv[1]);console.log(JSON.stringify(identity('test',x)))",json.dumps(raw)],text=True,cwd=Path(__file__).resolve().parents[2]))
            event=fixture.normalize({'source':'test','alert':raw});self.assertEqual(value,{'event_key':event['event_key'],'episode':event['episode']})
    def test_pre_migration_completed_job_backfills_only_trusted_reference(self):
        self.engine.delivery=None;self.github.request.return_value={'number':7};key=self.process()
        d=Delivery(self.store,'test',enabled=True);e=self.store.get(key)['event'];episode=e['episode']
        d.receipt(e,{'key':'Test/spi/'+episode,'episode':episode,'immutable_key':True,'copies':[{'chat_id':1,'message_id':3}]})
        d.github=Mock();url='https://github.com/alchemmist/homelab/issues/7#issuecomment-99'
        d.github.primary_reference.return_value={'kind':'comment','role':'primary','issue_number':7,'id':99,'html_url':url}
        self.assertTrue(d.backfill());d.github.request.assert_not_called();self.assertEqual(d.links.snapshot(episode)['url'],url)
        self.assertEqual(self.store.get(key)['state'],'complete');self.assertEqual(self.store.get(key)['stage'],4)
    def historical_pair(self):
        d=Delivery(self.store,'test',enabled=True);d.github=Mock()
        for status in ['firing','resolved']:
            e=fixture.event(status);self.store.enqueue(e);self.store.save(e['event_key'],4,{},state='complete',issue=7)
        e=fixture.event();d.receipt(e,{'key':'Test/spi/'+e['episode'],'episode':e['episode'],'immutable_key':True,'copies':[{'chat_id':1,'message_id':3}]})
        return d,e
    def test_historical_resolved_failure_then_firing_then_resolved_converges(self):
        d,e=self.historical_pair()
        def reference(marker,issue):
            if fixture.event('resolved')['event_key']in marker:raise Failure('UPSTREAM_NETWORK_ERROR')
            return {'kind':'comment','role':'primary','issue_number':7,'id':99,'html_url':'https://github.com/alchemmist/homelab/issues/7#issuecomment-99'}
        # Force recovery order: resolved temporarily unavailable, then firing.
        with self.store.db()as c:c.execute('UPDATE jobs SET created=created+10 WHERE id=?',(fixture.event('resolved')['event_key'],))
        d.github.primary_reference.side_effect=reference;d.backfill();d.backfill()
        self.assertEqual(d.links.snapshot(e['episode'])['revision'],1)
        d.github.primary_reference.side_effect=None
        url='https://github.com/alchemmist/homelab/issues/7#issuecomment-100'
        d.github.primary_reference.return_value={'kind':'comment','role':'primary','issue_number':7,'id':100,'html_url':url}
        with self.store.db()as c:c.execute('UPDATE spi_primary_backfill SET available=0')
        d.backfill();self.assertEqual(d.links.snapshot(e['episode'])['revision'],2);self.assertEqual(d.links.snapshot(e['episode'])['url'],url)
        self.assertFalse(d.backfill()) # late firing cannot downgrade revision2
    def test_historical_resolved_becomes_complete_after_firing(self):
        d,e=self.historical_pair();resolved=fixture.event('resolved')
        with self.store.db()as c:c.execute("UPDATE jobs SET state='retry' WHERE id=?",(resolved['event_key'],))
        d.github.primary_reference.return_value={'kind':'issue','role':'primary','issue_number':7,'id':7,'html_url':'https://github.com/alchemmist/homelab/issues/7'}
        d.backfill();self.assertEqual(d.links.snapshot(e['episode'])['revision'],1)
        with self.store.db()as c:c.execute("UPDATE jobs SET state='complete' WHERE id=?",(resolved['event_key'],))
        url='https://github.com/alchemmist/homelab/issues/7#issuecomment-100'
        d.github.primary_reference.return_value={'kind':'comment','role':'primary','issue_number':7,'id':100,'html_url':url}
        d.backfill();self.assertEqual(d.links.snapshot(e['episode'])['url'],url);self.assertEqual(d.links.snapshot(e['episode'])['revision'],2)
    def test_historical_resolved_without_publication_keeps_valid_firing(self):
        d,e=self.historical_pair()
        with self.store.db()as c:c.execute('UPDATE jobs SET created=created+10 WHERE id=?',(fixture.event('resolved')['event_key'],))
        def reference(marker,issue):
            if fixture.event('resolved')['event_key']in marker:raise Failure('PRIMARY_PUBLICATION_MISSING')
            return {'kind':'issue','role':'primary','issue_number':7,'id':7,'html_url':'https://github.com/alchemmist/homelab/issues/7'}
        d.github.primary_reference.side_effect=reference;d.backfill();d.backfill()
        self.assertEqual(d.links.snapshot(e['episode'])['revision'],1)
