"""Explicit build-only recognition collection. Never downloads or loads models."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import stat
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'packaging/recognition-lock.json'
VC_RUNTIME = ('msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll','vcomp140.dll')
LICENSE_COMPONENTS = {'whisper.cpp','openai-whisper','rapidocr','rapidocr-models','onnxruntime'}


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _record(path):
    path=Path(path); info=path.lstat()
    if not stat.S_ISREG(info.st_mode): raise RuntimeError('Recognition artifact must be a regular file: '+path.name)
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024): digest.update(chunk)
    return {'name':path.name,'size_bytes':info.st_size,'sha256':digest.hexdigest()}


def _verified(path,expected):
    actual=_record(path)
    if (actual['size_bytes'],actual['sha256'])!=(expected['size_bytes'],expected['sha256']):
        raise RuntimeError('Recognition artifact identity mismatch: '+actual['name'])
    return actual


def _versions(lock,platform):
    if sys.version_info[:2]!=(3,12): raise RuntimeError('Recognition packaging requires CPython 3.12')
    result={}
    rows=list(lock['platforms'][platform]['artifacts'])
    source=lock['platforms'][platform].get('source_only_dependency')
    if source: rows.append(source)
    for row in rows:
        name='antlr4-python3-runtime' if row['name']=='antlr4-python3-runtime-built-wheel' else row['name']
        try: actual=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise RuntimeError('Recognition dependency missing: '+name) from None
        if actual!=row['version']: raise RuntimeError('Recognition dependency version mismatch: '+row['name'])
        result[name]=actual
    return result


def _licenses(directory):
    directory=Path(directory); data=_read(directory/'manifest.json')
    selected=[row for row in data['files'] if row['component'] in LICENSE_COMPONENTS]
    if {row['component'] for row in selected}!=LICENSE_COMPONENTS: raise RuntimeError('Recognition licenses missing')
    if not any('ThirdPartyNotices' in row['file'] for row in selected if row['component']=='onnxruntime'):
        raise RuntimeError('ONNX Runtime third-party notices missing')
    for row in selected:
        relative=Path(row['file'])
        if relative.is_absolute() or '..' in relative.parts: raise RuntimeError('Invalid recognition license path')
        if _record(directory/relative)['sha256']!=row['sha256']: raise RuntimeError('Recognition license hash mismatch')
    return selected


def _pe_x64(path):
    try:
        stream=Path(path).open('rb')
    except OSError as exc:
        raise RuntimeError('Windows recognition runtime missing: '+Path(path).name) from exc
    with stream:
        header=stream.read(64)
        if len(header)!=64 or header[:2]!=b'MZ': raise RuntimeError('Windows recognition runtime is not PE x64')
        offset=struct.unpack_from('<I',header,60)[0]
        if not 64<=offset<=1024*1024: raise RuntimeError('Invalid Windows runtime PE header')
        stream.seek(offset); marker=stream.read(6)
    if marker[:4]!=b'PE\x00\x00' or marker[4:]!=b'\x64\x86': raise RuntimeError('Windows recognition runtime is not PE x64')


def collect_licenses():
    """Collect locked recognition dependency texts; no compliance claim."""
    directory=ROOT/'packaging/resources/licenses'; manifest=_read(directory/'manifest.json')
    lock=_read(LOCK)
    selected={row['name'].lower().replace('_','-') for platform in lock['platforms'].values() for row in platform['artifacts']}
    entries=[]
    for distribution in importlib.metadata.distributions():
        name=distribution.metadata['Name']; version=distribution.version
        if name.lower().replace('_','-') not in selected: continue
        for relative in distribution.files or ():
            if not any(part in relative.name.lower() for part in ('license','licence','copying','notice')): continue
            if relative.suffix.lower() in ('.py','.pyc','.so','.dll','.dylib','.exe'): continue
            if '..' in relative.parts: continue
            source=Path(distribution.locate_file(relative))
            if not source.is_file() or not source.resolve().is_relative_to(Path(sys.prefix).resolve()): continue
            source.read_bytes().decode('utf-8')
            target=directory/'recognition/python'/f'{name}-{version}'/str(relative)
            target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(source,target)
            entries.append({'file':str(target.relative_to(directory)),'component':name.lower().replace('_','-'),
                'version':version,'source':'Actual isolated build-environment distribution license text',
                'sha256':_record(target)['sha256']})
    manifest['files']=[row for row in manifest['files'] if not row['file'].startswith('recognition/python/')]+entries
    (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return entries


def prepare(source_root,output_root,*,platform,vc_root=None,lock_path=LOCK):
    """Copy only verified engine files from explicitly supplied build inputs."""
    if platform not in ('macos_arm64','windows_x64'): raise RuntimeError('Unsupported recognition build platform')
    lock=_read(lock_path); versions=_versions(lock,platform); _licenses(ROOT/'packaging/resources/licenses')
    source_root,output_root=Path(source_root),Path(output_root)
    sources=[]
    if platform=='macos_arm64':
        expected=lock['whisper'][platform]; source=source_root/expected['filename']
        sources.append((source,_verified(source,expected)))
    else:
        for expected in lock['whisper'][platform]['candidate_engine_files']:
            source=source_root/expected['name']; _pe_x64(source)
            sources.append((source,_verified(source,expected)))
        if vc_root is None: raise RuntimeError('Explicit MSVC/OpenMP redistributable inputs missing')
        for name in VC_RUNTIME:
            source=Path(vc_root)/name; _pe_x64(source)
            sources.append((source,{**_record(source),'origin':'explicit Visual Studio redistributable input; recorded at build'}))
    destination=output_root/'bin'; destination.mkdir(parents=True,exist_ok=True)
    for source,record in sources:
        shutil.copy2(source,destination/record['name'])
    inventory={'schema_version':1,'platform':platform,'whisper_version':lock['whisper']['version'],
        'source_commit':lock['whisper']['source_commit'],'packages':versions,
        'engine_files':[record for _,record in sources],'models_bundled':False,'model_download':'explicit-only',
        'validation':'build inputs checked; frozen/target-host acceptance separate'}
    (output_root/'recognition-inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return inventory


def check(output_root,*,platform,lock_path=LOCK):
    lock=_read(lock_path); inventory=_read(Path(output_root)/'recognition-inventory.json')
    if inventory['platform']!=platform or inventory['models_bundled'] is not False: raise RuntimeError('Recognition inventory policy mismatch')
    expected=([lock['whisper'][platform]['filename']] if platform=='macos_arm64'
        else [row['name'] for row in lock['whisper'][platform]['candidate_engine_files']]+list(VC_RUNTIME))
    if set(row['name'] for row in inventory['engine_files'])!=set(expected): raise RuntimeError('Recognition engine dependency inventory incomplete')
    locked=([lock['whisper'][platform]] if platform=='macos_arm64' else lock['whisper'][platform]['candidate_engine_files'])
    by_name={row.get('name',row.get('filename')):row for row in locked}
    for row in inventory['engine_files']:
        path=Path(output_root)/'bin'/row['name']; _verified(path,row)
        if row['name'] in by_name: _verified(path,by_name[row['name']])
        if platform=='windows_x64': _pe_x64(path)
    versions=_versions(lock,platform)
    if inventory['packages']!=versions: raise RuntimeError('Recognition dependency inventory does not match lock')
    _licenses(ROOT/'packaging/resources/licenses')
    return inventory


def requirements(platform,*,lock_path=LOCK):
    lock=_read(lock_path); rows=[row for row in lock['platforms'][platform]['artifacts'] if row['name']!='antlr4-python3-runtime-built-wheel']
    source=lock['platforms'][platform].get('source_only_dependency')
    if source: rows.append(source)
    return ''.join(f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}\n" for row in rows)


def finalize(output_root,frozen_root,*,platform):
    """Record actual post-PyInstaller bytes while retaining admitted source IDs."""
    inventory=check(output_root,platform=platform)
    for row in inventory['engine_files']:
        actual=Path(frozen_root)/'bin'/row['name']
        if platform=='windows_x64': _verified(actual,row)
        else:
            with actual.open('rb') as stream: header=stream.read(8)
            if header!=b'\xcf\xfa\xed\xfe\x0c\x00\x00\x01':
                raise RuntimeError('Frozen recognition tool is not Mach-O arm64')
        row['source_sha256']=row['sha256']; row['source_size_bytes']=row['size_bytes']
        row.update(_record(actual))
    inventory['validation']='source identity admitted; actual frozen bytes recorded; target-host acceptance separate'
    (Path(frozen_root)/'recognition-inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return inventory


def collect(output_root,*,platform):
    """PyInstaller collection: CPU backend/config/metadata, without ONNX weights."""
    check(output_root,platform=platform)
    from PyInstaller.utils.hooks import collect_data_files,collect_dynamic_libs,collect_submodules,copy_metadata
    datas=collect_data_files('rapidocr',includes=['config.yaml','default_models.yaml'])
    datas+=collect_data_files('onnxruntime',excludes=['**/*.onnx'])
    datas+=collect_data_files('yijianjifa.features.downloads',includes=['model_manifest.json'])
    for package in ('rapidocr','onnxruntime'): datas+=copy_metadata(package,recursive=True)
    datas+=[(str(LOCK),'.'),(str(Path(output_root)/'recognition-inventory.json'),'.')]
    excluded=tuple('rapidocr.inference_engine.'+name for name in ('paddle','pytorch','openvino','mnn','tensorrt'))
    hidden=collect_submodules('rapidocr',filter=lambda name:not name.startswith(excluded))
    hidden+=collect_submodules('onnxruntime.capi')
    binaries=collect_dynamic_libs('onnxruntime')
    inventory=_read(Path(output_root)/'recognition-inventory.json')
    binaries+=[(str(Path(output_root)/'bin'/row['name']),'bin') for row in inventory['engine_files']]
    if any(Path(source).suffix.lower()=='.onnx' for source,_ in datas+binaries): raise RuntimeError('Implicit OCR models entered bundle collection')
    return datas,binaries,hidden


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','check','requirements','finalize'))
    parser.add_argument('--platform',required=True,choices=('macos_arm64','windows_x64'))
    parser.add_argument('--source-root',type=Path); parser.add_argument('--output-root',type=Path)
    parser.add_argument('--vc-root',type=Path); parser.add_argument('--requirements-output',type=Path)
    parser.add_argument('--frozen-root',type=Path)
    args=parser.parse_args()
    if args.operation=='requirements':
        args.requirements_output.write_text(requirements(args.platform),encoding='utf-8'); return
    if args.output_root is None: parser.error('--output-root required')
    if args.operation=='prepare':
        if args.source_root is None: parser.error('--source-root required')
        collect_licenses()
        prepare(args.source_root,args.output_root,platform=args.platform,vc_root=args.vc_root)
    elif args.operation=='finalize':
        if args.frozen_root is None: parser.error('--frozen-root required')
        finalize(args.output_root,args.frozen_root,platform=args.platform)
    else: check(args.output_root,platform=args.platform)


if __name__=='__main__': main()
