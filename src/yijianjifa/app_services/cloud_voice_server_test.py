"""Source-only real-server acceptance; never reuse customer or local-demo data."""
import json
import os
from pathlib import Path
import sys


SERVER_TEST_ENDPOINT = 'https://39.108.82.213'
_CLIENT_KIND = 'yjjf-server-cloud-voice-client-v1'
_MARKER = '.server-cloud-voice-test.json'


def prepare_server_test_client(data_dir):
    if getattr(sys, 'frozen', False):
        raise ValueError('服务器配音联调仅限源码客户端。')
    directory = Path(data_dir)
    if not directory.is_absolute():
        raise ValueError('服务器配音联调资料目录必须使用绝对路径。')
    if (directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents)
            or (directory.exists() and any(path.is_symlink() for path in directory.rglob('*')))):
        raise ValueError('服务器配音联调资料不能使用符号链接。')
    expected = {'kind': _CLIENT_KIND, 'data_dir': str(directory.resolve()),
                'endpoint': SERVER_TEST_ENDPOINT}
    if directory.exists() and any(directory.iterdir()):
        try:
            value = json.loads((directory / _MARKER).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            value = None
        if value != expected:
            raise ValueError('请选择独立的服务器配音联调资料目录，不能复用原资料或本地模拟资料。')
    else:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(directory / _MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(expected, stream, ensure_ascii=False)
    return SERVER_TEST_ENDPOINT
