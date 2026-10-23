"""Native-selected local files become stable owned copies, never network inputs.

Selections live only in backend memory for this application lifetime. Receiving
does not register library material, create processing tasks, or modify originals.
The caller owns the shared local slot; all byte/probe work is outside receipts.
"""
import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

from .contracts import Candidate, DownloadError, Progress
from .files import _Guard

EXTENSIONS=frozenset({'.mp4','.mov','.m4v'})
MAX_BYTES=2*1024*1024*1024
SPACE_MARGIN=256*1024*1024

def selected_files(values):
    if not isinstance(values,(list,tuple)) or not 1<=len(values)<=200:
        raise DownloadError('input_limit')
    result=[]
    for selected in values:
        if not isinstance(selected,(str,Path)):raise DownloadError('input_limit')
        path=Path(selected)
        name=''.join(c for c in path.name if ord(c)>=32 and ord(c)!=127)[:250] or '未命名视频'
        value={'name':name,'error_code':None}
        try:
            if not path.is_absolute() or len(str(path))>4000:raise DownloadError('unsafe_source')
            if path.suffix.lower() not in EXTENSIONS:raise DownloadError('local_format_unsupported')
            with _Guard(path,directory=False) as guard:
                stamp=guard.file_stamp()
                if not 0<stamp[0]<=MAX_BYTES:raise DownloadError('size_limit' if stamp[0]>MAX_BYTES else 'media_invalid')
                guard.verify()
                value.update(path=str(path),identity=tuple(guard.identity),stamp=tuple(stamp))
        except DownloadError as error:value['error_code']=error.code
        result.append(value)
    return tuple(result)

def _check(cancel,deadline):
    if cancel.cancelled():raise DownloadError('cancelled')
    if time.monotonic()>=deadline:raise DownloadError('timeout')

def _verify_selected(guard,selection):
    guard.verify()
    if tuple(guard.identity)!=selection['identity'] or tuple(guard.file_stamp())!=selection['stamp']:
        raise DownloadError('identity_mismatch')

def _read(guard,selection,cancel,deadline,progress,writer=None):
    stage='local_copy' if writer is not None else 'local_verify'
    descriptor=guard.reader_fd();digest=hashlib.sha256();count=0
    try:
        os.lseek(descriptor,0,os.SEEK_SET)
        with os.fdopen(descriptor,'rb') as source:
            descriptor=None
            while True:
                _check(cancel,deadline)
                chunk=source.read(1024*1024)
                if not chunk:break
                count+=len(chunk)
                if count>MAX_BYTES or count>selection['stamp'][0]:raise DownloadError('size_limit')
                digest.update(chunk)
                if writer is not None:
                    if shutil.disk_usage(writer.name.parent).free<len(chunk)+SPACE_MARGIN:raise DownloadError('disk_space')
                    writer.write(chunk)
                progress(Progress(stage,count,selection['stamp'][0]))
        _verify_selected(guard,selection)
        if count!=selection['stamp'][0]:raise DownloadError('identity_mismatch')
        return digest.hexdigest()
    finally:
        if descriptor is not None:os.close(descriptor)

class _Writer:
    def __init__(self,stream,path):self.stream,self.name=stream,path
    def write(self,value):return self.stream.write(value)

def receive_file(runtime,selection,cancel,progress):
    """Return (candidate_id, owned source), with caller holding one local slot."""
    temporary=None;deadline=time.monotonic()+30*60
    try:
        if selection['error_code']:raise DownloadError(selection['error_code'])
        with _Guard(selection['path'],directory=False) as source:
            _verify_selected(source,selection)
            sha=_read(source,selection,cancel,deadline,progress)
            key='local:'+sha
            with runtime.engine.begin() as connection:
                cid=runtime.repository.save_candidate(connection,Candidate(uuid.uuid4().hex,'local',key,key,None,
                    title=selection['name'],author='本地视频'))
            with runtime._lock:existing=runtime._local_ready.get(cid)
            if existing is not None:
                try:
                    with runtime.files.open_resource(existing,cancel=cancel) as bound:
                        runtime.files._validate(bound.path,'source_video',cancel)
                        bound.verify()
                        _verify_selected(source,selection)
                        return cid,bound.resource
                except DownloadError:
                    _check(cancel,deadline)
                    with runtime._lock:runtime._local_ready.pop(cid,None)
            grant=runtime.files.local_cache_grant()
            if shutil.disk_usage(runtime.paths.cache_dir).free<selection['stamp'][0]+SPACE_MARGIN:raise DownloadError('disk_space')
            _check(cancel,deadline)
            temporary=runtime.files.allocate(cid,'source_video',grant_id=grant)
            with runtime.files.open_temporary(temporary) as target:
                flags=os.O_WRONLY|getattr(os,'O_NOFOLLOW',0)
                descriptor=os.open(temporary if os.name=='nt' else temporary.name,flags,
                    **({} if os.name=='nt' else {'dir_fd':target.guard.parent_fd}))
                with os.fdopen(descriptor,'wb') as output:
                    info=os.fstat(output.fileno())
                    if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=target.guard.identity:
                        raise DownloadError('identity_mismatch')
                    copied=_read(source,selection,cancel,deadline,progress,_Writer(output,temporary))
                    output.flush();os.fsync(output.fileno())
                if copied!=sha:raise DownloadError('identity_mismatch')
                target.guard.verify();target.area.verify();target.root.verify()
                _verify_selected(source,selection)
            _check(cancel,deadline)
            # The existing validator deliberately accepts only MOV-family
            # video, with external data references and remote protocols disabled.
            resource=runtime.files.promote(cid,temporary,'source_video',cancel=cancel)
            temporary=None
            if resource.sha256!=sha:raise DownloadError('identity_mismatch')
            # The original was guarded through the finished, flushed copy. Its
            # later move/change cannot invalidate this verified stable snapshot.
            return cid,resource
    except OSError as error:
        raise DownloadError('disk_space' if error.errno==errno.ENOSPC else 'identity_mismatch') from None
    finally:
        if temporary is not None:runtime._release(temporary)
