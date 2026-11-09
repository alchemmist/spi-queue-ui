"""Standalone graph gateway role. No SPI queue/model/S3 credentials required."""
import hashlib
import hmac
import http.server
import json
import os
import pathlib
import socket
import stat
import threading

from graph_contract import GraphError, canonical, _object, parse_annotation, resolve
from graph_renderer import Renderer, MAX_REQUEST_BYTES


def secret(path):
    with open(path, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027
                or info.st_uid not in (0, os.getuid()) or info.st_size > 4096):
            raise ValueError('GRAPH_CONFIGURATION_INVALID')
        value = stream.read(4097).decode('ascii').strip()
    if not 16 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError('GRAPH_CONFIGURATION_INVALID')
    return value


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, renderer, tokens):
        if set(tokens) != {'telegram', 'spi'} or tokens['telegram'] == tokens['spi']:
            raise ValueError('GRAPH_CONFIGURATION_INVALID')
        self.renderer = renderer
        self.identities = {k: hashlib.sha256(v.encode()).digest() for k, v in tokens.items()}
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # No request/credential-bearing traceback logging.


def expire(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass

    def handle(self):
        self.connection.settimeout(5)
        timer = threading.Timer(25, expire, (self.connection,))
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        finally:
            timer.cancel()

    def reply(self, status, data, mime='application/json', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        if self.path == '/healthz':
            self.reply(200, b'{"status":"ok"}')
        else:
            self.reply(404, b'{"code":"NOT_FOUND"}')

    def do_POST(self):
        if self.path not in ('/v1/render', '/v1/primary'):
            self.reply(404, b'{"code":"NOT_FOUND"}'); return
        auths = self.headers.get_all('Authorization') or []
        auth = auths[0] if len(auths) == 1 else ''
        if not auth.startswith('Bearer ') or len(auth) > 4103:
            self.reply(401, b'{"code":"UNAUTHORIZED"}'); return
        digest = hashlib.sha256(auth[7:].encode()).digest()
        caller = None
        for name, expected in self.server.identities.items():
            if hmac.compare_digest(digest, expected):
                caller = name
        if caller is None:
            self.reply(401, b'{"code":"UNAUTHORIZED"}'); return
        lengths = self.headers.get_all('Content-Length') or []
        try:
            if (self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1
                    or not lengths[0].isascii() or not lengths[0].isdigit()
                    or not 0 < int(lengths[0]) <= MAX_REQUEST_BYTES
                    or self.headers.get_content_type() != 'application/json'):
                raise GraphError()
            data = self.rfile.read(int(lengths[0]))
            if len(data) != int(lengths[0]):
                raise GraphError()
            request = json.loads(data, object_pairs_hook=_object)
            if self.path == '/v1/primary':
                if not isinstance(request,dict) or set(request)!={'annotation','labels','starts_at','ends_at','status','observed_at'}:
                    raise GraphError()
                specs=parse_annotation(request['annotation'])
                if not specs:raise GraphError('MISSING_GRAPH')
                request=resolve(specs[0],request['labels'],starts_at=request['starts_at'],
                    observed_at=request['observed_at'],ends_at=request['ends_at'],status=request['status'],purpose='telegram')
            result, meta = self.server.renderer.render(request, caller=caller,
                                                       seconds=4 if caller == 'telegram' else 20)
            metadata = json.loads(meta)
            self.reply(200, result, 'image/png', {
                'X-Content-SHA256': metadata['sha256'],
                'X-Render-Digest': metadata['render_digest'],
                'X-Graph-From': str(metadata['from_ms']), 'X-Graph-To': str(metadata['to_ms']),
                'X-Graph-Mode': metadata['mode']})
        except (GraphError, ValueError, RecursionError, UnicodeError) as exc:
            code = exc.code if isinstance(exc, GraphError) else 'INVALID_SPEC'
            status = 503 if code in ('BUSY', 'RENDER_TIMEOUT', 'RENDER_FAILED') else 422
            self.reply(status, canonical({'code': code}).encode('ascii'))


def main():
    # Fixed mounts: no ambient credentials and no caller-selected policy/network origin.
    policy_path = pathlib.Path('/graph-policy/panels.json')
    if policy_path.stat().st_size > 65536:
        raise ValueError('GRAPH_CONFIGURATION_INVALID')
    policy = json.loads(policy_path.read_bytes(), object_pairs_hook=_object)
    tokens = {name: secret('/graph-secrets/' + name) for name in ('telegram', 'spi')}
    renderer = Renderer(secret('/graph-secrets/grafana'), policy)
    Server(('0.0.0.0', 8080), renderer, tokens).serve_forever()


if __name__ == '__main__':
    main()
