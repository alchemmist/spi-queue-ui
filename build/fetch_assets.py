"""Build-time public asset fetcher. Strict allowlist, immutable pins, no weights.
This module is never imported by the runtime; production never downloads assets.
"""
import hashlib
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request

REVISION = '91bd022d5b49442a868bc35008f6c21e1860edfa'
TOKENIZER = {
    'tokenizer.json': '06b9509352d2af50381ab2247e083b80d32d5c0aba91c272ca9ff729b6a0e523',
    'chat_template.jinja': 'c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041',
    'tokenizer_config.json': '5f7aa0d810000c4a0940b35c8f16e56c3439260fccc13a321826ebc3ccf2501f',
}
WHEEL_SHA = '38337540fbbddff8e999d59970f3c6f35a82de10053206a7562f1ea02d046fa5'
class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https': raise ValueError('ASSET_REDIRECT_NOT_HTTPS')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch(url, expected, destination):
    if urllib.parse.urlsplit(url).scheme != 'https': raise ValueError('ASSET_URL_NOT_HTTPS')
    digest=hashlib.sha256(); size=0
    try:
        with urllib.request.build_opener(HTTPSOnly()).open(url, timeout=45) as response, destination.open('xb') as output:
            while chunk := response.read(65536):
                size += len(chunk)
                if size > 32*1024*1024: raise ValueError('ASSET_TOO_LARGE')
                digest.update(chunk); output.write(chunk)
        if digest.hexdigest() != expected: raise ValueError('ASSET_HASH_MISMATCH')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise

def main(directory):
    root=Path(directory); root.mkdir(parents=True, exist_ok=True)
    for name, digest in TOKENIZER.items():
        fetch('https://huggingface.co/RedHatAI/Qwen3.8-27B-INT4/resolve/'+REVISION+'/'+name, digest, root/name)
    here=Path(__file__).resolve().parent
    url=(here/'wheel-url.txt').read_text().strip()
    fetch(url, WHEEL_SHA, root/url.rsplit('/',1)[1])
    for item in json.loads((here/'broker-wheels.lock.json').read_text()):
        fetch(item['url'],item['sha256'],root/item['filename'])

if __name__ == '__main__': main(sys.argv[1])
