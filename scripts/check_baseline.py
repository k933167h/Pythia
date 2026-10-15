"""Offline, non-invasive checks for PYTHIA's original source tree."""
from pathlib import Path
import ast
import sys
import tomllib

root = Path(__file__).resolve().parents[1]
files = sorted((root / 'engine').rglob('*.py'))
if not files:
    raise SystemExit('FAIL: no Python engine sources')
for path in files:
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
assert project['project']['name'] == 'pythia'
assert (root / 'run-all.sh').is_file()
assert (root / 'integrations' / 'osiris').is_dir()
print(f'PASS: {len(files)} Python files parsed; project metadata and expected paths found')
print('NOT TESTED: runtime, external feeds, Ollama, MCP, Osiris UI, forecast accuracy')
