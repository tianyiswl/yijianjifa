"""Public platform facts and exact host-only transport routing.

Transport presence describes an injected implementation, not platform acceptance,
login, scheduling support, or public visibility. This catalog performs no IO.
"""
from collections.abc import Mapping


_PLATFORMS = {
    'douyin': ('抖音', ('ordinary', 'platform_schedule'),
               ('publish_now', 'custom_cover', 'result_query', 'background_execution')),
    'kuaishou': ('快手', (), ('login', 'ordinary', 'platform_schedule', 'result_query', 'background_execution')),
    'channels': ('视频号', (), ('login', 'ordinary', 'platform_schedule', 'result_query', 'background_execution')),
    'bilibili': ('B站', (), ('login', 'ordinary', 'platform_schedule', 'result_query', 'background_execution')),
}


class PlatformCatalog:
    def __init__(self, *, transports=None):
        if transports is not None and not isinstance(transports, Mapping):
            raise ValueError('invalid platform transports')
        self._transports = {}
        for platform, transport in (transports or {}).items():
            base, content_type = self._route(platform)
            self.get(base)
            if not callable(transport) and not (
                    callable(getattr(transport, 'submit', None)) and
                    callable(getattr(transport, 'reconcile', None))):
                raise ValueError('host transport must be callable or a submit/reconcile adapter')
            if getattr(transport, 'platform_id', base) != base:
                raise ValueError('transport platform mismatch')
            if content_type != 'video' and getattr(transport, 'content_type', None) != content_type:
                raise ValueError('transport content type mismatch')
            self._transports[platform] = transport

    def get(self, platform):
        if type(platform) is not str or platform not in _PLATFORMS:
            raise ValueError('unsupported platform')
        label, verified, pending = _PLATFORMS[platform]
        return {'platform': platform, 'label': label,
                'verified_capabilities': list(verified),
                'pending_capabilities': list(pending),
                'transport_available': platform in self._transports}

    def list(self):
        return [self.get(platform) for platform in _PLATFORMS]

    def transport_for(self, platform):
        base, _ = self._route(platform)
        self.get(base)
        if platform not in self._transports:
            raise ValueError('platform transport is not integrated')
        return self._transports[platform]

    @staticmethod
    def _route(key):
        if type(key) is not str:
            raise ValueError('unsupported platform route')
        if ':' not in key:
            return key, 'video'
        if key != 'douyin:image_post':
            raise ValueError('unsupported platform content route')
        return 'douyin', 'image_post'

    def transport_for_content(self, platform, content_type='video'):
        # Images never fall through to a video's upload or capability contract.
        if content_type not in {'video', 'image_post'}:
            raise ValueError('unsupported content type')
        return self.transport_for(platform if content_type == 'video' else platform + ':' + content_type)
