"""Record verified-wheel installed library bytes and retain its legal notices."""
import hashlib
import importlib.metadata
import json
import pathlib
import shutil

dist = importlib.metadata.distribution('tokenizers')
manifest = {}
legal = pathlib.Path('/opt/spi-tokenizer/licenses/tokenizers')
legal.mkdir(parents=True, exist_ok=True)
for item in dist.files:
    path = pathlib.Path(dist.locate_file(item)).resolve()
    if path.is_file() and '__pycache__' not in str(path) and path.suffix != '.pyc':
        manifest[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        if 'license' in path.name.lower() or 'notice' in path.name.lower():
            shutil.copyfile(path, legal / path.name)
pathlib.Path('/opt/spi-tokenizer/library-hashes.json').write_text(json.dumps(manifest, sort_keys=True))
