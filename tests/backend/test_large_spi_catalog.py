"""Catalog-size regressions: mock HTTP/model, no production inference."""
import copy
import tempfile
import time
import unittest
from unittest.mock import Mock
import test_triage as f
from integrations import GitHub, Model
from core import Failure

GOOD = {'issue_number': 0, 'reason': 'No shared cause established.', 'confidence': 'low'}

class LargeCatalogTests(unittest.TestCase):
    @staticmethod
    def issues(n):
        return [{'number': i + 1, 'title': '[SPI] fixture', 'body': 'bounded fixture',
                 'updated_at': '2026-11-08T00:00:00Z'} for i in range(n)]

    def test_121_open_issues_can_be_fingerprinted_without_model_review(self):
        g = GitHub(Mock(), 'unused')
        rows = self.issues(121)
        g.pages = Mock(side_effect=lambda path: iter(copy.deepcopy(rows)))
        actual = g.issues()
        self.assertEqual(len(actual), 121)
        self.assertEqual(g.open_signature(), f.digest([(x['number'], x['updated_at']) for x in rows]))
        g.http.request.assert_not_called()

    def test_all_241_issues_reviewed_with_unchanged_per_request_bounds(self):
        m = Model(Mock(), 'test-only', store=Mock())
        seen = []
        def call(task, data, schema, deadline):
            batch = data['open_issues']
            self.assertLessEqual(len(batch), 12)
            self.assertLessEqual(m.budget_reservation(m.payload(task, data, schema)), 30000)
            self.assertLessEqual(deadline - time.monotonic(), 1800)
            seen.extend(x['number'] for x in batch)
            return dict(GOOD)
        m.call = call
        result = m.match(f.event(), self.issues(241))
        self.assertEqual(seen, list(range(1, 242)))
        self.assertEqual(result['reviewed_numbers'], seen)
        self.assertEqual(result['candidates'], [])
        m.http.request.assert_not_called()

    def test_admission_resume_reuses_existing_v3_paid_batch_receipts(self):
        m = Model(Mock(), 'test-only', store=Mock())
        rows = self.issues(121)
        saved = []
        calls = []
        def first(task, data, schema, deadline):
            calls.append([x['number'] for x in data['open_issues']])
            if len(calls) == 3:
                raise Failure('LLM_ADMISSION_DEFERRED', 61)
            return dict(GOOD)
        m.call = first
        with self.assertRaisesRegex(Failure, 'LLM_ADMISSION_DEFERRED'):
            m.match(f.event(), rows, on_progress=lambda p: saved.append(copy.deepcopy(p)))
        self.assertEqual(saved[-1]['version'], 3)
        self.assertEqual(len(saved[-1]['reviews']), 2)
        completed = [x for batch in calls[:2] for x in batch]
        resumed = []
        def second(task, data, schema, deadline):
            resumed.extend(x['number'] for x in data['open_issues'])
            return dict(GOOD)
        m.call = second
        result = m.match(f.event(), rows, progress=saved[-1])
        self.assertEqual(result['reviewed_numbers'], list(range(1, 122)))
        self.assertFalse(set(completed) & set(resumed))
        self.assertEqual(completed + resumed, list(range(1, 122)))

if __name__ == '__main__':
    unittest.main()
