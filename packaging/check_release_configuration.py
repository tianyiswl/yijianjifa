"""Inspect build-time cloud settings offline; never contact a voice service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from release_configuration import cloud_voice_data


def check_configuration(*, official=False, frozen_root=None, environ=None):
    environment = os.environ if environ is None else environ
    # Exercise the exact configuration generator used by both PyInstaller specs
    # in disposable scratch space, rather than maintaining a second URL policy.
    with tempfile.TemporaryDirectory(prefix='p011-build-config-check-') as scratch:
        datas = cloud_voice_data(scratch, environ=environment)
        endpoint = json.loads(Path(datas[0][0]).read_text(encoding='utf-8'))['endpoint'] if datas else ''
    if official and not endpoint:
        raise ValueError('Official builds require P011_RELEASE_CLOUD_VOICE_ENDPOINT')
    if frozen_root is not None:
        path = Path(frozen_root) / 'cloud-voice-service.json'
        if not endpoint:
            if path.exists():
                raise ValueError('Unexpected cloud voice configuration in an unconfigured build')
        else:
            try:
                configured = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise ValueError('Frozen cloud voice configuration is missing or unreadable') from None
            if configured != {'endpoint': endpoint}:
                raise ValueError('Frozen cloud voice configuration does not match build settings')
    return {'official_build': official, 'cloud_voice_enabled': bool(endpoint),
            'endpoint': endpoint,
            'frozen_config_verified': frozen_root is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official', action='store_true', help='Require an explicitly configured cloud endpoint')
    parser.add_argument('--frozen-root', type=Path, help='Also verify the actual frozen resource directory')
    args = parser.parse_args()
    try:
        report = check_configuration(official=args.official, frozen_root=args.frozen_root)
    except ValueError as error:
        parser.exit(2, str(error) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
