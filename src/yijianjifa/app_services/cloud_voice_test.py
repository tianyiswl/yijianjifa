"""Explicit source-only loopback test setup; never a customer configuration."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


LOCAL_TEST_KIND = 'yjjf-local-cloud-voice-test-v1'
LOCAL_CLIENT_KIND = 'yjjf-local-cloud-voice-client-v1'
LOCAL_TEST_VOICE = 'doubao:zh_female_vv_uranus_bigtts'
_MARKER = '.local-cloud-voice-test.json'


def validate_test_endpoint(endpoint):
    if getattr(sys, 'frozen', False):
        raise ValueError('本地配音测试仅限源码客户端。')
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        valid = (parts.scheme == 'http' and parts.hostname == '127.0.0.1'
                 and port is not None and 1 <= port <= 65535
                 and parts.netloc == f'127.0.0.1:{port}'
                 and not parts.path and not parts.query and not parts.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError('本地配音测试只能连接明确端口的本机回环地址。')
    return endpoint


def require_test_client(data_dir):
    directory = Path(data_dir)
    _reject_links(directory)
    try:
        marker = json.loads((directory / _MARKER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        marker = None
    if (not isinstance(marker, dict) or marker.get('kind') != LOCAL_CLIENT_KIND
            or marker.get('data_dir') != str(directory.resolve())):
        raise ValueError('本地配音测试必须使用独立标记的测试资料目录。')


def _reject_links(directory):
    if (directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents)
            or (directory.exists() and any(path.is_symlink() for path in directory.rglob('*')))):
        raise ValueError('本地测试资料不能使用指向其他位置的符号链接。')


def prepare_test_client(service_info, data_dir):
    descriptor = Path(service_info)
    directory = Path(data_dir)
    if not descriptor.is_absolute() or not directory.is_absolute():
        raise ValueError('请使用本地测试服务和资料目录的绝对路径。')
    try:
        info = json.loads(descriptor.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('无法读取本地测试服务信息，请先启动测试服务。') from None
    if (not isinstance(info, dict) or info.get('kind') != LOCAL_TEST_KIND
            or info.get('local_test') is not True):
        raise ValueError('请选择专用本地配音测试服务。')
    endpoint = validate_test_endpoint(info.get('endpoint'))
    _reject_links(directory)
    if directory.exists():
        if any(directory.iterdir()):
            require_test_client(directory)
            return endpoint
    else:
        directory.mkdir(parents=True, mode=0o700)
    with os.fdopen(os.open(directory / _MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
        json.dump({'kind': LOCAL_CLIENT_KIND, 'data_dir': str(directory.resolve()),
                   'service_info': str(descriptor)}, stream)
    return endpoint
