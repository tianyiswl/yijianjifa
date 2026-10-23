"""Voice choices and bundled official samples; no synthesis on fixed audition."""
import json
import threading
from pathlib import Path
from yijianjifa.features.mixing.tts.volcengine import VOICES, MANIFEST


class VoiceCatalog:
    def __init__(self, paths, media, config):
        self.file = paths.data_dir / 'voice-preferences.json'
        self.media, self.config = media, config
        self.samples = Path(__file__).parents[1] / 'webui/assets/voice_samples'
        self.tokens = {}
        self._lock = threading.RLock()

    def read(self):
        with self._lock:
            try:
                value = json.loads(self.file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                value = {}
            status = self.config.status()
            configured = status['configured']
            allowed = (self.config.available_voice_ids() if configured and callable(getattr(type(self.config), 'available_voice_ids', None))
                       else set(VOICES) if configured else set())
            selected = value.get('selected', '')
            if selected not in VOICES:
                selected = ''
            favorites = [v for v in value.get('favorites', []) if v in VOICES]
            items = MANIFEST
            if status.get('local_test'):
                from .cloud_voice_test import LOCAL_TEST_VOICE
                items = [{**item, 'name': '本地测试音色（系统语音模拟）', 'sample_file': None,
                          'sample_source': None, 'local_test': True, 'style': '流程测试',
                          'scenes': ['本地流程测试'], 'gender': '系统声音'}
                         for item in MANIFEST if item['id'] == LOCAL_TEST_VOICE]
            return {
                'selected': selected, 'favorites': favorites, 'configured': configured,
                'mode': status.get('mode', 'own'),
                'items': [{**item, 'available': item['id'] in allowed,
                           'standard_sample': bool(item.get('sample_file') and
                               (self.samples / item['sample_file']).is_file())}
                          for item in items],
            }

    def save(self, selected, favorites):
        with self._lock:
            if selected not in ('', *VOICES) or not isinstance(favorites, list) or any(v not in VOICES for v in favorites):
                raise ValueError('音色选择无效。')
            if selected and not self.config.status()['configured']:
                raise ValueError('请先选择并登录云配音，或使用系统配音。')
            if selected and callable(getattr(type(self.config), 'available_voice_ids', None)) and selected not in self.config.available_voice_ids():
                raise ValueError('当前配音服务尚未提供这个音色，请重新选择。')
            self.file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.file.with_suffix('.tmp')
            temporary.write_text(json.dumps({'selected': selected, 'favorites': list(dict.fromkeys(favorites))}), encoding='utf-8')
            temporary.replace(self.file)
            return self.read()

    def standard(self, voice):
        with self._lock:
            if voice not in VOICES:
                raise ValueError('请选择支持的音色。')
            if voice not in self.tokens:
                item = next(v for v in MANIFEST if v['id'] == voice)
                name = item.get('sample_file')
                if not name or not (self.samples / name).is_file():
                    raise ValueError('此音色的示例文件不可用，请使用文字试听。')
                token = self.media.registry.grant(self.samples / name)
                self.media.mime_types[token] = 'audio/mpeg'
                self.tokens[voice] = token
            return {'url': 'p011media://asset/' + self.tokens[voice], 'name': VOICES[voice]}
