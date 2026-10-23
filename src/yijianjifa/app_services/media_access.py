"""Media operations resolve native selections and library IDs, never JS paths."""
import mimetypes
import math
import base64
from yijianjifa.os_integration.background_process import run_hidden
import subprocess
import threading
import os
from collections import OrderedDict
from pathlib import Path

from yijianjifa.features.library.copy_service import CategoryRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.preferences import LibraryPreferences
from yijianjifa.webui.media_registry import MediaRegistry


# Supported media must not depend on a machine's MIME registry (Windows may
# have no WebP association). Resource validation remains the caller's job.
_MEDIA_MIME_TYPES = {
    '.mp4':'video/mp4', '.m4v':'video/mp4', '.mov':'video/quicktime',
    '.mkv':'video/x-matroska', '.avi':'video/x-msvideo', '.webm':'video/webm',
    '.mp3':'audio/mpeg', '.wav':'audio/wav', '.m4a':'audio/mp4',
    '.aac':'audio/aac', '.flac':'audio/flac', '.ogg':'audio/ogg',
    '.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.webp':'image/webp',
}


def native_video_surface(value):
    """Finite CSS rectangles from the private bridge, never window handles/paths."""
    if not isinstance(value,dict) or set(value)!={'rect','clip','visible'} or type(value['visible']) is not bool:
        raise ValueError('invalid video surface')
    result={'visible':value['visible']}
    for key in ('rect','clip'):
        rect=value[key]
        if not isinstance(rect,(list,tuple)) or len(rect)!=4 or any(type(n) not in (int,float) or abs(n)>32768 or not math.isfinite(n) for n in rect):
            raise ValueError('invalid video surface')
        if rect[2]<0 or rect[3]<0 or (key=='rect' and (rect[2]==0 or rect[3]==0)):
            raise ValueError('invalid video surface')
        result[key]=list(rect)
    x,y,w,h=result['rect'];cx,cy,cw,ch=result['clip']
    if cx<x-.01 or cy<y-.01 or cx+cw>x+w+.01 or cy+ch>y+h+.01 or (result['visible'] and (cw==0 or ch==0)):
        raise ValueError('invalid video clipping')
    return result


class MediaAccess:
    def __init__(self,engine,paths,*,native_video=False):
        self._engine,self._paths=engine,paths
        self.native_video=native_video
        self.registry=MediaRegistry()
        self.mime_types={}
        self._preview=None
        self._read_players={}
        self._read_lock=threading.RLock()
        self._thumbnails=OrderedDict()
        self._durations=OrderedDict()

    def import_files(self,kind,selected,*,keep_copy,category_ids,folder=False):
        service=MediaStorageService(self._engine,self._paths,kind=kind)
        if type(keep_copy) is not bool or not set(category_ids)<={c.id for c in CategoryRepository(self._engine).list(kind)}:
            raise ValueError('invalid import options')
        if not selected:return {'cancelled':True,'imported':0,'reused':0,'errors':[]}
        existing={r.id for r in service.list_assets()}
        files=[]
        for raw in selected:
            path=Path(raw)
            if path.is_symlink():continue
            if folder:
                root=path.resolve(strict=True)
                for child in sorted(root.rglob('*')):
                    if child.is_file() and not child.is_symlink() and child.resolve().is_relative_to(root) and child.suffix.lower() in service.supported_extensions:
                        files.append(child)
            else:files.append(path)
        result={'cancelled':False,'imported':0,'reused':0,'errors':[]}
        for path in files:
            try:
                item=service.import_media(path,keep_copy=keep_copy,category_ids=category_ids)
                result['reused' if item.id in existing else 'imported']+=1
                existing.add(item.id)
            except Exception:
                result['errors'].append({'name':path.name,'message':'无法导入，请检查文件、权限与格式。'})
        LibraryPreferences(self._engine).set_keep_copy(kind,keep_copy)
        return result

    def preview(self,kind,identity):
        record=self._record(kind,identity)
        result=self.grant_read(record.effective_path,name=record.display_name,kind=kind,owner='library')
        if self.native_video and kind in ('video','finished'):result['native_video']=True
        return result

    def _native_grant(self,token):
        import re
        if not self.native_video or type(token) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{32}',token) or not self.mime_types.get(token,'').startswith('video/'):
            raise ValueError('invalid video grant')
        self.registry.size(token)

    def play_native(self,token,window,*,surface=None):
        self._native_grant(token)
        if surface is None:window.play_media(self,token)
        else:window.play_media(self,token,native_video_surface(surface))
        return {'playing':True}

    def position_native(self,token,window,surface):
        self._native_grant(token)
        window.position_media(token,native_video_surface(surface))
        return {'positioned':True}

    def expand_native(self,token,window):
        self._native_grant(token)
        window.expand_media(token)
        return {'expanded':True}

    def grant_read(self,path,*,name,kind,owner,verify_fd=None):
        """Trusted backend paths only; callers validate their resource ownership."""
        with self._read_lock:
            if owner not in self._read_players and len(self._read_players)>=64:raise ValueError('preview limit')
            token=self.registry.grant(path)
            try:
                if verify_fd is not None:
                    descriptor=self.registry.duplicate(token)
                    try: verify_fd(descriptor)
                    finally: os.close(descriptor)
            except Exception:
                self.registry.revoke(token)
                raise
            self.mime_types[token]=(_MEDIA_MIME_TYPES.get(Path(path).suffix.lower())
                or mimetypes.guess_type(Path(path).name)[0] or 'application/octet-stream')
            self.revoke_read(owner)
            self._read_players[owner]=token
            if owner=='library':self._preview=token
            return {'url':'p011media://asset/'+token,'name':name,'kind':kind}

    def revoke_read(self,owner):
        with self._read_lock:
            token=self._read_players.pop(owner,None)
            if token:self.registry.revoke(token);self.mime_types.pop(token,None)
            if owner=='library':self._preview=None

    def thumbnail(self,kind,identity):
        if kind not in ('video','finished'): raise ValueError('not a video')
        record=self._record(kind,identity)
        source=record.effective_path
        if kind=='finished':
            from .finished_covers import FinishedCovers
            _,cover=FinishedCovers(self._engine,self._paths).read(identity)
            if cover is not None: source=cover
        stat=source.stat()
        key=(kind,identity,str(source),stat.st_size,stat.st_mtime_ns)
        if key not in self._thumbnails:
            from yijianjifa.os_integration.resources import ffmpeg_path
            result=run_hidden([str(ffmpeg_path()),'-v','error','-i',str(source),
                '-frames:v','1','-vf','scale=240:160:force_original_aspect_ratio=decrease',
                '-f','image2pipe','-vcodec','mjpeg','pipe:1'],capture_output=True,timeout=20)
            if result.returncode or not result.stdout.startswith(b'\xff\xd8'): raise ValueError('thumbnail unavailable')
            self._thumbnails[key]='data:image/jpeg;base64,'+base64.b64encode(result.stdout).decode('ascii')
            while len(self._thumbnails)>128:self._thumbnails.popitem(last=False)
        self._thumbnails.move_to_end(key)
        duration=None
        if kind=='video':
            if key not in self._durations:
                try:
                    from yijianjifa.os_integration.resources import ffprobe_path
                    import math
                    result=run_hidden([str(ffprobe_path()),'-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(source)],capture_output=True,text=True,timeout=10)
                    value=float(result.stdout.strip())
                    self._durations[key]=round(value*1000) if result.returncode==0 and math.isfinite(value) and value>0 else None
                except (ValueError,OSError,subprocess.TimeoutExpired):self._durations[key]=None
                while len(self._durations)>128:self._durations.popitem(last=False)
            duration=self._durations[key]
        return {'image':self._thumbnails[key],'duration_ms':duration}

    def reveal(self,kind,identity,*,original=False):
        from yijianjifa.os_integration.reveal import reveal_path
        record=self._record(kind,identity)
        return {'shown':reveal_path(record.original_path if original else record.effective_path)}

    def _record(self,kind,identity):
        if kind=='finished':
            from yijianjifa.features.finished.service import FinishedVideoService
            record=FinishedVideoService(self._engine).get(identity)
        else:
            record=MediaStorageService(self._engine,self._paths,kind=kind).get(identity)
            if record and record.kind!=kind:record=None
        if record is None:raise ValueError('unknown media')
        return record

    def close(self):
        with self._read_lock:
            self.registry.close();self.mime_types.clear();self._thumbnails.clear();self._read_players.clear()


class DropAccess:
    """Consume native grants and reuse ordinary media/Excel core operations."""
    def __init__(self, registry, media, excel):
        self._registry,self._media,self._excel=registry,media,excel

    def media(self, token, *, kind, keep_copy, category_ids):
        selected=self._registry.consume(token)
        data={'cancelled':False,'imported':0,'reused':0,'errors':[]}
        for path in selected:
            result=self._media.import_files(kind,[path],keep_copy=keep_copy,category_ids=category_ids,folder=path.is_dir())
            data['imported']+=result['imported']
            data['reused']+=result['reused']
            data['errors'].extend(result['errors'])
        return data

    def excel(self, token):
        selected=self._registry.consume(token)
        if len(selected)!=1 or selected[0].suffix.lower()!='.xlsx':
            raise ValueError('请每次拖入一份.xlsx文案表格。')
        return self._excel.preview(selected[0])
