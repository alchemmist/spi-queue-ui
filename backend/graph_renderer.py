"""Credential-isolated fixed-origin Grafana PNG renderer, shared by Telegram and SPI.

This module does not import service.py or open the SPI database. A gateway process
owns its own Grafana credential, policy and bounded cache, never worker secrets.
"""
from collections import OrderedDict
import hashlib
import hmac
import re
import http.client
import threading
import time

from graph_contract import (GraphError, MAX_PNG_BYTES, canonical, render_path,
                            validate_png)
from s3_evidence import HTTPConnection

GRAFANA_HOST = 'kube-prometheus-stack-grafana.cattle-monitoring-system.svc.cluster.local'
MAX_REQUEST_BYTES = 32768


class Renderer:
    def __init__(self, token, policy, *, transport=None, clock=time.monotonic,
                 cache_bytes=16 * 1024 * 1024):
        # Policy is generated from Git-owned graph inventory, not alert input.
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError('GRAPH_CONFIGURATION_INVALID')
        if not isinstance(policy, dict) or not policy:
            raise ValueError('GRAPH_CONFIGURATION_INVALID')
        for key, names in policy.items():
            if not isinstance(key, str) or not isinstance(names, list) or any(not isinstance(n, str) for n in names):
                raise ValueError('GRAPH_CONFIGURATION_INVALID')
        if type(cache_bytes) is not int or not MAX_PNG_BYTES <= cache_bytes <= 64 * 1024 * 1024:
            raise ValueError('GRAPH_CONFIGURATION_INVALID')
        self._token, self._policy, self._transport = token, policy, transport
        self._clock, self._cache_limit = clock, cache_bytes
        self._cache, self._bytes = OrderedDict(), 0
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(2)
        self._spi = threading.BoundedSemaphore(1)
        self._inflight = {}

    def _wire(self, path, seconds):
        connection = HTTPConnection(GRAFANA_HOST, 80, timeout=seconds)
        connection.budget = seconds
        response = None
        try:
            connection.request('GET', path, headers={
                'Authorization': 'Bearer ' + self._token,
                'Accept': 'image/png', 'Connection': 'close'})
            response = connection.getresponse()
            body = response.read(MAX_PNG_BYTES + 1)
            return response.status, response.getheader('Content-Type', ''), body
        finally:
            if response is not None:
                response.close()
            connection.close()

    def _cached(self, digest):
        item = self._cache.get(digest)
        if item and self._clock() - item[0] >= 3600:
            self._bytes -= len(item[1][0]); del self._cache[digest]
            item = None
        if item:
            self._cache.move_to_end(digest)
            return item[1]
        return None

    def render(self, request, *, caller, seconds):
        if caller not in ('telegram', 'spi') or type(seconds) not in (int, float) or not 0 < seconds <= 20:
            raise GraphError()
        if not isinstance(request, dict) or len(canonical(request)) > MAX_REQUEST_BYTES:
            raise GraphError()
        if set(request) != {'version', 'dashboard_uid', 'panel_id', 'variables', 'from_ms',
                            'to_ms', 'width', 'height', 'theme', 'mode', 'digest'}:
            raise GraphError()
        if type(request['version']) is not int or request['version'] != 1 or request['mode'] not in ('onset', 'ongoing', 'resolved'):
            raise GraphError()
        digest = request['digest']
        expected = hashlib.sha256(canonical({k: v for k, v in request.items() if k != 'digest'}).encode('ascii')).hexdigest()
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or not hmac.compare_digest(digest, expected):
            raise GraphError()
        path = render_path(request)
        key = request['dashboard_uid'] + ':' + str(request['panel_id'])
        if key not in self._policy or sorted(request['variables']) != sorted(self._policy[key]):
            raise GraphError('PANEL_NOT_ALLOWED')
        deadline = self._clock() + seconds
        with self._lock:
            cached = self._cached(digest)
            if cached:
                return cached
            existing = self._inflight.get(digest)
            if existing is None:
                existing = threading.Event()
                self._inflight[digest] = existing
                leader = True
            else:
                leader = False
        if not leader:
            if not existing.wait(max(0, deadline - self._clock())):
                raise GraphError('RENDER_TIMEOUT')
            with self._lock:
                cached = self._cached(digest)
            if cached:
                return cached
            raise GraphError('RENDER_FAILED')
        spi_slot = slot = False
        try:
            if caller == 'spi':
                spi_slot = self._spi.acquire(blocking=False)
                if not spi_slot:
                    raise GraphError('BUSY')
            slot = self._slots.acquire(blocking=False)
            if not slot:
                raise GraphError('BUSY')
            try:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError()
                status, mime, body = (self._transport or self._wire)(path, remaining)
                if self._clock() > deadline:
                    raise TimeoutError()
            except TimeoutError:
                raise GraphError('RENDER_TIMEOUT') from None
            except (OSError, ValueError, http.client.HTTPException):
                raise GraphError('RENDER_FAILED') from None
            if status != 200 or mime.split(';')[0].strip().lower() != 'image/png':
                raise GraphError('RENDER_FAILED')
            metadata = validate_png(body)
            metadata.update({'render_digest': digest, 'from_ms': request['from_ms'],
                             'to_ms': request['to_ms'], 'mode': request['mode']})
            # Store immutable serialized metadata; callers receive an independent copy.
            result = (body, canonical(metadata))
            with self._lock:
                self._cache[digest] = (self._clock(), result)
                self._bytes += len(body)
                while self._bytes > self._cache_limit:
                    _, (_, (old, _)) = self._cache.popitem(last=False)
                    self._bytes -= len(old)
            return result
        finally:
            if slot:
                self._slots.release()
            if spi_slot:
                self._spi.release()
            with self._lock:
                self._inflight.pop(digest, None)
                existing.set()
