"""Safe projections of persistent V2 children, never private snapshots."""
import json
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow
from yijianjifa.features.publish.coordinator import ERROR_CODES,ERROR_STAGES,ERROR_CATEGORIES
from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.storage.models import Setting

STAGES={'waiting':'等待处理','running':'正在核验并提交','paused_auth':'等待账号验证','result_unknown':'提交结果待核对',
    'published':'已公开','platform_scheduled':'平台已接收定时安排','platform_accepted':'平台已接收','processing':'平台处理中',
    'reviewing':'平台审核中','waiting_adapter':'发布待接入','waiting_configuration':'发布资料待处理',
    'pre_submit_failed':'提交前失败','cancelled':'已取消本地目标','interrupted':'执行中断待核对','missed_schedule':'已错过本机安排'}
SUBMITTED={'published','platform_scheduled','platform_accepted','processing','reviewing','platform_rejected'}


REASONS={
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
FAILURE_CATEGORIES={'dependency_missing':'客户端运行依赖不可用，请重新安装或更新客户端。',
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


def safe_fields(fields):
    result={k:v for k,v in fields.items() if k in {'title','body','tags','category_id','category_name','category_mode','category_choice','creation_statement_choice',
        'creation_statement_id','creation_statement_name','content_authorization','reprint_source','copyright','source','channels_short_title',
        'content_declaration_choice','channels_original'}
        and type(v) in (str,int,bool)}
    for key,allowed in [('cover',('kind','frame_ms','missing')),('location',('id','name','address')),('declaration',('id','label'))]:
        value=fields.get(key)
        if key in fields:result[key]={k:value[k] for k in allowed if k in value and type(value[k]) in (str,int,bool)} if type(value) is dict else None
    return result


def summary(children,excluded=0):
    image_posts={d['content_id'] for d in children if d.get('content_type')=='image_post'}
    videos={d['video_id'] for d in children if d.get('content_type','video')=='video'}
    result = {'videos':len(videos),'accounts':len({d['account_id'] for d in children}),
        'targets':len(children),'ready':sum(d.get('can_submit',d.get('state')=='waiting') for d in children),
        'blocked':sum(not d.get('can_submit',d.get('state')=='waiting') for d in children),'excluded':excluded}
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
            child['execution_log']=json.loads(log.value_json) if log else []
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
            state=child['state'];final=child['final_started']
            child['submission_status']='uncertain' if state in {'result_unknown','interrupted'} else 'submitted' if final or state in SUBMITTED else 'not_submitted'
            actions=[]
            stopped=not attempt.owner_hash or service.deliveries.workers.stopped(attempt.owner_hash)
            child['worker_running']=not stopped
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
            if (row.content_type=='image_post' and not child.get('error_code') and
                    state in {'published','platform_scheduled','platform_accepted','processing','reviewing'}):
                child['reason_message']=''
            evidence=json.loads(attempt.result_json) if attempt.result_json else {}
            phase=evidence.get('error_stage');category=evidence.get('error_category')
            if (child.get('error_code')=='adapter_failed' and type(phase) is str and phase in ERROR_STAGES
                    and type(category) is str and category in ERROR_CATEGORIES):
                child.update(error_stage=phase,error_category=category)
                impact='已进入提交阶段，结果仍待核对，请勿重复投稿。' if final else '此目标尚未提交，不会自动重发。'
                child['reason_message']=FAILURE_STAGES[phase]+'失败：'+FAILURE_CATEGORIES[category]+impact
                if row.content_type=='image_post':
                    child['reason_message']=child['reason_message'].replace('视频上传准备','图片上传准备').replace('视频文件','图片文件')
        result['summary']=summary(result['deliveries'],snapshot.get('excluded_count',0))
        result['counts']={state:sum(c['state']==state for c in result['deliveries']) for state in sorted({c['state'] for c in result['deliveries']})}
        result['accepted_count']=sum(c['state'] in SUBMITTED-{'published','platform_rejected'} for c in result['deliveries'])
        result['published_count']=sum(c['state']=='published' for c in result['deliveries'])
        result['controls']=['pause'] if not result['paused'] else ['resume']
    return result
