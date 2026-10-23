"""Human-run catalog/one-selected-work gate; safe summary only, no private URLs."""
import argparse
import hashlib
import json
import math
import os
import stat
import tempfile
import re
import time
from urllib.parse import urlsplit
import subprocess
from pathlib import Path
from yijianjifa.features.downloads.browser import DownloadBrowserPool
from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.downloads.input import normalize_inputs
from yijianjifa.features.downloads.network import SecureFetcher, SourceRegistry, PublicConnector, CloudflareDoHResolver
from yijianjifa.features.downloads.providers import DouyinProvider

class Token:
    def cancelled(self): return False

def safe_ffprobe(data):
    """Only numeric facts and known media enums; source tags/filenames never escape."""
    def number(value, integer=False):
        try:
            result=float(value)
            if not math.isfinite(result) or result<0: return None
            return int(result) if integer and result.is_integer() else (None if integer else result)
        except (TypeError,ValueError,OverflowError): return None
    if not isinstance(data,dict): return {'duration_seconds':None,'streams':[]}
    fmt=data.get('format') if isinstance(data.get('format'),dict) else {}
    result={'duration_seconds':number(fmt.get('duration')),'streams':[]}
    streams=data.get('streams')
    if not isinstance(streams,list): return result
    for row in streams[:16]:
        if not isinstance(row,dict) or row.get('codec_type') not in ('video','audio'): continue
        codec=row.get('codec_name')
        safe={'codec_type':row['codec_type'],'codec_name':codec if codec in ('h264','hevc','av1','vp8','vp9','aac','mp3','opus','vorbis','pcm_s16le') else 'unknown'}
        for field in ('width','height','sample_rate','channels'):
            if field in row: safe[field]=number(row[field],True)
        result['streams'].append(safe)
    return result


BUILD_ROOT = Path(__file__).resolve().parents[1] / 'build'


def output_child(root, name):
    """Reject concrete destinations that could escape a previously validated root."""
    root = Path(root).absolute()
    if Path(name).name != name or name in ('', '.', '..'): raise DownloadError('unsafe_source')
    child = root / name
    if any(part.is_symlink() for part in (child, *child.parents)):
        raise DownloadError('unsafe_source')
    if child.exists() and not child.is_file(): raise DownloadError('unsafe_source')
    return child


def write_report(root, name, value):
    temporary = None
    try:
        destination = output_child(root, name)
        descriptor, filename = tempfile.mkstemp(prefix='.report-', suffix='.tmp', dir=root)
        temporary = Path(filename)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        output_child(root, name)  # Refuse a link created while serializing.
        os.replace(temporary, destination)  # Never open/truncate an existing child.
        temporary = None
    except OSError:
        raise DownloadError('unsafe_source') from None
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def consume_control(root):
    path = output_child(root, 'control.json')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise DownloadError('unsafe_source') from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode): raise DownloadError('unsafe_source')
        raw = os.read(descriptor, 513)
    finally:
        os.close(descriptor)
    # Consume only this exact control file, not a concurrent replacement.
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino): path.unlink()
    except FileNotFoundError:
        pass
    try:
        value = json.loads(raw) if len(raw) <= 512 else None
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


def validate_args(args):
    if args.managed_default and (not args.managed_data or args.allow_observed_page_init or args.prepare_verification):
        raise DownloadError('unsupported_source')
    if args.managed_data and not args.managed_default: raise DownloadError('unsupported_source')
    if args.select_id is not None and not re.fullmatch(r'\d{1,32}', args.select_id):
        raise DownloadError('input_limit')
    if not 1 <= args.max_pages <= 25: raise DownloadError('input_limit')
    root = Path(args.output).absolute()
    consumer = Path(args.consumer_data).absolute() if args.consumer_data else root / 'isolated-data'
    build = BUILD_ROOT.resolve()
    for path in (root, consumer):
        if path == build or build not in path.resolve().parents: raise DownloadError('unsafe_source')
        if any(part.is_symlink() for part in (path, *path.parents) if part != Path('/')):
            raise DownloadError('unsafe_source')
    if args.managed_data:
        source = Path(args.managed_data).absolute()
        if any(part.is_symlink() for part in (source, *source.parents) if part != Path('/')):
            raise DownloadError('unsafe_source')
        source = source.resolve()
        for path in (root.resolve(), consumer.resolve()):
            if path == source or source in path.parents or path in source.parents:
                raise DownloadError('unsafe_source')
    for name in ('summary.json', 'verification.json', 'control.json'):
        output_child(root, name)
    if args.select_id is not None: output_child(root, args.select_id + '.mp4')
    return root, consumer


def collect(provider, source, root, report, *, selected_id, max_pages, connector):
    """Fresh in-memory candidates only; there is no report/private-URL import path."""
    candidates = []
    cursor = None
    for page_index in range(max_pages):
        page = provider.resolve(source, cursor, cancel=Token())
        candidates.extend(page.candidates)
        report['catalog'] = dict(ids=[c.work_id for c in candidates], count=len(candidates),
                                 pages=page_index + 1, completion=page.completion, has_more=page.has_more)
        if not page.has_more: break
        cursor = page.cursor
    report['state'] = 'catalog-read'
    if selected_id is None: return
    selected = [c for c in candidates if c.work_id == selected_id]
    if len(selected) != 1: raise DownloadError('source_missing')
    report['selected_id'] = selected_id
    path = output_child(root, selected_id + '.mp4')
    media = provider.media(selected[0], cancel=Token())
    SecureFetcher(provider.registry, connector=connector).download(media, path, cancel=Token(), progress=lambda _: None)
    output_child(root, selected_id + '.mp4')
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels', '-of', 'json', str(path)],
        stderr=subprocess.DEVNULL, timeout=30))
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
    report['works'].append(dict(work_id=selected_id, bytes=path.stat().st_size, sha256=digest.hexdigest(),
                                watermark=media.watermark, ffprobe=safe_ffprobe(probe)))
    report['frame_review'] = 'pending'
    report['state'] = 'downloaded-needs-frame-review'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--explicit-cloudflare-doh',action='store_true')
    parser.add_argument('--visible',action='store_true')
    parser.add_argument('--allow-observed-page-init',action='store_true')
    parser.add_argument('--output',default='build/download-probe/task-2')
    parser.add_argument('--consumer-data')
    parser.add_argument('--prepare-verification',action='store_true')
    parser.add_argument('--profile-only',action='store_true')
    parser.add_argument('--managed-default',action='store_true')
    parser.add_argument('--managed-data',help='Explicit closed-client data directory; never auto-discovered')
    parser.add_argument('--select-id',help='One public work ID; omitted means catalog only')
    parser.add_argument('--max-pages',type=int,default=1)
    args=parser.parse_args()
    try: root,consumer=validate_args(args)
    except DownloadError as error:
        print(json.dumps({'state':'blocked','error_code':error.code})); return
    root.mkdir(parents=True,exist_ok=True)
    report={'mode':'anonymous-public-read-only','state':'blocked','works':[]}
    lease=None; provider=None; engine=None
    try:
        connector=PublicConnector(resolver=CloudflareDoHResolver()) if args.explicit_cloudflare_doh else PublicConnector()
        managed_sessions = None
        account_id = None
        if args.managed_default:
            report['mode'] = 'managed-default-read-only'
            report['expected_identity_match'] = False
            from yijianjifa.features.downloads.probe_session import open_managed_sessions
            managed_sessions, engine = open_managed_sessions(args.managed_data)
            account_id = managed_sessions.default_account_id()
            if account_id is None: raise DownloadError('account_unavailable')
        lease=DownloadBrowserPool(consumer,connector=connector,visible=args.visible,
            anonymous_probe_initializers=args.allow_observed_page_init,managed_sessions=managed_sessions).acquire(account_id)
        cdp=lease.context.new_cdp_session(lease.page)
        cdp.send('Network.enable',{'maxPostDataSize':0})
        request_kinds={}
        network_events=[]
        network_started=time.monotonic()
        def event_row(row):
            row['elapsed_ms']=int((time.monotonic()-network_started)*1000)
            network_events.append(row)
        def sent(event):
            req=event.get('request',{}); parsed=urlsplit(req.get('url',''))
            label={('mcs.zijieapi.com','/webid'):'visitor',('mssdk.bytedance.com','/web/r/token'):'security',('www.douyin.com','/service/2/abtest_config/'):'config'}.get((parsed.hostname,parsed.path))
            if label is None and '/aweme/' in parsed.path and req.get('method')=='GET': label='public_aweme_read'
            if label is None: label='asset' if event.get('type') in ('Script','Stylesheet','Font') else 'other'
            if len(request_kinds)<2000: request_kinds[event['requestId']]=label
            if label in ('visitor','security','config','public_aweme_read') and len(network_events)<100:
                event_row({'kind':label,'event':'request','method':req.get('method')})
        def response_extra(event):
            label=request_kinds.get(event['requestId'])
            if label in ('visitor','security','config','public_aweme_read') and len(network_events)<100:
                event_row({'kind':label,'event':'response','status':event.get('statusCode')})
        def failed(event):
            label=request_kinds.get(event['requestId'],'other')
            if len(network_events)<100:
                value=event.get('errorText','')
                code=value if re.fullmatch(r'net::ERR_[A-Z_]+',value) else 'network_failure'
                cors=event.get('corsErrorStatus',{}).get('corsError')
                if cors is not None and not re.fullmatch(r'[A-Za-z]{1,80}',str(cors)): cors='other_cors_failure'
                event_row({'kind':label,'event':'failed','code':code,'cors':cors})
        cdp.on('Network.requestWillBeSent',sent)
        cdp.on('Network.responseReceivedExtraInfo',response_extra)
        cdp.on('Network.loadingFailed',failed)
        report['native_network_events']=network_events
        if args.prepare_verification:
            if not args.visible or not args.profile_only or normalize_inputs(args.source)[0].kind!='douyin_profile':
                raise DownloadError('unsupported_source')
            cdp.detach()  # No auth traffic/credentials/QR contents are captured during human login.
            report['mode']='explicit-manual-consumer-verification'
            event=lease.begin_verification(args.source)
            def verification_state(state, code=None):
                write_report(root,'verification.json',{'state':state,'event_id':event.id,'reason':event.reason,'title':'视频下载 · 主页读取验证','error_code':code})
            verification_state('ready')
            print(json.dumps({'state':'verification_ready','event_id':event.id},ensure_ascii=False),flush=True)
            while True:
                lease.poll_verification(cancel=Token())
                action=consume_control(root)
                if action is None: continue
                if action.get('event_id')!=event.id: continue
                if action.get('action')=='cancel': lease.close(); raise DownloadError('cancelled')
                if action.get('action')!='resume': continue
                verification_state('checking')
                try:
                    lease.complete_verification(cancel=Token())
                    verification_state('resolved')
                    report['identity_verified']=True
                    break
                except DownloadError as error:
                    if lease.closed: raise
                    verification_state('required',error.code)
                    print(json.dumps({'state':'verification_required','error_code':error.code}),flush=True)
        registry=SourceRegistry(); provider=DouyinProvider(registry,lease)
        collect(provider, normalize_inputs(args.source)[0], root, report, selected_id=args.select_id,
                max_pages=args.max_pages, connector=connector)
        if args.managed_default: report['expected_identity_match'] = True
    except DownloadError as error:
        report['state']='blocked'
        report['error_code']=error.code
        if lease and not lease.closed and not args.prepare_verification:
            try:
                report['resolved_work_ids']=re.findall(r'/(?:video|share/video)/(\d+)',urlsplit(lease.page.url).path)
                body=lease.page.locator('body').inner_text(timeout=2000)
                report['visible_signals']={key:term in body for key,term in [('login','扫码登录'),('verification','验证'),('unavailable','无法访问'),('gone','已删除')]}
                report['visible_text_length']=len(body)
            except Exception: pass
    except Exception:
        report['state']='blocked'
        report['error_code']='probe_environment'
    finally:
        if engine: engine.dispose()
        if provider: provider.close()
        if lease:
            report['proxy_failure_counts']=dict(lease.proxy.failure_counts)
            report['proxy_traffic']=dict(lease.proxy.traffic)
            report['browser_phase']=lease.last_phase
            report['browser_failure']=lease.last_failure
            if lease.verification_event:
                report['verification']={'reason':lease.verification_event.reason,'state':lease.verification_event.state}
            report['detail_response_statuses']=lease.observed_statuses
            report['read_evidence']=lease.read_evidence
            lease.close()
            if args.prepare_verification and lease.verification_event:
                event=lease.verification_event
                try:
                    write_report(root,'verification.json',{'state':event.state,'event_id':event.id,'reason':event.reason,'title':'视频下载 · 主页读取验证','error_code':report.get('error_code')})
                except DownloadError as error:
                    report['state']='blocked'; report['error_code']=error.code
    try: write_report(root,'summary.json',report)
    except DownloadError as error:
        report['state']='blocked'; report['error_code']=error.code
    print(json.dumps({'state':report['state'],'error_code':report.get('error_code'),'work_count':len(report['works'])}))

if __name__=='__main__': main()
