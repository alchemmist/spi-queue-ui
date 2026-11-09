"""Checkpointed binary graph evidence. PNG bytes stay on the existing private PVC.

Only small, content-addressed references enter job JSON. No base64 or image bytes
are sent to the model or stored in its context. Existing S3 publication performs
its ordinary immutable PUT/GET verification on the reconstructed exact bytes.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from graph_contract import GraphError, canonical, parse_annotation, resolve, validate_png

ARTIFACT_ROOT = Path('/data/graphs')
MIN_FREE_BYTES = 256 * 1024 * 1024
HEX = re.compile(r'[0-9a-f]{64}\Z')


def _directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GraphError('GRAPH_CHECKPOINT_INVALID')
    return path


def _read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise GraphError('GRAPH_CHECKPOINT_INVALID')
        result = stream.read(limit+1)
    if len(result) > limit:
        raise GraphError('GRAPH_CHECKPOINT_INVALID')
    return result


def _immutable(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.capture-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
            try: os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                if _read(path, len(data)) != data:
                    raise GraphError('GRAPH_CHECKPOINT_CONFLICT')
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def read_artifact(ref):
    if (not isinstance(ref, dict) or set(ref) != {'event_key', 'render_digest'}
            or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in ref.values())):
        raise GraphError('GRAPH_CHECKPOINT_INVALID')
    directory = _directory(_directory(ARTIFACT_ROOT)/ref['event_key'])
    data = _read(directory/(ref['render_digest']+'.png'), 2*1024*1024)
    validate_png(data)
    return data


def collect(event, client, *, deadline=None):
    specs = parse_annotation(event.get('annotations', {}).get('spi_graphs'))
    if not specs:
        return [], []
    event_key = event.get('event_key')
    if not isinstance(event_key, str) or not HEX.fullmatch(event_key):
        raise GraphError('GRAPH_CHECKPOINT_INVALID')
    directory = _directory(_directory(ARTIFACT_ROOT)/event_key)
    capture = directory/'capture.json'
    if capture.exists():
        state = json.loads(_read(capture, 65536))
        if state.get('specs') != specs:
            raise GraphError('GRAPH_CHECKPOINT_CONFLICT')
        observed = state['observed_at']
    else:
        observed = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _immutable(capture, canonical({'specs': specs, 'observed_at': observed}).encode('ascii'))
    artifacts, missing = [], []
    for spec in specs:
        try:
            remaining = 20 if deadline is None else min(20, deadline-time.monotonic())
            request = resolve(spec, event['labels'], starts_at=event['startsAt'], observed_at=observed,
                              ends_at=event.get('endsAt'), status=event['status'], purpose='spi')
            path = directory/(request['digest']+'.png')
            if path.exists():
                data = _read(path, 2*1024*1024)
                meta = validate_png(data)
            else:
                if remaining <= 0:raise GraphError('GRAPH_CAPTURE_DEFERRED')
                data, meta = client.render(request, seconds=remaining)
                validate_png(data)
                # A bounded private spool must not exhaust the queue PVC. Never delete
                # checkpointed historical files to make room for a new screenshot.
                used = sum(p.stat().st_size for p in ARTIFACT_ROOT.glob('*/*.png') if not p.is_symlink())
                if used + len(data) > 512*1024*1024:
                    raise GraphError('GRAPH_STORAGE_BUDGET')
                if os.statvfs(directory).f_bavail*os.statvfs(directory).f_frsize < len(data)+MIN_FREE_BYTES:
                    raise GraphError('GRAPH_STORAGE_BUDGET')
                _immutable(path, data)
            source = {'kind': 'grafana_panel_png', 'graph_id': spec['id'], 'title': spec['title'],
                      'dashboard_uid': spec['dashboard_uid'], 'panel_id': spec['panel_id'],
                      'variables': request['variables'], 'from_ms': request['from_ms'],
                      'to_ms': request['to_ms'], 'observed_at': observed,
                      'collected_at': datetime.datetime.fromtimestamp(path.stat().st_mtime,datetime.timezone.utc).isoformat(), 'mode': request['mode'],
                      'render_digest': request['digest']}
            artifacts.append({'name': 'graph-'+spec['id']+'.png', 'source': source,
                              'sha256': meta['sha256'], 'stored_bytes': len(data),
                              'reference': {'event_key': event_key, 'render_digest': request['digest']}})
        except (GraphError, OSError, ValueError, KeyError) as exc:
            if isinstance(exc,GraphError) and exc.code=='GRAPH_CAPTURE_DEFERRED':raise
            missing.append({'source': 'graph:'+spec['id'], 'reason': exc.code if isinstance(exc, GraphError) else 'GRAPH_CAPTURE_FAILED'})
    return artifacts, missing


def attach(bundle, artifacts, missing=()):
    """Upgrade storage manifest only. Text/model context and legacy parts remain intact."""
    if not artifacts and not missing:
        return bundle
    import copy
    bundle = copy.deepcopy(bundle)
    # Deepcopy preserves internal aliases: detach the model's index deliberately.
    bundle['model_context'] = copy.deepcopy(bundle['model_context'])
    manifest = bundle['manifest']
    manifest['missing'] += list(missing)
    if not artifacts:
        return bundle
    manifest['format'] = 'socrat-evidence-v3'
    for row in manifest['artifacts']:
        row['stored_bytes'] = row['stored_chars']
    refs = {}
    for item in artifacts:
        aid = 'E'+str(len(manifest['artifacts'])+1).zfill(3)
        raw = read_artifact(item['reference'])
        if len(raw) != item['stored_bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise GraphError('GRAPH_CHECKPOINT_INVALID')
        manifest['artifacts'].append({'id': aid, 'name': item['name'], 'source': item['source'],
            'sha256': item['sha256'], 'stored_bytes': len(raw), 'mime_type': 'image/png',
            'truncated': False, 'redaction_applied': False,
            'redaction_note': 'Grafana PNG is not text-redacted; same private evidence authorization applies.'})
        refs[aid] = item['reference']
    bundle['binary_refs'] = refs
    return bundle
