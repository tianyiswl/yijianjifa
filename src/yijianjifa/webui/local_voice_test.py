"""Start an isolated source client for the explicitly marked local voice demo."""
import argparse
from pathlib import Path

from yijianjifa.app_services.cloud_voice_test import prepare_test_client
from yijianjifa.os_integration.paths import AppPaths
from .single_instance import InstanceLock


def main():
    parser = argparse.ArgumentParser(description='源码客户端本地云配音流程测试')
    parser.add_argument('--service-info', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    endpoint = prepare_test_client(args.service_info, args.data_dir)
    data = args.data_dir.resolve()
    paths = AppPaths(data, data / 'cache', data / 'logs', data / 'managed_media')
    for path in (paths.cache_dir, paths.logs_dir, paths.managed_media_dir):
        path.mkdir(parents=True, exist_ok=True)
    from .mac_app import run
    with InstanceLock(data):
        run(paths, host_lock_held=True, cloud_voice_endpoint=endpoint, local_voice_test=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
