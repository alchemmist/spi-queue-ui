#!/usr/bin/env python3
"""Offline workflow policy. --fix edits local JSON only; never contacts n8n."""
import argparse
import copy
import json
import re
from pathlib import Path
import sys

POLICY = 'n8n-workflow-policy.json'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def load(path):
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)


def inside(root, name):
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('policy paths must be repository-relative')
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('symlinks and paths outside the repository are forbidden')
    return path


def run(root, fix=False):
    root = root.resolve()
    policy = load(inside(root, POLICY))
    if policy.get('version') != 1 or not policy.get('handler_id') or not policy.get('workflow_roots'):
        raise ValueError('invalid workflow policy')
    target = policy['handler_id']
    if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', target):
        raise ValueError('invalid handler identity')
    handler = policy.get('handler_template')
    if handler:
        inside(root, handler)
    files = set()
    for name in policy['workflow_roots']:
        directory = inside(root, name)
        if not directory.is_dir():
            raise ValueError('configured workflow root is missing')
        for path in directory.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('symlinked workflow JSON is forbidden')
            if path.is_file() and path.suffix == '.json':
                files.add(path)
    errors, repairs, checked, handler_seen = [], [], 0, False
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        try:
            document = load(path)
        except (ValueError, UnicodeError):
            errors.append(f'{relative}: invalid/duplicate-key JSON; repair explicitly')
            continue
        original = copy.deepcopy(document)
        candidates = document if isinstance(document, list) else [document]
        for index, w in enumerate(candidates):
            if not isinstance(w, dict) or not {'name', 'nodes'} <= w.keys():
                continue
            if 'connections' not in w:
                errors.append(f'{relative}: workflow is missing connections')
                continue
            checked += 1
            label = relative + (f'#{index}' if isinstance(document, list) else '')
            settings = w.get('settings', {})
            if not isinstance(w['nodes'], list) or not isinstance(w['connections'], dict) or not isinstance(settings, dict):
                errors.append(f'{label}: invalid workflow shape; repair explicitly')
                continue
            if relative == handler:
                handler_seen = True
                if (w['name'] != policy['handler_name'] or isinstance(document, list)
                        or not any(isinstance(n, dict) and n.get('type') == 'n8n-nodes-base.errorTrigger' for n in w['nodes'])):
                    errors.append(f'{label}: handler identity/trigger mismatch')
                if settings.get('errorWorkflow'):
                    errors.append(f'{label}: error handler must not have an error target (recursion)')
                continue
            existing = settings.get('errorWorkflow')
            if existing == target:
                continue
            if existing not in (None, ''):
                errors.append(f'{label}: different error handler; refusing to overwrite')
                continue
            if fix:
                w.setdefault('settings', {})['errorWorkflow'] = target
            else:
                errors.append(f'{label}: missing settings.errorWorkflow; run python scripts/n8n/check_failure_alerts.py --fix')
        if document != original:
            repairs.append((path, document))
    if handler and not handler_seen:
        errors.append('configured handler template is missing or is not a workflow')
    if not checked:
        errors.append('no workflow templates discovered; check policy roots')
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        if fix:
            print('No files changed: resolve conflicts before --fix.', file=sys.stderr)
        return 1
    for path, document in repairs:
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'Fixed {path.relative_to(root)}')
    print(f'Workflow failure policy OK: {checked} templates, {len(repairs)} files fixed; no live API calls.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fix', action='store_true', help='Fill missing local settings; never overwrite a different handler')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        return run(args.root, args.fix)
    except (ValueError, OSError):
        print('Workflow policy/configuration error; no automatic repair. Check paths and JSON.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
