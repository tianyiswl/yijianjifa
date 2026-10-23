"""Explicit setup and loopback listener for the private operator panel."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys
from wsgiref.simple_server import make_server
from urllib.parse import urlsplit

from .__main__ import _SilentHandler, _ThreadedServer
from .auth import _hash_password
from .operator_web import create_operator_app, validate_origin
from .store import Store
from .billing import SupplierBalance, save_credentials

_KIND='yjjf-cloud-voice-operator-v1'
_FIELDS={'kind','database','password_hash','operator_code_key','origin','test_mode'}


def _safe_path(value):
    path=Path(value)
    if not path.is_absolute() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('管理配置和数据库必须使用无符号链接的绝对路径。')
    return path


def validate_port(value):
    if type(value) is not int or not 1<=value<=65535:raise ValueError('管理端口需为1至65535。')
    return value


def create_operator_config(destination,database,password,*,origin,test_mode=False):
    from cryptography.fernet import Fernet
    path,database=_safe_path(destination),_safe_path(database)
    if not database.is_file():raise ValueError('请先选择已经初始化的独立云配音数据库。')
    if not isinstance(password,str) or not 6<=len(password)<=50:
        raise ValueError('经营管理密码需为6至50位，请勿使用配音用户的密码。')
    origin=validate_origin(origin)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.parent.stat().st_mode&0o077:raise ValueError('管理配置目录必须仅经营服务账号可读。')
    payload={'kind':_KIND,'database':str(database),'password_hash':_hash_password(password),
             'operator_code_key':Fernet.generate_key().decode(),'origin':origin,'test_mode':bool(test_mode)}
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
        json.dump(payload,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
    return path


def load_operator_config(source):
    from cryptography.fernet import Fernet
    path=_safe_path(source)
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or path.parent.stat().st_mode&0o077:
        raise ValueError('管理配置和目录必须为私有权限，请勿放在网站或共享目录。')
    if info.st_size>8192:raise ValueError('管理配置不正确。')
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(payload,dict) or set(payload)!=_FIELDS or payload['kind']!=_KIND or
                type(payload['test_mode']) is not bool or
                not re.fullmatch(r'pbkdf2_sha256\$600000\$[0-9a-f]{32}\$[0-9a-f]{64}',payload['password_hash'])):
            raise ValueError()
        Fernet(payload['operator_code_key'])
        payload['origin']=validate_origin(payload['origin'])
        if not _safe_path(payload['database']).is_file():raise ValueError()
    except (ValueError,TypeError,KeyError):raise ValueError('管理配置不完整，服务未启动；不会覆盖原密钥。') from None
    return payload


def main(argv=None):
    parser=argparse.ArgumentParser(description='经营者独立兑换码管理，默认仅本机访问，不读取供应商密钥。')
    sub=parser.add_subparsers(dest='command',required=True)
    setup=sub.add_parser('init',help='首次设置独立管理密码和加密码密钥，不覆盖旧配置')
    setup.add_argument('--config',type=Path,required=True);setup.add_argument('--database',type=Path,required=True)
    setup.add_argument('--origin',required=True);setup.add_argument('--test-mode',action='store_true')
    billing=sub.add_parser('billing-init',help='单独保存只读费用密钥，不覆盖已有配置')
    billing.add_argument('--config',type=Path,required=True)
    serve=sub.add_parser('serve',help='只监听127.0.0.1，公网须经独立HTTPS部署审查')
    serve.add_argument('--config',type=Path,required=True);serve.add_argument('--port',type=int,default=18771)
    serve.add_argument('--billing-config',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=='billing-init':
            ak=getpass.getpass('只读费用 Access Key ID（不回显）：')
            sk=getpass.getpass('只读费用 Secret Access Key（不回显）：')
            save_credentials(args.config,ak,sk)
            print('只读费用配置已保存；请在管理页刷新余额。',flush=True)
            return 0
        if args.command=='init':
            if args.config.exists() or args.config.is_symlink():
                print('管理配置已存在；不会覆盖原密码或经营密钥，请先核对已有配置。',file=sys.stderr);return 1
            first=getpass.getpass('设置经营管理密码（6至50位，不回显）：')
            if not 6<=len(first)<=50:
                print('经营管理密码需为6至50位；未保存，请重新执行设置。',file=sys.stderr);return 1
            second=getpass.getpass('再次输入管理密码：')
            if first!=second:
                print('两次密码不一致；未保存，请重新执行设置。',file=sys.stderr);return 1
            create_operator_config(args.config,args.database,first,origin=args.origin,test_mode=args.test_mode)
            print('管理配置已保存。请单独安全备份此文件，切勿交给软件用户。',flush=True)
            return 0
        config=load_operator_config(args.config);port=validate_port(args.port)
        address=urlsplit(config['origin'])
        if address.scheme=='http' and (address.hostname!='127.0.0.1' or address.port!=port):
            raise ValueError('本机管理地址和实际监听端口需一致。')
        store=Store(config['database'])
        app=create_operator_app(store,password_hash=config['password_hash'],operator_code_key=config['operator_code_key'],
                                origin=config['origin'],test_mode=config['test_mode'],
                                supplier_balance=SupplierBalance(args.billing_config or args.config.with_name('billing.json')))
        with make_server('127.0.0.1',port,app,server_class=_ThreadedServer,handler_class=_SilentHandler) as server:
            print('兑换码管理已启动，仅监听本机。'+(' 测试环境，无真实收款。' if config['test_mode'] else ''),flush=True)
            print('访问地址：'+config['origin']+'/operator/',flush=True)
            try:server.serve_forever()
            except KeyboardInterrupt:pass
    except ImportError:
        print('经营管理需要安装本服务的operator可选依赖。原云配音服务不受影响。',file=sys.stderr);return 1
    except OSError as error:
        if args.command=='init':
            message={13:'管理配置保存失败：目录或数据库访问权限不足。',17:'管理配置已存在；不会覆盖原密码或经营密钥。'}.get(error.errno,'管理配置保存失败：请核对私有目录、磁盘空间和文件状态。')
            print(message,file=sys.stderr);return 1
        print('管理服务未能启动，请核对私有配置、数据库和端口；原配置不会被覆盖。',file=sys.stderr);return 1
    except ValueError:
        if args.command=='init':
            print('管理配置校验失败：请核对独立数据库、私有目录权限和HTTPS地址；未覆盖原配置。',file=sys.stderr);return 1
        print('管理服务未能启动，请核对私有配置、数据库和端口；原配置不会被覆盖。',file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
