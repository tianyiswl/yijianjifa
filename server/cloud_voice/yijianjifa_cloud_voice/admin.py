"""Legacy server-only CLI; the optional operator Web application is separate."""

import argparse
import json
import os
from pathlib import Path
import sys

from .credits import CreditService
from .errors import CloudError
from .store import Store


def main(argv=None):
    parser=argparse.ArgumentParser(description='一剪即发云配音服务端运营工具')
    parser.add_argument('--database',required=True,help='经营方服务器数据库路径')
    sub=parser.add_subparsers(dest='command',required=True)
    issue=sub.add_parser('issue',help='确认外部收款后生成固定套餐兑换码')
    issue.add_argument('--count',type=int,default=1)
    issue.add_argument('--order-ref',required=True)
    issue.add_argument('--output',required=True,help='仅经营方可读的新文件；禁止覆盖已有文件')
    issue.add_argument('--confirm-payment-received',action='store_true',required=True)
    status=sub.add_parser('status',help='按兑换码记录编号查询状态，不显示原码')
    status.add_argument('code_id')
    revoke=sub.add_parser('revoke',help='作废尚未兑换的码')
    revoke.add_argument('code_id')
    ledger=sub.add_parser('usage',help='按配音账号编号查询字数流水')
    ledger.add_argument('account_id')
    ledger.add_argument('--page',type=int,default=0)
    balance=sub.add_parser('balance',help='查询配音账号额度')
    balance.add_argument('account_id')
    args=parser.parse_args(argv)
    credits=CreditService(Store(args.database))
    try:
        if args.command=='issue':
            destination=Path(args.output)
            # Open exclusively before issuing. A failed output cannot silently overwrite codes.
            descriptor=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:
                with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                    issued=credits.issue(args.count,order_ref=args.order_ref)
                    stream.write(json.dumps({'pack':{'price_fen':1990,'words':10000},'codes':issued},ensure_ascii=False,indent=2))
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                # A write failure requires operator reconciliation; do not issue again automatically.
                raise CloudError('issuance_output_failed','发码输出未能完整保存，请核对原批次记录，勿直接再次发码。',500) from None
            print(json.dumps({'issued_count':len(issued),'batch_id':issued[0]['batch_id'],
                              'message':'兑换码已保存到受限文件，终端不显示原码。'},ensure_ascii=False))
        elif args.command=='status':
            print(json.dumps(credits.code_status(args.code_id),ensure_ascii=False))
        elif args.command=='revoke':
            print(json.dumps(credits.revoke(args.code_id),ensure_ascii=False))
        elif args.command=='usage':
            print(json.dumps(credits.usage(args.account_id,args.page),ensure_ascii=False))
        else:
            print(json.dumps(credits.balance(args.account_id),ensure_ascii=False))
    except (CloudError,OSError) as exc:
        message=exc.message if isinstance(exc,CloudError) else '无法访问指定文件，请检查经营方服务器路径和权限。'
        print(message,file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
