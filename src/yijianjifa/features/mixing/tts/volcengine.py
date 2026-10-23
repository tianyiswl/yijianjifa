"""Doubao Speech 2.0: local Keychain configuration and bounded HTTP synthesis."""
import base64
import hashlib
import json
import math
import shutil
import threading
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .base import VoiceInfo, VoiceResult, system_tts_adapter
from .macos_say import _probe_duration_ms

MANIFEST=json.loads((Path(__file__).with_name('voice_manifest.json')).read_text(encoding='utf-8'))['items']
VOICES={item['id']:item['name'] for item in MANIFEST}

class VolcConfig:
    def __init__(self, data_dir):
        self.service = 'yijianjifa.volcengine.' + hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:16]
    def _read(self):
        import keyring
        value = keyring.get_password(self.service, 'speech')
        return json.loads(value) if value else {}
    def status(self):
        value = self._read()
        return {'configured': bool(value.get('token')), 'app_id': value.get('app_id', '')}
    def save(self, app_id, token):
        if not isinstance(app_id, str) or not app_id.isdigit() or len(app_id)>32:
            raise ValueError('请填写正确的 APP ID。')
        if not isinstance(token, str) or len(token)>4096 or any(c.isspace() for c in token):
            raise ValueError('Access Token 格式不正确。')
        old = self._read()
        token = token or (old.get('token') if old.get('app_id')==app_id else '')
        if not token: raise ValueError('请填写该应用的 Access Token。')
        import keyring
        keyring.set_password(self.service, 'speech', json.dumps({'app_id':app_id,'token':token}))
        return self.status()

class RoutedTTS:
    def __init__(self, config, cache_dir, system=None, opener=urlopen):
        self.config, self.cache = config, Path(cache_dir)/'doubao-tts'
        self.system, self.opener = system or system_tts_adapter(), opener
        self.lock = threading.RLock()
    def list_voices(self):
        return [VoiceInfo(k,v) for k,v in VOICES.items()] if self.config.status()['configured'] else []
    def synthesize(self, text, voice, speed, output):
        if not voice or not voice.startswith('doubao:'):
            return self.system.synthesize(text, voice, speed, output)
        if voice not in VOICES: raise ValueError('此火山音色暂不支持，请重新选择。')
        if not isinstance(text,str) or not text.strip() or len(text)>4000:
            raise ValueError('火山配音每段需为 1—4000 字。')
        if not math.isfinite(speed) or not .5<=speed<=2:
            raise ValueError('火山配音语速支持 0.5—2 倍。')
        with self.lock:
            credentials=self.config._read()
            if not credentials.get('token'): raise ValueError('请先在设置中配置火山配音。')
            key=hashlib.sha256(json.dumps([credentials['app_id'],'seed-tts-2.0',text,voice,speed,'mp3'],ensure_ascii=False).encode()).hexdigest()
            self.cache.mkdir(parents=True,exist_ok=True)
            cached=self.cache/(key+'.mp3')
            if not cached.exists():
                body={'user':{'uid':'yijianjifa'},'req_params':{'text':text,'speaker':voice.split(':',1)[1],
                      'audio_params':{'format':'mp3','sample_rate':24000,'speech_rate':round((speed-1)*100)}}}
                request=Request('https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse',
                    data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Api-App-Id':credentials['app_id'],
                    'X-Api-Access-Key':credentials['token'],'X-Api-Resource-Id':'seed-tts-2.0','X-Api-Request-Id':str(uuid.uuid4())})
                chunks=[]; complete=False; size=0
                try:
                    with self.opener(request,timeout=60) as response:
                        for raw in response:
                            if len(raw)>16*1024*1024: raise ValueError('火山返回的数据过大。')
                            if not raw.startswith(b'data:'): continue
                            event=json.loads(raw[5:])
                            if event.get('code') not in (0,20000000): raise ValueError('火山配音未完成，请检查服务权限和剩余额度。')
                            if event.get('data'):
                                chunk=base64.b64decode(event['data'],validate=True);size+=len(chunk)
                                if size>32*1024*1024:raise ValueError('配音音频过大。')
                                chunks.append(chunk)
                            if event.get('code')==20000000:complete=True
                    if not complete or not chunks:raise ValueError('火山配音连接中断，未保存不完整音频，请稍后重试。')
                    temporary=self.cache/(key+'.partial.mp3')
                    try:
                        temporary.write_bytes(b''.join(chunks))
                        if _probe_duration_ms(temporary)<=0:raise ValueError('火山未返回有效音频。')
                        temporary.replace(cached)
                    finally:temporary.unlink(missing_ok=True)
                except HTTPError as exc:
                    raise ValueError('火山认证失败，请检查 APP ID 和 Access Token。' if exc.code in (401,403) else '火山服务暂不可用，请检查额度后重试。') from None
                except (URLError,TimeoutError):raise ValueError('火山配音连接超时，请稍后重试。') from None
            duration=_probe_duration_ms(cached)
            target=Path(output).with_suffix('.mp3');target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(cached,target)
            return VoiceResult(target,duration)
