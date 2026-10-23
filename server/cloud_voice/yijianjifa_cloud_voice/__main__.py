import argparse
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler,WSGIServer,make_server

from .wsgi import create_from_environment


class _SilentHandler(WSGIRequestHandler):
    def log_message(self,format,*args):
        # Standard request logs could retain URLs, identifiers, or accidentally supplied secrets.
        pass


class _ThreadedServer(ThreadingMixIn,WSGIServer):
    daemon_threads=True


def main(argv=None):
    parser=argparse.ArgumentParser(description='本机开发用云配音服务；生产环境请使用受维护的WSGI服务器和HTTPS代理。')
    parser.add_argument('--port',type=int,default=8768)
    args=parser.parse_args(argv)
    app=create_from_environment()
    # No public-address option: operators cannot accidentally expose the development server.
    try:
        with make_server('127.0.0.1',args.port,app,server_class=_ThreadedServer,handler_class=_SilentHandler) as server:
            server.serve_forever()
    finally:
        app.close()


if __name__=='__main__':
    main()
