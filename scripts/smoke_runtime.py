"""Read-only smoke probe for a locally running PYTHIA engine.

Usage: python scripts/smoke_runtime.py http://127.0.0.1:8000
Never points at a public endpoint by default. Does not trigger predictions.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

base = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8000').rstrip('/')
url = urllib.parse.urlsplit(base)
if url.scheme not in ('http', 'https') or not url.hostname:
    raise SystemExit('Expected a valid HTTP(S) base URL')


def read(path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f'{path}: HTTP {response.status}')
        return json.load(response)


try:
    health = read('/health')
    assert health.get('status') == 'ok', 'engine did not report healthy'
    links = read('/links')
    predictions = read('/predictions')
    assert isinstance(predictions.get('predictions'), list)
    assert isinstance(predictions.get('horizons'), (list, tuple))
    print('PASS: /health, /links, /predictions returned expected shapes')
    print('DEPENDENCIES:', {k: links.get(k) for k in ('engine', 'osiris', 'oracle')})
    print('FORECAST_COUNT:', len(predictions['predictions']))
    if not links.get('osiris') or not links.get('oracle'):
        print('NOT READY: live Osiris and/or local model unavailable')
        sys.exit(2)
    if not predictions['predictions']:
        print('NOT READY: no forecasts generated yet')
        sys.exit(2)
except (urllib.error.URLError, TimeoutError, ValueError, AssertionError) as exc:
    raise SystemExit(f'FAIL: {exc}') from exc
