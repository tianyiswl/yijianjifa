"""Read the existing queues without copying state or exposing task snapshots."""
import json
import platform
import re
import sys
from pathlib import Path
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.mixing.queue import MixTask
from yijianjifa.features.tasks.service import TaskCenterService, TaskItemRow
from yijianjifa.features.tasks.diagnostics import DiagnosticBundle, _redact_text
from yijianjifa.features.finished.service import FinishedVideo
from yijianjifa.features.accounts.service import AccountService

ACTION={'action_required','missed_waiting_user','interrupted','paused_auth','result_unknown','pre_submit_failed','failed','completed_with_errors','partial'}
STATUS={'running':{'running','publishing'},'waiting':{'waiting','waiting_publish'},'paused':{'paused','interrupted'},'action':ACTION,
        'failed':{'failed','pre_submit_failed','completed_with_errors'},'ambiguous':{'result_unknown'},
        'done':{'completed','published','platform_scheduled','succeeded','skipped'},'cancelled':{'cancelled'}}


def _serial_progress(value):
    """仅复制有限的只读进度字段；不把内部快照或宽松布尔值暴露给界面。"""
    if type(value) is not dict or value.get('mode')!='per_item_v1':return None
    numbers=('quantity','item_index','item_number','target_count','item_received','items_received',
             'targets_received','targets_total','waiting_count','needs_action_count','error_count')
    if any(type(value.get(k)) is not int or value[k]<0 for k in numbers):return None
    if type(value.get('needs_action')) is not bool:return None
    if value.get('stage') not in {'making','publishing','waiting_schedule','needs_attention','paused','completed'}:return None
    if value.get('waiting_reason') not in {'none','dispatch','account','receipt','schedule','verification','result','authorization','configuration'}:return None
    if (not 1<=value['item_number']<=value['quantity'] or value['item_index']!=value['item_number']-1
            or value['item_received']>value['target_count'] or value['items_received']>value['quantity']
            or value['targets_total']!=value['quantity']*value['target_count']
            or value['targets_received']>value['targets_total']):return None
    result={k:value[k] for k in (*numbers,'mode','stage','waiting_reason','needs_action')}
    skipped=value.get('skipped_count',0)
    if type(skipped) is not int or skipped<0 or skipped>value['error_count']:return None
    result['skipped_count']=skipped
    for k in ('item_title','reason'):
        if type(value.get(k)) is not str:return None
        result[k]=_redact_text(value[k])
    return result


def _serial_label(serial,state):
    if state in {'cancelled','cancelling'}:return {'cancelled':'已取消','cancelling':'正在取消'}[state]
    if serial['error_count']>serial.get('skipped_count',0):return '本条处理遇到错误，后续等待处理'
    if state in {'paused','interrupted'} or serial['stage']=='paused':return '已暂停，等待继续'
    if serial['stage']=='completed':return f"本批处理完成，{serial['skipped_count']}项因投稿上限已跳过" if serial.get('skipped_count') else '本批全部视频已被平台接收'
    if serial['stage']=='making':return '正在制作'
    return {'dispatch':'等待发布开始','account':'等待账号空闲','receipt':'等待平台确认接收',
            'schedule':'等待约定发布时间','verification':'等待完成平台验证','result':'等待核对原结果',
            'authorization':'等待明确继续本批','configuration':'等待核对本批配置'}.get(serial['waiting_reason'],'等待平台确认接收')


def _publishing_counts(execution,total):
    """旧批次仅按可完整读取的真实目标计数，不沿用把未知结果算失败的汇总。"""
    publishing=execution.get('publishing') if type(execution) is dict else None
    if type(publishing) is not dict or publishing.get('complete') is not True or publishing.get('omitted',0)!=0:return None
    targets=publishing.get('targets')
    if type(total) is not int or type(targets) is not list or len(targets)!=total:return None
    identities=[target.get('id') for target in targets if type(target) is dict]
    if any(type(identity) is not str or not identity for identity in identities) or len(set(identities))!=total:return None
    counts=dict(accepted=0,error=0,verification=0,result=0,account=0,waiting=0)
    for target in targets:
        if type(target) is not dict:return None
        state=target.get('state')
        if state in {'published','platform_scheduled','platform_accepted','processing','reviewing'}:counts['accepted']+=1
        elif state in {'pre_submit_failed','platform_rejected','failed'}:counts['error']+=1
        elif state=='paused_auth' or state in {'result_unknown','interrupted','unknown'} and target.get('needs_verification') is True:counts['verification']+=1
        elif state in {'result_unknown','interrupted','unknown'}:counts['result']+=1
        elif state=='waiting' and target.get('blocked_by'):counts['account']+=1
        elif state in {'waiting','running','publishing','queued','submitting','uploading','cancelled','waiting_adapter','waiting_configuration','action_required','missed_schedule','missed_waiting_user'}:counts['waiting']+=1
        else:return None
    return counts


def _execution_log_metadata():
    """读取固定构建清单的提交号，不把个人路径或任意清单文本带进日志。"""
    from yijianjifa import APP_VERSION
    commit='未记录'
    if getattr(sys,'frozen',False):
        try:
            manifest=Path(sys.executable).resolve().parent/'build-manifest.json'
            if manifest.is_file() and manifest.stat().st_size<=65536:
                value=json.loads(manifest.read_text(encoding='utf-8-sig'))
                candidate=value.get('commit') if type(value) is dict else None
                if type(candidate) is str and re.fullmatch(r'[0-9a-fA-F]{40}',candidate):commit=candidate.lower()
        except (OSError,UnicodeError,ValueError,TypeError):pass
    system={'Windows':'Windows','Darwin':'macOS','Linux':'Linux'}.get(platform.system(),'未记录')
    return ['应用版本：'+APP_VERSION,'构建提交：'+commit,'日志格式：1','系统：'+system]


def _mix_diagnostic(value):
    if not isinstance(value,dict):return {}
    result={k:_redact_text(value[k]) for k in ('stage','material','reason','error_type') if isinstance(value.get(k),str)}
    if isinstance(value.get('materials'),list):
        result['materials']=[_redact_text(v) for v in value['materials'] if isinstance(v,str)]
    return result


def _mix_execution_log(value):
    if not isinstance(value,list):return []
    result=[]
    for entry in value[-500:]:
        if not isinstance(entry,dict) or not isinstance(entry.get('step'),str) or not re.fullmatch(r'[a-zA-Z_.]{1,80}',entry['step']):continue
        event=_mix_diagnostic(entry)
        event['step']=entry['step']
        if isinstance(entry.get('time'),str):event['time']=_redact_text(entry['time'])
        result.append(event)
    return result


class TaskCenterAccess:
    def __init__(self,engine,*,multi_publish=None):self.engine=engine;self.multi_publish=multi_publish

    def _multi_row(self,t,data):
        result=dict(id=t.id,kind='publish',version=2,content_type=data.get('content_type','video'),name=t.name,state=data['state'],quantity=data['quantity'],
            completed=data['completed'],succeeded=data['published_count'],failed=sum(d['state']=='pre_submit_failed' for d in data['deliveries']),
            summary=data['summary'],counts=data['counts'],accepted_count=data['accepted_count'],published_count=data['published_count'],
            position=t.queue_order,account='',created_at=t.created_at.isoformat())
        if data.get('status_label'):
            result['status_label']=data['status_label']
        return result

    def _rows(self,daily_cache=None):
        with session_scope(self.engine) as session:
            mix=list(session.scalars(select(MixTask).order_by(MixTask.position,MixTask.created_at)))
            result=[]
            for t in mix:
                snapshot=json.loads(t.snapshot_json)
                titles=[c.get('title','') for c in snapshot.get('copies',[]) if isinstance(c,dict)]
                result.append(dict(id=t.id,kind='make',name='制作批次'+(' · '+titles[0] if titles else ''),state=t.state,
                    quantity=t.quantity,completed=t.completed_count,succeeded=t.success_count,failed=t.failure_count,
                    position=t.position,account='',created_at=t.created_at.isoformat()))
                if snapshot.get('daily_publish'):
                    frozen=snapshot['daily_publish']
                    from yijianjifa.features.mixing.serial_gate import has_flow_marker
                    if has_flow_marker(snapshot):result[-1]['workflow_serial']=True
                    from .daily_mixing import read_status
                    daily = read_status(self.engine, t.id)
                    if daily_cache is not None:daily_cache[t.id]=daily
                    result[-1]['workflow_needs_attention'] = daily['phase'] in {'needs_attention', 'needs_login'}
                    if daily['phase'] in {'checking_login', 'needs_login'}:
                        result[-1]['status_label'] = {'checking_login': '正在检查账号登录', 'needs_login': '等待登录后继续'}[daily['phase']]
                    if t.state in {'completed', 'completed_with_errors'}:
                        result[-1]['status_label'] = {
                            'checking_login':'制作完成，正在检查登录', 'needs_login':'制作完成，等待登录',
                            'publishing':'制作完成，发布中', 'needs_attention':'制作完成，发布需要处理',
                            'accepted':'平台已接收', 'completed':'制作及发布完成', 'cancelled':'已停止发布',
                        }.get(daily['phase'], '制作完成')
                    serial=_serial_progress(daily.get('serial'))
                    if serial:
                        result[-1].update(serial=serial,status_label=_serial_label(serial,t.state),
                            workflow_needs_attention=(serial['needs_action'] or serial['error_count']>0) and t.state not in {'cancelled','cancelling'},
                            workflow_has_errors=serial['error_count']>0)
                    elif t.state=='waiting_publish':result[-1]['status_label']='等待读取发布进度'
        ids={t['id'] for t in result};accounts={a.id:a.nickname for a in AccountService(self.engine).list()}
        for t in TaskCenterService(self.engine).list():
            if t.id in ids:continue
            if t.kind=='multi_publish':
                if self.multi_publish:data=self.multi_publish.detail(t.id)
                else:
                    from .multi_publish import MultiPublishService
                    from .multi_publish_dto import detail
                    data=detail(MultiPublishService(self.engine,None,read_only=True),t.id)
                if all(d['hidden'] for d in data['deliveries']):continue
                result.append(self._multi_row(t,data))
                continue
            if t.kind=='download':
                from .download_dto import batch_detail
                summary=batch_detail(self.engine,t.id);counts=summary['counts']
                result.append(dict(id=t.id,kind='download',name=t.name,state=t.state,quantity=summary['total'],
                    completed=sum(counts.get(k,0) for k in ('succeeded','partial','failed','skipped','cancelled')),
                    succeeded=counts.get('succeeded',0),failed=counts.get('failed',0),partial=counts.get('partial',0),position=t.queue_order,
                    account='',created_at=t.created_at.isoformat()))
                continue
            with session_scope(self.engine) as session:
                items=list(session.scalars(select(TaskItemRow).where(TaskItemRow.task_id==t.id)))
            result.append(dict(id=t.id,kind='publish',name=t.name,state=t.state,quantity=len(items),
                completed=sum(i.state in {'completed','published','failed','platform_scheduled'} for i in items),
                succeeded=sum(i.state in {'completed','published','platform_scheduled'} for i in items),failed=sum(i.state=='failed' for i in items),
                position=t.queue_order,account=accounts.get(t.account_id,''),created_at=t.created_at.isoformat()))
        for row in result:
            if row['kind']=='publish':
                with session_scope(self.engine) as session:
                    review=session.get(Setting,'publish-review:'+row['id'])
                    entries=json.loads(review.value_json) if review else []
                    if entries:row['manual_review_label']=entries[-1]['label']
                    from .task_removal import reviewed_removed
                    row['reviewed_removed']=reviewed_removed(session,row['id'])
        with session_scope(self.engine) as session:
            hidden={r.key.removeprefix('task-hidden:') for r in session.scalars(select(Setting).where(Setting.key.like('task-hidden:%')))}
        return sorted((r for r in result if r['id'] not in hidden),key=lambda t:(t['created_at'],t['id']),reverse=True)

    def list(self,query,tab,status,offset,limit):
        rows=[r for r in self._rows() if (tab=='all' or (tab=='action' and (r['state'] in ACTION or r.get('workflow_needs_attention'))) or tab==r['kind'])
              and (not status or r['state'] in STATUS[status] or status=='action' and r.get('workflow_needs_attention')
                   or status=='failed' and r.get('workflow_has_errors'))
              and query.casefold() in ' '.join([r['name'],r['id'],r['account']]).casefold()]
        return {'total':len(rows),'items':rows[offset:offset+limit]}

    def detail(self,pid):
        from yijianjifa.features.tasks.service import TaskCenterRow
        with session_scope(self.engine) as s:
            row=s.get(TaskCenterRow,pid)
            v2=row is not None and row.kind=='multi_publish'
        if v2:
            if self.multi_publish:data=self.multi_publish.detail(pid)
            else:
                from .multi_publish import MultiPublishService
                from .multi_publish_dto import detail
                data=detail(MultiPublishService(self.engine,None,read_only=True),pid)
            record=self._multi_row(row,data)
            return {**data,'task':record,'items':data['deliveries'],'settings':{'kind':'multi_publish'},'issues':[],'outputs':[]}
        daily_cache={}
        record=next((r for r in self._rows(daily_cache) if r['id']==pid),None)
        if record is None:raise KeyError(pid)
        result={'task':record,'controls':[],'settings':{},'items':[],'issues':[],'outputs':[]}
        if record['kind']=='download':
            from .download_dto import batch_detail
            data=batch_detail(self.engine,pid)
            result['settings']={'kind':'download','options':data['options']}
            state=record['state']
            result['controls']=['cancel'] if state in {'waiting','running'} else ['resume'] if state in {'interrupted','paused','paused_auth','completed_with_errors','partial','failed','cancelled'} else []
            result['items']=[{'index':i,'id':item['id'],'state':item['state'],'title':item['title'],'stages':item['stages']} for i,item in enumerate(data['items'])]
            result['outputs']=[{'id':o['id'],'name':o['label'],'index':i,'kind':o['kind']} for i,item in enumerate(data['items']) for o in item['outputs']]
            result['download_task_id']=pid
            return result
        if record['kind']=='make':
            state=record['state']
            if state in {'waiting','running','waiting_publish'}:result['controls'].append('pause')
            if state in {'paused','interrupted'} and not record.get('workflow_serial'):result['controls'].append('resume')
            if state in {'waiting','running','waiting_publish','paused','interrupted'}:result['controls'].append('cancel')
            if state=='waiting':result['controls']+=['up','down']
            with session_scope(self.engine) as session:
                task=session.get(MixTask,pid);snapshot=json.loads(task.snapshot_json)
                plan=snapshot.get('plan',{});result['settings']={k:plan[k] for k in ('resolution','aspect_ratio','frame_fit','quantity','material_category_ids','copy_category_ids','background_music','transition') if k in plan}
                for k in ('total_ms','segment_ms','total_range_ms','segment_range_ms'):result['settings'][k]=snapshot.get(k)
                option_keys = {'voice', 'speed', 'sentence_cut', 'spoken_enabled', 'full_enabled',
                               'batch_full_text', 'keep_original_audio', 'original_gain', 'voice_gain',
                               'music_gain', 'spoken_style', 'full_style'}
                result['settings']['options'] = {k: v for k, v in snapshot.get('options', {}).items()
                                                 if k in option_keys}
                if snapshot.get('daily_publish'):
                    daily = daily_cache[pid]
                    result['daily_publish'] = {k: daily[k] for k in (
                        'phase', 'publish_task_ids', 'accepted', 'pending', 'failed', 'total',
                        'making_failed', 'published', 'resolved_start', 'can_continue') if k in daily}
                    result['daily_publish']['reason'] = _redact_text(str(daily.get('reason') or ''))
                    if 'serial' in record:result['daily_publish']['serial']=record['serial']
                    saved=session.get(Setting,'mix.daily.status:' + pid)
                    result['daily_publish']['login_log'] = [{k:event[k] for k in ('time','step','stage','accounts') if k in event}
                        for event in (json.loads(saved.value_json).get('login_log', []) if saved else [])[-50:]]
                handoff=session.get(Setting,'mix-publish-status:'+pid)
                if handoff:
                    status=json.loads(handoff.value_json)
                    result['publish_handoff']={
                        'tasks':[{k:t[k] for k in ('id','state','quantity','submitted')} for t in status['tasks']],
                        'blocked':[{'indices':b['indices'],'code':'post_mix_review_required'} for b in status['blocked']]}
                publish_after=snapshot.get('publish_after')
                if publish_after:
                    if not handoff and task.state in {'completed','completed_with_errors'}:
                        result['publish_handoff']={'tasks':[],'blocked':[{'indices':[], 'code':'post_mix_review_required'}]}
                    result['settings']['publish_after']={
                        'account_id':publish_after['account']['id'],
                        'linkage':publish_after['linkage'],
                        'execution_enabled':False,
                        'hold_reason':publish_after['hold_reason']}
                for index in range(task.quantity):
                    receipt=session.get(Setting,f'mix-result:{pid}:{index}')
                    # A receipt is authoritative for finished items. Unstarted items
                    # inherit a held/cancelled batch state, never a fabricated result.
                    pending_state = 'waiting'
                    if task.state in {'paused', 'interrupted', 'cancelled', 'cancelling'}:
                        pending_state = task.state
                    elif task.state == 'running' and index == task.current_index:
                        pending_state = 'running'
                    item=json.loads(receipt.value_json) if receipt else {'state':pending_state}
                    visible={'index':index,'state':item['state']}
                    diagnostic=_mix_diagnostic(item.get('diagnostic'))
                    events=_mix_execution_log(item.get('execution_log'))
                    if diagnostic:visible['diagnostic']=diagnostic
                    if events:visible['execution_log']=events
                    for key in ('error','time'):
                        if isinstance(item.get(key),str):visible[key]=_redact_text(item[key])
                    result['items'].append(visible)
                    if publish_after:result['items'][-1]['planned_publish_time']=publish_after['slots'][index]['time']
                    if item.get('error'):
                        bundle=DiagnosticBundle.from_raw(dict(task_id=pid,task_type='make',title='本地制作失败',object=f'第{index+1}条视频',
                            stage=diagnostic.get('stage') or '未记录',reason=diagnostic.get('reason') or item['error'],impact='此条未完成；其他成功成品保留。',
                            next_action='根据错误检查素材、音色和输出权限，修复后重新创建所需任务。',occurred_at=item.get('time',''),
                            error_code='LOCAL_RENDER_FAILED',app_version='WebUI Mac 开发版',os=platform.platform(),raw_error=item['error'],
                            recent_steps=[event['step'] for event in events]))
                        bundle.payload['index']=index
                        for key in ('material','materials','error_type'):
                            if key in diagnostic:bundle.payload[key]=diagnostic[key]
                        result['issues'].append(bundle.payload)
                    success=session.get(Setting,f'mix-success:{pid}:{index}')
                    if success:
                        f=session.get(FinishedVideo,json.loads(success.value_json)['finished_id'])
                        if f:result['outputs'].append({'id':f.id,'name':f.display_name,'index':index})
        else:
            core=TaskCenterService(self.engine);issue=core.latest_issue(pid)
            if issue:result['issues'].append(DiagnosticBundle.from_issue(issue,task_type='publish',environment={'app_version':'WebUI Mac 开发版','os':platform.platform()}).payload)
            snapshot=json.loads(core.get(pid).snapshot_json)
            if snapshot.get('kind') in {'normal_publish','commerce_publish'}:
                if snapshot.get('execution_enabled') is False and record['state'] in {'paused','waiting','result_unknown','missed_waiting_user'}:result['controls'].append('cancel')
                result['settings']={'kind':snapshot['kind'],'platform':'douyin','hold_reason':snapshot.get('hold_reason',''),'execution_enabled':snapshot.get('execution_enabled') is True}
            with session_scope(self.engine) as session:
                ordered=list(session.scalars(select(TaskItemRow).where(TaskItemRow.task_id==pid).order_by(TaskItemRow.id)))
                if snapshot.get('kind') in {'normal_publish','commerce_publish'}:
                    positions={x['account_id']+':'+x['video_id']:i for i,x in enumerate(snapshot.get('items',[]))}
                    ordered.sort(key=lambda r:positions.get(r.item_key,len(positions)))
                if snapshot.get('kind')=='normal_publish' and snapshot.get('execution_enabled') is False:
                    from .publish_control import cancellable_items
                    from yijianjifa.features.publish.history import PublishHistoryRow
                    histories=list(session.scalars(select(PublishHistoryRow).where(PublishHistoryRow.task_id==pid)))
                    account_ids=dict.fromkeys(item['account_id'] for _,item,_ in cancellable_items(ordered,histories,snapshot))
                    labels={a.id:a.nickname for a in AccountService(self.engine).list()}
                    result['account_controls']=[{'account_id':aid,'label':labels.get(aid,aid),'controls':['cancel']} for aid in account_ids]
                for i,r in enumerate(ordered):
                    item={'index':i,'state':r.state}
                    if snapshot.get('kind') in {'normal_publish','commerce_publish'}:
                        detail=json.loads(r.detail_json)
                        item.update({k:detail[k] for k in ('video_id','account_id','time','title','body','tags') if type(detail.get(k)) is str})
                        mode=detail.get('execution_mode')
                        item['execution_mode']=mode if mode in ('platform_schedule','software_schedule','publish_now') else 'unspecified'
                        item['software_consent']=detail.get('software_consent') is True
                        item['repeat_confirmed']=detail.get('repeat_confirmed') is True
                        cover=detail.get('cover')
                        if type(cover) is dict:item['cover']={k:cover[k] for k in ('kind','frame_ms') if type(cover.get(k)) in (str,int) or cover.get(k) is None}
                        location=detail.get('location')
                        item['location']={k:location[k] for k in ('id','name','address') if type(location.get(k)) is str} if type(location) is dict else None
                        declaration=detail.get('declaration')
                        item['declaration']={k:declaration[k] for k in ('id','label') if type(declaration.get(k)) is str} if type(declaration) is dict else None
                        if snapshot.get('kind')=='commerce_publish':
                            for field,keys in (('store',('id','name','region','scope','commission','address','favorite')),('platform_music',('id','title')),('declaration',('id','label'))):
                                value=detail.get(field)
                                item[field]={k:value[k] for k in keys if type(value.get(k)) in (str,bool)} if type(value) is dict else None

                    result['items'].append(item)
        if record['kind']=='publish':
            from .publish_review import PublishReview
            result['manual_review']=PublishReview(self.engine).read(pid)
        if 'daily_publish' in result:
            from .mix_execution import read_execution
            result['execution']=daily_cache[pid].get('execution') or read_execution(self.engine,pid)
            result['execution'].pop('serial',None)
            if 'serial' in record:result['execution']['serial']=record['serial']
        return result

    def diagnostic(self,pid):
        data=self.detail(pid)
        return {'task':data['task'],'issues':data['issues'],'items':data['items'],'manual_review':data.get('manual_review')}

    def _daily_copy_children(self,pid,daily):
        """Follow only the native reverse binding; never recurse through tasks."""
        from yijianjifa.features.tasks.service import TaskCenterRow
        from .daily_mixing import SOURCE
        values=daily.get('publish_task_ids',[])
        if type(values) is not list:return [],False
        children=[];seen={pid};complete=len(values)<=100
        for child in values[:100]:
            if type(child) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}',child):
                complete=False;continue
            if child in seen:
                complete=False
                continue
            seen.add(child)
            with session_scope(self.engine) as session:
                source=session.get(Setting,SOURCE+child)
                try:binding=json.loads(source.value_json) if source else {}
                except (TypeError,ValueError):binding={}
                task=session.get(TaskCenterRow,child)
                valid=(type(binding) is dict and binding.get('mix_task_id')==pid and task is not None
                       and task.kind=='multi_publish' and session.get(Setting,'task-hidden:'+child) is None)
            if not valid:
                complete=False;continue
            try:data=self.detail(child)
            except (KeyError,ValueError,TypeError):
                complete=False;continue
            if not any(not target.get('hidden') for target in data.get('deliveries',[])):
                complete=False;continue
            children.append(data)
        return children,complete

    @staticmethod
    def _append_publish_execution(lines,data):
        platforms={'douyin':'抖音','kuaishou':'快手','channels':'视频号','bilibili':'B站'}
        found=False
        for delivery in data.get('deliveries',[]):
            if delivery.get('hidden'):continue
            lines.extend(['','目标：'+str(delivery.get('delivery_id',delivery.get('id',''))),
                          '平台：'+platforms.get(delivery.get('platform'),str(delivery.get('platform',''))),
                          '账号：'+str(delivery.get('account_label','未记录')),
                          ('图文：' if delivery.get('content_type')=='image_post' else '视频：')+str(delivery.get('effective_fields',{}).get('title','未记录')),
                          '状态：'+str(delivery.get('state','未记录')),
                          '发布时间：'+('立即发布' if delivery.get('execution_mode')=='publish_now' else str(delivery.get('scheduled_at') or '未记录'))])
            if delivery.get('reason_message'):lines.append('原因：'+str(delivery['reason_message']))
            from .multi_publish_dto import safe_identity_execution_events
            events=safe_identity_execution_events(delivery.get('execution_log',[]))
            if delivery.get('platform')=='douyin':
                from .multi_publish_dto import identity_diagnostic_summary, result_diagnostic_summary
                diagnostic=identity_diagnostic_summary(events) or result_diagnostic_summary(events)
                if diagnostic:lines.append('诊断摘要：'+diagnostic)
            if not events:lines.append('尚未记录执行日志。')
            for entry in events[-500:]:
                if not isinstance(entry,dict) or not re.fullmatch(r'[a-zA-Z_.]{1,80}',str(entry.get('step',''))):continue
                checks={k:v for k,v in entry.get('checks',{}).items() if
                        isinstance(k,str) and re.fullmatch(r'[a-zA-Z_]{1,64}',k) and
                        (type(v) in (bool,int) or (type(v) is str and re.fullmatch(r'[a-zA-Z_]{1,64}',v)))}
                stamp=str(entry.get('time','未记录'))
                lines.append(stamp+' · '+entry['step']+' · '+json.dumps(checks,ensure_ascii=False,sort_keys=True))
                found=True
        return found

    def execution_log_text(self,pid):
        """Copy the current saved task events, never a stale frontend snapshot."""
        from .mix_execution import read_execution, _text, _stamp
        data=self.detail(pid);task=data['task']
        lines=['一剪即发 · 任务执行日志','任务：'+str(task['name']),
               '任务编号：'+str(task['id']),'状态：'+str(task['state']),
               '创建时间：'+str(task.get('created_at','未记录'))]
        lines.extend(_execution_log_metadata())
        children=[];complete=True
        execution=data.get('execution') or (read_execution(self.engine,pid) if task.get('kind')=='make' else None)
        if execution:
            making=execution['making']
            lines.extend(['',f"制作进度：已完成 {making['completed']}/{making['total']} 条 · 成功 {making['succeeded']} 条 · 失败 {making['failed']} 条"])
            report=execution.get('report') or {}
            if report.get('ready'):lines.extend(['',report['text'],''])
        if data.get('daily_publish'):
            daily = data['daily_publish']
            children,complete=self._daily_copy_children(pid,daily)
            labels = {'making':'制作中','publishing':'发布中','accepted':'平台已接收',
                      'checking_login':'正在检查账号登录','needs_login':'等待登录后继续',
                      'needs_attention':'需要处理','completed':'已完成','cancelled':'已取消'}
            lines.extend(['', '制作后发布：'+labels.get(daily.get('phase'), '等待处理')])
            # An invalid/hidden link must not supply another task's current
            # title/account or its aggregate publishing counts to this copy.
            serial=task.get('serial')
            if serial:
                lines.append(f"逐条进度：第{serial['item_number']}/{serial['quantity']}条 · 当前条平台已接收 {serial['item_received']}/{serial['target_count']} · 全局 {serial['targets_received']}/{serial['targets_total']}")
                lines.append('当前状态：'+_serial_label(serial,task['state']))
                lines.append(f"发布统计：错误 {serial['error_count']} 项 · 等待 {serial['waiting_count']} 项 · 等待处理 {serial['needs_action_count']} 项")
            elif complete:
                counts=_publishing_counts(execution,daily.get('total'))
                if counts is not None:
                    lines.append(f"发布进度：平台已接收 {counts['accepted']}/{daily['total']} 项 · 错误 {counts['error']} 项 · 等待核对 {counts['result']} 项 · 等待验证 {counts['verification']} 项 · 等待账号 {counts['account']} 项 · 其他等待 {counts['waiting']} 项")
                else:lines.append('部分发布记录尚不能完整核实，未推算错误或等待数量。')
            reasons=dict.fromkeys(_text(value) for value in (daily.get('reason'),(execution or {}).get('reason')) if value)
            for reason in reasons:
                if reason:lines.append('原因：'+reason)
            publishing=execution.get('publishing') if execution else None
            if type(publishing) is dict:
                targets=publishing.get('targets')
                if type(targets) is list and targets:
                    lines.extend(['','发布结果一览'])
                    for target in targets[:400]:
                        if type(target) is not dict:continue
                        fields=[_text(target.get(key)) for key in ('account_label','title','state_label')]
                        message=' · '.join(value for value in fields if value)
                        reason=_text(target.get('reason'))
                        if reason:message+='；原因：'+reason
                        if message:lines.append(message)
                if publishing.get('complete') is False:
                    lines.append('部分发布记录暂不可读取，请查看本批任务。')
            # The panel's final 100 combined events may already be all publish
            # steps. Read this parent's bounded login history independently.
            with session_scope(self.engine) as session:
                row=session.get(Setting,'mix.daily.status:'+pid)
                try:progress=json.loads(row.value_json) if row else {}
                except (TypeError,ValueError):progress={}
            login_log=progress.get('login_log',[]) if type(progress) is dict else []
            represented_failures=set()
            for entry in login_log[-100:] if type(login_log) is list else []:
                if (type(entry) is not dict or _stamp(entry.get('time')) is None
                        or entry.get('stage') not in {'before_make','before_publish'}
                        or entry.get('step') not in {'daily.login_verified','daily.login_check_started','daily.login_check_step','daily.login_check_finished'}):continue
                total=entry.get('accounts')
                if type(total) is not int or not 1<=total<=200:continue
                message='账号登录检查已通过（'+str(total)+'个账号）'
                if entry['step']!='daily.login_verified':
                    index=entry.get('index')
                    if type(index) is not int or not 1<=index<=total:continue
                    message=f'第{index}/{total}个账号：'+_text(entry.get('message'))
                    if entry['step']=='daily.login_check_finished':
                        represented_failures.add((entry['stage'],index,total,_text(entry.get('account_label')),_text(entry.get('message'))))
                fields=[entry['time'],entry['step'],
                        '开始制作前' if entry['stage']=='before_make' else '整批制作完成后、提交平台前',
                        _text(entry.get('account_label')),message]
                lines.append(' · '.join(field for field in fields if field))
            # An early failed account may fall outside the 100-step panel tail.
            # These are this round's saved summaries, never inferred old events.
            failures=progress.get('login_failures',[]) if type(progress) is dict else []
            for entry in failures[:200] if type(failures) is list else []:
                if (type(entry) is not dict or _stamp(entry.get('time')) is None
                        or entry.get('stage') not in {'before_make','before_publish'}):continue
                index,total=entry.get('index'),entry.get('accounts')
                if type(index) is not int or type(total) is not int or not 1<=index<=total<=200:continue
                label,message=_text(entry.get('account_label')),_text(entry.get('message'))
                if not message or (entry['stage'],index,total,label,message) in represented_failures:continue
                fields=[entry['time'],'daily.login_check_summary',
                        '开始制作前' if entry['stage']=='before_make' else '整批制作完成后、提交平台前',
                        label,f'第{index}/{total}个账号：'+message]
                lines.append(' · '.join(field for field in fields if field))
        if execution and complete:
            current=execution.get('current',{})
            fields=[_text(current.get(key)) for key in ('label','account_label','title')]
            if type(current.get('index')) is int and type(current.get('total')) is int:
                fields.insert(1,f"第{current['index']}/{current['total']}条")
            if any(fields):lines.append('当前：'+' · '.join(field for field in fields if field))
        found=self._append_publish_execution(lines,data)
        if task.get('kind')=='make' and data.get('items'):
            for item in data['items']:
                lines.extend(['',f"第{item['index']+1}条视频",'状态：'+str(item.get('state','未记录'))])
                diagnostic=item.get('diagnostic',{})
                for key,label in (('material','失败素材'),('stage','失败步骤'),('error_type','错误类型')):
                    if diagnostic.get(key):lines.append(label+'：'+str(diagnostic[key]))
                if diagnostic.get('materials'):
                    lines.append('参与素材：'+'；'.join(diagnostic['materials']))
                reason=diagnostic.get('reason') or item.get('error')
                if reason:lines.append('失败原因：'+str(reason))
                events=item.get('execution_log',[])
                if not events:lines.append('此条未记录执行步骤；未补造历史日志。')
                for entry in events:
                    fields=[str(entry.get('time','未记录')),str(entry.get('step','未记录'))]
                    fields.extend(label+'：'+str(entry[key]) for key,label in (
                        ('stage','步骤'),('material','素材'),('reason','原因'),('error_type','错误类型')) if entry.get(key))
                    lines.append(' · '.join(fields))
            for child in children:
                lines.extend(['','对应发布任务：'+str(child['task']['id']),
                              '发布任务状态：'+str(child['task']['state'])])
                self._append_publish_execution(lines,child)
            return _redact_text('\n'.join(lines))
        for issue in data.get('issues',[]):
            if issue.get('reason'):lines.append('失败原因：'+str(issue['reason']))
        if not found:lines.append('此任务没有已记录的执行步骤；未补造历史日志。')
        return _redact_text('\n'.join(lines))
