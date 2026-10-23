"""Build-only canonical copies of the two explicitly selected Mac media tools."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import subprocess


def _record(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise RuntimeError('Media tool must be a regular executable: '+str(path))
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return {'size_bytes':info.st_size,'mode':stat.S_IMODE(info.st_mode),
            'sha256':digest.hexdigest(),'device':info.st_dev,'inode':info.st_ino}


def _ditto(source,target):
    try:
        subprocess.run(['/usr/bin/ditto',str(source),str(target)],check=True,timeout=30,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError,OSError) as exc:
        raise RuntimeError('Media tool ditto failed') from exc


def stage(ffmpeg,ffprobe,output_root):
    sources={'ffmpeg':Path(ffmpeg).resolve(strict=True),'ffprobe':Path(ffprobe).resolve(strict=True)}
    before={name:_record(path) for name,path in sources.items()}
    output=Path(output_root)
    if output.is_symlink():raise RuntimeError('Media staging must be an empty private directory')
    output.mkdir(parents=True,exist_ok=True)
    if any(output.iterdir()):raise RuntimeError('Media staging must be an empty private directory')
    inventory={'schema_version':1,'tools':{}}
    for name,source in sources.items():
        target=output/name
        _ditto(source,target)
        copied=_record(target)
        if any(copied[key]!=before[name][key] for key in ('size_bytes','mode','sha256')):
            raise RuntimeError('Media tool copy identity mismatch: '+name)
        inventory['tools'][name]={'source_path':str(source),'source':before[name],'copy':copied}
    if any(_record(source)!=before[name] for name,source in sources.items()):
        raise RuntimeError('Media tool source identity changed during staging')
    with (output/'media-inventory.json').open('x',encoding='utf-8') as stream:
        json.dump(inventory,stream,indent=2);stream.write('\n')
    return inventory


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg',type=Path,required=True)
    parser.add_argument('--ffprobe',type=Path,required=True)
    parser.add_argument('--output-root',type=Path,required=True)
    args=parser.parse_args()
    stage(args.ffmpeg,args.ffprobe,args.output_root)
    print('Media tools copied and verified under canonical build names.')
