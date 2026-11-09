"""Pure offline regression suite; fixtures are not production executions."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('failure_policy', ROOT / 'scripts/n8n/check_failure_alerts.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'version': 1, 'handler_id': 'handler1', 'handler_name': 'Failure Handler',
                       'handler_template': None, 'workflow_roots': ['workflows']}
        self.put(policy.POLICY, self.config)
        (self.root / 'workflows').mkdir()

    def put(self, path, data):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2)+'\n')
        return p

    def workflow(self, **settings):
        return {'name': 'Example', 'nodes': [{'name': 'Run', 'type': 'n8n-nodes-base.code',
                 'parameters': {'jsCode': 'return items;'}, 'credentials': {'native': {'id': 'reference-only'}}}],
                'connections': {}, 'settings': {'executionTimeout': 60, **settings}}

    def run_policy(self, fix=False):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return policy.run(self.root, fix)

    def test_check_fails_without_writing_and_fix_only_adds_setting(self):
        w = self.workflow(); p = self.put('workflows/new.json', w); before = p.read_bytes()
        self.assertEqual(self.run_policy(), 1); self.assertEqual(p.read_bytes(), before)
        self.assertEqual(self.run_policy(True), 0)
        expected = copy.deepcopy(w); expected['settings']['errorWorkflow'] = 'handler1'
        self.assertEqual(json.loads(p.read_text()), expected)
        fixed = p.read_bytes(); self.assertEqual(self.run_policy(True), 0); self.assertEqual(p.read_bytes(), fixed)

    def test_missing_settings_can_be_generated(self):
        w = self.workflow(); del w['settings']; p = self.put('workflows/new.json', w)
        self.assertEqual(self.run_policy(True), 0)
        self.assertEqual(json.loads(p.read_text())['settings'], {'errorWorkflow': 'handler1'})

    def test_discovery_includes_new_nested_files(self):
        self.put('workflows/existing.json', self.workflow(errorWorkflow='handler1'))
        self.assertEqual(self.run_policy(), 0)
        self.put('workflows/new/path/workflow.json', self.workflow())
        self.assertEqual(self.run_policy(), 1)

    def test_other_handler_conflict_is_not_overwritten_or_partially_fixed(self):
        missing = self.put('workflows/missing.json', self.workflow())
        other = self.put('workflows/other.json', self.workflow(errorWorkflow='another'))
        before = (missing.read_bytes(), other.read_bytes())
        self.assertEqual(self.run_policy(True), 1)
        self.assertEqual((missing.read_bytes(), other.read_bytes()), before)

    def test_array_exports(self):
        p = self.put('workflows/export.json', [self.workflow(), self.workflow(errorWorkflow='handler1')])
        self.assertEqual(self.run_policy(True), 0)
        self.assertTrue(all(w['settings']['errorWorkflow'] == 'handler1' for w in json.loads(p.read_text())))

    def handler(self):
        self.config['handler_template'] = 'workflows/handler.json'; self.put(policy.POLICY, self.config)
        w = self.workflow(); w['name'] = 'Failure Handler'; w['nodes'][0]['type'] = 'n8n-nodes-base.errorTrigger'
        return self.put('workflows/handler.json', w)

    def test_only_explicit_handler_is_exempt(self):
        self.handler(); self.assertEqual(self.run_policy(), 0)
        w = self.workflow(); w['name'] = 'Failure Handler'; w['nodes'][0]['type'] = 'n8n-nodes-base.errorTrigger'
        self.put('workflows/other.json', w); self.assertEqual(self.run_policy(), 1)

    def test_handler_recursion_is_rejected_not_removed_by_fix(self):
        p = self.handler(); w = json.loads(p.read_text()); w['settings']['errorWorkflow'] = 'handler1'; self.put('workflows/handler.json', w)
        self.assertEqual(self.run_policy(True), 1)
        self.assertEqual(json.loads(p.read_text())['settings']['errorWorkflow'], 'handler1')

    def test_handler_identity_and_presence_fail_closed(self):
        p = self.handler(); w = json.loads(p.read_text()); w['nodes'] = []; self.put('workflows/handler.json', w)
        self.assertEqual(self.run_policy(), 1)
        p.unlink(); self.assertEqual(self.run_policy(), 1)

    def test_duplicate_keys_and_invalid_json_fail_closed(self):
        p = self.put('workflows/bad.json', self.workflow()); p.write_text('{"name":"one","name":"two"}')
        self.assertEqual(self.run_policy(True), 1)
        p.write_text('{invalid'); self.assertEqual(self.run_policy(True), 1)

    def test_bad_settings_or_missing_connections_is_not_silently_skipped(self):
        w = self.workflow(); w['settings'] = None; self.put('workflows/bad.json', w)
        self.assertEqual(self.run_policy(True), 1)
        w = self.workflow(); del w['connections']; self.put('workflows/bad.json', w)
        self.assertEqual(self.run_policy(True), 1)

    def test_symlink_and_path_traversal_rejected(self):
        self.put('workflows/valid.json', self.workflow(errorWorkflow='handler1'))
        (self.root/'workflows/link.json').symlink_to(self.root/policy.POLICY)
        with self.assertRaises(ValueError): self.run_policy(True)
        (self.root/'workflows/link.json').unlink()
        self.config['workflow_roots'] = ['../outside']; self.put(policy.POLICY, self.config)
        with self.assertRaises(ValueError): self.run_policy(True)

    def test_symlinked_subdirectory_cannot_hide_unchecked_templates(self):
        self.put('workflows/valid.json', self.workflow(errorWorkflow='handler1'))
        self.put('elsewhere/hidden.json', self.workflow())
        (self.root/'workflows/hidden').symlink_to(self.root/'elsewhere', target_is_directory=True)
        with self.assertRaises(ValueError): self.run_policy(True)

    def test_empty_discovery_is_not_a_green_check(self):
        self.assertEqual(self.run_policy(), 1)

    def test_actual_repository_policy(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(policy.run(ROOT), 0)


if __name__ == '__main__':
    unittest.main()
