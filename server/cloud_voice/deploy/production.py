#!/usr/bin/env python3
"""One private Waitress process; no fork, reload, public listener or access log."""
import importlib.metadata
import logging
import signal
import sys


PRODUCTION_PORT = 18769
QA_PORT = 18770
WAITRESS_VERSION = '3.0.2'


class Runtime:
    def __init__(self, app, server, socket_map):
        self.app, self.server, self.socket_map = app, server, socket_map
        self.closed = False

    def run(self):
        try:
            self.server.run()
        except OSError:
            # QA closes the event-loop sockets from its controlling thread.
            # An unexpected running-service socket error must still surface.
            if not self.closed:
                raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        from waitress import wasyncore
        # Stop only the listener first. BaseWSGIServer.close() would also close
        # its trigger while an active channel may still need pull_trigger().
        wasyncore.dispatcher.close(self.server)
        self.server.task_dispatcher.shutdown(timeout=90)
        wasyncore.close_all(self.socket_map)
        if self.server.task_dispatcher.threads:
            # Keep ownership until process exit if a supplier call did not drain.
            raise RuntimeError('云配音请求仍在结束，进程退出后释放数据库锁。')
        self.app.close()


def build_server(*, port=PRODUCTION_PORT):
    if type(port) is not int or port not in (PRODUCTION_PORT, QA_PORT):
        raise ValueError('只允许正式或隔离验收的固定回环端口。')
    if sys.version_info[:2] not in ((3, 12), (3, 13)):
        raise RuntimeError('独立云配音服务需要 Python 3.12 或 3.13。')
    if importlib.metadata.version('waitress') != WAITRESS_VERSION:
        raise RuntimeError('需先安装部署清单锁定的 Waitress 版本。')
    from waitress import create_server, wasyncore
    from waitress.task import ThreadedTaskDispatcher
    from yijianjifa_cloud_voice.wsgi import create_from_environment

    # No user-configurable log format, request body, header or response logging.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('waitress').setLevel(logging.WARNING)
    app = create_from_environment()  # Acquire the DB lock once, in this process.
    sockets = {}
    dispatcher = ThreadedTaskDispatcher()
    dispatcher.set_thread_count(8)
    try:
        server = create_server(app, map=sockets, _dispatcher=dispatcher,
            host='127.0.0.1', port=port, threads=8, connection_limit=32,
            backlog=32, max_request_body_size=1024 * 1024,
            max_request_header_size=16 * 1024, channel_timeout=90,
            cleanup_interval=5, asyncore_loop_timeout=1,
            channel_request_lookahead=0, log_socket_errors=False,
            expose_tracebacks=False, ident='YJJF-Cloud-Voice')
    except BaseException:
        dispatcher.shutdown()
        wasyncore.close_all(sockets)
        app.close()
        raise
    return Runtime(app, server, sockets)


class _StopRequested(Exception):
    pass


def main():
    if len(sys.argv) != 1:
        print('生产入口不接受地址、数据库或公开监听参数。', file=sys.stderr)
        return 2
    def stop(signum, frame):
        raise _StopRequested()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runtime = None
    outcome = 0
    try:
        runtime = build_server()
        runtime.run()
    except _StopRequested:
        pass
    except Exception:
        # Do not dump environment, configuration, HTTP bodies or credentials.
        print('云配音服务未能运行，请核对受限配置、数据库锁及端口。', file=sys.stderr)
        outcome = 1
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                print('云配音服务退出未完成，请核对进程状态后再启动。', file=sys.stderr)
                outcome = 1
    return outcome


if __name__ == '__main__':
    raise SystemExit(main())
