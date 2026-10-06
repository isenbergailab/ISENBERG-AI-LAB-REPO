"""Reject runtime artifacts and obvious credential shapes before publication."""
from pathlib import Path
import hashlib
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '__pycache__', '.pytest_cache', '.venv', 'venv'}
PRIVATE_DIRS = {'state', 'credentials', 'secrets', 'demo-vault'}
PRIVATE_SUFFIXES = {'.key', '.pem', '.dpapi', '.sqlite3', '.sqlite', '.db', '.log', '.bak', '.backup', '.ics', '.eml', '.pdf', '.zip'}
PATTERNS = {
    'API credential': re.compile(r'sk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{20,}'),
    'GitHub credential': re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}'),
    'Hugging Face credential': re.compile(r'hf_[A-Za-z0-9]{30,}'),
    'Google credential': re.compile(r'AIza[0-9A-Za-z_-]{35}'),
    'AWS credential': re.compile(r'AKIA[0-9A-Z]{16}'),
    'private key': re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
}
ALLOWED_SUFFIXES = {'.py', '.toml', '.md', '.txt', '.yaml', '.sh', '.ps1'}
ALLOWED_NAMES = {'.gitignore', '.dockerignore', 'Dockerfile'}


def inspect(root=ROOT):
    errors, manifest = [], []
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if SKIP.intersection(rel.parts):
            continue
        if path.is_symlink():
            errors.append(f'{rel.as_posix()}: symlink forbidden')
            continue
        if not path.is_file():
            continue
        if (PRIVATE_DIRS.intersection(rel.parts) or path.suffix.lower() in PRIVATE_SUFFIXES
                or path.name.startswith('.env') or path.name.startswith('config.') and path.name != 'config.example.toml'):
            errors.append(f'{rel.as_posix()}: runtime or credential artifact')
            continue
        if path.suffix not in ALLOWED_SUFFIXES and path.name not in ALLOWED_NAMES:
            errors.append(f'{rel.as_posix()}: unreviewed file type')
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            errors.append(f'{rel.as_posix()}: non-text artifact')
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                errors.append(f'{rel.as_posix()}: {label}')
        manifest.append({'path': rel.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
    return errors, manifest


if __name__ == '__main__':
    errors, manifest = inspect()
    for error in errors:
        print('ERROR:', error)
    if errors:
        sys.exit(1)
    if '--manifest' in sys.argv:
        print(json.dumps(manifest, indent=2))
    else:
        print(f'Publication checks passed: {len(manifest)} reviewed text files.')
        print('Also review names, affiliations, and private URLs manually.')
