"""Fixed-origin immutable evidence storage; trusted caller injects credentials.
Transport test seam: (method, path, headers, payload, seconds) -> status, headers, bytes.
No job state is retained. Returned verification must be persisted by the caller.
"""
import datetime
import hashlib
import hmac
import http.client
import socket
import threading
import time
import re
import queue
import urllib.parse
from email.utils import parsedate_to_datetime
ORIGIN = 'http://rustfs.rustfs.svc.cluster.local:9000'
BUCKET = 'spi-evidence'
MAX_BYTES = 2 * 1024 * 1024
# Process-wide admission bound; timed-out callers do not release resolver slots.
MAX_RESOLVERS = 4
_RESOLVER_SLOTS = threading.BoundedSemaphore(MAX_RESOLVERS)
KEY = re.compile(r'spi/v1/[0-9a-f]{64}/[0-9a-f]{64}/(?:manifest\.json|E[0-9]{3}-[A-Za-z0-9._-]{1,100})\Z')
class StoragePermanent(Exception):
    def __init__(self): super().__init__('S3_PERMANENT')
class StorageConflict(StoragePermanent):
    def __init__(self): Exception.__init__(self, 'S3_CONFLICT')
class StorageTransient(Exception):
    def __init__(self, retry_after=None, *, reason="UNSPECIFIED"):
        self.retry_after = retry_after
        self.reason = reason if reason in {"UNSPECIFIED","DNS_RESOLUTION","TIMEOUT","HTTP_TRANSPORT","OPERATION_DEADLINE","HTTP_408","HTTP_429","HTTP_5XX","INCOMPLETE_READ","READBACK_NOT_READY"} else "UNSPECIFIED"
        super().__init__('S3_TRANSIENT')
def expire(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def watchdog(sock, seconds):
    timer = threading.Timer(seconds, expire, (sock,))
    timer.daemon = True
    timer.start()
    return timer


class DeadlineResponse(http.client.HTTPResponse):
    def close(self):
        try:
            super().close()
        finally:
            timer = getattr(self, 'deadline_timer', None)
            if timer is not None:
                timer.cancel()


class DeadlineConnection:
    response_class = DeadlineResponse

    def connect(self):
        start = time.monotonic()
        self.deadline_expired = False
        self.response_owns_deadline = False
        connected_socket = []
        def abort():
            self.deadline_expired = True
            sock = self.sock if self.sock is not None else (connected_socket[0] if connected_socket else None)
            if sock is not None:
                expire(sock)
        self.deadline_timer = threading.Timer(self.budget, abort)
        self.deadline_timer.daemon = True
        self.deadline_timer.start()
        try:
            super().connect()
            connected_socket.append(self.sock)
            if self.deadline_expired or time.monotonic() - start >= self.budget:
                expire(self.sock)
                raise TimeoutError('upstream deadline')
        except BaseException:
            self.close()
            raise

    def getresponse(self):
        # HTTPConnection closes/detaches its socket before returning a response
        # with Connection: close. The response's file still owns the descriptor:
        # keep its watchdog alive until that response is explicitly closed.
        self.transferring_response = True
        try:
            response = super().getresponse()
            response.deadline_timer = self.deadline_timer
            self.response_owns_deadline = True
            return response
        except BaseException:
            self.deadline_timer.cancel()
            raise
        finally:
            self.transferring_response = False

    def close(self):
        timer = getattr(self, 'deadline_timer', None)
        if timer and not (getattr(self, 'transferring_response', False) or
                          getattr(self, 'response_owns_deadline', False)):
            timer.cancel()
        super().close()


class HTTPConnection(DeadlineConnection, http.client.HTTPConnection):
    def connect(self):
        # Admission and resolution share one deadline. Only the resolver releases
        # capacity: a timed-out caller cannot admit more stuck native DNS calls.
        started = time.monotonic()
        deadline = started + min(self.timeout, self.budget)
        slots = _RESOLVER_SLOTS
        if not slots.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError('resolution capacity deadline')
        results = queue.Queue(maxsize=1)
        def resolve():
            try:
                try: results.put(socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM))
                except OSError: results.put(None)
            finally:
                slots.release()
        try:
            threading.Thread(target=resolve, daemon=True).start()
        except BaseException:
            slots.release()
            raise
        try: addresses = results.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty: raise TimeoutError('resolution deadline') from None
        if time.monotonic() >= deadline: raise TimeoutError('resolution deadline')
        if not addresses: raise socket.gaierror(socket.EAI_AGAIN,'resolution failed')
        self.budget -= time.monotonic() - started
        if self.budget <= 0: raise TimeoutError('resolution deadline')
        def create(address, timeout, source_address):
            # Conservative first-address only; caller retries transient failures.
            family, kind, proto, _, target = addresses[0]
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(min(timeout, self.budget))
                if source_address: sock.bind(source_address)
                sock.connect(target)
                return sock
            except BaseException:
                sock.close()
                raise
        self._create_connection = create
        super().connect()


def sign(method, url, access, secret, now=None, region='us-east-1', payload=b''):
    """AWS SigV4, unsigned query unsupported; canonical URI encoded exactly once."""
    u = urllib.parse.urlsplit(url)
    if u.query or u.fragment or u.username or u.password:
        raise ValueError('invalid upstream')
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime('%Y%m%dT%H%M%SZ')
    day = stamp[:8]
    digest = hashlib.sha256(payload).hexdigest()
    headers = {'host': u.netloc, 'x-amz-content-sha256': digest, 'x-amz-date': stamp}
    names = ';'.join(sorted(headers))
    canonical = '\n'.join([method, urllib.parse.quote(u.path, safe='/~'), '',
                           ''.join(k + ':' + headers[k] + '\n' for k in sorted(headers)), names, digest])
    scope = day + '/' + region + '/s3/aws4_request'
    message = 'AWS4-HMAC-SHA256\n' + stamp + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
    key = ('AWS4' + secret).encode()
    for part in (day, region, 's3', 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    headers['Authorization'] = 'AWS4-HMAC-SHA256 Credential=' + access + '/' + scope + ', SignedHeaders=' + names + ', Signature=' + signature
    return headers

def retry_after(value):
    try:
        if not isinstance(value, str) or len(value) > 128: return None
        seconds = int(value) if value.isascii() and value.isdigit() else (parsedate_to_datetime(value) - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return min(86400, max(0, seconds))
    except (ValueError, TypeError, OverflowError):
        return None

class S3Evidence:
    def __init__(self, access, secret, *, origin=ORIGIN, operation_seconds=30, transport=None):
        if origin != ORIGIN or not (type(operation_seconds) in (int, float) and 0 < operation_seconds <= 60):
            raise StoragePermanent()
        if any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9/+=_.-]{1,256}', x) for x in (access, secret)):
            raise StoragePermanent()
        self._access, self._secret = access, secret
        self._seconds, self._transport = operation_seconds, transport

    def _wire(self, method, path, headers, payload, remaining):
        conn = HTTPConnection('rustfs.rustfs.svc.cluster.local', 9000, timeout=min(5, remaining))
        conn.budget = remaining
        response = None
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            body = b''
            if response.status == 200 and method == 'GET':
                if response.getheader('Content-Encoding', 'identity') != 'identity': raise StoragePermanent()
                length = response.getheader('Content-Length')
                if length is not None and (not length.isascii() or not length.isdigit() or int(length) > MAX_BYTES): raise StoragePermanent()
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES: raise StoragePermanent()
                if length is not None and len(body) != int(length): raise StorageTransient()
            return response.status, dict(response.getheaders()), body
        finally:
            if response is not None: response.close()
            conn.close()

    def _request(self, method, key, payload, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise StorageTransient(reason="OPERATION_DEADLINE")
        path = '/' + BUCKET + '/' + key
        headers = sign(method, ORIGIN + path, self._access, self._secret, payload=payload)
        if method == 'PUT': headers['If-None-Match'] = '*'
        headers['Connection'] = 'close'
        try:
            result = (self._transport or self._wire)(method, path, headers, payload, remaining)
        except (OSError, http.client.HTTPException) as exc:
            reason="DNS_RESOLUTION" if isinstance(exc,socket.gaierror) else "TIMEOUT" if isinstance(exc,TimeoutError) else "HTTP_TRANSPORT"
            raise StorageTransient(reason=reason) from None
        if time.monotonic() >= deadline: raise StorageTransient(reason="OPERATION_DEADLINE")
        status, headers, body = result
        if not isinstance(body, bytes) or len(body) > MAX_BYTES: raise StoragePermanent()
        if status in (408, 429) or 500 <= status <= 599:
            raise StorageTransient(retry_after(next((v for k,v in headers.items() if k.lower() == 'retry-after'), None)),reason='HTTP_5XX' if status>=500 else 'HTTP_'+str(status))
        if status not in (200, 201, 204, 404, 409, 412): raise StoragePermanent()
        return status, body

    def ensure_object(self, key, exact_bytes):
        if not isinstance(key, str) or not KEY.fullmatch(key) or '..' in key or type(exact_bytes) is not bytes or len(exact_bytes) > MAX_BYTES:
            raise StoragePermanent()
        deadline = time.monotonic() + self._seconds
        digest = hashlib.sha256(exact_bytes).hexdigest()
        def verify(status, body):
            if status != 200: raise StorageTransient(reason="READBACK_NOT_READY")
            if len(body) != len(exact_bytes) or hashlib.sha256(body).hexdigest() != digest: raise StorageConflict()
            return {'bucket': BUCKET, 'key': key, 'sha256': digest, 'size': len(body), 'verified': True}
        status, body = self._request('GET', key, b'', deadline)
        if status == 200: return verify(status, body)
        if status != 404: raise StoragePermanent()
        try:
            status, _ = self._request('PUT', key, exact_bytes, deadline)
            if status not in (200, 201, 204, 409, 412): raise StoragePermanent()
        except StorageTransient:
            return verify(*self._request('GET', key, b'', deadline))
        return verify(*self._request('GET', key, b'', deadline))
