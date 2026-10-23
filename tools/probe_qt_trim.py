"""CI-only Qt candidate validation; never run on a user's installed client."""
import json
import os
from pathlib import Path
import sys

if os.environ.get('GITHUB_ACTIONS')!='true':raise SystemExit('This destructive probe requires a disposable CI environment')
import PySide6
root=Path(PySide6.__file__).resolve().parent
manifest=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
targets=[]
for item in manifest['files']:
    prefix='_internal/PySide6/'
    if not item['relative'].startswith(prefix):continue
    path=root/item['relative'][len(prefix):]
    if not path.resolve().is_relative_to(root):raise ValueError('Unsafe probe path')
    if not path.is_file() or path.stat().st_size!=item['size']:raise RuntimeError('Installed Qt differs from audited bundle: '+path.name)
    targets.append(path)
for path in targets:path.unlink()
print('Trimmed Qt candidate in disposable CI runtime:',len(targets),'files; version',PySide6.__version__,flush=True)
