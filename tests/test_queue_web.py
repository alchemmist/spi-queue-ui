"""Local-only HTTP/Helm checks; never calls live cluster or inference."""
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'frontend'))
spec = importlib.util.spec_from_file_location('queue_web', ROOT / 'frontend/queue_web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)
RealConnection = http.client.HTTPConnection
RealAuthorization = web.Handler.authorization

class QueueWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth_patch = patch.object(web.Handler, 'authorization', return_value=200)
        cls.auth_patch.start()
        cls.server = web.Server(('127.0.0.1', 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
        cls.auth_patch.stop()
    def request(self, method='GET', path='/', body=None, extra=None):
        headers = {'Host': 'spi.socrat.alchemmist.xyz', 'X-authentik-groups': 'homelab-owner'}
        headers.update(extra or {})
        c = RealConnection('127.0.0.1', self.server.server_port, timeout=3)
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse(); result = (r.status, dict(r.headers), r.read()); c.close()
        return result
    def test_static_csp_and_no_framework(self):
        code, headers, body = self.request()
        self.assertEqual(code, 200)
        self.assertIn('private, no-store', headers['Cache-Control'])
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        self.assertNotIn("'unsafe-inline'", headers['Content-Security-Policy'])
        self.assertNotIn(b'https://', body)
        self.assertIn(b'href="/style.css"', body)
        self.assertIn(b'src="/app.js"', body)
        self.assertNotIn(b'/queue/', body)
        self.assertNotIn('Location', headers)
        self.assertEqual(self.request(path='/app.js')[0], 200)
        self.assertEqual(self.request(path='/style.css')[0], 200)
        js = (ROOT / 'frontend/app.js').read_text()
        self.assertNotIn('innerHTML', js)
        self.assertIn('textContent', js)
        self.assertIn('expected_updated', js)
    def test_host_group_and_arbitrary_path_denied(self):
        self.assertEqual(self.request(extra={'Host':'evil.invalid'})[0], 400)
        self.assertEqual(self.request(path='/queue/../../service.py')[0], 404)
        self.assertEqual(self.request(path='/queue/app.js?x=1')[0], 404)
        self.assertEqual(self.request(path='/queue')[0], 308)
        self.assertEqual(self.request(path='/queue/')[1]['Location'], '/')
        self.assertEqual(self.request(path='/queue/app.js')[1]['Location'], '/app.js')
    def test_anonymous_challenge_returns_exact_queue_and_does_not_trust_identity_header(self):
        with patch.object(web.Handler, 'authorization', return_value=401):
            code, headers, _ = self.request(extra={'X-authentik-groups':'homelab-owner'})
        self.assertEqual(code, 302)
        self.assertEqual(headers['Location'], '/login')

    def test_csrf_rejected_before_upstream(self):
        for extra in ({}, {'Origin':'https://evil.invalid', 'X-SPI-Queue':'1'},
                      {'Origin':web.ORIGIN, 'X-SPI-Queue':'1', 'Sec-Fetch-Site':'cross-site'}):
            with patch.object(web.http.client, 'HTTPConnection') as upstream:
                self.assertEqual(self.request('POST', '/api/cancel', '{}', extra)[0], 403)
                upstream.assert_not_called()
    def test_fixed_upstream_and_no_cookie_identity_or_worker_header_forwarding(self):
        response = Mock(status=200)
        response.read.return_value = b'{"items":[],"counts":{}}'
        connection = Mock(); connection.getresponse.return_value = response
        headers = {'Origin':web.ORIGIN, 'X-SPI-Queue':'1', 'Content-Type':'application/json',
                   'Cookie':'opaque-test', 'X-Worker-Token':'must-not-forward'}
        with patch.object(web, 'TOKEN', 'local-fixture-token'), patch.object(web.http.client, 'HTTPConnection', return_value=connection) as factory:
            code, _, body = self.request('POST', '/api/list', '{}', headers)
        self.assertEqual(code, 200)
        factory.assert_called_once_with('spi-triage.automation.svc.cluster.local', 8080, timeout=8)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('POST', '/operator/queue/list'))
        self.assertEqual(set(kwargs['headers']), {'Content-Type', 'X-Queue-Token'})
        self.assertNotIn(b'local-fixture-token', body)
        connection.close.assert_called_once()
    def test_old_tab_api_remains_a_fixed_authenticated_alias(self):
        self.assertEqual(web.ROUTES['/queue/api/list'], web.ROUTES['/api/list'])
        self.assertEqual(web.ROUTES['/queue/api/cancel'], web.ROUTES['/api/cancel'])

    def test_exhausted_capacity_returns_http_503_not_a_proxy_bad_gateway(self):
        acquired = 0
        try:
            for _ in range(8):
                self.assertTrue(self.server.slots.acquire(False))
                acquired += 1
            with self.assertLogs(level='WARNING') as logs:
                code, headers, body = self.request()
            self.assertEqual(code, 503)
            self.assertEqual(json.loads(body), {'code':'SERVICE_BUSY'})
            self.assertEqual(headers['Retry-After'], '1')
            self.assertIn('queue_frontend_busy', str(logs.output))
        finally:
            for _ in range(acquired):self.server.slots.release()
        self.assertEqual(self.request()[0], 200)

    def test_upstream_failures_log_only_error_type_not_private_details(self):
        headers = {'Origin':web.ORIGIN, 'X-SPI-Queue':'1', 'Content-Type':'application/json'}
        with patch.object(web.http.client, 'HTTPConnection', side_effect=OSError('PRIVATE_SENTINEL')):
            with self.assertLogs(level='WARNING') as logs:
                self.assertEqual(self.request('POST', '/api/list', '{}', headers)[0], 502)
        self.assertIn('OSError', str(logs.output))
        self.assertNotIn('PRIVATE_SENTINEL', str(logs.output))

    def test_no_arbitrary_proxy_target_or_cors(self):
        headers = {'Origin':web.ORIGIN, 'X-SPI-Queue':'1', 'Content-Type':'application/json'}
        with patch.object(web.http.client, 'HTTPConnection') as upstream:
            self.assertEqual(self.request('POST', '/api/claim', '{}', headers)[0], 404)
            upstream.assert_not_called()
        code, response_headers, _ = self.request('OPTIONS')
        self.assertEqual(code, 405)
        self.assertNotIn('Access-Control-Allow-Origin', response_headers)

class QueueAuthorizationTests(unittest.TestCase):
    def test_caller_identity_headers_never_authorize_without_native_session(self):
        from email.message import Message
        from queue_oidc import OIDC
        handler = Mock(); handler.headers = Message()
        handler.headers['X-authentik-groups'] = 'homelab-owner'
        handler.headers['Authorization'] = 'Bearer forged'
        handler.headers['Cookie'] = 'authentik_proxy=forged'
        oidc = OIDC('fixture', 's'*48)
        with patch.object(web, 'OIDC_CLIENT', oidc):
            self.assertEqual(RealAuthorization(handler), 401)
        with patch.object(web, 'OIDC_CLIENT', None):
            self.assertEqual(RealAuthorization(handler), 503)

    def test_only_opaque_server_side_session_authorizes(self):
        import time
        from email.message import Message
        from queue_oidc import OIDC, SESSION_COOKIE
        handler = Mock(); handler.headers = Message()
        oidc = OIDC('fixture', 's'*48)
        identity = 'x'*43
        oidc.sessions[identity] = {'sub':'native-fixture', 'until':time.time()+30}
        handler.headers['Cookie'] = SESSION_COOKIE+'='+identity
        with patch.object(web, 'OIDC_CLIENT', oidc):
            self.assertEqual(RealAuthorization(handler), 200)
            oidc.logout(handler.headers)
            self.assertEqual(RealAuthorization(handler), 401)

class LogoutBrowserTests(unittest.TestCase):
    def test_safe_logout_failure_messages_and_redirects(self):
        program = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const logout=source.slice(source.indexOf("$('logout').onclick"));
(async()=>{
 for(const scenario of [200,401,403,503,'TimeoutError','TypeError']){
  const status={},button={},redirects=[];
  const context={$:id=>id==='logout'?button:status,
    window:{location:{assign:path=>redirects.push(path)}},
    AbortSignal:{timeout:()=>({})},fetch:async()=>{
     if(typeof scenario==='string')throw Object.assign(new Error('PRIVATE_DIAGNOSTIC'),{name:scenario});
     return {ok:scenario===200,status:scenario};
    }};
  vm.runInNewContext(logout,context);await button.onclick();
  if(scenario===200||scenario===401)assert.deepStrictEqual(redirects,['/signed-out']);
  else {
   assert.deepStrictEqual(redirects,[]);
   assert(!status.textContent.includes('PRIVATE_DIAGNOSTIC'));
   if(typeof scenario==='number')assert(status.textContent.includes('HTTP '+scenario));
   else assert(status.textContent.includes(scenario==='TimeoutError'?'timed out':'connection'));
  }
 }
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        subprocess.run(['node','-e',program,str(ROOT/'frontend/app.js')],check=True,timeout=10)

if __name__ == '__main__':unittest.main()
