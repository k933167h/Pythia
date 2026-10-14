"""Offline dependency declaration audit; no installs or network access."""
from pathlib import Path
import json
import tomllib

root = Path(__file__).resolve().parents[1]
project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
deps = project['project'].get('dependencies', [])
assert deps, 'No Python dependencies declared'
assert (root / 'uv.lock').is_file(), 'Missing Python lockfile'
print('Python dependencies:', len(deps))
for dep in deps:
    print(' -', dep)
    if '>=' in dep and '==' not in dep:
        print('   NOTE: lower-bound-only requirement; verify lockfile freshness')
print('Python lockfile: present')
packages = sorted((root / 'integrations' / 'osiris').rglob('package.json'))
if packages:
    for path in packages:
        package = json.loads(path.read_text(encoding='utf-8'))
        print('Node manifest:', path.relative_to(root), package.get('name', 'unnamed'))
else:
    print('NOT TESTED: Osiris Node dependency audit; upstream Osiris is installed separately')
print('NOT TESTED: vulnerability advisories; run an online audit in a controlled environment')
