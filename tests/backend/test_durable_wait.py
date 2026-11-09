"""Offline primitive coverage only: no claim of end-to-end workflow integration."""
import tempfile
import unittest
import test_triage as fixtures
from core import Store, Failure
from durable_wait import WaitOwners


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name + '/db', minute_token_limit=60000)
        self.key = self.store.enqueue(fixtures.event())['job_id']
        self.store.claim()
        self.owners = WaitOwners(self.store)
        self.owner = self.owners.bind(self.key, '1234', 'revision1', 'match', 100)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeat_wait_restart_fence_and_deadline(self):
        owner = self.owner
        for now in (100, 200, 300):
            waiting = self.owners.defer(owner, 'LLM_ADMISSION_DEFERRED', 61, now)
            self.assertEqual(waiting['due'], now + 66)
            self.assertEqual(self.owners.defer(owner, 'LLM_ADMISSION_DEFERRED', 61, now + 1), waiting)
            restarted = WaitOwners(Store(self.store.path, minute_token_limit=60000))
            owner = restarted.resume(waiting, now + 66)
            with self.assertRaises(Failure):
                restarted.resume(waiting, now + 66)
        self.assertEqual(owner['deadline'], 21700)
        with self.store.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM job_failure_budget').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM llm_admission').fetchone()[0], 0)

    def test_strict_errors_and_owners(self):
        for code in ('LLM_INVALID_EVIDENCE_REFERENCE', 'LLM_HTTP_401', 'UNKNOWN'):
            with self.assertRaises(Failure):
                self.owners.defer(self.owner, code, 61, 100)
        with self.assertRaises(Failure):
            self.owners.bind(self.key, '5678', 'revision1', 'match', 100)
        waiting = self.owners.defer(self.owner, 'LLM_ADMISSION_DEFERRED', 61, 100)
        for bad in ({**waiting, 'revision': 'revision2'}, {**waiting, 'stage': 'analyze'}, {**waiting, 'fence': 'bad'}):
            with self.assertRaises(Failure):
                self.owners.resume(bad, 166)
        with self.assertRaises(Failure):
            self.owners.resume(waiting, 165)
        with self.assertRaises(Failure):
            self.owners.resume(waiting, waiting['expires'])

    def test_opt_in_preserves_rpm_parallel_and_restart(self):
        reservation = self.store.reserve_llm(1, now=100)
        restarted = Store(self.store.path, minute_token_limit=60000)
        with self.assertRaises(Failure):
            restarted.reserve_llm(1, now=101)
        self.store.finish_llm(reservation, now=101)
        for now in (102, 103):
            reservation = restarted.reserve_llm(1, now=now)
            restarted.finish_llm(reservation, now=now)
        with self.assertRaises(Failure):
            restarted.reserve_llm(1, now=104)
        restarted.reserve_llm(1, now=162)

    def test_idempotent_bind_and_invalid_wait_bounds(self):
        self.assertEqual(self.owner, self.owners.bind(self.key, '1234', 'revision1', 'match', 101))
        for value in (float('nan'), float('inf'), -1, True, '61'):
            with self.assertRaises(Failure):
                self.owners.defer(self.owner, 'LLM_ADMISSION_DEFERRED', value, 100)
        with self.store.db() as c:
            c.execute('UPDATE workflow_wait_owner SET deadline=200 WHERE job_id=?', (self.key,))
        with self.assertRaises(Failure):
            self.owners.defer(self.owner, 'LLM_ADMISSION_DEFERRED', 61, 100)

    def test_minute_budget_not_context_bound(self):
        for now in (100, 101):
            reservation = self.store.reserve_llm(30000, now=now)
            self.store.finish_llm(reservation, now=now)
        with self.assertRaises(Failure) as error:
            self.store.reserve_llm(1, now=102)
        self.assertEqual(error.exception.code, 'LLM_ADMISSION_DEFERRED')
        with self.assertRaises(Failure) as error:
            self.store.reserve_llm(30001, now=200)
        self.assertEqual(error.exception.code, 'LLM_REQUEST_TOO_LARGE')
        with self.assertRaises(ValueError):
            Store(self.store.path, minute_token_limit=True)


if __name__ == '__main__':
    unittest.main()
