"""Explicit operator transport to the existing native localhost n8n API.
Run on its host or establish an authorized private SSH tunnel yourself.
No automatic activation, credential creation, retries or public auth fallback.
"""
import json
from pathlib import Path
import urllib.request
BASE = Path(__file__).resolve().parents[2]
CONFIG = Path.home() / '.config/n8n'
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
def api(method, path, body=None):
    if path.startswith('/') or '://' in path or '..' in path:
        raise ValueError('INVALID_N8N_API_PATH')
    request = urllib.request.Request('http://127.0.0.1:5678/api/v1/' + path,
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'X-N8N-API-KEY':(CONFIG/'api-key').read_text().strip(), 'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('N8N_RESPONSE_TOO_LARGE')
        return json.loads(raw) if raw else {}
