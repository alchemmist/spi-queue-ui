"""Isolated temporary SQLite only. No live cancellation or model requests."""
import json
import tempfile
import time
import unittest
from pathlib import Path
import test_triage as f
from core import Failure, Store
from queue_admin import QueueAdmin, authorized
from email.message import Message

class QueueAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'queue.sqlite')
        self.api = QueueAdmin(self.store)
        self.key = self.store.enqueue(f.event())['job_id']

    def row(self):
        return next(x for x in self.api.list({})['items'] if x['id'] == self.key)

    def test_dedicated_auth_does_not_accept_worker_key_duplicates_or_non_ascii(self):
        token = 'local-fixture-' * 4
        headers = Message(); headers['X-Worker-Token'] = token
        self.assertFalse(authorized(headers, token))
        headers['X-Queue-Token'] = token
        self.assertTrue(authorized(headers, token))
        headers['X-Queue-Token'] = token
        self.assertFalse(authorized(headers, token))
        other = Message(); other['X-Queue-Token'] = 'несовпадение'
        self.assertFalse(authorized(other, token))
        self.assertFalse(authorized(other, ''))

    def test_minimal_dto_no_raw_event_result_or_fences(self):
        value = self.row()
        self.assertTrue(value['can_cancel'])
        self.assertEqual(value['state'], 'queued')
        self.assertEqual(value['failures'], 0)
        for field in ('event', 'result', 'intent', 'fence', 'request_fence', 'response', 'episode'):
            self.assertNotIn(field, value)
        self.assertEqual(self.api.list({})['limits']['tokens_per_minute'], 30000)

    def test_cancel_preserves_row_checkpoint_budget_and_previous_error_audit(self):
        with self.store.db() as c:
            c.execute("UPDATE jobs SET state='retry',error='GITHUB_READ_TIMEOUT',result=? WHERE id=?", (json.dumps({'safe_checkpoint': 'retain'}), self.key))
            c.execute('INSERT INTO job_failure_budget VALUES(?,3)', (self.key,))
        value = self.row()
        result = self.api.cancel({'job_id': self.key, 'expected_updated': value['updated']})
        self.assertEqual(result['status'], 'cancelled')
        after = self.store.get(self.key)
        self.assertEqual(after['state'], 'cancelled')
        self.assertEqual(after['result'], {'safe_checkpoint': 'retain'})
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT failures FROM job_failure_budget WHERE job_id=?', (self.key,)).fetchone()[0], 3)
            audit = c.execute('SELECT * FROM queue_operator_actions').fetchone()
            self.assertEqual(audit['previous_error'], 'GITHUB_READ_TIMEOUT')
            self.assertEqual(audit['previous_state'], 'retry')
        self.assertEqual(self.api.list({})['total'], 0)
        self.assertEqual(self.store.enqueue(f.event())['duplicate'], True)
        self.assertEqual(self.store.get(self.key)['state'], 'cancelled')

    def test_duplicate_cancel_has_one_audit_receipt(self):
        request = {'job_id': self.key, 'expected_updated': self.row()['updated']}
        self.api.cancel(request)
        self.assertTrue(self.api.cancel(request)['already_applied'])
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM queue_operator_actions').fetchone()[0], 1)

    def test_stale_version_rejected_after_claim(self):
        item = self.row()
        self.store.claim()
        with self.assertRaises(Failure):
            self.api.cancel({'job_id': self.key, 'expected_updated': item['updated']})
        self.assertEqual(self.store.get(self.key)['state'], 'processing')

    def test_cancel_wins_then_claim_cannot_take_it(self):
        item = self.row()
        self.api.cancel({'job_id': self.key, 'expected_updated': item['updated']})
        self.assertFalse(self.store.claim().get('job_id'))

    def test_wait_owner_never_cancellable_even_if_job_is_retry(self):
        self.store.claim()
        owner = self.store.owners.bind(self.key, '123', 'durable-v1', 'match', time.time())
        self.store.owners.defer(owner, 'LLM_ADMISSION_DEFERRED', 61, time.time())
        with self.store.db() as c:c.execute("UPDATE jobs SET state='retry' WHERE id=?", (self.key,))
        item = self.row()
        self.assertFalse(item['can_cancel'])
        with self.assertRaisesRegex(Failure, 'QUEUE_ITEM_NOT_CANCELLABLE'):
            self.api.cancel({'job_id': self.key, 'expected_updated': item['updated']})

    def test_terminal_and_publication_states_are_not_cancellable(self):
        for state, intent, issue, result in [('complete', None, 0, {}), ('dead', None, 0, {}),
                  ('cancelled', None, 0, {}), ('dry_run', None, 0, {}),
                  ('retry', '{}', 0, {}), ('retry', None, 1, {}),
                  ('retry', None, 0, {'primary_publication': {'id': 1}})]:
            with self.subTest(state=state, intent=intent, issue=issue, result=result):
                with self.store.db() as c:
                    c.execute('UPDATE jobs SET state=?,intent=?,issue=?,result=? WHERE id=?', (state, intent, issue, json.dumps(result), self.key))
                current = self.store.get(self.key)
                with self.assertRaisesRegex(Failure, 'QUEUE_ITEM_NOT_CANCELLABLE'):
                    self.api.cancel({'job_id': self.key, 'expected_updated': current['updated']})

    def test_audit_failure_rolls_back_cancellation(self):
        with self.store.db() as c:
            c.execute("CREATE TRIGGER block_operator_audit BEFORE INSERT ON queue_operator_actions BEGIN SELECT RAISE(ABORT,'blocked'); END")
        item = self.row()
        with self.assertRaises(Exception):
            self.api.cancel({'job_id': self.key, 'expected_updated': item['updated']})
        self.assertEqual(self.store.get(self.key)['state'], 'queued')

    def test_query_and_cancel_validation(self):
        for bad in ({'state': 'all;DROP TABLE jobs'}, {'limit': True}, {'limit': 101}, {'offset': -1}):
            with self.assertRaisesRegex(Failure, 'INVALID_QUEUE_QUERY'):self.api.list(bad)
        for bad in ({}, {'job_id': self.key, 'expected_updated': float('nan')},
                    {'job_id': self.key, 'expected_updated': True},
                    {'job_id': '../', 'expected_updated': 1}):
            with self.assertRaisesRegex(Failure, 'INVALID_QUEUE_CANCEL'):self.api.cancel(bad)

if __name__ == '__main__':unittest.main()
