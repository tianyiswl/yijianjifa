#!/usr/bin/env python3
"""Private production operator process, separate from the customer voice API."""
import importlib.metadata
import logging
import os
from pathlib import Path
import signal
import stat
import sys


PRODUCTION_DATABASE = '/var/lib/yijianjifa-cloud-voice/voice.db'
PRODUCTION_ORIGIN = 'https://39.108.82.213'
PRODUCTION_PORT = 18771
WAITRESS_VERSION = '3.0.2'


class Runtime:
    def __init__(self, app, server, socket_map):
        self.app, self.server, self.socket_map = app, server, socket_map
        self.closed = False

    def run(self):
        try:
            self.server.run()
        except OSError:
            if not self.closed:
                raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        from waitress import wasyncore
        # Keep the trigger alive until active requests have drained. The Store
        # owns no persistent connection; each transaction closes its connection.
        wasyncore.dispatcher.close(self.server)
        self.server.task_dispatcher.shutdown(timeout=90)
        wasyncore.close_all(self.socket_map)
        if self.server.task_dispatcher.threads:
            raise RuntimeError('经营管理请求仍在结束，请核对进程状态。')


def _configuration_path(value):
    if not isinstance(value, (str, Path)) or not value:
        raise ValueError('请通过受限环境指定独立经营管理配置。')
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('经营管理配置必须使用无符号链接的绝对路径。')
    info, parent = path.stat(), path.parent.stat()
    if (not stat.S_ISREG(info.st_mode) or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(parent.st_mode) != 0o700
            or info.st_uid != os.geteuid() or parent.st_uid != os.geteuid()):
        raise ValueError('经营配置需由运行用户拥有，文件0600，目录0700。')
    return path


def build_server(*, config_path=None, expected_database=None, port=None):
    """Build a production listener; explicit temporary DB/port support local QA."""
    if sys.version_info[:2] not in ((3, 12), (3, 13)):
        raise RuntimeError('独立经营管理服务需要 Python 3.12 或 3.13。')
    if importlib.metadata.version('waitress') != WAITRESS_VERSION:
        raise RuntimeError('经营管理需安装部署清单锁定的 Waitress 版本。')
    database = Path(PRODUCTION_DATABASE if expected_database is None else expected_database)
    port = PRODUCTION_PORT if port is None else port
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('经营管理监听端口不正确。')
    if database == Path(PRODUCTION_DATABASE) and port != PRODUCTION_PORT:
        raise ValueError('正式经营管理只允许固定回环端口。')
    if not database.is_absolute() or database.is_symlink() or any(parent.is_symlink() for parent in database.parents):
        raise ValueError('经营管理需使用明确的独立正式数据库。')
    from waitress import create_server, wasyncore
    from waitress.task import ThreadedTaskDispatcher
    from yijianjifa_cloud_voice.operator import load_operator_config
    from yijianjifa_cloud_voice.operator_web import create_operator_app
    from yijianjifa_cloud_voice.store import Store
    from yijianjifa_cloud_voice.billing import SupplierBalance

    # The supplier environment is neither imported nor parsed by this process.
    source = _configuration_path(config_path if config_path is not None else os.environ.get('YJJF_OPERATOR_CONFIG'))
    config = load_operator_config(source)
    if (config['test_mode'] is not False or config['origin'] != PRODUCTION_ORIGIN
            or config['database'] != str(database)):
        raise ValueError('经营管理仅接入正式字数库和指定HTTPS地址，不接受测试配置。')
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('waitress').setLevel(logging.WARNING)
    store = Store(database)
    app = create_operator_app(store, password_hash=config['password_hash'],
                              operator_code_key=config['operator_code_key'],
                              origin=config['origin'], test_mode=False,
                              supplier_balance=SupplierBalance(source.with_name('billing.json')))
    sockets = {}
    dispatcher = ThreadedTaskDispatcher()
    dispatcher.set_thread_count(4)
    try:
        server = create_server(app, map=sockets, _dispatcher=dispatcher,
            host='127.0.0.1', port=port, threads=4, connection_limit=16,
            backlog=16, max_request_body_size=4096,
            max_request_header_size=16 * 1024, channel_timeout=60,
            cleanup_interval=5, asyncore_loop_timeout=1,
            channel_request_lookahead=0, log_socket_errors=False,
            expose_tracebacks=False, ident='YJJF-Operator')
    except BaseException:
        dispatcher.shutdown()
        wasyncore.close_all(sockets)
        raise
    return Runtime(app, server, sockets)


class _StopRequested(Exception):
    pass


def main():
    if len(sys.argv) != 1:
        print('正式经营管理入口不接受地址、数据库或口令参数。', file=sys.stderr)
        return 2

    def stop(signum, frame):
        raise _StopRequested()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runtime, outcome = None, 0
    try:
        runtime = build_server()
        runtime.run()
    except _StopRequested:
        pass
    except Exception:
        # Never print configuration, environment, passwords or code ciphertext.
        print('经营管理服务未能运行，请核对私有配置、正式数据库和回环端口。', file=sys.stderr)
        outcome = 1
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                print('经营管理退出未完成，请核对进程状态后再启动。', file=sys.stderr)
                outcome = 1
    return outcome


if __name__ == '__main__':
    raise SystemExit(main())
