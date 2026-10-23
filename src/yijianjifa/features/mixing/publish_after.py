"""Freeze the V1 minimal post-mix schedule before any rendering starts."""
from datetime import datetime,timedelta,timezone
import hashlib
import math
from yijianjifa.features.accounts.service import AccountService


def freeze(engine,quantity,settings,*,now=None):
    if type(settings) is not dict or not {'account_id','start','interval_hours','linkage'}<=set(settings) or set(settings)-{'account_id','start','interval_hours','linkage','software_consent'}:
        raise ValueError('制作后发布只支持单账号、定时与衔接方式。')
    if type(quantity) is not int or not 1<=quantity<=200:
        raise ValueError('制作后发布数量须为1至200条。')
    if type(settings['account_id']) is not str or not 1<=len(settings['account_id'])<=64:
        raise ValueError('请选择一个抖音账号。')
    if settings['linkage'] not in ('per_item','whole_batch'):
        raise ValueError('请选择逐条或整批衔接。')
    consent=settings.get('software_consent',False)
    if type(consent) is not bool:raise ValueError('软件定时需要明确同意。')
    interval=settings['interval_hours']
    if type(interval) not in (int,float) or not math.isfinite(interval) or not .5<=interval<=336 or interval*2!=int(interval*2):
        raise ValueError('间隔须为0.5小时的倍数。')
    if type(settings['start']) is not str:raise ValueError('请选择定时开始时间。')
    start=datetime.fromisoformat(settings['start'])
    if start.tzinfo is None or start.utcoffset() is None:raise ValueError('开始时间缺少时区。')
    now=now or datetime.now(timezone.utc)
    slots=[{'index':i,'time':(start+timedelta(hours=i*interval)).isoformat()} for i in range(quantity)]
    if start<now+timedelta(hours=2.5) or datetime.fromisoformat(slots[-1]['time'])>now+timedelta(days=13):
        raise ValueError('所有定时时间须在当前时间2.5小时后至13天后之间。')
    account=AccountService(engine).get(settings['account_id'])
    if account is None or account.platform!='douyin' or account.login_state!='valid' or not account.session_ref:
        raise ValueError('当前账号需要登录。')
    return {'version':1,'platform':'douyin','content_type':'normal_video','mode':'scheduled_only',
        'account':{'id':account.id,'platform_user_id':account.platform_user_id,
                   'session_fingerprint':hashlib.sha256(account.session_ref.encode()).hexdigest()},
        'linkage':settings['linkage'],'interval_hours':interval,'slots':slots,'software_consent':consent,
        'execution_enabled':False,'hold_reason':'platform_execution_not_authorized'}
