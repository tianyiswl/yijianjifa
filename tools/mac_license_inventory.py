"""Collect installed license evidence for a particular PyInstaller Analysis TOC.

Read-only toward the environment; writes a new inventory directory. This is an
inventory, not a release/compliance verdict. Run with the Mac build interpreter.
"""
import argparse
import ast
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import subprocess


def is_license_file(path):
    path=Path(path)
    if any(part.endswith('.dSYM') for part in path.parts):return False
    if path.suffix.lower() in {'.py','.pyi','.pyc','.so','.dylib','.dll','.pyd','.yml','.plist','.c','.h','.m','.mm','.cpp','.cc'}:return False
    return bool(re.match(r'^(licen[sc]e|copying|copyright|notice)(?:$|[._-])',path.name,re.I))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('toc',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    analysis=ast.literal_eval(args.toc.read_text())
    sources=set()
    binaries=[]
    for group in analysis:
        if not isinstance(group,list):continue
        for row in group:
            if not isinstance(row,tuple) or len(row)!=3 or not isinstance(row[1],str):continue
            if row[2] in {'PYMODULE','PYSOURCE','EXTENSION','BINARY','DATA'}:
                source=Path(row[1]).resolve()
                if source.is_file():sources.add(source)
                if row[2] in {'EXTENSION','BINARY'}:binaries.append((row[0],source))
    args.output.mkdir(parents=True,exist_ok=False)
    packages=[];matched=set()
    for dist in sorted(metadata.distributions(),key=lambda d:d.metadata.get('Name','').lower()):
        files=list(dist.files or [])
        owned={Path(dist.locate_file(f)).resolve() for f in files}
        used=owned & sources
        if not used:continue
        matched.update(used)
        name=dist.metadata['Name'];version=dist.version
        folder=args.output/'licenses'/re.sub(r'[^A-Za-z0-9_.-]','_',name+'-'+version)
        copied=[]
        for item in files:
            relative=Path(str(item));source=Path(dist.locate_file(item))
            if not is_license_file(relative):continue
            if not source.is_file() or relative.is_absolute() or '..' in relative.parts:continue
            target=folder/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
            copied.append({'file':target.relative_to(args.output).as_posix(),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
        packages.append({'name':name,'version':version,'license_expression':dist.metadata.get('License-Expression'),
                         'license_metadata':dist.metadata.get('License'),'project_urls':dist.metadata.get_all('Project-URL') or [],
                         'matched_files':len(used),'license_files':copied})
    media={}
    for name,path in binaries:
        if name not in {'bin/ffmpeg','bin/ffprobe'}:continue
        result=subprocess.run([str(path),'-L'],capture_output=True,text=True,check=True,timeout=30)
        filename=Path(name).name+'-license-output.txt'
        (args.output/filename).write_text(result.stdout+result.stderr)
        media[name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'license_output':filename}
    unmatched=sorted(str(p) for p in sources-matched if 'site-packages' in p.parts)
    native=[]
    for name,path in sorted(set(binaries)):
        native.append({'bundle_path':name,'source':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'python_distribution_owned':path in matched})
    native_packages=[]
    roots={Path(*path.parts[:6]) for _,path in binaries if str(path).startswith('/opt/homebrew/Cellar/')}
    for root in sorted(roots):
        folder=args.output/'native-licenses'/root.parent.name/root.name
        evidence=[]
        for source in sorted(root.iterdir()):
            if not source.is_file():continue
            if not re.search(r'licen[sc]e|copying|copyright|notice',source.name,re.I) and source.name not in {'INSTALL_RECEIPT.json','sbom.spdx.json'}:continue
            target=folder/source.name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
            evidence.append({'file':target.relative_to(args.output).as_posix(),'source':str(source),
                             'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
        native_packages.append({'name':root.parent.name,'version':root.name,'evidence':evidence,
                                'license_text_found':any(re.search(r'licen[sc]e|copying|copyright|notice',Path(e['file']).name,re.I) for e in evidence)})
    report={'release_ready':False,'toc_sha256':hashlib.sha256(args.toc.read_bytes()).hexdigest(),
            'python':analysis[12] if len(analysis)>12 and isinstance(analysis[12],str) else None,
            'packages':packages,'native_packages':native_packages,'missing_license_files':[p['name'] for p in packages if not p['license_files']],
            'unmatched_site_package_sources':unmatched,'native_binaries':native,'media':media,
            'limitations':['Installed metadata inventory only; bundled transitive/native libraries need separate upstream evidence.',
                          'No source-distribution obligations, exact source correspondence, signing or release acceptance cleared.']}
    (args.output/'inventory.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'packages':len(packages),'copied_license_files':sum(len(p['license_files']) for p in packages),
                      'missing_license_files':report['missing_license_files'],'unmatched_site_packages':len(unmatched),
                      'native_binary_entries':len(native),'release_ready':False},ensure_ascii=False))

if __name__=='__main__':main()
