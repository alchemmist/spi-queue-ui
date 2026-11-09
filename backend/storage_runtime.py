"""Fail-closed dedicated file credentials; no ambient AWS credential discovery."""
import json
import os
import stat
from s3_evidence import S3Evidence

MAX_CREDENTIAL_BYTES = 8192


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('invalid credentials')
        result[key] = value
    return result


def load_storage():
    backend = os.environ.get('SPI_EVIDENCE_BACKEND', 'legacy')
    path = os.environ.get('SPI_S3_WRITER_FILE')
    try:
        if backend not in ('legacy', 's3'):
            raise ValueError()
        if path is None and backend == 'legacy':
            return None, backend
        if path != '/s3-writer/credentials.json':
            raise ValueError()
        # Follow projected Secret symlinks, then validate the opened inode (not
        # the symlink mode). Kubernetes root:fsGroup 0440 is intentional.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_mode & 0o007
                    or st.st_mode & 0o022 or st.st_uid not in (0, os.getuid())
                    or (st.st_mode & 0o040 and st.st_gid not in (os.getgid(), *os.getgroups()))
                    or st.st_size > MAX_CREDENTIAL_BYTES):
                raise ValueError()
            raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict) or set(value) != {'access', 'secret'}:
            raise ValueError()
        if any(not isinstance(v, str) or not 1 <= len(v) <= 2048
               or any(ord(c) < 33 or ord(c) > 126 for c in v) for v in value.values()):
            raise ValueError()
        return S3Evidence(value['access'], value['secret']), backend
    except Exception:
        raise RuntimeError('S3_WRITER_CONFIGURATION_INVALID') from None
