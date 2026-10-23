"""Validate a concrete removal manifest against a disposable frozen bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

parser=argparse.ArgumentParser()
parser.add_argument('archive',type=Path)
parser.add_argument('manifest',type=Path)
parser.add_argument('workspace',type=Path)
args=parser.parse_args()
assert not args.workspace.exists(), 'Probe workspace must be new'
assert hashlib.file_digest(args.archive.open('rb'),'sha256').hexdigest()=='f1906060c1a04feaeeb05e6ae0e12903871b74b187ed6cac44f319445dac9485'
args.workspace.mkdir(parents=True)
with zipfile.ZipFile(args.archive) as z:z.extractall(args.workspace)
root=args.workspace/'一剪即发';exe=root/'一剪即发.exe'
manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
def checks(label):
    outputs={}
    for kind,extra,timeout in [('bundle',['--bundle-check'],240),('startup',['--data-dir',str(args.workspace/(label+'-data')),'--smoke-check'],90)]:
        report=args.workspace/(label+'-'+kind+'.json')
        done=subprocess.run([str(exe),*extra,str(report)],timeout=timeout,cwd=root)
        if done.returncode or not report.exists():raise RuntimeError(label+' '+kind+' failed: '+str(done.returncode))
        outputs[kind]=json.loads(report.read_text(encoding='utf-8-sig'))
    return outputs
before=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
baseline=checks('baseline')
removed=[]
for item in manifest['files']:
    path=root/item['relative']
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('Unsafe removal path')
    assert path.is_file() and path.stat().st_size==item['size']
    path.unlink();removed.append(item)
candidate=checks('candidate')
after=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
result=dict(before_bytes=before,after_bytes=after,removed_bytes=sum(x['size'] for x in removed),removed_count=len(removed),
            baseline=baseline,candidate=candidate,limits='Frozen fixture checks only; no real account publishing, no user device or full UI workflow validation.')
(args.workspace/'trim-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('TRIM_RESULT='+json.dumps(result,ensure_ascii=True),flush=True)
