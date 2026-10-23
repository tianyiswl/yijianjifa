"""Start the isolated source client against the operator's verified HTTPS IP."""
import argparse
from pathlib import Path

from yijianjifa.app_services.cloud_voice_server_test import prepare_server_test_client
from yijianjifa.os_integration.paths import AppPaths
from .single_instance import InstanceLock


def main():
    parser = argparse.ArgumentParser(description='源码客户端服务器云配音联调（真实服务、独立资料）')
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    endpoint = prepare_server_test_client(args.data_dir)
    data = args.data_dir.resolve()
    paths = AppPaths(data, data / 'cache', data / 'logs', data / 'managed_media')
    for path in (paths.cache_dir, paths.logs_dir, paths.managed_media_dir):
        path.mkdir(parents=True, exist_ok=True)
    from .mac_app import run
    with InstanceLock(data):
        run(paths, host_lock_held=True, cloud_voice_endpoint=endpoint)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
