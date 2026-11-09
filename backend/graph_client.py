"""Bounded internal graph gateway client; no arbitrary URL or ambient credentials."""
import hashlib
import hmac
import http.client
import json
import re
import time

from graph_contract import GraphError, MAX_PNG_BYTES, canonical, render_path, validate_png
from s3_evidence import HTTPConnection

GATEWAY_HOST = 'socrat-alert-graphs.cattle-monitoring-system.svc.cluster.local'


class GraphClient:
    def __init__(self, token, *, transport=None):
        if not isinstance(token, str) or not 16 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError('GRAPH_CONFIGURATION_INVALID')
        self._token, self._transport = token, transport

    def _wire(self, request, seconds):
        conn = HTTPConnection(GATEWAY_HOST, 8080, timeout=seconds)
        conn.budget = seconds
        response = None
        try:
            conn.request('POST', '/v1/render', body=canonical(request).encode('ascii'), headers={
                'Authorization': 'Bearer ' + self._token,
                'Content-Type': 'application/json', 'Accept': 'image/png', 'Connection': 'close'})
            response = conn.getresponse()
            body = response.read(MAX_PNG_BYTES + 1)
            return response.status, {k.lower(): v for k, v in response.getheaders()}, body
        finally:
            if response is not None:
                response.close()
            conn.close()

    def render(self, request, *, seconds=20):
        if type(seconds) not in (int, float) or not 0 < seconds <= 20:
            raise GraphError()
        if not isinstance(request, dict) or not isinstance(request.get('digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', request['digest']):
            raise GraphError()
        render_path(request)
        started = time.monotonic()
        try:
            status, headers, body = (self._transport or self._wire)(request, seconds)
        except TimeoutError:
            raise GraphError('RENDER_TIMEOUT') from None
        except (OSError, http.client.HTTPException):
            raise GraphError('RENDER_FAILED') from None
        if time.monotonic() - started > seconds:
            raise GraphError('RENDER_TIMEOUT')
        if status != 200:
            code = 'RENDER_FAILED'
            if len(body) <= 4096:
                try:
                    candidate = json.loads(body).get('code')
                    if candidate in ('BUSY', 'RENDER_TIMEOUT', 'MISSING_LABEL', 'PANEL_NOT_ALLOWED', 'INVALID_SPEC'):
                        code = candidate
                except (ValueError, AttributeError, TypeError):
                    pass
            raise GraphError(code)
        if headers.get('content-type', '').split(';')[0].strip().lower() != 'image/png':
            raise GraphError('RENDER_FAILED')
        meta = validate_png(body)
        checksum = headers.get('x-content-sha256', '')
        digest = headers.get('x-render-digest', '')
        if (not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum)
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or not hmac.compare_digest(checksum, meta['sha256'])
                or not hmac.compare_digest(digest, request['digest'])):
            raise GraphError('IMAGE_INTEGRITY')
        meta.update({'render_digest': request['digest'], 'from_ms': request['from_ms'],
                     'to_ms': request['to_ms'], 'mode': request['mode']})
        return body, meta
