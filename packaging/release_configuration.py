"""Explicit, non-secret service defaults for an official frozen build."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def cloud_voice_data(root, *, environ=None):
    environment = os.environ if environ is None else environ
    configured = environment.get('P011_RELEASE_CLOUD_VOICE_ENDPOINT', '')
    try:
        # Keep this standalone build helper independent of the installed app.
        # Validate raw input before urlsplit/strip silently discard controls.
        if not isinstance(configured, str) or any(ord(char) < 32 or ord(char) == 127 for char in configured):
            raise ValueError()
        configured.encode('utf-8')
        endpoint = configured.strip().rstrip('/')
        if not endpoint:
            # A prior build's generated file is deliberately not picked up.
            return []
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname
                or any(char.isspace() for char in parsed.netloc)
                or parsed.username is not None or parsed.password is not None
                or '?' in endpoint or '#' in endpoint):
            raise ValueError()
        parsed.port
    except (ValueError, UnicodeError):
        raise ValueError('Cloud voice build configuration requires a credential-free HTTPS endpoint') from None
    target = Path(root) / 'build/release-config/cloud-voice-service.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'endpoint': endpoint}, ensure_ascii=False) + '\n', encoding='utf-8')
    return [(str(target), '.')]
