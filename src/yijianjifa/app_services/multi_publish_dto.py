"""Safe projections of persistent V2 children, never private snapshots."""
import json
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow
from yijianjifa.features.publish.coordinator import ERROR_CODES,ERROR_STAGES,ERROR_CATEGORIES
from yijianjifa.features.publish.outcomes import COMPLETED_DELIVERY_STATES
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.storage.models import Setting

STAGES={'waiting':'等待处理','running':'正在核验并提交','paused_auth':'等待账号验证','result_unknown':'提交结果待核对',
    'published':'已公开','platform_scheduled':'平台已接收定时安排','platform_accepted':'平台已接收','processing':'平台处理中',
    'reviewing':'平台审核中','waiting_adapter':'发布待接入','waiting_configuration':'发布资料待处理',
    'pre_submit_failed':'提交前失败','cancelled':'已取消本地目标','interrupted':'执行中断待核对','missed_schedule':'已错过本机安排'}
SUBMITTED={'published','platform_scheduled','platform_accepted','processing','reviewing','platform_rejected'}


REASONS={
    'platform_daily_post_limit':'今天投稿次数已达到平台上限，本条未发布，已跳过。',
    'posting_limit_skipped':'该账号今日投稿次数已达上限，本条未提交，已跳过。',
    'douyin_daily_post_limit':'今天投稿次数已达到上限，请明天再试。本条未发布，已跳过。',
    'bilibili_tag_requires_topic':'B站有标签只能通过话题入口添加，请移除或通过话题入口选择后再发布；此目标尚未提交，不会自动重发。',
    'image_count_mismatch':'平台显示的图片张数与本篇图文不符，请核对上传页面；此篇尚未提交。',
    'image_order_unverified':'尚不能核对平台中的图片顺序，请检查上传页面；此篇尚未提交。',
    'image_upload_unverified':'平台尚未确认图片上传完成，请检查网络和上传页面；此篇尚未提交。',
    'image_fields_unverified':'图文标题、正文、标签或发布时间未能核对，请检查原页面；此篇尚未提交。',
    'image_receipt_invalid':'原图文提交结果尚未核实，请核对平台作品列表；不会自动重复提交。',
    'image_declaration_unverified':'平台未显示可核对的所选声明，请检查声明设置；此篇尚未提交。',
    'image_type_unverified':'未能进入抖音图文发布页面，请检查账号后台；此篇尚未提交。',
    'kuaishou_author_statement_required':'当前快手页面必须选择作者声明，请修改本目标声明后继续；此条尚未提交。',
    'kuaishou_declaration_options_unverified':'当前快手作者声明的选项或文字无法核对，请检查原页面；客户端不会代选，此条尚未提交。',
    'channels_declaration_options_unverified':'当前视频号页面未提供可唯一核对的所选内容声明，请检查原页面；客户端不会忽略声明，此条尚未提交。',
    'channels_original_unavailable':'当前视频号原创开关或确认状态无法核对，请在原验证页面确认原创权益；此条尚未提交。',
    'channels_original_video_too_short':'视频号原创声明要求视频至少5秒，请使用符合条件的视频，或按内容实际情况取消原创声明；此条尚未提交。',
    'official_service_not_configured':'官方发布服务尚未配置，请联系接入维护者；此目标仍只保存在本机。',
    'official_grant_not_configured':'当前账号尚未完成官方发布授权，请先完成账号授权。',
    'official_contract_pending':'视频号发布与结果核对方式尚待验证，请等待正式接入。',
    'adapter_not_integrated':'此平台发布通道尚未接入，请等待接入完成。',
    'adapter_contract_unverified':'此平台发布通道尚未验证，请等待接入完成。',
    'platform_capability_unverified':'当前通道尚未支持所选发布方式，请核对平台能力或调整时间方式。',
    'native_schedule_not_supported':'当前通道不支持平台定时，请调整本目标时间方式。',
    'category_required':'尚未填写分区，请修改本目标资料并选择官方分区。',
    'category_list_required':'分区不在当前官方选项中，请修改本目标资料并重新选择。',
    'creation_statement_required':'请在 B站发布设置中选择创作声明。',
    'platform_options_required':'请先读取 B站分区和创作声明选项，再选择发布资料。',
    'schedule_out_of_range':'所选时间不在当前平台允许的定时范围内，请调整时间。',
    'bilibili_schedule_requires_five_minutes':'B站定时需按5分钟刻度选择，例如11:00、11:05；请调整开始时间或发布间隔，并保持完整的整分钟时间。',
    'platform_fields_invalid':'平台发布资料不符合要求，请检查本目标设置后重新准备。',
    'short_title_invalid':'视频号短标题需为 6–16 个字，也可以留空。',
    'copyright_required':'尚未选择原创或转载，请修改本目标资料。',
    'repost_source_required':'转载来源尚未填写，请修改本目标资料。',
    'cover_upload_not_supported':'当前B站通道尚不支持封面上传，请修改本目标资料并明确选择不使用封面。',
    'cover_requires_validation':'当前封面不受此通道支持，请修改资料并清除封面或恢复公共资料。',
    'owned_cover_required':'尚无可用的冻结首帧封面，请先补齐成品封面。',
    'official_fields_require_configuration':'平台必填资料或封面尚不符合要求，请修改本目标资料并核对分区、原创信息、标签与封面。',
    'platform_fields_require_validation':'地点、声明或平台专用资料尚未通过校验，请修改本目标资料。',
    'account_verification_required':'账号需要重新验证，请到账号管理处理原账号。',
    'reprepare_required':'接入条件已变化，请重新保存本目标资料以检查原安排。',
    'target_paused':'此目标已暂停，尚未提交的操作已停止。',
    'batch_paused':'本批已暂停，请继续本批后再执行。',
    'software_schedule_not_due':'尚未到本机安排时间，请保持客户端运行。',
    'software_schedule_missed':'已错过本机安排时间，请调整本目标时间。',
    'existing_target_unresolved':'同一账号和视频已有未决记录，请先核对原结果。',
    'account_result_unresolved':'同一账号有发布结果尚未确认，本目标暂缓；请先核对原结果，勿重复投稿。',
    'observed_account_result_unresolved':'最近一次处理在等待账号空闲；同一账号还有发布结果待核对，本目标暂缓，请先核对原结果，勿重复投稿。',
    'account_parked_or_unresolved':'同一账号仍在处理其他目标或等待平台验证，本目标暂缓；请先完成原操作。',
    'readiness_not_checked':'当前仅显示已保存状态，发布条件需由运行中的客户端核对。',
    'cancel_requested':'已请求取消本地目标；已提交内容仍需核对原结果。',
    'verification_required':'需完成平台验证，请从任务记录继续处理。',
    'result_unknown':'提交结果尚未确认，请核对原结果，勿重复投稿。',
    'interrupted':'执行已中断，原操作是否提交尚待核对。',
    'paused_auth':'等待账号验证，请继续同账号目标完成验证。',
    'waiting_adapter':'发布通道尚未接入，请等待接入完成。',
    'waiting_configuration':'发布资料或接入条件待处理，请查看本目标资料。',
    'pre_submit_failed':'提交前处理失败，请修改资料后重新准备本目标。',
    'adapter_failed':'客户端未能完成发布处理，请检查原账号登录与发布资料；此目标不会自动重发。',
    'account_mismatch':'观察到的账号与原目标不一致，请核对原账号。',
    'permission_missing':'当前账号缺少所需权限，请完成官方授权。',
    'timeout':'本次处理超时，请按当前目标状态核对原操作。',
}


FAILURE_STAGES={'adapter_setup':'发布通道准备','account_validation':'账号校验','page_preparation':'发布页面准备',
    'verification_resume':'原会话验证','verification_observation':'验证后页面读取','final_validation':'提交前最终校验','submission_result':'提交结果读取','receipt_recording':'回执记录',
    'page_open':'发布浏览器启动','session_restore':'所选账号登录资料恢复','page_navigation':'平台页面打开','account_observation':'页面账号读取',
    'upload_preparation':'视频上传准备','form_preparation':'发布资料填写','schedule_preparation':'定时设置'}
FAILURE_CATEGORIES={'cover_unconfirmed':'封面确认后，尚未从平台页面读回已保存的封面。请检查该账号后台封面状态；当前不能确认是平台未保存还是页面读取不兼容。',
    'existing_scheduled_work':'该账号后台已有与本次文案匹配、定时时间相同的作品。为避免重复发布，本目标尚未上传或提交。请先核对后台原作品；其他已接收的平台无需重发。',
    'dependency_missing':'客户端运行依赖不可用，请重新安装或更新客户端。',
    'account_busy':'该账号正被其他窗口或任务使用，请先完成原操作。',
    'session_unavailable':'所选账号的本地登录资料不可用，请在账号管理重新登录原账号。',
    'worker_thread_mismatch':'原发布会话的执行环境不一致，请联系维护者核对。',
    'timeout':'等待平台页面响应超时，请检查网络和原页面状态。',
    'page_context_unavailable':'原页面已关闭或正在跳转，请先检查原页面和账号状态。',
    'identity_mismatch':'页面中的账号与所选原账号不一致，请核对原账号。',
    'resource_unavailable':'本地文件或运行资源不可用，请检查视频文件与客户端运行环境。',
    'browser_launch_failed':'发布浏览器未能启动，请重新打开客户端；若仍失败请联系维护者。',
    'profile_in_use':'该账号的浏览器资料正被原窗口占用，请先完成或关闭原操作。',
    'page_closed':'原发布页面已经关闭，请核对任务状态后处理。',
    'existing_draft':'原页面已有未完成草稿，请在该账号后台处理草稿后，再处理本目标。',
    'form_changed':'当前发布表单的控件或内容与要求不符，请核对原页面与发布资料。',
    'javascript_read_failed':'读取平台页面结构失败，请联系维护者并提供本目标的错误阶段。',
    'execution_failed':'客户端处理平台页面时遇到异常，请核对原页面或联系维护者。'}


_READ_FIELDS=frozenset({'error_kind','endpoint','request_index','http_status','content_kind',
                        'read_operation','request_elapsed_ms'})
_READ_ERRORS={'http_error':'接口返回异常状态','fetch_error':'请求失败',
              'json_error':'响应解析失败','body_read_error':'响应内容读取失败',
              'timeout':'请求超时','page_context_unavailable':'页面执行环境已失效',
              'browser_unavailable':'发布页面或浏览器已关闭',
              'javascript_error':'页面脚本执行失败','unknown':'读取异常，未取得更具体分类'}
_READ_ENDPOINTS={'pc_user_info_before':'账号身份接口','profile_user_info':'账号资料接口',
                 'pc_user_info_after':'身份复核接口'}
_SOURCE_FIELDS=frozenset({'source_module','source_function','source_line'})
_READ_SOURCE_FUNCTIONS={
    'chromium_multi_publish':frozenset({'read','_read_identity','_observe','_advance','_readback','_navigate'}),
    'chromium_publish':frozenset({'evaluate_script'}),
    'chromium_publish_session':frozenset({'_step','_open','_release_page','_login_read'}),
    'read_diagnostics':frozenset({'error_details','_validated_details','__init__','details'}),
}
_READ_EXCEPTION_TYPES=frozenset({
    'Error','TimeoutError','TypeError','ValueError','RuntimeError','OSError',
    'Exception','IdentityReadFailure','IdentityReadTimeout','AccountBusy','AccountIdentityMismatch',
    'FileNotFoundError','PermissionError','ConnectionError','ConnectionResetError',
    'ConnectionAbortedError','BrokenPipeError','JSONDecodeError','OperationalError','IntegrityError',
    'UnknownError',
})


def _read_event_context(raw):
    """新增身份失败事件仅公开固定上下文，不沿用任意原始键值。"""
    result={}
    enums={'state':frozenset({'started','completed','failed'}),
           'phase':frozenset({'baseline','prepare','result','unknown'}),
           'error_category':ERROR_CATEGORIES,'stage':ERROR_STAGES|{'unknown'}}
    for key,allowed in enums.items():
        value=raw.get(key)
        if type(value) is str and value in allowed:result[key]=value
    if type(raw.get('final_started')) is bool:result['final_started']=raw['final_started']
    if type(raw.get('elapsed_ms')) is int and 0<=raw['elapsed_ms']<=86400000:
        result['elapsed_ms']=raw['elapsed_ms']
    if type(raw.get('log_schema')) is int and raw['log_schema']==1:result['log_schema']=1
    if 'exception_type' in raw:
        name=raw['exception_type']
        result['exception_type']=name if type(name) is str and name in _READ_EXCEPTION_TYPES else 'UnknownError'
    return result


def safe_identity_execution_events(events):
    """公开新增诊断前再次收口，不改库内原始记录或旧事件字段。"""
    from yijianjifa.features.publish.read_diagnostics import identity_failure_details
    if type(events) is not list:return []
    result=[]
    for entry in events:
        if type(entry) is not dict or type(entry.get('checks')) is not dict:
            result.append(entry);continue
        raw=entry['checks']
        if entry.get('step')=='douyin.result_readback' and 'receipt_gate' in raw:
            # 新结果诊断也按字段收口，历史记录不能把页面内容混入新字段。
            checks={key:raw[key] for key in ('ready','uploaded','final_started','search_matches','loading')
                    if type(raw.get(key)) is bool}
            if type(raw.get('page_kind')) is str and raw['page_kind'] in {'manage','upload','post','other'}:
                checks['page_kind']=raw['page_kind']
            for key in ('search_count','text_match_count','result_count','schedule_match_count'):
                if type(raw.get(key)) is int and 0<=raw[key]<=10000:checks[key]=raw[key]
            if type(raw.get('stable_reads')) is int and 0<=raw['stable_reads']<=2:
                checks['stable_reads']=raw['stable_reads']
            for key,allowed in (
                    ('receipt_gate',{'identity','page','not_ready','query','no_exact_text','no_result_rows','no_schedule_match',
                                     'ambiguous_schedule','review_unconfirmed','unstable','confirmed','invalid_rows'}),
                    ('review_state',{'scheduled','reviewing','published','other','missing','ambiguous'})):
                if type(raw.get(key)) is str and raw[key] in allowed:checks[key]=raw[key]
            result.append({**entry,'checks':checks});continue
        if entry.get('step')=='douyin.final_click' and 'click_reported_attempted' in raw:
            checks=_read_event_context(raw)
            if type(raw['click_reported_attempted']) is bool:
                checks['click_reported_attempted']=raw['click_reported_attempted']
            result.append({**entry,'checks':checks});continue
        # 新增诊断从严收口；没有新增字段的旧日志及其他步骤保持原投影。
        scoped=bool(raw.keys()&(_READ_FIELDS|_SOURCE_FIELDS)) and (
            entry.get('step')=='douyin.identity' or
            entry.get('step')=='execution.error' and raw.get('stage')=='account_observation')
        if not scoped:
            result.append(entry);continue
        checks=_read_event_context(raw)
        details={key:raw[key] for key in _READ_FIELDS if key in raw}
        if details.keys()-{'error_kind'}:
            validated=identity_failure_details(details)
            if validated:checks.update(validated)
        elif type(details.get('error_kind')) is str and details['error_kind'] in _READ_ERRORS:
            checks['error_kind']=details['error_kind']
        module,function,line=raw.get('source_module'),raw.get('source_function'),raw.get('source_line')
        if (type(module) is str and module in _READ_SOURCE_FUNCTIONS and type(function) is str
                and function in _READ_SOURCE_FUNCTIONS[module] and type(line) is int and 1<=line<=10000000):
            checks.update(source_module=module,source_function=function,source_line=line)
        result.append({**entry,'checks':checks})
    return result


def identity_diagnostic_summary(events):
    """只从已验证的身份读取事件生成固定中文摘要，不回显页面原文。"""
    content={'json':'JSON','html':'HTML','other':'其他','missing':'未提供'}
    for entry in reversed(safe_identity_execution_events(events)[-500:]):
        if type(entry) is not dict or type(entry.get('checks')) is not dict:continue
        checks=entry['checks']
        if not ((entry.get('step')=='douyin.identity' and checks.get('state')=='failed')
                or (entry.get('step')=='execution.error' and checks.get('stage')=='account_observation')):continue
        value={key:checks[key] for key in _READ_FIELDS if key in checks}
        if not value:continue
        request=('第'+str(value['request_index'])+'/3次请求（'+_READ_ENDPOINTS[value['endpoint']]+'）'
                 if 'request_index' in value and 'endpoint' in value else '账号身份读取')
        reason=('返回 HTTP '+str(value['http_status']) if value['error_kind']=='http_error'
                and 'http_status' in value else _READ_ERRORS[value['error_kind']])
        parts=[request+reason]
        if value.get('content_kind') in content:parts.append('响应类型：'+content[value['content_kind']])
        if 'request_elapsed_ms' in value:parts.append('耗时'+str(value['request_elapsed_ms'])+' 毫秒')
        return '；'.join(parts)+'。'
    return ''


def result_diagnostic_summary(events):
    """只解释最后一次有限回执门槛，不推断平台实际接收与否。"""
    reasons={'identity':'回执中的账号身份未核实','page':'尚未回到作品管理页',
             'not_ready':'作品列表尚未达到可读取条件','query':'作品搜索条件尚未匹配',
             'no_exact_text':'未识别到标题和正文完全匹配的作品',
             'no_result_rows':'尚未解析到可核对的作品回执',
             'no_schedule_match':'已识别作品中没有匹配的排期',
             'ambiguous_schedule':'存在多条相同排期，不能确认原作品',
             'review_unconfirmed':'作品状态未满足定时回执条件',
             'unstable':'匹配结果尚未连续两次稳定','invalid_rows':'作品结果结构尚不能核实'}
    for entry in reversed(safe_identity_execution_events(events)[-500:]):
        if type(entry) is not dict or entry.get('step')!='douyin.result_readback':continue
        checks=entry.get('checks',{})
        if type(checks) is not dict or checks.get('final_started') is not True:continue
        gate=checks.get('receipt_gate')
        if gate=='confirmed':return ''
        if gate not in reasons:return ''
        counts=['识别'+str(checks['result_count'])+'条'] if 'result_count' in checks else []
        if 'text_match_count' in checks:counts.insert(0,'文本匹配'+str(checks['text_match_count'])+'处')
        if 'schedule_match_count' in checks:counts.append('排期匹配'+str(checks['schedule_match_count'])+'条')
        return '平台结果回读：'+reasons[gate]+('（'+'，'.join(counts)+'）' if counts else '')+'。'
    return ''


def safe_fields(fields):
    result={k:v for k,v in fields.items() if k in {'title','body','tags','category_id','category_name','category_mode','category_choice','creation_statement_choice',
        'creation_statement_id','creation_statement_name','content_authorization','reprint_source','copyright','source','channels_short_title',
        'content_declaration_choice','channels_original'}
        and type(v) in (str,int,bool)}
    for key,allowed in [('cover',('kind','frame_ms','missing')),('location',('id','name','address')),('declaration',('id','label'))]:
        value=fields.get(key)
        if key in fields:result[key]={k:value[k] for k in allowed if k in value and type(value[k]) in (str,int,bool)} if type(value) is dict else None
    return result


def observed_account_wait_blocker(session,row,attempt):
    """只解释当前尝试最后一次占用观察，不改变新请求的领取或发布权限。"""
    try:payload=json.loads(row.snapshot_json)
    except (TypeError,ValueError):return None
    item=session.get(TaskItemRow,row.item_id)
    if (type(payload) is not dict or payload.get('allow_new_request') is not True or item is None
            or item.state!='waiting' or row.cancel_requested or attempt is None
            or attempt.delivery_id!=row.id or attempt.number!=row.attempt
            or attempt.final_json or attempt.result_json):return None
    from yijianjifa.features.publish.execution_log import log_key
    def events(delivery):
        saved=session.get(Setting,log_key(delivery.id,delivery.attempt))
        try:value=json.loads(saved.value_json) if saved else []
        except (TypeError,ValueError):return []
        return value[-500:] if type(value) is list else []
    saved=events(row)
    last=saved[-1] if saved else None
    if (type(last) is not dict or last.get('step')!='account.wait'
            or type(last.get('checks')) is not dict or last['checks'].get('busy') is not True):return None
    candidates=session.execute(select(PublishDeliveryRow,TaskItemRow.state,DeliveryAttemptRow).join(
        TaskItemRow,TaskItemRow.id==PublishDeliveryRow.item_id).join(DeliveryAttemptRow,
        (DeliveryAttemptRow.delivery_id==PublishDeliveryRow.id)&(DeliveryAttemptRow.number==PublishDeliveryRow.attempt)).where(
        PublishDeliveryRow.platform==row.platform,PublishDeliveryRow.platform_user_id==row.platform_user_id,
        PublishDeliveryRow.account_id==row.account_id,PublishDeliveryRow.id!=row.id,
        TaskItemRow.state.in_({'result_unknown','interrupted'}),DeliveryAttemptRow.final_json.is_not(None)
        ).order_by(PublishDeliveryRow.id)).all()
    for original,state,original_attempt in candidates:
        try:final=json.loads(original_attempt.final_json)
        except (TypeError,ValueError):continue
        if type(final) is not dict or final.get('request_digest')!=original.request_digest:continue
        closed=False
        for event in events(original):
            if type(event) is not dict or type(event.get('checks')) is not dict:continue
            step=event.get('step');checks=event['checks']
            if step=='browser.close' and checks.get('closed') is True:closed=True
            elif step=='submission.authorized' or (step in {row.platform+'.browser_launch',row.platform+'.final_click'}
                    and checks.get('state')=='completed'):closed=False
        if not closed:return {'delivery_id':original.id,'state':state}
    return None


def summary(children,excluded=0):
    image_posts={d['content_id'] for d in children if d.get('content_type')=='image_post'}
    videos={d['video_id'] for d in children if d.get('content_type','video')=='video'}
    result = {'videos':len(videos),'accounts':len({d['account_id'] for d in children}),
        'targets':len(children),'ready':sum(d.get('can_submit',d.get('state')=='waiting') for d in children),
        'blocked':sum(d.get('state') not in COMPLETED_DELIVERY_STATES and
                      not d.get('can_submit',d.get('state')=='waiting') for d in children),'excluded':excluded}
    if image_posts:
        result.update(image_posts=len(image_posts),contents=len(videos)+len(image_posts))
    return result


def detail(service,task_id,*,coordinator=None):
    result=service.get_task(task_id)
    if result.get('legacy_version'):return result
    with session_scope(service.engine) as s:
        center=s.get(TaskCenterRow,task_id);snapshot=json.loads(center.snapshot_json)
        result['content_type']=snapshot.get('content_type','video')
        result['created_at']=center.created_at.isoformat()
        auth=s.get(Setting,'multi_publish.authorization:'+task_id)
        authorized=json.loads(auth.value_json)['delivery_ids'] if auth else []
        for child in result['deliveries']:
            row=s.get(PublishDeliveryRow,child['id']);p=json.loads(row.snapshot_json)
            attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id,DeliveryAttemptRow.number==row.attempt))
            from yijianjifa.features.publish.execution_log import log_key
            log=s.get(Setting,log_key(row.id,row.attempt))
            child['execution_log']=safe_identity_execution_events(json.loads(log.value_json)) if log else []
            account=s.get(AccountRow,row.account_id)
            child['delivery_id']=child['id'];child['account_label']=account.nickname if account else '账号已不可用'
            child['effective_fields']=safe_fields(p['fields']);child['stage']=STAGES.get(child['state'],'需要处理')
            if row.content_type=='image_post':
                child['image_count']=len(p.get('images',[]))
            from yijianjifa.features.publish.platforms.official_authorization import pending_descriptor
            try:adapter=service.catalog.transport_for_content(row.platform,row.content_type)
            except ValueError:adapter=None
            child['field_schema']=getattr(adapter,'public_field_schema',pending_descriptor(row.platform)['field_schema'])
            child['paused']=s.get(Setting,'multi_publish.target_pause:'+child['id']) is not None
            child['accepted']=child['id'] in authorized
            if child['paused']:
                child['can_submit']=False;child['readiness_reason']='target_paused';child['stage']='目标已暂停'
                child.pop('blocked_by',None)
            observed_blocker=None
            if child['state']=='waiting' and not child['paused'] and not result['paused'] and not child.get('blocked_by'):
                observed_blocker=observed_account_wait_blocker(s,row,attempt)
                if observed_blocker:child['blocked_by']=observed_blocker
            state=child['state'];final=child['final_started']
            child['submission_status']='uncertain' if state in {'result_unknown','interrupted'} else 'submitted' if final or state in SUBMITTED else 'not_submitted'
            actions=[]
            stopped=not attempt.owner_hash or service.deliveries.workers.stopped(attempt.owner_hash)
            child['worker_running']=not stopped
            # 未证明停止的原会话仍受保护，但已记录的未知结果不代表正在提交。
            child['execution_active']=not stopped and (state=='running' or
                state=='result_unknown' and final and not attempt.result_json)
            if child.get('blocked_by'):
                child['stage']='暂缓 · 等待核对原结果' if child['blocked_by']['state'] in {'result_unknown','interrupted'} else '暂缓 · 等待同一账号'
            if child['can_submit']:actions.append('start')
            if not final and state in {'waiting','running','paused_auth'} and not child['cancel_requested'] and not child['paused']:actions.append('pause')
            if child['paused'] and state=='waiting' and not child['cancel_requested']:actions.append('resume')
            if state in {'waiting','waiting_adapter','waiting_configuration','pre_submit_failed','missed_schedule'} and not final and stopped and not child['cancel_requested']:
                actions.extend(['reprepare','reschedule'])
            if state=='paused_auth' and coordinator and child['id'] in coordinator._contexts:actions.append('resume')
            if state in {'result_unknown','interrupted','platform_scheduled','platform_accepted','processing','reviewing','paused_auth'} and stopped and attempt.owner_hash:
                try:adapter=service.catalog.transport_for_content(row.platform,row.content_type)
                except ValueError:adapter=None
                if 'result_query' in getattr(adapter,'verified_capabilities',()):actions.append('reconcile')
            if not child['cancel_requested'] and state not in SUBMITTED|{'cancelled'}:actions.append('cancel')
            actions.append('hide');child['allowed_actions']=actions
            reason=child.get('error_code') or child.get('readiness_reason')
            if child.get('error_code') is not None and (type(child['error_code']) is not str or child['error_code'] not in ERROR_CODES):
                child['error_code']=None;reason=child.get('readiness_reason')
            child['reason_message']=REASONS.get(reason,STAGES.get(state,'需要处理')+'；请按下方可用操作处理。') if reason else ''
            if observed_blocker:child['reason_message']=REASONS['observed_account_result_unresolved']
            if not child.get('error_code') and state in COMPLETED_DELIVERY_STATES:
                child['reason_message']=''
            evidence=json.loads(attempt.result_json) if attempt.result_json else {}
            from yijianjifa.features.publish.platform_feedback import failure_message
            platform_reason=failure_message(evidence,state)
            from yijianjifa.features.publish.posting_limits import verified_limit
            child['posting_limited']=verified_limit(row,attempt,state)
            if child['posting_limited']:child['stage']='投稿上限，已跳过'
            phase=evidence.get('error_stage');category=evidence.get('error_category')
            if (child.get('error_code')=='adapter_failed' and type(phase) is str and phase in ERROR_STAGES
                    and type(category) is str and category in ERROR_CATEGORIES):
                child.update(error_stage=phase,error_category=category)
                impact='已进入提交阶段，结果仍待核对，请勿重复投稿。' if final else '此目标尚未提交，不会自动重发。'
                child['reason_message']=FAILURE_STAGES[phase]+'失败：'+FAILURE_CATEGORIES[category]+impact
                if row.content_type=='image_post':
                    child['reason_message']=child['reason_message'].replace('视频上传准备','图片上传准备').replace('视频文件','图片文件')
                if child.get('platform')=='douyin':
                    diagnostic=identity_diagnostic_summary(child['execution_log'])
                    if diagnostic:
                        child['diagnostic_summary']=diagnostic
                        child['reason_message']=diagnostic+impact
            if child.get('platform')=='douyin' and final and state in {'result_unknown','interrupted'}:
                diagnostic=child.get('diagnostic_summary') or result_diagnostic_summary(child['execution_log'])
                if diagnostic:
                    child['diagnostic_summary']=diagnostic
                    child['reason_message']=diagnostic+'已进入提交阶段，结果仍待核对，请勿重复投稿。'
                elif child.get('error_code')=='timeout':
                    child['reason_message']='最终提交后等待平台结果确认超时，请核对原作品与排期，请勿重复投稿。'
            if platform_reason:child['reason_message']=platform_reason
        deferred=[child for child in result['deliveries'] if not child['hidden'] and child['state']=='waiting'
                  and child.get('blocked_by',{}).get('state') in {'result_unknown','interrupted'}]
        if deferred:
            active=[child for child in result['deliveries'] if not child['hidden'] and child['execution_active']]
            accounts={child['account_id'] for child in deferred}
            result['status_label']=(f'其他账号正在处理，{len(deferred)}项暂缓待核对' if any(child['account_id'] not in accounts for child in active)
                else f'正在核对提交结果，{len(deferred)}项等待同一账号' if active
                else f'等待核对发布结果，{len(deferred)}项暂缓')
        result['summary']=summary(result['deliveries'],snapshot.get('excluded_count',0))
        result['counts']={state:sum(c['state']==state for c in result['deliveries']) for state in sorted({c['state'] for c in result['deliveries']})}
        result['accepted_count']=sum(c['state'] in SUBMITTED-{'published','platform_rejected'} for c in result['deliveries'])
        result['published_count']=sum(c['state']=='published' for c in result['deliveries'])
        result['controls']=['pause'] if not result['paused'] else ['resume']
    return result
