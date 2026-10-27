"""Finite download bridge. Only short writes and safe DTO reads use this lane.

Runtime workers own network/hash/media/recognition. Native selections are taken
outside the receipt transaction, after checking a previously committed receipt.
"""
import hashlib
import json
import re
import threading
from typing import get_args
from sqlalchemy import text
from .library_queries import _unique_object
from yijianjifa.features.downloads.contracts import DownloadError, DownloadOptions, ErrorCode, Region

MODEL_IDS=('whisper-small-multilingual','rapidocr-cls-mobile-v2','rapidocr-det-ppocrv6-small','rapidocr-rec-ppocrv6-small')
MESSAGES={
 'single_category_only':'每条内容只能选择一个分类。',
 'input_limit':'输入数量、长度或设置超出限制，请检查后分批操作。',
 'unsafe_source':'此链接或文件位置不符合安全读取范围，请重新选择。',
 'redirect_limit':'链接跳转次数过多，请重新粘贴作品链接。',
 'size_limit':'文件超过大小限制，当前下载未完成。',
 'timeout':'当前步骤超时，成功结果已保留，可重试未完成步骤。',
 'disk_space':'保存位置空间不足，请腾出空间或更换目录。',
 'cancelled':'已取消当前操作，成功结果保留。',
 'network':'网络读取未完成，请检查连接后重试。',
 'source_missing':'读取会话或来源已失效，请重新读取作品链接。',
 'unsupported_source':'此来源或操作暂不支持，请检查所选内容。',
 'metadata_missing':'未取得完整的作品或任务资料，请重新读取。',
 'verification_required':'需要登录或手动验证，请打开验证窗口后继续。',
 'account_busy':'此账号正在执行其他操作，请结束后重试。',
 'account_unavailable':'默认抖音账号不可用，请到账号管理检查。',
 'session_missing':'账号还没有可复用的登录会话，请到账号管理登录。',
 'session_invalid':'账号登录档案不可用，请重新登录后重试。',
 'identity_mismatch':'文件归属或账号身份发生变化，已停止操作，请重新核对。',
 'no_audio':'此视频没有可提取的音轨，成功的视频结果保留。',
 'media_invalid':'文件格式、时长或完整性检查未通过，请重新下载。',
 'media_tool_missing':'本机处理组件缺失，请使用完整测试包或联系店铺。',
 'media_failed':'本机媒体处理未完成，成功结果保留，可检查后重试。',
 'model_required':'所需字幕识别模型尚未安装，请下载或离线导入模型。',
 'model_invalid':'识别模型校验未通过，请重新下载或导入正确文件。',
 'model_unknown':'此识别模型不在支持范围内，请使用列表中的模型。',
 'subtitle_conflict':'字幕版本已变化，已保留未保存修改，请重新核对版本。',
 'subtitle_invalid':'字幕文字或时间无效，请检查起止时间与相邻字幕。',
 'no_speech':'没有取得可信的语音字幕，请检查音轨或改用画面字幕。',
 'ocr_failed':'画面字幕识别未完成，可调整识别区域后重试。',
 'no_text':'所选区域没有识别到字幕，请调整区域或改用语音字幕。',
 'local_format_unsupported':'本地视频目前支持 MP4、MOV 和 M4V，请选择这些格式的视频。',
 'local_source_missing':'本地视频的受控副本已不可用，请重新选择或拖入原视频。',
 'local_reselect_required':'本地视频接收尚未完成，请重新选择或拖入视频；已接收的副本保留。',
 'origin_denied':'此页面无权操作客户端。','invalid_request':'请求内容不符合要求，请检查输入。',
 'request_conflict':'同一次操作的内容发生变化，请核对已有结果后重新操作。',
 'internal_error':'操作未完成，成功结果保留，请查看任务记录后重试。',
 'app_closing':'程序正在退出，请稍候。',
}
READS={'resolve.status','resolve.list','output.read','settings.read','list','detail','preview.status','subtitle.read','models.list'}
WRITES={'resolve.start','settings.save','start','cancel','resume','retry','decision','preview.prepare','preview','reveal','import','file.delete','preview.region.save','subtitle.save','models.download','models.cancel'}
NATIVE={'output.choose','models.import','subtitle.export','local.choose','local.drop'}
ALL={'download.'+name for name in READS|WRITES|NATIVE}

def local_message(code):
    return {
        'media_invalid':'本地视频的格式、时长或完整性检查未通过，请检查文件或重新选择本地视频。',
        'source_missing':MESSAGES['local_source_missing'],
        'identity_mismatch':'所选本地视频或保存位置已变化，已停止操作，请重新选择并检查文件。',
        'metadata_missing':'本地视频接收资料不可用，请重新选择本地视频；已完成的结果保留。',
        'size_limit':'本地视频超过 2 GiB 大小限制，请选择较小的视频。',
        'no_audio':'本地视频没有可提取的音轨，已完成的其他结果保留。',
    }.get(code,MESSAGES.get(code,''))

class DownloadCommands:
    def __init__(self,engine,runtime):
        self.engine,self.runtime=engine,runtime
        self._native_lock=threading.RLock()
    @staticmethod
    def error(code,rid):
        if code not in MESSAGES:code='internal_error'
        return {'v':1,'request_id':rid,'ok':False,'error':{'code':code,'message':MESSAGES[code],
            'retryable':code in {'network','timeout','disk_space','account_busy','internal_error'}}}
    @staticmethod
    def validate(op,p):
        def exact(keys):
            if set(p)!=set(keys):raise DownloadError('input_limit')
        def identity(v):
            if type(v) is not str or not re.fullmatch(r'[A-Za-z0-9_:-]{1,64}',v):raise DownloadError('input_limit')
        def identities(values,limit=200):
            if type(values) is not list or not 1<=len(values)<=limit or len(set(values))!=len(values):raise DownloadError('input_limit')
            for v in values:identity(v)
        def filter_(v):
            if (type(v) is not dict or set(v)!={'query','state'} or type(v['query']) is not str or len(v['query'])>256
                or v['state'] not in ('','all','waiting','queued','running','succeeded','partial','failed','skipped','cancelled','interrupted','paused','paused_auth','completed','completed_with_errors','cancelling','not_downloaded','downloaded','received','processed')):raise DownloadError('input_limit')
        def opts(v):
            if type(v) is not dict or set(v)!={'save_video','prefer_clean','audio_format','silent_video','asr_language','ocr_enabled','model_ids'} or type(v['model_ids']) is not list:raise DownloadError('input_limit')
            DownloadOptions(**{**v,'model_ids':tuple(v['model_ids'])})
            if not set(v['model_ids'])<=set(MODEL_IDS):raise DownloadError('model_unknown')
        def origin(v):
            if v not in ('asr','ocr','platform_caption','embedded_caption'):raise DownloadError('input_limit')
        def cursor(v):
            if v is not None and (type(v) is not str or len(v)>512):raise DownloadError('input_limit')
        if type(op) is not str:raise DownloadError('input_limit')
        name=op.removeprefix('download.')
        if op not in ALL or type(p) is not dict:raise DownloadError('input_limit')
        if name in ('output.choose','output.read','settings.read','models.list','local.choose'):exact(())
        elif name=='local.drop':
            exact(('token',))
            if type(p['token']) is not str or not re.fullmatch(r'[a-f0-9]{32}',p['token']):raise DownloadError('input_limit')
        elif name=='settings.save':
            if set(p) not in ({'options','filter'},{'options','filter','source_mode'}):raise DownloadError('input_limit')
            opts(p['options']);filter_(p['filter'])
            if 'source_mode' in p and p['source_mode'] not in ('link','local'):raise DownloadError('input_limit')
        elif name in ('list','resolve.list'):
            exact(('page','filter','session_id') if name=='resolve.list' else ('page','filter'));filter_(p['filter'])
            if type(p['page']) is not int or not 1<=p['page']<=100000:raise DownloadError('input_limit')
            if name=='resolve.list':identity(p['session_id'])
        elif name=='resolve.start':
            if set(p)-{'input','session_id','cursor','show_browser'} or ('input' in p)==('session_id' in p):raise DownloadError('input_limit')
            if 'input' in p:
                if set(p)-{'input','show_browser'} or type(p['input']) is not str or not 1<=len(p['input'])<=8192 or '\0' in p['input']:raise DownloadError('input_limit')
            else:
                if not {'session_id','cursor'}<=set(p):raise DownloadError('input_limit')
                identity(p['session_id']);cursor(p['cursor'])
            if 'show_browser' in p and type(p['show_browser']) is not bool:raise DownloadError('input_limit')
        elif name=='start':
            if set(p) not in ({'candidate_ids','options','directory_grant_id'},{'candidate_ids','options','directory_grant_id','confirm_existing'}):raise DownloadError('input_limit')
            if 'confirm_existing' in p and type(p['confirm_existing']) is not bool:raise DownloadError('input_limit')
            identities(p['candidate_ids']);identity(p['directory_grant_id']);opts(p['options'])
        elif name in ('detail','cancel'):
            if set(p) not in ({'task_id'},{'job_id'}):raise DownloadError('input_limit')
            identity(next(iter(p.values())))
        elif name in ('resolve.status','preview.status','models.cancel'):exact(('job_id',));identity(p['job_id'])
        elif name=='resume':
            exact(('task_id','show_browser'));identity(p['task_id'])
            if type(p['show_browser']) is not bool:raise DownloadError('input_limit')
        elif name=='retry':
            exact(('task_id','item_ids','stages'));identity(p['task_id']);identities(p['item_ids'])
            if type(p['stages']) is not list or not p['stages'] or len(p['stages'])>5 or len(set(p['stages']))!=len(p['stages']) or not set(p['stages'])<={'video','audio','silent','asr','ocr'}:raise DownloadError('input_limit')
        elif name=='decision':
            exact(('task_id','item_id','choice'));identity(p['task_id']);identity(p['item_id'])
            if p['choice'] not in ('original','skip'):raise DownloadError('input_limit')
        elif name in ('preview.prepare','preview.region.save'):
            exact(('candidate_id',) if name=='preview.prepare' else ('candidate_id','region'));identity(p['candidate_id'])
            if name.endswith('save'):
                if type(p['region']) is not dict or set(p['region'])!={'x','y','width','height'}:raise DownloadError('input_limit')
                Region(**p['region'])
        elif name in ('preview','reveal','file.delete'):exact(('resource_id',));identity(p['resource_id'])
        elif name=='import':
            exact(('resource_id','category_ids'));identity(p['resource_id'])
            if type(p['category_ids']) is not list or len(p['category_ids'])>100:raise DownloadError('input_limit')
            if len(p['category_ids'])>1:raise DownloadError('single_category_only')
            if p['category_ids']:identities(p['category_ids'],1)
        elif name in ('models.download','models.import'):
            exact(('model_id',))
            if p['model_id'] not in MODEL_IDS:raise DownloadError('model_unknown')
        elif name in ('subtitle.read','subtitle.save','subtitle.export'):
            keys={'resource_id','origin'}|({'cursor'} if name.endswith('read') else {'expected_revision','patches'} if name.endswith('save') else {'format'})
            exact(keys);identity(p['resource_id']);origin(p['origin'])
            if name.endswith('read'):cursor(p['cursor'])
            elif name.endswith('export'):
                if p['format'] not in ('txt','srt'):raise DownloadError('input_limit')
            else:
                if type(p['expected_revision']) is not int or p['expected_revision']<0 or type(p['patches']) is not list or not 1<=len(p['patches'])<=200 or len(json.dumps(p,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8'))>12288:raise DownloadError('input_limit')
                seen=set()
                for patch in p['patches']:
                    if type(patch) is not dict or not {'id'}<set(patch) or set(patch)-{'id','text','start_ms','end_ms'}:raise DownloadError('input_limit')
                    identity(patch['id'])
                    if patch['id'] in seen:raise DownloadError('input_limit')
                    seen.add(patch['id'])
                    if 'text' in patch and (type(patch['text']) is not str or len(patch['text'])>10000 or '\0' in patch['text']):raise DownloadError('input_limit')
                    for k in ('start_ms','end_ms'):
                        if k in patch and (type(patch[k]) is not int or patch[k]<0):raise DownloadError('input_limit')
    def parse(self,raw):
        if type(raw) is not str or len(raw.encode('utf-8'))>16384:raise ValueError()
        obj=json.loads(raw,object_pairs_hook=_unique_object)
        if type(obj) is not dict or set(obj)!={'v','request_id','operation','params'}:raise ValueError()
        if type(obj['v']) is not int or obj['v']!=1 or type(obj['request_id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',obj['request_id']):raise ValueError()
        self.validate(obj['operation'],obj['params']);return obj
    @staticmethod
    def digest(obj):return hashlib.sha256(json.dumps({k:obj[k] for k in ('v','operation','params')},sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    def receipt(self,connection,obj):
        row=connection.execute(text('SELECT payload_hash,response_json FROM web_command_receipts WHERE request_id=:id'),{'id':obj['request_id']}).first()
        if row:return json.loads(row[1]) if row[0]==self.digest(obj) else self.error('request_conflict',obj['request_id'])
    def commit(self,obj,native=None,cancelled=False):
        with self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                result=self.receipt(c,obj)
                if result is None:
                    data={'cancelled':True} if cancelled else self.runtime.enqueue(c,obj['operation'],obj['params'],native=native)
                    result={'v':1,'request_id':obj['request_id'],'ok':True,'data':data}
                    c.execute(text('INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES(:id,:hash,:response)'),dict(id=obj['request_id'],hash=self.digest(obj),response=json.dumps(result,ensure_ascii=False,allow_nan=False)))
                c.commit()
            except Exception:c.rollback();raise
        if result.get('ok') and not cancelled:
            try:self.runtime.wake()
            except Exception:pass  # Committed receipt remains authoritative even if scheduler wake failed.
        return result
    def dispatch(self,raw,*,trusted_origin=False):
        return self._dispatch(raw,trusted_origin=trusted_origin)
    def dispatch_native(self,raw,picker,*,trusted_origin=False):
        return self._dispatch(raw,trusted_origin=trusted_origin,picker=picker)
    def _dispatch(self,raw,*,trusted_origin=False,picker=None):
        if trusted_origin is not True:return self.error('origin_denied',None)
        rid=None
        try:
            obj=self.parse(raw);rid=obj['request_id'];name=obj['operation'].removeprefix('download.')
            if name in READS:return {'v':1,'request_id':rid,'ok':True,'data':self.runtime.query(obj['operation'],obj['params'])}
            if name in NATIVE:
                if picker is None:return self.error('origin_denied',rid)
                with self._native_lock:
                    with self.engine.connect() as c:previous=self.receipt(c,obj)
                    if previous is not None:return previous
                    selected=picker(obj['operation'],obj['params'])
                    if not selected:return self.commit(obj,cancelled=True)
                    native=self.runtime.prepare_native(obj['operation'],obj['params'],selected)
                    return self.commit(obj,native=native)
            return self.commit(obj)
        except DownloadError as error:return self.error(error.code,rid)
        except (ValueError,TypeError,UnicodeError,RecursionError,KeyError):return self.error('invalid_request',rid)
        except Exception:return self.error('internal_error',rid)
