"""Observe only newly displayed notices after the current final action.

Upload completion, stale notices and the user's caption never grant success.
Natural submit responses remain preferred; a missing notice uses the existing
receipt lookup. No API requests, clicks or retries are made by this module.
"""
from yijianjifa.features.publish.platform_feedback import safe_message
from yijianjifa.features.publish.coordinator import AdapterOutcome

INSTALL = r'''() => {
window.p011SubmitFeedback?.observer?.disconnect();
const selector='[role="alert"],[role="status"],[class*="toast"],.el-message,.ant-message-notice-content,.bili-message,[class*="message-content"]';
const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none'&&!e.closest('[hidden],[aria-hidden="true"],[contenteditable="true"],textarea,input');
function notice(e){
 if(!visible(e))return null;
 const message=(e.innerText||'').replace(/\s+/g,' ').trim();
 if(!message||message.length>400)return null;
 if(/^(?:(?:视频|作品|稿件)\s*)?(?:定时发布|发布|发表|投稿|提交)(?:已)?成功(?:[。！!]|[，,：:].{0,100})?$/.test(message))return {state:'accepted',message};
 if(/验证码|扫码|安全验证|身份验证/.test(message))return null;
 const failure=/(?:发布|发表|投稿|提交).{0,24}(?:失败|被限制|过于频繁|操作频繁|不允许|无法)|(?:已达到|已达|达到|超过|超出).{0,12}(?:上限|限制)|(?:发布|发表|投稿|提交).{0,24}(?:次数|数量|额度).{0,8}(?:已用完|已用尽|已满)|(?:标题|视频|封面|标签|分区|内容).{0,24}(?:不符合|违规|超出|不能为空)|(?:账号|账户).{0,16}(?:封禁|禁言|无发布权限)/.test(message);
 if(!failure)return null;
 return {state:/网络|超时|繁忙|服务异常|系统异常|服务器|系统错误/.test(message)?'unknown':'rejected',message};
}
const previous=new Set(Array.from(document.querySelectorAll(selector)).filter(e=>notice(e)));
const watch={value:null};
watch.observer=new MutationObserver(()=>{
 for(const e of document.querySelectorAll(selector)){
  if(previous.has(e))continue;
  const value=notice(e);if(!value)continue;
  if(!watch.value||watch.value.state!=='rejected')watch.value=value;
 }
});
window.p011SubmitFeedback=watch;
watch.observer.observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true});
}'''
READ = '() => window.p011SubmitFeedback?.value || null'


def install(surface):
    try:
        surface.evaluate(INSTALL)
    except Exception:
        pass  # Changed/closed pages still use the existing receipt path.


def read(surface):
    try:
        value = surface.evaluate(READ)
    except Exception:
        return None
    if type(value) is not dict or value.get('state') not in {'accepted', 'rejected', 'unknown'}:
        return None
    message = safe_message(value.get('message'))
    return {'state': value['state'], 'message': message} if message else None


def outcome(session, surface, platform, request_digest):
    if not session.final or session.phase != 'result':
        return None
    value = read(surface)
    if value is None:
        return None
    if value['state'] == 'accepted':
        scheduled = session.payload['execution_mode'] == 'platform_schedule'
        return AdapterOutcome('platform_scheduled' if scheduled else 'platform_accepted',
            scheduled_at=session.payload.get('scheduled_at') if scheduled else None,
            submission_notice={'kind': 'publish_success_toast', 'platform': platform, 'request_digest': request_digest})
    return AdapterOutcome('platform_rejected' if value['state'] == 'rejected' else 'result_unknown',
        error_code='platform_rejected' if value['state'] == 'rejected' else 'result_unknown',
        platform_message=value['message'])


def recorded_outcome(engine, session):
    """Reopening a completed task cannot erase its already recorded acceptance."""
    import json
    from sqlalchemy import select
    from yijianjifa.storage.db import session_scope
    from yijianjifa.features.publish.deliveries import PublishDeliveryRow, DeliveryAttemptRow
    from yijianjifa.features.publish.coordinator import has_submission_notice
    if not session.final:
        return None
    with session_scope(engine) as db:
        row = db.get(PublishDeliveryRow, session.binding.get('delivery_id'))
        if row is None or row.request_digest != session.binding.get('digest'):
            return None
        attempt = db.scalar(select(DeliveryAttemptRow).where(
            DeliveryAttemptRow.delivery_id==row.id, DeliveryAttemptRow.number==row.attempt))
        if attempt is None or not attempt.final_json or not attempt.result_json:
            return None
        evidence = json.loads(attempt.result_json)
        final = json.loads(attempt.final_json)
        if not has_submission_notice(evidence, platform=row.platform, request_digest=row.request_digest,
                final_started=final.get('request_digest')==row.request_digest):
            return None
        if evidence['state']=='platform_scheduled' and (row.execution_mode!='platform_schedule'
                or evidence.get('scheduled_at')!=row.scheduled_at):
            return None
        return AdapterOutcome(evidence['state'], scheduled_at=evidence.get('scheduled_at'),
            receipt_reference=evidence.get('receipt_reference'), submission_notice=evidence['submission_notice'])
