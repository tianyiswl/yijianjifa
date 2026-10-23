"""Local system-voice audition: copy ID in, revocable audio capability out."""
import math
from pathlib import Path
import shutil
from yijianjifa.os_integration.background_process import run_hidden
import subprocess
import tempfile
import threading

from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.mixing.tts.base import system_tts_adapter
from yijianjifa.os_integration.resources import ffmpeg_path


class VoiceAccess:
    def __init__(self, engine, paths, media, *, adapter=None):
        self._engine, self._paths, self._media = engine, paths, media
        self._adapter = adapter or system_tts_adapter()
        self._token = self._folder = None
        self._closed = False
        self._lock = threading.RLock()

    def voices(self):
        with self._lock:
            if self._closed:raise ValueError('试听已关闭')
            return {'items':[{'id':v.id,'name':v.name} for v in self._adapter.list_voices()]}

    def audition(self, copy_id, voice, speed):
        with self._lock:
            if self._closed:raise ValueError('试听已关闭')
            if type(copy_id) is not str or not 1<=len(copy_id)<=64:raise ValueError('文案编号无效')
            if type(speed) not in (float,int) or not math.isfinite(speed) or not .5<=speed<=2.5:raise ValueError('语速无效')
            if voice is not None and (type(voice) is not str or voice not in {v.id for v in self._adapter.list_voices()}):
                raise ValueError('所选音色暂不可用，请到配音管理选择系统配音或登录云配音。')
            copy = CopyRepository(self._engine).get(copy_id)
            if copy is None or not copy.voice.strip():raise ValueError('当前文案没有可试听的口播稿')
            if len(copy.voice)>4000:raise ValueError('试听口播超过4000字，请选择较短文案；完整制作不受此试听限制')
            return self.audition_text(copy.voice,voice,speed)

    def audition_text(self,text,voice,speed):
        with self._lock:
            if self._closed:raise ValueError('试听已关闭')
            if type(text) is not str or not 1<=len(text.strip())<=4000:raise ValueError('请输入 1—4000 字的试听文字。')
            if type(speed) not in (int,float) or not math.isfinite(speed) or not .5<=speed<=2.5:raise ValueError('试听语速无效。')
            if voice is not None and (type(voice) is not str or voice not in {v.id for v in self._adapter.list_voices()}):raise ValueError('所选音色暂不可用，请到配音管理选择系统配音或登录云配音。')
            folder = Path(tempfile.mkdtemp(prefix='audition-',dir=self._paths.cache_dir))
            try:
                result=self._adapter.synthesize(text,voice,speed,folder/'speech.aiff')
                if result.duration_ms<=0:raise ValueError('系统没有生成有效配音')
                output=folder/'speech.wav'
                run_hidden([str(ffmpeg_path()),'-y','-v','error','-i',str(result.output_path),'-ac','1','-ar','44100','-c:a','pcm_s16le',str(output)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
                token=self._media.registry.grant(output)
                self._media.mime_types[token]='audio/wav'
                self._release()
                self._token,self._folder=token,folder
                return {'url':'p011media://asset/'+token,'duration_ms':result.duration_ms,'text':text,'voice':voice}
            except Exception:
                shutil.rmtree(folder,ignore_errors=True)
                raise

    def _release(self):
        if self._token:
            self._media.registry.revoke(self._token)
            self._media.mime_types.pop(self._token,None)
            self._token=None
        if self._folder:
            shutil.rmtree(self._folder,ignore_errors=True)
            self._folder=None

    def close(self):
        with self._lock:
            self._closed=True
            self._release()
