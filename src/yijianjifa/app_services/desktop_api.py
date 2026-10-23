"""Finite local library/mixing API; platform operations are intentionally absent."""
from dataclasses import asdict
import json
import re

from .library_queries import LibraryCommands, InvalidRequest, CommandRejected, ContextualCommandRejected, _unique_object
from .task_runtime import TaskRuntime
from yijianjifa.features.mixing.desktop_service import DesktopMixService
from yijianjifa.features.library.copy_service import CopyRepository, CategoryRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.usage_progress import UsageProgressService
from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.features.mixing.tts.hosted import VoicePaused


class DesktopApi(LibraryCommands):
    _daily_operations = {'mix.daily.save', 'mix.daily.submit'}
    _operations = LibraryCommands._operations | {'settings.publish.save','task.delete','video.note.save','publish.reschedule','publish.cancel','account.save','account.default','account.delete','finished.cover.save','finished.assignment.apply','notification.read','notification.read_all','preset.save','preset.delete','settings.sound.save','settings.mix_preset.save','preset.mix.save','mix.submit','task.pause','task.resume','task.cancel','task.move','media.rename','library.categories','finished.categories','media.preference.save','media.delete','copy.progress.reset','finished.save'}
    _operations |= _daily_operations
    _operations |= {'mix.daily.continue'}

    @staticmethod
    def _error(code, request_id):
        # Daily publishing adds diagnostics for codes the library API does not define.
        try:
            return LibraryCommands._error(code, request_id)
        except KeyError:
            pass
        from .daily_mixing import MESSAGES
        from .multi_publish_dto import REASONS
        result = LibraryCommands._error('invalid_request', request_id)
        result['error'].update(code=code, message=MESSAGES.get(code, REASONS.get(code, '本批设置未通过校验，请检查账号和平台发布资料。')))
        return result

    def __init__(self, engine, paths, *, login_provider=None, login_providers=None, download_runtime=None, publish_adapters=None, host_lock_held=False, official_publish_service=None):
        super().__init__(engine)
        from .publish_control import recover_local_software
        recover_local_software(engine)
        # A visible browser is an explicit choice for this client session.
        # All ordinary and multi-platform adapters read this shared mode.
        from yijianjifa.storage.db import session_scope
        from yijianjifa.storage.models import Setting
        with session_scope(engine) as session:
            row = session.get(Setting, 'publish.browser_mode')
            if row is not None:
                row.value_json = json.dumps('background')
        self._paths = paths
        from .download_commands import DownloadCommands
        self._download_runtime = download_runtime
        self._download_commands = DownloadCommands(engine,download_runtime) if download_runtime is not None else None
        from .use_term import UseTerm
        self._use_term=UseTerm(engine)
        from .scheduled_publish import ScheduledPublish
        self._scheduled_publish=ScheduledPublish(engine,paths,getattr(login_provider,"scheduled_publish",None))
        from .login_runtime import LoginRuntime
        self._login_runtime=LoginRuntime(engine,paths,login_provider,providers=login_providers)
        self._mix = DesktopMixService(engine, paths)
        from .mix_publish_handoff import MixPublishHandoff
        self._runtime = TaskRuntime(self._mix,handoff=MixPublishHandoff(engine,paths))
        from .finished_assignment import FinishedAssignment
        self._assignment = FinishedAssignment(engine)
        from .finished_covers import FinishedCovers
        self._covers = FinishedCovers(engine,paths)
        from .cover_batch_jobs import CoverBatchJobs
        self._cover_jobs = CoverBatchJobs(self._covers)
        from .publish_preparation import PublishPreparation
        from .commerce_declarations import CommerceDeclarations
        from .douyin_declaration_catalog import catalog as douyin_declarations
        self._metadata_provider=getattr(login_provider,"metadata",None)
        self._publish_declarations=CommerceDeclarations(engine,capability="ordinary",catalog=douyin_declarations())
        from .publish_locations import PublishLocations
        self._publish_locations=PublishLocations(engine,fetch=(lambda a,q,scope:self._fetch_publish_metadata(a,"locations",q,scope)) if self._metadata_provider else None, scoped_fetch=bool(self._metadata_provider))
        self._publish_preparation = PublishPreparation(engine,paths,declarations=self._publish_declarations,locations=self._publish_locations,covers=self._covers)
        from .multi_publish_runtime import MultiPublishRuntime
        from .multi_publish_commands import MultiPublishCommands
        from yijianjifa.features.publish.platforms.official_authorization import make_official_adapters
        adapters=dict(publish_adapters if publish_adapters is not None else (getattr(login_provider,'publish_adapters',None) or {}))
        for platform,adapter in make_official_adapters(engine,service=official_publish_service).items():
            if platform in adapters:raise ValueError('duplicate host platform adapter')
            adapters[platform]=adapter
        self._multi_publish=MultiPublishRuntime(engine,paths,adapters=adapters,host_lock_held=host_lock_held)
        self._multi_commands=MultiPublishCommands(engine,self._multi_publish,declarations=self._publish_declarations,locations=self._publish_locations)
        self._multi_commands.login_platforms=self._login_runtime.platforms
        from .daily_mixing import DailyMixingService
        self._daily = DailyMixingService(engine, paths, self._multi_commands)
        self._daily.voice_adapter = lambda: self._mix.tts
        from .daily_login_runtime import DailyLoginRuntime
        self._daily.auth = DailyLoginRuntime(self._login_runtime, begin=self._daily.auth_begin,
            update=self._daily.auth_update, accept=self._daily.auth_accept, is_current=self._daily.auth_current,
            progress=self._daily.auth_progress)
        self._daily.wake_making = self.start_queue
        self._runtime.daily_handoff = self._daily
        from .image_post_commands import ImagePostCommands
        from .image_post_publishing import ImagePostPublishing
        self._image_posts = ImagePostCommands(engine, paths)
        self._image_publish = ImagePostPublishing(engine, paths, self._multi_publish,
            declarations=self._publish_declarations)
        from .publish_watch import PublishWatch
        self._publish_watch = PublishWatch(engine)
        from .commerce_catalog import CommerceCatalog
        self._commerce_catalog = CommerceCatalog(engine)
        from .commerce_music import CommerceMusic
        self._commerce_music = CommerceMusic(engine)
        from .commerce_declarations import CommerceDeclarations
        self._commerce_declarations = CommerceDeclarations(engine)
        from .commerce_preparation import CommercePreparation
        self._commerce_preparation = CommercePreparation(engine,paths,catalog=self._commerce_catalog,music=self._commerce_music,declarations=self._commerce_declarations,covers=self._covers)

    def _fetch_publish_metadata(self,*args):
        try:return self._metadata_provider(*args)
        except Exception as exc:raise CommandRejected('publish_metadata_unavailable') from exc

    def configure_downloads(self, media):
        if self._download_runtime is None:
            from .download_runtime import DownloadRuntime
            from .download_commands import DownloadCommands
            self._download_runtime=DownloadRuntime(self._engine,self._paths,media=media)
            self._download_commands=DownloadCommands(self._engine,self._download_runtime)

    def dispatch_download_native(self, raw, picker, *, trusted_origin=False):
        if self._download_commands is None:
            from .download_commands import DownloadCommands
            return DownloadCommands.error('media_tool_missing',None)
        return self._download_commands.dispatch_native(raw,picker,trusted_origin=trusted_origin)

    def start_local_checks(self):
        self._multi_publish.start()
        if self._download_runtime is not None:self._download_runtime.start()
        return self._publish_watch.start()

    def start_queue(self):
        if self._daily.closing.is_set():
            return False
        return self._runtime.start()

    def close(self, timeout=0):
        self._daily.closing.set()
        daily_closed=self._daily.auth.close(timeout)
        multi_closed=self._multi_publish.close(timeout)
        if self._metadata_provider:self._metadata_provider.close()
        self._publish_preparation.close()
        self._publish_declarations.close()
        self._publish_locations.close()
        self._commerce_catalog.close()
        self._commerce_music.close()
        self._commerce_declarations.close()
        self._commerce_preparation.close()
        downloads_closed = self._download_runtime.close(timeout) if self._download_runtime is not None else True
        publishing_closed = self._scheduled_publish.close(timeout)
        login_closed = self._login_runtime.close(timeout)
        checks_closed = self._publish_watch.close(timeout)
        queue_closed = self._runtime.close(timeout)
        covers_closed = self._cover_jobs.close(timeout)
        if publishing_closed and checks_closed and queue_closed and covers_closed and login_closed and downloads_closed:self._covers.close()
        return daily_closed and multi_closed and publishing_closed and checks_closed and queue_closed and covers_closed and login_closed and downloads_closed

    def dispatch(self, raw, *, trusted_origin=False):
        if trusted_origin is not True:
            return self._error('origin_denied',None)
        rid=None
        try:
            if type(raw) is not str or len(raw.encode('utf-8'))>16384:raise InvalidRequest()
            obj=json.loads(raw,object_pairs_hook=_unique_object)
            if type(obj) is not dict or set(obj)!={'v','request_id','operation','params'}:raise InvalidRequest()
            rid=obj['request_id'];op=obj['operation'];p=obj['params']
            if type(rid) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',rid):rid=None;raise InvalidRequest()
            if type(obj['v']) is not int or obj['v']!=1 or type(op) is not str or type(p) is not dict:raise InvalidRequest()
            if op == 'help.open':
                if set(p) != {'target'} or p['target'] not in ('guide', 'download'):
                    raise InvalidRequest()
                import webbrowser
                urls = {'guide': 'https://39.108.82.213/operator/guide',
                        'download': 'https://github.com/tianyiswl/yijianjifa'}
                opened = webbrowser.open(urls[p['target']], new=2)
                if not opened:
                    return {'v': 1, 'request_id': rid, 'ok': False, 'error': {
                        'code': 'browser_unavailable', 'message': '无法打开浏览器，请检查默认浏览器设置。', 'retryable': True}}
                return {'v': 1, 'request_id': rid, 'ok': True, 'data': {'opened': True}}
            if op.startswith('image_posts.'):
                return self._image_posts.dispatch(obj)
            if op.startswith('image_publish.'):
                try:
                    if op=='image_publish.context':
                        if p:raise InvalidRequest()
                        data=self._image_publish.context()
                    elif op=='image_publish.preview':data=self._image_publish.preview(p)
                    elif op=='image_publish.submit':data=self._image_publish.submit(p,rid)
                    elif op=='image_publish.settings.save':data=self._image_publish.save_settings(p)
                    else:raise InvalidRequest()
                except ValueError as error:
                    code=str(error).strip("'")
                    messages={'select douyin account':'请选择一个抖音账号。',
                              'account_verification_required':'抖音账号登录已失效，请到账号管理重新登录。',
                              'image_post_title_required':'请先填写每篇图文的标题。',
                              'image_post_unavailable':'所选图文已删除或没有图片，请重新选择。',
                              'image_post_resource_changed':'图片副本已改变或丢失，请重新导入。',
                              'target identity or session changed':'账号登录信息已改变，请重新选择账号。',
                              'multiple_posts_need_interval_or_daily':'多篇图文请选择按间隔或每天数量安排。',
                              'scheduled_time_in_past':'发布时间已过，请重新选择。',
                              'daily_schedule_crosses_midnight':'当天安排超过午夜，请减小数量或间隔。',
                              'image_post_changed_before_save':'图文刚被修改，请重新点击开始发布。',
                              'image_post_order_invalid':'图片顺序无效，请重新保存图文。',
                              'adapter_not_integrated':'图文发布通道未加载，请重新打开新版客户端。'}
                    message=messages.get(code)
                    if message is None and op in {'image_publish.preview','image_publish.submit'}:
                        check=self._image_publish.preview(p)
                        message=next((issue['message'] for issue in check.get('issues',[]) if issue.get('message')),None)
                    return self._image_posts.error(rid,message or '图文设置无效，请检查账号、图片和发布时间。','image_publish_invalid')
                return {'v':1,'request_id':rid,'ok':True,'data':data}
            if op.startswith('multi_publish.'):
                return self._multi_commands.dispatch(obj)
            if op=='task.delete':
                self._validate(op,p)
                result=self._multi_commands.delete_v2(p,rid)
                if result is not None:return result
            if op.startswith('download.'):
                if self._download_commands is None:
                    from .download_commands import DownloadCommands
                    return DownloadCommands.error('media_tool_missing',rid)
                return self._download_commands.dispatch(raw,trusted_origin=True)
            if op == 'mix.daily.read':
                if p:raise InvalidRequest()
                data = self._daily.read()
            elif op == 'mix.daily.status':
                if set(p) != {'id'} or type(p['id']) is not str or not 1 <= len(p['id']) <= 64:raise InvalidRequest()
                data = self._daily.status(p['id'])
            elif op=='product.use_term':
                if p:raise InvalidRequest()
                data=self._use_term.status()
            elif op=='account.login.availability':
                if p:raise InvalidRequest()
                data={'available':self._login_runtime.available(),'platforms':self._login_runtime.platforms()}
            elif op=='account.capabilities.prepare':
                if set(p)!={'account_id'} or type(p['account_id']) is not str:raise InvalidRequest()
                from yijianjifa.features.accounts.service import AccountService
                from yijianjifa.features.accounts.capability_probe import fixture_path,FIXTURE_SHA256
                account=AccountService(self._engine).get(p['account_id'])
                if account.platform!='douyin':raise InvalidRequest()
                path=fixture_path()
                data={'nickname':account.nickname,'fixture_sha256':FIXTURE_SHA256,'duration_seconds':3,'size_bytes':path.stat().st_size,
                      'available':self._login_runtime.available('douyin') and callable(getattr(self._login_runtime.provider,'check_capabilities',None))}
            elif op=='account.capabilities.start':
                from yijianjifa.features.accounts.capability_probe import FIXTURE_SHA256
                if set(p)!={'account_id','upload_consent','fixture_sha256'} or p['upload_consent'] is not True or p['fixture_sha256']!=FIXTURE_SHA256:raise InvalidRequest()
                data=self._login_runtime.start(rid,p['account_id'],probe=True,upload_consent=True)
            elif op in {'account.login.start','account.backend.start'}:
                if set(p) not in ({'account_id'},{'account_id','platform'}):raise InvalidRequest()
                requested=p.get('platform')
                from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
                if 'platform' in p:PlatformCatalog().get(requested)
                requested=self._login_runtime.resolve_platform(p['account_id'],requested)
                if not self._login_runtime.available(requested):raise CommandRejected('platform_not_integrated')
                data=self._login_runtime.start(rid,p['account_id'],platform=requested,backend=op=='account.backend.start')
            elif op in {'account.login.status','account.login.cancel'}:
                if set(p)!={'id'} or type(p['id']) is not str or not re.fullmatch('[a-f0-9]{48}',p['id']):raise InvalidRequest()
                data=self._login_runtime.status(p['id']) if op.endswith('status') else self._login_runtime.cancel(p['id'])
            elif op=='local.checks.status':
                if p:raise InvalidRequest()
                data=self._publish_watch.status()
            elif op=='commerce.confirm':
                if set(p)!={'token'}:raise InvalidRequest()
                from .commerce_confirmation import CommerceConfirmation
                data=CommerceConfirmation(self._engine,self._paths,self._commerce_preparation).confirm(p['token'])
            elif op=='commerce.declarations.query':
                data=self._commerce_declarations.query(p)
            elif op=='commerce.prepare':
                data=self._commerce_preparation.prepare(p)
            elif op=='commerce.music.query':
                data=self._commerce_music.query(p)
            elif op in {'commerce.stores.search','commerce.stores.page'}:
                data=self._commerce_catalog.search(p) if op.endswith('search') else self._commerce_catalog.page(p)
            elif op in {'commerce.context','commerce.preview'}:
                from .commerce_planning import CommercePlanning
                commerce=CommercePlanning(self._engine,catalog=self._commerce_catalog,music=self._commerce_music,declarations=self._commerce_declarations,covers=self._covers)
                if op=='commerce.context':
                    if p:raise InvalidRequest()
                    data=commerce.context()
                else:data=commerce.preview(p)
            elif op=='mix.publish.retry':
                if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
                original=self._mix.queue.get(p['id'])
                if original.snapshot.get('daily_publish'):
                    obj['operation']='mix.daily.continue'
                    return self.dispatch(json.dumps(obj),trusted_origin=True)
                data=self._runtime.handoff(p['id'])
                if data is None:raise InvalidRequest()
            elif op=='publish.cover.batch.start':
                data=self._cover_jobs.start(p)
            elif op in {'publish.cover.batch.status','publish.cover.batch.cancel'}:
                if set(p)!={'id'}:raise InvalidRequest()
                data=self._cover_jobs.status(p['id']) if op.endswith('status') else self._cover_jobs.cancel(p['id'])
            elif op=='publish.cover.batch':
                if set(p)!={'id','token','video_ids'}:raise InvalidRequest()
                data=self._covers.stage_for_videos(p['id'],p['token'],p['video_ids'])
            elif op=='publish.cover.stage':
                if set(p)!={'id','token'}:raise InvalidRequest()
                data=self._covers.stage_for_publish(p['id'],p['token'])
            elif op=='publish.locations.recent':
                data=self._publish_locations.recent(p)
            elif op=='publish.locations.search':
                data=self._publish_locations.search(p)
            elif op=='publish.declarations.query':
                data=self._publish_declarations.query(p)
            elif op=='publish.progress.status':
                if p:raise InvalidRequest()
                from yijianjifa.webui.publish_progress import progress
                data=progress.status()
            elif op=='publish.intervention.status':
                if p:raise InvalidRequest()
                from yijianjifa.webui.publish_intervention import broker
                data=broker.status()
            elif op=='publish.intervention.respond':
                if not {'id','action'}<=set(p) or set(p)-{'id','action','code'}:raise InvalidRequest()
                from yijianjifa.webui.publish_intervention import broker
                data=broker.respond(p['id'],p['action'],p.get('code',''))
            elif op in {'publish.browser_mode.read','publish.browser_mode.save'}:
                from yijianjifa.storage.db import session_scope
                from yijianjifa.storage.models import Setting
                if op.endswith('save'):
                    if set(p)!={'mode'} or p['mode'] not in {'background','visible'}:raise InvalidRequest()
                elif p:raise InvalidRequest()
                with session_scope(self._engine) as session:
                    row=session.get(Setting,'publish.browser_mode')
                    if op.endswith('save'):
                        if row:row.value_json=json.dumps(p['mode'])
                        else:session.add(Setting(key='publish.browser_mode',value_json=json.dumps(p['mode'])))
                        data={'mode':p['mode']}
                    else:data={'mode':json.loads(row.value_json) if row else 'background'}
            elif op=='publish.execution.availability':
                if p:raise InvalidRequest()
                data={'available':self._scheduled_publish.transport is not None}
            elif op=='publish.execution.check':
                if set(p)!={'id'} or type(p['id']) is not str:raise InvalidRequest()
                data=self._scheduled_publish.check(p['id'])
            elif op=='publish.execution.start':
                if set(p)!={'id','digest','consent'} or type(p['id']) is not str or type(p['digest']) is not str or p['consent'] is not True:raise InvalidRequest()
                data=self._scheduled_publish.start(p['id'],p['digest'],consent=p['consent'])
            elif op=='publish.confirm':
                if set(p)!={'token'}:raise InvalidRequest()
                from .publish_confirmation import PublishConfirmation
                data=PublishConfirmation(self._engine,self._paths,self._publish_preparation).confirm(p['token'])
            elif op=='publish.prepare':
                data=self._publish_preparation.prepare(p)
            elif op in ('publish.candidates','publish.preview'):
                from .publish_planning import PublishPlanning
                planner=PublishPlanning(self._engine,declarations=self._publish_declarations,locations=self._publish_locations,covers=self._covers)
                if op=='publish.candidates':
                    if not {'query','offset','limit'}<=set(p) or set(p)-{'query','offset','limit','advanced','include_published','account_ids','allow_new_request'}:raise InvalidRequest()
                    data=planner.candidates(**p)
                else:data=planner.preview(p)
            elif op=='account.list':
                if set(p)!={'query','platform','group','state','offset','limit'} or type(p['query']) is not str or len(p['query'])>200 or p['platform'] not in ('','douyin','kuaishou','channels','bilibili') or type(p['group']) is not str or len(p['group'])>128 or p['state'] not in ('','ok','relogin','pending') or type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise InvalidRequest()
                from .account_access import AccountAccess
                data=AccountAccess(self._engine).list(**p)
            elif op=='account.get':
                if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
                from .account_access import AccountAccess
                data=AccountAccess(self._engine).get(p['id'])
            elif op=='finished.assignment.preview':
                data=self._assignment.preview(p)
            elif op=='task.center.list':
                from .task_center_access import TaskCenterAccess,STATUS
                if set(p)!={'query','tab','status','offset','limit'} or type(p['query']) is not str or len(p['query'])>200 or p['tab'] not in ('all','make','publish','download','action') or p['status'] not in ('',*STATUS) or type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise InvalidRequest()
                data=TaskCenterAccess(self._engine,multi_publish=self._multi_publish).list(**p)
            elif op=='task.center.detail':
                if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
                from .task_center_access import TaskCenterAccess
                data=TaskCenterAccess(self._engine,multi_publish=self._multi_publish).detail(p['id'])
            elif op=='publish.review.allow_rearrange':
                if set(p)!={'id','digest','consent'}:raise InvalidRequest()
                from .publish_review import PublishReview
                data=PublishReview(self._engine).allow_rearrange(p['id'],p['digest'],p['consent'],rid)
            elif op=='publish.review.save':
                if set(p)!={'id','digest','outcome','note','consent'}:raise InvalidRequest()
                from .publish_review import PublishReview
                data=PublishReview(self._engine).save(p['id'],p['digest'],p['outcome'],p['note'],p['consent'],rid)
            elif op=='notification.list':
                if not {'offset','limit'}<=set(p) or set(p)-{'offset','limit','view'} or p.get('view','all') not in ('all','unread_recent') or type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise InvalidRequest()
                from yijianjifa.features.notifications.service import NotificationService
                rows=NotificationService(self._engine).history()
                unread=sum(not n.read for n in rows)
                if p.get('view')=='unread_recent':
                    rows=sorted(rows,key=lambda n:n.created_at,reverse=True)
                    rows=[n for n in rows if not n.read]+[n for n in rows if n.read][:max(0,5-unread)]
                data={'total':len(rows),'unread':unread,'items':[{'id':n.id,'task_id':n.task_id,'status':n.status,'message':n.message,'read':n.read,'created_at':n.created_at.isoformat()} for n in rows[p['offset']:p['offset']+p['limit']]]}
            elif op=='settings.publish.read':
                if set(p)!={'view'} or p['view'] not in ('publish','commerce'):raise InvalidRequest()
                from yijianjifa.storage.db import session_scope
                from yijianjifa.storage.models import Setting
                with session_scope(self._engine) as session:
                    row=session.get(Setting,'schedule.'+p['view'])
                    data={'settings':json.loads(row.value_json) if row else None}
            elif op=='settings.mix_preset.read':
                if p:raise InvalidRequest()
                from yijianjifa.storage.db import session_scope
                from yijianjifa.storage.models import Setting
                with session_scope(self._engine) as session:
                    row=session.get(Setting,'mix.last_preset')
                    data={'id':json.loads(row.value_json) if row else ''}
            elif op=='settings.sound.read':
                if p:raise InvalidRequest()
                data={'enabled':self._mix.settings.notification_sound_enabled}
            elif op=='preset.references':
                if p:raise InvalidRequest()
                from yijianjifa.features.accounts.service import AccountService
                data={'accounts':[{'id':a.id,'name':a.nickname} for a in AccountService(self._engine).list('douyin')]}
            elif op=='preset.list':
                if set(p)!={'query','kind','offset','limit'} or type(p['query']) is not str or len(p['query'])>200 or p['kind'] not in ('','mix','normal_publish','commerce','mix_then_publish') or type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise InvalidRequest()
                from yijianjifa.features.presets.service import PresetService
                from .preset_catalog import read
                rows=[r for r in PresetService(self._engine).list(kind=p['kind'] or None) if p['query'].casefold() in r.name.casefold()]
                data={'total':len(rows),'items':[read(self._engine,r.id) for r in rows[p['offset']:p['offset']+p['limit']]]}
            elif op=='preset.get':
                if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
                from .preset_catalog import read
                data=read(self._engine,p['id'])
            elif op=='preset.mix.list':
                if set(p)!={'offset','limit'} or type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise InvalidRequest()
                from yijianjifa.features.presets.service import PresetService
                from .mix_presets import read
                rows=PresetService(self._engine).list(kind='mix')
                data={'total':len(rows),'items':[read(self._engine,r.id) for r in rows[p['offset']:p['offset']+p['limit']]]}
            elif op=='preset.mix.get':
                if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
                from .mix_presets import read
                data=read(self._engine,p['id'])
            elif op=='finished.list':
                if not {'query','origin','offset','limit'}<=set(p) or set(p)-{'query','origin','offset','limit','category_ids'} or type(p['query']) is not str or len(p['query'])>200 or p['origin'] not in ('all','generated','external') or type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise InvalidRequest()
                if type(p.get('category_ids',[])) is not list or len(p.get('category_ids',[]))>100 or any(type(v) is not str or not 1<=len(v)<=64 for v in p.get('category_ids',[])):raise InvalidRequest()
                from sqlalchemy import select
                from yijianjifa.features.finished.service import FinishedVideo, FinishedVideoService
                from yijianjifa.storage.db import session_scope
                with session_scope(self._engine) as session:
                    query=select(FinishedVideo).order_by(FinishedVideo.created_at.desc(),FinishedVideo.id)
                    if p['origin']!='all':query=query.where(FinishedVideo.origin_type==p['origin'])
                    rows=[FinishedVideoService._record(row) for row in session.scalars(query)]
                rows=[r for r in rows if p['query'].casefold() in '\n'.join([r.id,r.display_name,r.title,r.body,r.tags]).casefold()]
                selected=set(p.get('category_ids',[]));core=FinishedVideoService(self._engine)
                if selected:
                    rows=[r for r in rows if (selected.intersection(core.category_ids(r.id)) or ('__none__' in selected and not core.category_ids(r.id)))]
                items=[self._finished_data(r) for r in rows[p['offset']:p['offset']+p['limit']]]
                data={'total':len(rows),'items':self._with_created_at(FinishedVideo,items)}
            elif op=='task.detail':
                if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
                from yijianjifa.storage.db import session_scope
                from yijianjifa.features.tasks.service import TaskCenterRow
                with session_scope(self._engine) as session:
                    center=session.get(TaskCenterRow,p['id'])
                    is_multi=center is not None and center.kind=='multi_publish'
                if is_multi:
                    from .task_center_access import TaskCenterAccess
                    return {'v':1,'request_id':rid,'ok':True,'data':TaskCenterAccess(self._engine,multi_publish=self._multi_publish).detail(p['id'])}
                task=self._mix.queue.get(p['id']);snapshot=task.snapshot
                from .task_center_access import TaskCenterAccess
                task_items=TaskCenterAccess(self._engine).detail(task.id)['items']
                option_keys={'voice','speed','sentence_cut','spoken_enabled','full_enabled','batch_full_text','keep_original_audio','original_gain','voice_gain','spoken_style','full_style','music_gain'}
                data={'id':task.id,'state':task.state,'quantity':task.quantity,
                      'completed':task.completed_count,'succeeded':task.success_count,'failed':task.failure_count,
                      'has_error':bool(task.last_error),'created_at':task.created_at,'plan':snapshot.get('plan',{}),
                      'total_ms':snapshot.get('total_ms'),'segment_ms':snapshot.get('segment_ms'),
                      'total_range_ms':snapshot.get('total_range_ms'),'segment_range_ms':snapshot.get('segment_range_ms'),
                      'item_total_ms':snapshot.get('item_total_ms'),
                      'options':{k:v for k,v in snapshot.get('options',{}).items() if k in option_keys},
                      'copies':[{k:c.get(k,'') for k in ('id','title','voice','body','tags','full')} for c in snapshot.get('copies',[])],
                      'items':[{'index':r['index'],'state':r['state'],'has_error':r['state']=='failed'} for r in task_items]}
                from .mix_execution import read_execution
                data['execution']=read_execution(self._engine,task.id)
            elif op=='task.recovery':
                if p:raise InvalidRequest()
                data=self._runtime.recovery()
            elif op=='task.poll':
                if set(p)!={'epoch','after'}:raise InvalidRequest()
                if p['epoch'] is not None and (type(p['epoch']) is not str or len(p['epoch'])>64):raise InvalidRequest()
                if p['after'] is not None and (type(p['after']) is not int or p['after']<0):raise InvalidRequest()
                data=self._runtime.poll(**p)
                if 'tasks' in data:
                    from .mix_execution import read_execution
                    for task in data['tasks']:task['execution']=read_execution(self._engine,task['id'])
            elif op=='library.list':
                if not {'kind','query','category_ids','offset','limit'}<=set(p) or set(p)-{'kind','query','category_ids','offset','limit','status'}:raise InvalidRequest()
                if p.get('status','all') not in ('all','ready','missing','busy'):raise InvalidRequest()
                if p['kind'] not in ('video','music','copy') or type(p['query']) is not str or len(p['query'])>200:raise InvalidRequest()
                if type(p['category_ids']) is not list or len(p['category_ids'])>100 or not all(type(x) is str and len(x)<=64 for x in p['category_ids']):raise InvalidRequest()
                if type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise InvalidRequest()
                data=self._list(p)
            else:
                result=super().dispatch(raw,trusted_origin=True)
                if result.get('ok') and op in {'mix.daily.submit', 'mix.daily.continue', 'task.resume'}:
                    self._daily.start_check(result['data']['id'])
                if result.get('ok') and op in {'task.pause', 'task.cancel', 'task.delete'}:
                    self._daily.auth.cancel(p['id'])
                if op=='task.cancel' and result.get('ok') and p['id'] not in self._mix.queue._active_ids:
                    # LibraryCommands returns only after the cancellation and
                    # receipt commit. Replays also retry outstanding cleanup;
                    # failed receipts/conflicting requests must not remove bytes.
                    try:
                        self._mix.executor.discard_cancelled(p['id'])
                    except Exception:
                        error=self._error('internal_error',rid)
                        error['error'].update(code='cancel_cleanup_unknown',message='取消已保存，但待清理文件的处理结果尚未确认。请重试，或重新打开任务记录查看。')
                        return error
                return result
            return {'v':1,'request_id':rid,'ok':True,'data':data}
        except CommandRejected as exc:
            return self._error(str(exc),rid)
        except KeyError:
            return self._error('not_found',rid)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._error('invalid_request',rid)
        except Exception:
            return self._error('internal_error',rid)

    def _list(self,p):
        rows=[];query=p['query'].casefold();selected=set(p['category_ids'])
        if p['kind']=='copy':
            repo=CopyRepository(self._engine);progress=UsageProgressService(self._engine)
            for item in repo.list_all():
                cats=sorted(repo.category_ids(item.id));data=asdict(item)
                if selected and not (selected.intersection(cats) or ('__none__' in selected and not cats)):continue
                if query and query not in '\n'.join(data.values()).casefold():continue
                data.update(category_ids=cats,used_mixing=progress.is_used('mixing',item.id),used_finished=progress.is_used('finished_metadata',item.id))
                rows.append(data)
        else:
            service=MediaStorageService(self._engine,self._paths,kind=p['kind'])
            for item in service.list_assets():
                if query and query not in (item.id+' '+item.display_name).casefold():continue
                cats=sorted(service.category_ids(item.id))
                if selected and not (selected.intersection(cats) or ('__none__' in selected and not cats)):continue
                from yijianjifa.storage.db import session_scope
                from yijianjifa.features.library.active_references import ensure_not_referenced
                busy=False
                with session_scope(self._engine) as session:
                    try:ensure_not_referenced(session,[item.id,item.content_hash,item.original_path,item.managed_path])
                    except RuntimeError:busy=True
                missing=not item.effective_path.is_file()
                status=p.get('status','all')
                if (status=='missing' and not missing) or (status=='ready' and missing) or (status=='busy' and not busy):continue
                rows.append(dict(id=item.id,display_name=item.display_name,kind=item.kind,
                                 category_ids=cats,note=self._video_note(item.kind,item.id),missing=missing,busy=busy,managed=item.managed_path is not None,**self._media_presentation(item)))
        from yijianjifa.storage.models import CopyRecord, MediaAsset
        model=CopyRecord if p['kind']=='copy' else MediaAsset
        if rows:
            from sqlalchemy import select
            from yijianjifa.storage.db import session_scope
            order=select(model.id)
            if p['kind']!='copy':order=order.where(MediaAsset.kind==p['kind'])
            with session_scope(self._engine) as session:
                positions={identity:index for index,identity in enumerate(session.scalars(order.order_by(model.created_at.desc(),model.id)))}
            # Display order is independent of the repositories used by mixing.
            rows.sort(key=lambda item:positions.get(item['id'],len(positions)))
        return {'items':self._with_created_at(model,rows[p['offset']:p['offset']+p['limit']]),'total':len(rows)}

    def _with_created_at(self,model,items):
        """Read real creation stamps once per page; SQLite naive stamps are UTC."""
        if not items:return items
        from datetime import timezone
        from sqlalchemy import select
        from yijianjifa.storage.db import session_scope
        with session_scope(self._engine) as session:
            stamps=dict(session.execute(select(model.id,model.created_at).where(model.id.in_([item['id'] for item in items]))).all())
        for item in items:
            stamp=stamps.get(item['id'])
            item['created_at']=((stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)).isoformat()
                                if stamp is not None else None)
        return items

    def _video_note(self,kind,identity,engine=None):
        from sqlalchemy import text
        from yijianjifa.storage.db import session_scope
        table='finished_videos' if kind=='finished' else 'media_assets'
        with session_scope(engine if engine is not None else self._engine) as session:
            return session.execute(text(f'SELECT note FROM {table} WHERE id=:id'),{'id':identity}).scalar() or ''

    @staticmethod
    def _media_presentation(item):
        from yijianjifa.features.library.display_names import presentation
        return presentation(item)

    def _finished_data(self,item,engine=None):
        from yijianjifa.features.finished.service import FinishedVideoService
        return {**{k:getattr(item,k) for k in ('id','origin_type','display_name','duration_ms','title','body','tags','copy_id','task_locked')},'note':self._video_note('finished',item.id,engine),'missing':not item.effective_path.is_file(),**self._media_presentation(item),'category_ids':sorted(FinishedVideoService(engine if engine is not None else self._engine).category_ids(item.id))}

    @staticmethod
    def _validate(op,p):
        if op == 'mix.daily.continue':
            if set(p) != {'id'} or type(p['id']) is not str or not 1 <= len(p['id']) <= 64:raise InvalidRequest()
            return
        if op in DesktopApi._daily_operations:
            from .daily_mixing import DailyMixingService
            DailyMixingService.validate(p, for_submit=op.endswith('submit'))
            return
        if op=='video.note.save':
            if set(p)!={'kind','id','note'} or p['kind'] not in ('video','finished') or type(p['id']) is not str or not 1<=len(p['id'])<=64 or type(p['note']) is not str or len(p['note'])>2000:raise InvalidRequest()
            return
        if op=='publish.reschedule':
            from .publish_reschedule import validate_request
            validate_request(p)
            return
        if op=='task.delete':
            if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
            return
        if op=='publish.cancel':
            if set(p) not in ({'id'},{'id','account_id'}) or any(type(v) is not str or not 1<=len(v)<=64 for v in p.values()):raise InvalidRequest()
            return
        if op in {'account.save','account.default','account.delete'}:
            expected={'id','remark','group_name'} if op=='account.save' else {'id'}
            if set(p)!=expected or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
            if op=='account.save' and (type(p['remark']) is not str or len(p['remark'])>255 or type(p['group_name']) is not str or len(p['group_name'])>128):raise InvalidRequest()
            return
        if op in {'finished.assignment.apply','finished.cover.save'}:
            if set(p)!={'token'} or type(p['token']) is not str or not re.fullmatch(r'[a-f0-9]{48}',p['token']):raise InvalidRequest()
        elif op=='notification.read_all':
            if p:raise InvalidRequest()
        elif op=='notification.read':
            if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
        elif op=='settings.publish.save':
            if set(p)!={'view','settings'} or p['view'] not in ('publish','commerce'):raise InvalidRequest()
            v=p['settings']
            if type(v) is not dict or set(v)!={'mode','start','interval','per_day','days','clock_mode','date','times'}:raise InvalidRequest()
            if v['mode'] not in ('daily','interval','exact','immediate') or v['clock_mode'] not in ('interval','custom'):raise InvalidRequest()
            if any(type(v[k]) is not str or len(v[k])>32 for k in ('start','date','interval','per_day','days')):raise InvalidRequest()
            if type(v['times']) is not list or len(v['times'])>100 or any(type(x) is not str or len(x)>5 for x in v['times']):raise InvalidRequest()
        elif op=='settings.mix_preset.save':
            if set(p)!={'id'} or type(p['id']) is not str or len(p['id'])>64:raise InvalidRequest()
        elif op=='settings.sound.save':
            if set(p)!={'enabled'} or type(p['enabled']) is not bool:raise InvalidRequest()
        elif op=='preset.delete':
            if set(p)!={'ids'} or type(p['ids']) is not list or not 1<=len(p['ids'])<=100 or any(type(x) is not str or not 1<=len(x)<=64 for x in p['ids']):raise InvalidRequest()
        elif op=='preset.save':
            if set(p)!={'id','payload'} or (p['id'] is not None and (type(p['id']) is not str or not 1<=len(p['id'])<=64)):raise InvalidRequest()
            from .preset_catalog import validate
            if type(p['payload']) is not dict or p['payload'].get('kind')=='mix':raise InvalidRequest()
            validate(p['payload'])
        elif op=='preset.mix.save':
            if set(p)!={'id','name','settings'} or (p['id'] is not None and (type(p['id']) is not str or not 1<=len(p['id'])<=64)):raise InvalidRequest()
            if type(p['name']) is not str or not 1<=len(p['name'].strip())<=255 or type(p['settings']) is not dict or {'batch_full_text','publish_after'}&set(p['settings']):raise InvalidRequest()
            DesktopApi._validate('mix.submit',p['settings'])
        elif op=='finished.categories':
            if set(p)!={'ids','category_ids','mode'} or p['mode'] not in ('append','replace'):raise InvalidRequest()
            for key in ('ids','category_ids'):
                if type(p[key]) is not list or len(p[key])>100 or any(type(v) is not str or not 1<=len(v)<=64 for v in p[key]):raise InvalidRequest()
            if not p['ids']:raise InvalidRequest()
        elif op=='finished.save':
            if set(p)!={'id','display_name','title','body','tags'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
            if any(type(p[k]) is not str for k in ('display_name','title','body','tags')) or not 1<=len(p['display_name'].strip())<=255:raise InvalidRequest()
        elif op=='copy.progress.reset':
            if set(p)!={'scope'} or p['scope'] not in ('mixing','finished_metadata'):raise InvalidRequest()
        elif op=='media.delete':
            if set(p)!={'kind','id'} or p['kind'] not in ('video','music') or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
        elif op in ('media.rename','library.categories','media.preference.save'):
            expected={'media.rename':{'kind','id','name'},'library.categories':{'kind','ids','category_ids','mode'},'media.preference.save':{'kind','keep_copy'}}[op]
            if set(p)!=expected:raise InvalidRequest()
            if p['kind'] not in (('video','music','copy') if op=='library.categories' else ('video','music')):raise InvalidRequest()
            if op=='media.rename':
                if type(p['id']) is not str or not 1<=len(p['id'])<=64 or type(p['name']) is not str or not 1<=len(p['name'].strip())<=255:raise InvalidRequest()
            elif op=='media.preference.save':
                if type(p['keep_copy']) is not bool:raise InvalidRequest()
            else:
                if p['mode'] not in ('append','replace'):raise InvalidRequest()
                for key in ('ids','category_ids'):
                    if type(p[key]) is not list or len(p[key])>100 or not all(type(x) is str and 1<=len(x)<=64 for x in p[key]):raise InvalidRequest()
                if not p['ids']:raise InvalidRequest()
        elif op=='mix.submit':
            allowed={'use_copy','publish_after','quantity','material_categories','copy_categories','total_ms','segment_ms','resolution','aspect_ratio','frame_fit','copy_mode','voice','speed','sentence_cut','spoken_enabled','full_enabled','batch_full_text','keep_original_audio','original_gain','voice_gain','spoken_style','full_style','music_gain','background_music','transition','total_range_ms','segment_range_ms'}
            if set(p)-allowed or 'quantity' not in p:raise InvalidRequest()
            import math
            from yijianjifa.features.mixing.duration_ranges import validate_range
            validate_range(p.get('total_range_ms'))
            validate_range(p.get('segment_range_ms'))
            from yijianjifa.features.mixing.effect_options import music_spec, transition_spec, validate_music_gain
            music_spec(p.get('background_music'))
            transition_spec(p.get('transition'))
            validate_music_gain(p.get('music_gain', .25))
            from yijianjifa.features.mixing.subtitle_styles import validate_style
            for key in ('spoken_style', 'full_style'):
                if key in p:
                    try:validate_style(p[key])
                    except ValueError:raise InvalidRequest()
            for key in ('use_copy','sentence_cut','spoken_enabled','full_enabled','keep_original_audio'):
                if key in p and type(p[key]) is not bool:raise InvalidRequest()
            for key,low,high in (('speed',.5,2.5),('original_gain',0,2),('voice_gain',0,2)):
                if key in p and (type(p[key]) not in (int,float) or not math.isfinite(p[key]) or not low<=p[key]<=high):raise InvalidRequest()
            if 'voice' in p and p['voice'] is not None and (type(p['voice']) is not str or not 1<=len(p['voice'])<=100):raise InvalidRequest()
            if 'batch_full_text' in p and (type(p['batch_full_text']) is not str or len(p['batch_full_text'])>4000):raise InvalidRequest()
            for key in ('quantity','total_ms','segment_ms'):
                if key in p and (type(p[key]) is not int or p[key]<1):raise InvalidRequest()
            for key in ('material_categories','copy_categories'):
                if key in p and (type(p[key]) is not list or len(p[key])>100 or not all(type(x) is str and 1<=len(x)<=64 for x in p[key])):raise InvalidRequest()
            for key,values in {'resolution':('720p','1080p'),'aspect_ratio':('9:16','16:9','1:1'),'frame_fit':('preserve_blur','crop_fill'),'copy_mode':('sequential','random')}.items():
                if key in p and p[key] not in values:raise InvalidRequest()
        elif op.startswith('task.'):
            if set(p)!=({'id','delta'} if op=='task.move' else {'id'}):raise InvalidRequest()
            if type(p['id']) is not str or not 1<=len(p['id'])<=64:raise InvalidRequest()
            if op=='task.move' and (type(p['delta']) is not int or p['delta'] not in (-1,1)):raise InvalidRequest()
        else:LibraryCommands._validate(op,p)

    def _apply(self,connection,op,p):
        if op == 'mix.daily.continue':
            from .daily_mixing import read_status, DailyMixingError
            try:
                self._daily.prepare_continue(connection,p['id'])
                return read_status(connection,p['id'])
            except DailyMixingError as error:
                raise ContextualCommandRejected(error.code,error.message) from None
            except VoicePaused as error:
                raise ContextualCommandRejected(error.code,str(error)) from None
        if op in self._daily_operations:
            from .daily_mixing import DailyMixingError
            try:
                return self._daily.submit(connection,p) if op.endswith('submit') else self._daily.save(connection,p)
            except DailyMixingError as error:
                raise ContextualCommandRejected(error.code,error.message) from None
            except VoicePaused as error:
                raise ContextualCommandRejected(error.code,str(error)) from None
        if op=='video.note.save':
            from sqlalchemy import text
            table='finished_videos' if p['kind']=='finished' else 'media_assets'
            condition=" AND kind='video'" if p['kind']=='video' else ''
            result=connection.execute(text(f'UPDATE {table} SET note=:note WHERE id=:id{condition}'),{'id':p['id'],'note':p['note']})
            if not result.rowcount:raise KeyError(p['id'])
            return {'id':p['id'],'note':p['note']}
        if op=='publish.reschedule':
            from .publish_reschedule import reschedule_local
            return reschedule_local(connection,p)
        if op=='task.delete':
            from .task_removal import remove_task
            return remove_task(connection,p['id'])
        if op=='publish.cancel':
            from .publish_control import cancel_local
            return cancel_local(connection,p['id'],p.get('account_id'))
        if op in {'account.save','account.default','account.delete'}:
            from .account_access import AccountAccess,public_account
            from yijianjifa.features.accounts.service import AccountService
            if op=='account.delete':
                with self._login_runtime._lock:
                    if self._login_runtime.thread and self._login_runtime.thread.is_alive():
                        raise CommandRejected('account_window_active')
                    return AccountService(connection).remove(p['id'])
            AccountAccess(connection).get(p['id'])
            core=AccountService(connection)
            item=core.set_default(core.get(p['id']).platform,p['id']) if op=='account.default' else core.update_metadata(p['id'],remark=p['remark'],group_name=p['group_name'])
            return public_account(item)
        if op=='finished.cover.save':
            return self._covers.save(connection,p['token'])
        if op=='finished.assignment.apply':
            return self._assignment.apply(connection,p['token'])
        if op=='notification.read_all':
            from yijianjifa.features.notifications.service import NotificationService
            return {'updated':NotificationService(connection).mark_all_read()}
        if op=='notification.read':
            from yijianjifa.features.notifications.service import NotificationService
            note=NotificationService(connection).mark_read(p['id'])
            return {'id':note.id,'read':note.read}
        if op=='settings.publish.save':
            from yijianjifa.storage.db import session_scope
            from yijianjifa.storage.models import Setting
            with session_scope(connection) as session:
                key='schedule.'+p['view'];row=session.get(Setting,key)
                if row:row.value_json=json.dumps(p['settings'])
                else:session.add(Setting(key=key,value_json=json.dumps(p['settings'])))
            return {'saved':True}
        if op=='settings.mix_preset.save':
            from yijianjifa.storage.db import session_scope
            from yijianjifa.storage.models import Setting
            with session_scope(connection) as session:
                row=session.get(Setting,'mix.last_preset')
                if row:row.value_json=json.dumps(p['id'])
                else:session.add(Setting(key='mix.last_preset',value_json=json.dumps(p['id'])))
            return {'id':p['id']}
        if op=='settings.sound.save':
            from yijianjifa.core.settings import GlobalSettings
            settings=GlobalSettings(connection,default_output_directory=self._paths.data_dir/'成品')
            settings.notification_sound_enabled=p['enabled']
            return {'enabled':settings.notification_sound_enabled}
        if op in ('preset.save','preset.delete'):
            from yijianjifa.features.presets.service import PresetService
            from .preset_catalog import validate,read
            service=PresetService(connection)
            if op=='preset.delete':
                for pid in p['ids']:service.delete(pid)
                return {'ids':p['ids']}
            model=validate(p['payload'])
            if p['id'] is None:stored=service.create(model)
            else:
                if service.get(p['id']).kind!=model.kind:raise InvalidRequest()
                stored=service.update(p['id'],model)
            return read(connection,stored.id)
        if op=='preset.mix.save':
            from .mix_presets import to_model,read
            from yijianjifa.features.presets.service import PresetService
            from yijianjifa.features.presets.models import MixPreset
            model=to_model(p['name'],p['settings']);service=PresetService(connection)
            if p['id'] is None:stored=service.create(model)
            else:
                if not isinstance(service.get(p['id']).preset,MixPreset):raise InvalidRequest()
                stored=service.update(p['id'],model)
            return read(connection,stored.id)
        if op=='finished.categories':
            from yijianjifa.features.finished.service import FinishedVideoService
            core=FinishedVideoService(connection)
            if not set(p['category_ids'])<={c.id for c in CategoryRepository(connection).list('finished')}:raise CommandRejected('invalid_category')
            for pid in dict.fromkeys(p['ids']):
                cats=set(p['category_ids'])
                if p['mode']=='append':cats|=core.category_ids(pid)
                core.set_categories(pid,cats)
            return {'ids':list(dict.fromkeys(p['ids']))}
        if op=='finished.save':
            from yijianjifa.features.finished.service import FinishedVideoService
            core=FinishedVideoService(connection)
            core.rename(p['id'],p['display_name'])
            item=core.update_metadata(p['id'],title=p['title'],body=p['body'],tags=p['tags'])
            return self._finished_data(item,connection)
        if op=='copy.progress.reset':
            UsageProgressService(connection).reset_cycle(p['scope'])
            return {'scope':p['scope']}
        if op=='media.delete':
            service=MediaStorageService(connection,self._paths,kind=p['kind'])
            item=service.get(p['id'])
            if item is None or item.kind!=p['kind']:raise KeyError(p['id'])
            try:
                # Retain owned files until a separately verified cleanup; never
                # remove bytes before the surrounding receipt transaction commits.
                service.delete_media_record(p['id'],delete_managed_file=False)
            except RuntimeError as exc:
                raise CommandRejected('delete_blocked') from exc
            return {'id':p['id'],'managed_copy_retained':item.managed_path is not None}
        if op=='media.preference.save':
            from yijianjifa.features.library.preferences import LibraryPreferences
            LibraryPreferences(connection).set_keep_copy(p['kind'],p['keep_copy'])
            return {'keep_copy':p['keep_copy']}
        if op in ('media.rename','library.categories'):
            repo=CopyRepository(connection) if p['kind']=='copy' else MediaStorageService(connection,self._paths,kind=p['kind'])
            ids=[p['id']] if op=='media.rename' else list(dict.fromkeys(p['ids']))
            for id in ids:
                item=repo.get(id)
                if item is None or (p['kind']!='copy' and item.kind!=p['kind']):raise KeyError(id)
            if op=='media.rename':
                item=repo.rename(p['id'],p['name'])
                return {'id':item.id,'display_name':item.display_name}
            valid={c.id for c in CategoryRepository(connection).list(p['kind'])}
            if not set(p['category_ids'])<=valid:raise CommandRejected('invalid_category')
            for id in ids:
                categories=set(p['category_ids'])
                if p['mode']=='append':categories|=repo.category_ids(id)
                repo.set_categories(id,categories)
            return {'ids':ids,'category_ids':p['category_ids'],'mode':p['mode']}
        if op=='mix.submit':
            from yijianjifa.features.mixing.desktop_service import EmptyCopySelection
            try:task=DesktopMixService(connection,self._paths,tts=self._mix.tts).submit(**p)
            except EmptyCopySelection:raise CommandRejected('empty_copy_selection')
            except VoicePaused as error:raise ContextualCommandRejected(error.code,str(error)) from None
            return {'id':task.id,'state':task.state,'quantity':task.quantity}
        if op.startswith('task.'):
            queue=MixQueue(connection,self._mix.executor,self._mix.settings)
            # Cancellation must retain the live executor's active-reference guard.
            queue._active_ids=self._mix.queue._active_ids
            original=queue.get(p['id'])
            daily=bool(original.snapshot.get('daily_publish'))
            if op=='task.pause':queue.request_pause(p['id'])
            elif op=='task.resume' and daily:self._daily.prepare_continue(connection,p['id'])
            elif op=='task.resume':queue.resume(p['id'])
            elif op=='task.cancel':queue.cancel_remaining(p['id'])
            else:queue.move(p['id'],p['delta'])
            task=queue.get(p['id'])
            if task.snapshot.get('daily_publish') and op in {'task.pause', 'task.cancel'}:
                self._daily.stop_automatic(connection, task.id, cancelled=op=='task.cancel')
            return {'id':task.id,'state':task.state}
        return super()._apply(connection,op,p)
