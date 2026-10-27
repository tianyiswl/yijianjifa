"""One source router shared by audition and the Mac/Windows mixing services."""
from contextlib import contextmanager
import json
from pathlib import Path

from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess, _private_json
from .base import system_tts_adapter
from .hosted import HostedTTS, VoicePaused


_RETIRED_MESSAGE = '原配音方式已停用，请选择系统配音或云配音后新建批次；原任务和已完成内容保留，不会自动重做或扣费。'


def _retired_source():
    return VoicePaused('own_voice_retired', _RETIRED_MESSAGE)


class VoiceSourceConfig:
    def __init__(self, own, cloud):
        self.own, self.cloud = own, cloud

    def status(self):
        if self.cloud.mode == 'own':
            # Retain the historical marker without opening the user's old keys.
            # Reinterpreting it as hosted could bill an old default silently.
            return {'configured': False, 'app_id': '', 'mode': 'own',
                    'enabled': bool(self.cloud.endpoint)}
        identity = self.cloud.identity()
        return {'configured': bool(self.cloud.endpoint and identity.get('account_id')), 'local_test': self.cloud.local_test,
                'app_id': '', 'mode': 'hosted', 'enabled': bool(self.cloud.endpoint)}

    def save(self, app_id, token):
        raise ValueError('客户端不再提供个人配音服务配置，请选择系统配音或云配音。')

    def require_default_voice_credit(self):
        """Changing a default needs fresh usable credit, without reserving words."""
        try:
            state = self.cloud.read()
        except VoicePaused:
            raise ValueError('暂时无法更新配音字数，请检查网络后重试。默认配音未更改。') from None
        if not state.get('enabled') or not state.get('authenticated'):
            raise ValueError('请先登录云配音，再设置默认配音。')
        available = state.get('available_words')
        if state.get('balance_current') is not True or type(available) is not int or available < 0:
            raise ValueError('配音字数暂未更新，请检查网络后重试。默认配音未更改。')
        if available == 0:
            raise ValueError('配音字数不足，请充值后再设为默认配音。')

    def available_voice_ids(self):
        if self.cloud.mode == 'own':
            return set()
        if not self.cloud.endpoint or not self.cloud.identity().get('account_id'):
            return set()
        try:
            return {item['id'] for item in self.cloud.available_voices()}
        except VoicePaused:
            # Bundled free samples remain useful when the hosted session is
            # expired. No alternate synthesis source is selected here.
            return set()


class VoiceSourceTTS:
    def __init__(self, config, own, hosted, system):
        self.config, self.own, self.hosted, self.system = config, own, hosted, system

    def list_voices(self):
        if self.config.cloud.mode == 'own':
            return []
        if not self.config.status()['configured']:
            return []
        return self.hosted.list_voices()

    def synthesize(self, text, voice, speed, output):
        if not voice or not voice.startswith('doubao:'):
            return self.system.synthesize(text, voice, speed, output)
        if self.config.cloud.mode == 'own':
            raise _retired_source()
        return self.hosted.synthesize(text, voice, speed, output)

    def freeze(self, snapshot):
        voice = snapshot.get('options', {}).get('voice')
        if not voice or not voice.startswith('doubao:') or not any(copy.get('voice', '').strip() for copy in snapshot.get('copies', [])):
            return {'source': 'system'}
        if self.config.cloud.mode == 'hosted':
            identity = self.hosted.freeze(snapshot)
            # Validate the current service catalog before a task can be queued.
            # A persisted preset is not evidence that a voice is still enabled.
            allowed = {item['id'] for item in self.config.cloud.available_voices()}
            if voice not in allowed:
                raise VoicePaused('voice_unavailable', '所选音色尚未开通，请先到配音管理选择可用音色，再开始制作。')
            return identity
        raise _retired_source()

    def _identity(self, snapshot):
        identity = snapshot.get('voice_identity')
        if isinstance(identity, dict):
            return identity
        # Before this feature all doubao requests used the user's own account.
        # Never turn a historical batch into a newly charged hosted batch.
        voice = snapshot.get('options', {}).get('voice')
        return {'source': 'own'} if voice and voice.startswith('doubao:') else {'source': 'system'}

    def _validate(self, snapshot):
        identity = self._identity(snapshot)
        source = identity.get('source')
        if source == 'system':
            return source
        if source == 'own':
            raise _retired_source()
        if source != self.config.cloud.mode:
            raise VoicePaused('voice_source_changed', '请恢复原批次的云配音来源后继续，不会自动更换声音。')
        if source == 'hosted':
            self.hosted._assert_account(identity)
        else:
            raise VoicePaused('voice_source_changed', '原批次配音来源无效，请联系支持。')
        return source

    def estimate_batch(self, snapshot):
        source = self._identity(snapshot).get('source')
        if source == 'own':
            raise _retired_source()
        if source == 'hosted':
            return self.hosted.estimate_batch(snapshot)
        return {'source': source, 'words': 0, 'exact': True, 'account_id': None, 'available_words': None}

    @contextmanager
    def batch_context(self, task_id, snapshot):
        source = self._validate(snapshot)
        if source == 'hosted':
            with self.hosted.batch_context(task_id, snapshot) as adapter:
                yield adapter
        else:
            yield self.system

    def release_batch(self, task_id, snapshot=None):
        if snapshot and self._identity(snapshot).get('source') == 'hosted':
            self.hosted.release_batch(task_id, snapshot)


def build_voice_services(paths, *, endpoint=None, local_test=False):
    cloud = CloudVoiceAccess(paths, endpoint=endpoint, local_test=local_test)
    if not cloud.mode_path.exists():
        preferences = Path(paths.data_dir) / 'voice-preferences.json'
        try:
            selected = json.loads(preferences.read_text(encoding='utf-8')).get('selected', '')
        except (OSError, ValueError, AttributeError):
            selected = ''
        if selected and selected.startswith('doubao:'):
            # Retain a non-billable legacy marker until the user explicitly
            # chooses a supported source. Never read or migrate old keys.
            _private_json(cloud.mode_path, {'mode': 'own'})
        else:
            # Persist the new-install default before the first cloud voice is
            # selected, so the next launch cannot mistake it for a legacy one.
            _private_json(cloud.mode_path, {'mode': 'hosted'})
    config = VoiceSourceConfig(None, cloud)
    system = system_tts_adapter()
    tts = VoiceSourceTTS(config, None, HostedTTS(cloud, paths.cache_dir), system)
    return config, tts, cloud
