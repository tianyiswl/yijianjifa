"""Validated local cover previews with immutable images and transactional choices."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
from yijianjifa.os_integration.background_process import run_hidden
import subprocess
import tempfile
import threading
import time
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.finished.covers import validate_frame_time
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.os_integration.resources import ffmpeg_path
from .library_queries import CommandRejected


class CoverBatchCancelled(Exception):
    pass


class FinishedCovers:
    MAX_STAGED_COVERS = 2000
    MAX_STAGED_BYTES = 512 * 1024 * 1024

    def __init__(self, engine, paths):
        self.engine, self.paths = engine, paths
        self.lock = threading.RLock()
        self.drafts = {}
        self.staged = {}
        self.stage_sources = {}
        self.staged_bytes = 0
        self.closed = False

    def _record(self, engine, pid):
        if type(pid) is not str or not 1 <= len(pid) <= 64:
            raise ValueError('成品编号无效。')
        record = FinishedVideoService(engine).get(pid)
        if record is None:
            raise ValueError('成品不存在。')
        return record

    @staticmethod
    def _saved(engine, pid):
        with session_scope(engine) as session:
            row = session.get(Setting, 'finished-cover:'+pid)
            return row.value_json if row else None

    def read(self, pid):
        self._record(self.engine, pid)
        saved = self._saved(self.engine, pid)
        if saved is None:
            return {'id':pid,'kind':'first_frame','frame_ms':0,'saved':False}, None
        data = json.loads(saved)
        digest = data['sha256']
        if not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('封面记录损坏。')
        path = self.paths.data_dir/'covers'/(digest+'.png')
        valid = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        return {'id':pid,'kind':data['kind'],'frame_ms':data['frame_ms'],'saved':True,'missing':not valid}, path if valid else None

    def frame(self, pid, kind, frame_ms):
        record = self._record(self.engine, pid)
        if kind not in ('first_frame','frame') or type(frame_ms) is not int or (kind=='first_frame' and frame_ms!=0) or not validate_frame_time(record.duration_ms,frame_ms).ok:
            raise ValueError('封面时间必须大于等于0，并小于视频时长。')
        return self._prepare(record,kind,frame_ms,record.effective_path)

    def image(self, pid, selected):
        record = self._record(self.engine, pid)
        if not selected:
            return None
        if len(selected)!=1:
            raise ValueError('请选择一张图片。')
        path = Path(selected[0])
        if path.is_symlink() or not path.is_file() or path.stat().st_size>50*1024*1024:
            raise ValueError('请选择有效的本地图片（不超过50MB）。')
        with path.open('rb') as f:header=f.read(12)
        if not (header.startswith(b'\x89PNG\r\n\x1a\n') or header.startswith(b'\xff\xd8\xff') or (header[:4]==b'RIFF' and header[8:12]==b'WEBP')):
            raise ValueError('图片损坏或不支持，请选择PNG、JPEG或WebP。')
        return self._prepare(record,'uploaded',None,path)

    def _prepare(self, record, kind, frame_ms, source):
        with self.lock:
            if self.closed:raise ValueError('封面预览已关闭。')
            for token,draft in list(self.drafts.items()):
                if draft['expires']<time.monotonic():
                    shutil.rmtree(draft['path'].parent,ignore_errors=True);del self.drafts[token]
            while len(self.drafts)>=16:
                old=self.drafts.pop(next(iter(self.drafts)));shutil.rmtree(old['path'].parent,ignore_errors=True)
            self.paths.cache_dir.mkdir(parents=True,exist_ok=True)
            folder=Path(tempfile.mkdtemp(prefix='cover-',dir=self.paths.cache_dir));output=folder/'cover.png'
            try:
                command=[str(ffmpeg_path()),'-nostdin','-v','error','-xerror','-err_detect','explode']
                if frame_ms is not None:command+=['-ss',str(frame_ms/1000)]
                command+=['-i',str(source),'-map','0:v:0','-frames:v','1','-vf',"scale=w='min(2048,iw)':h='min(2048,ih)':force_original_aspect_ratio=decrease",'-f','image2',str(output)]
                run_hidden(command,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
                content=output.read_bytes()
                if not content.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError()
                token=secrets.token_hex(24)
                self.drafts[token]={'id':record.id,'kind':kind,'frame_ms':frame_ms,'path':output,
                    'sha256':hashlib.sha256(content).hexdigest(),'video_hash':record.content_hash,
                    'previous':self._saved(self.engine,record.id),'expires':time.monotonic()+1200}
                return token
            except Exception:
                shutil.rmtree(folder,ignore_errors=True)
                raise ValueError('无法生成封面，请检查视频时间、文件是否损坏或图片格式。') from None

    def preview(self, token):
        with self.lock:
            draft=self.drafts.get(token)
            if not draft or draft['expires']<time.monotonic():raise ValueError('预览已过期，请重新生成。')
            return {k:draft[k] for k in ('id','kind','frame_ms')}, draft['path']

    def resolve_for_publish(self, token, pid, *, engine=None):
        """Read a validated preview for one video without changing its saved cover."""
        if type(token) is not str or not re.fullmatch('[a-f0-9]{48}',token):raise ValueError('invalid cover preview')
        with self.lock:
            draft=self.staged.get(token) or self.drafts.get(token)
            if self.closed or not draft or draft.get('expires',float('inf'))<time.monotonic():raise ValueError('cover preview expired')
            record=self._record(engine if engine is not None else self.engine,pid)
            if draft['id']!=pid or draft['video_hash']!=record.content_hash:raise ValueError('cover belongs to another video')
            path=draft['path']
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=draft['sha256']:raise ValueError('cover preview changed')
            return {k:draft[k] for k in ('kind','frame_ms','sha256')},path

    def stage_for_publish(self, pid, token):
        """Retain an applied cover for this app session, independently of previews."""
        if type(token) is not str or not re.fullmatch('[a-f0-9]{48}',token):raise ValueError('invalid cover preview')
        with self.lock:
            previous=self.stage_sources.get(token,token)
            if previous in self.staged:
                data,_=self.resolve_for_publish(previous,pid)
                return dict(id=pid,token=previous,kind=data['kind'],frame_ms=data['frame_ms'])
            data,source=self.resolve_for_publish(token,pid)
            draft=self.drafts[token]
            for staged_token,existing in self.staged.items():
                if all(existing[k]==draft[k] for k in ('id','video_hash','kind','frame_ms','sha256')):
                    self.resolve_for_publish(staged_token,pid)
                    self.stage_sources[token]=staged_token
                    return dict(id=pid,token=staged_token,kind=data['kind'],frame_ms=data['frame_ms'])
            content=source.read_bytes()
            if hashlib.sha256(content).hexdigest()!=data['sha256']:raise ValueError('cover preview changed')
            if len(self.staged)>=self.MAX_STAGED_COVERS or self.staged_bytes+len(content)>self.MAX_STAGED_BYTES:
                raise CommandRejected('cover_staging_full')
            folder=Path(tempfile.mkdtemp(prefix='publish-cover-',dir=self.paths.cache_dir))
            try:
                path=folder/'cover.png';path.write_bytes(content)
                if hashlib.sha256(path.read_bytes()).hexdigest()!=data['sha256']:raise ValueError('cover copy changed')
                staged_token=secrets.token_hex(24)
                self.staged[staged_token]=dict(id=pid,video_hash=draft['video_hash'],path=path,**data)
                self.stage_sources[token]=staged_token;self.staged_bytes+=len(content)
                return dict(id=pid,token=staged_token,kind=data['kind'],frame_ms=data['frame_ms'])
            except Exception:
                shutil.rmtree(folder,ignore_errors=True)
                raise

    def stage_for_videos(self, pid, token, video_ids, *, progress=None, cancelled=None):
        """Apply one choice to each target atomically within this app session."""
        if (type(video_ids) is not list or not 1 <= len(video_ids) <= 200
                or any(type(v) is not str or not 1 <= len(v) <= 64 for v in video_ids)
                or len(set(video_ids)) != len(video_ids)):
            raise ValueError('请选择1至200条不重复的成品。')
        if type(token) is not str or not re.fullmatch('[a-f0-9]{48}',token):
            raise ValueError('invalid cover preview')
        with self.lock:
            source_token=self.stage_sources.get(token,token)
            data,_=self.resolve_for_publish(source_token,pid)
            records=[self._record(self.engine,vid) for vid in video_ids]
            if data['kind'] != 'uploaded' and any(
                    not validate_frame_time(record.duration_ms,data['frame_ms']).ok for record in records):
                raise ValueError('封面时间必须小于每条视频的时长，请调整时间后重试。')
            previous=set(self.staged)
            aliases=dict(self.stage_sources)
            previous_bytes=self.staged_bytes
            generated=[]
            def checkpoint(done):
                if progress:progress(done,len(records))
                if cancelled and cancelled():raise CoverBatchCancelled()
            try:
                checkpoint(0)
                # Pin the source before generating further previews can evict it.
                retained=self.stage_for_publish(pid,token)
                _,source=self.resolve_for_publish(retained['token'],pid)
                result={}
                for index,record in enumerate(records):
                    if cancelled and cancelled():raise CoverBatchCancelled()
                    if record.id == pid:
                        result[record.id]=retained
                        checkpoint(index+1)
                        continue
                    preview=self._prepare(record,data['kind'],data['frame_ms'],
                        source if data['kind']=='uploaded' else record.effective_path)
                    generated.append(preview)
                    result[record.id]=self.stage_for_publish(record.id,preview)
                    checkpoint(index+1)
                return {'covers':result,'kind':data['kind'],'frame_ms':data['frame_ms']}
            except Exception:
                for new_token in set(self.staged)-previous:
                    shutil.rmtree(self.staged.pop(new_token)['path'].parent,ignore_errors=True)
                self.stage_sources=aliases
                self.staged_bytes=previous_bytes
                raise
            finally:
                # Internal previews are never exposed; do not accumulate aliases for them.
                for preview in generated:
                    draft=self.drafts.pop(preview,None)
                    if draft:shutil.rmtree(draft['path'].parent,ignore_errors=True)
                    self.stage_sources.pop(preview,None)

    def save(self, connection, token):
        with self.lock:
            draft=self.drafts.get(token)
            if not draft or draft['expires']<time.monotonic():raise CommandRejected('cover_stale')
            record=self._record(connection,draft['id'])
            if record.content_hash!=draft['video_hash'] or self._saved(connection,record.id)!=draft['previous']:
                raise CommandRejected('cover_stale')
            content=draft['path'].read_bytes()
            if hashlib.sha256(content).hexdigest()!=draft['sha256']:raise CommandRejected('cover_stale')
            folder=self.paths.data_dir/'covers';folder.mkdir(parents=True,exist_ok=True)
            target=folder/(draft['sha256']+'.png')
            try:
                with target.open('xb') as f:f.write(content)
            except FileExistsError:
                if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest()!=draft['sha256']:
                    raise ValueError('封面文件校验失败。')
            data={k:draft[k] for k in ('kind','frame_ms','sha256')}
            with session_scope(connection) as session:
                row=session.get(Setting,'finished-cover:'+record.id)
                if row is None:session.add(Setting(key='finished-cover:'+record.id,value_json=json.dumps(data)))
                else:row.value_json=json.dumps(data)
            return {'id':record.id,'kind':data['kind'],'frame_ms':data['frame_ms'],'saved':True}

    def close(self):
        with self.lock:
            self.closed=True
            for draft in self.drafts.values():shutil.rmtree(draft['path'].parent,ignore_errors=True)
            self.drafts.clear()
            for draft in self.staged.values():shutil.rmtree(draft['path'].parent,ignore_errors=True)
            self.staged.clear();self.stage_sources.clear();self.staged_bytes=0
