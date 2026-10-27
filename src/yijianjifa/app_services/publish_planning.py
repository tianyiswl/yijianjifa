"""Read-only ordinary-publish preparation; never creates or executes platform work."""
from datetime import datetime, timezone, timedelta
import math
from sqlalchemy import select
from pathlib import Path
from yijianjifa.storage.models import Category, CategoryLink
from yijianjifa.features.library.display_names import name_only
from yijianjifa.storage.db import session_scope
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideo, FinishedVideoService
from yijianjifa.features.publish.normal_service import NormalPublishService
from yijianjifa.features.publish.history import PublishHistoryRepository, DuplicatePublishBlocked, PublishHistoryRow, _ACTIVE_DUPLICATE_BLOCK_STATES, effective_history_state
from yijianjifa.features.publish.distribution import distribute, VideoAssignment
from yijianjifa.features.publish.schedule import ScheduleRule, PlatformLimits, build_schedule, validate_platform_window


class PublishPlanning:
    def __init__(self, engine, *, declarations=None, locations=None, covers=None,inherit_account_settings=True):
        self.covers=covers
        self.locations=locations
        self.declarations=declarations
        self.engine = engine
        self.inherit_account_settings=inherit_account_settings
        self.accounts = AccountService(engine)
        self.history = PublishHistoryRepository(engine)

    def candidates(self, *, query, offset, limit, advanced=False, include_published=False, account_ids=None,allow_new_request=False):
        if type(query) is not str or len(query)>200 or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('invalid candidate filter')
        if type(advanced) is not bool or type(include_published) is not bool or include_published and not advanced:
            raise ValueError('invalid publication filter')
        if type(allow_new_request) is not bool:raise ValueError('invalid publication intent')
        target_states = self.history.states_for_targets(account_ids) if account_ids is not None else None
        targets = [self.accounts.get(aid) for aid in account_ids] if account_ids is not None else []
        with session_scope(self.engine) as s:
            states={}
            if target_states is None:
                for history_row in s.scalars(select(PublishHistoryRow)):
                    states.setdefault(history_row.video_hash,set()).add(effective_history_state(s,history_row))
            rows = s.scalars(select(FinishedVideo).order_by(FinishedVideo.created_at.desc(), FinishedVideo.id.desc())).all()
            category_names=dict(s.execute(select(Category.id,Category.name)).all())
            links={}
            for target,cat in s.execute(select(CategoryLink.target_id,CategoryLink.category_id).where(CategoryLink.target_type=='finished')):links.setdefault(target,[]).append(cat)
            items=[]
            for v in rows:
                history=states.get(v.content_hash,set())
                target_details = []
                if target_states is None:
                    if not allow_new_request:
                        if history & _ACTIVE_DUPLICATE_BLOCK_STATES:continue
                        if 'published' in history and not (advanced and include_published):continue
                else:
                    for account in targets:
                        found = target_states.get((v.content_hash, account.id), set())
                        target_details.append(dict(account_id=account.id, platform=account.platform,
                                                   states=sorted(found), eligible=not self.history.target_blocking_states(found,allow_new_request=allow_new_request)))
                    if not any(t['eligible'] for t in target_details):continue
                    history = {state for t in target_details for state in t['states']}
                if query.casefold() not in '\n'.join((v.display_name,v.title,v.body,v.tags)).casefold():continue
                item = {**{k:getattr(v,k) for k in ('id','display_name','title','body','tags','duration_ms')},'published':'published' in history,'name':name_only(v.display_name),'format':Path(v.stored_path or v.original_path or '').suffix.removeprefix('.').upper(),'note':v.note or '', 'category_ids':links.get(v.id,[]),'category_names':[category_names.get(c,'未分类') for c in links.get(v.id,[])]}
                if target_states is not None:
                    item.update(targets=target_details, eligible_account_ids=[t['account_id'] for t in target_details if t['eligible']])
                items.append(item)
        default = self.accounts.default('douyin')
        return dict(items=items[offset:offset+limit],total=len(items),default_account_id=default.id if default else None)

    def preview(self, p, *, now=None, capability="ordinary"):
        if capability not in ("ordinary","commerce"):raise ValueError("invalid capability")
        required={'account_ids','video_ids','distribution','start','mode','interval_hours','daily_count','days'}
        if type(p) is not dict or not required<=set(p) or set(p)-required-{'moves','item_overrides','account_schedules','repeat_confirmations','time_choices','daily_times','item_schedules','declarations','locations','cover_overrides','allow_new_request'}:
            raise ValueError('invalid planning fields')
        allow_new_request=p.get('allow_new_request',False)
        if type(allow_new_request) is not bool:raise ValueError('invalid publication intent')
        for key in ('account_ids','video_ids'):
            values=p[key]
            if type(values) is not list or not 1<=len(values)<=200 or any(type(v) is not str or not 1<=len(v)<=64 for v in values) or len(set(values))!=len(values):
                raise ValueError('invalid selection')
        now=now or datetime.now(timezone.utc)
        schedule_keys={'start','mode','interval_hours','daily_count','days'}
        def parse_rule(values):
            if type(values) is not dict or not schedule_keys<=set(values) or set(values)-schedule_keys-{'daily_times'}:raise ValueError('invalid schedule fields')
            interval=values['interval_hours']
            if type(interval) not in (int,float) or not math.isfinite(interval) or not 0<interval<=336:raise ValueError('invalid interval')
            if values['mode'] not in ('daily','interval','exact','immediate') or any(type(values[k]) is not int or not 1<=values[k]<=200 for k in ('daily_count','days')):raise ValueError('invalid schedule')
            clocks=values.get('daily_times')
            if 'daily_times' in values and (values['mode']!='daily' or type(clocks) is not list):raise ValueError('invalid daily times')
            # None denotes an explicit immediate rule, with no scheduled clock time.
            if values['mode']=='immediate':
                if values['start'] is not None:raise ValueError('immediate mode cannot include a scheduled time')
                return None
            if type(values['start']) is not str or len(values['start'])>64:raise ValueError('invalid start')
            start=datetime.fromisoformat(values['start'])
            if start.tzinfo is None or start.utcoffset() is None:raise ValueError('start requires timezone')
            return ScheduleRule(values['mode'],start,interval,values['daily_count'],values['days'],tuple(clocks) if clocks is not None else None)
        common_rule=parse_rule({k:p[k] for k in schedule_keys|{'daily_times'} if k in p})
        exceptions=p.get('account_schedules',{})
        if type(exceptions) is not dict or any(k not in p['account_ids'] for k in exceptions):raise ValueError('invalid account schedule')
        account_rules={k:parse_rule(v) for k,v in exceptions.items()}
        accounts={pid:self.accounts.get(pid) for pid in p['account_ids']}
        if any(a.platform!='douyin' for a in accounts.values()):raise ValueError('unsupported platform')
        if capability=='ordinary' and self.inherit_account_settings:
            from .account_publish_settings import AccountPublishSettings
            defaults=AccountPublishSettings(self.engine,declarations=self.declarations,locations=self.locations)
            p=defaults.ordinary_params(p)
            self.declarations=defaults.declarations;self.locations=defaults.locations
        assignments=distribute(p['video_ids'],p['account_ids'],mode=p['distribution'])
        moves=p.get('moves',{})
        if type(moves) is not dict or len(moves)>200 or any(k not in p['video_ids'] or type(v) is not str or v not in accounts for k,v in moves.items()):raise ValueError('invalid manual assignment')
        if moves and p['distribution']!='split_across_accounts':raise ValueError('manual assignment requires split mode')
        assignments=[VideoAssignment(x.video_id,moves.get(x.video_id,x.account_id)) for x in assignments]
        keys={x.account_id+':'+x.video_id for x in assignments}
        cover_overrides=p.get('cover_overrides',{})
        if type(cover_overrides) is not dict or len(cover_overrides)>2000 or any(key not in keys for key in cover_overrides):raise ValueError('invalid cover overrides')
        if cover_overrides and self.covers is None:raise ValueError('cover preview unavailable')
        item_schedules=p.get('item_schedules',{})
        if type(item_schedules) is not dict or len(item_schedules)>2000:raise ValueError('invalid item schedules')
        item_rules={}
        for key,value in item_schedules.items():
            if key not in keys or type(value) is not dict or set(value)!={'mode','start'} or value['mode'] not in ('exact','immediate'):raise ValueError('invalid item schedule')
            item_rules[key]=parse_rule({**value,'interval_hours':3,'daily_count':1,'days':1})
        repeats=p.get('repeat_confirmations',[])
        if type(repeats) is not list or len(repeats)>2000 or any(type(k) is not str or k not in keys for k in repeats) or len(set(repeats))!=len(repeats):raise ValueError('invalid repeat confirmation')
        time_choices=p.get('time_choices',{})
        if type(time_choices) is not dict or len(time_choices)>2000:raise ValueError('invalid time choices')
        for key,choice in time_choices.items():
            if key not in keys or type(choice) is not dict or set(choice)!={'mode','software_consent'} or choice['mode'] not in ('publish_now','software_schedule') or type(choice['software_consent']) is not bool:raise ValueError('invalid time choice')
        overrides=p.get('item_overrides',{})
        if type(overrides) is not dict or len(overrides)>2000:raise ValueError('invalid overrides')
        for key,patch in overrides.items():
            if key not in keys or type(patch) is not dict or set(patch)-{'title','body','tags'} or any(type(v) is not str or len(v)>10000 for v in patch.values()):raise ValueError('invalid item override')
        if len(assignments)>2000:raise ValueError('too many assignments')
        videos={pid:FinishedVideoService(self.engine).get(pid) for pid in p['video_ids']}
        immediate_pairs={x.account_id+':'+x.video_id for x in assignments if item_rules.get(x.account_id+':'+x.video_id,account_rules.get(x.account_id,common_rule)) is None}
        if immediate_pairs & time_choices.keys():raise ValueError('immediate mode cannot include time choices')
        slots={}
        issues=[]
        for pid in accounts:
            rule=account_rules.get(pid,common_rule)
            count=sum(x.account_id==pid for x in assignments)
            if rule is None:
                if not count:issues.append(dict(account_id=pid,code='account_has_no_videos'))
                slots[pid]=iter(())
                continue
            if rule.mode=='daily':
                capacity=rule.daily_count*rule.days
                if count>capacity:issues.append(dict(account_id=pid,code='schedule_overfilled',expected=capacity,actual=count))
                if capability=='commerce' and count<capacity:issues.append(dict(account_id=pid,code='schedule_underfilled',expected=capacity,actual=count))
                if capability=='ordinary' and not count:issues.append(dict(account_id=pid,code='account_has_no_videos'))
                last=rule.start+timedelta(hours=(rule.daily_count-1)*rule.interval_hours)
                if rule.daily_times is None and rule.days>1 and last>=rule.start+timedelta(days=1):issues.append(dict(account_id=pid,code='daily_groups_overlap'))
            elif not count:issues.append(dict(account_id=pid,code='account_has_no_videos'))
            slots[pid]=iter(validate_platform_window(build_schedule(rule,count=min(count,rule.daily_count*rule.days) if rule.mode=='daily' else count,now=now),now=now,limits=PlatformLimits(2.5,13,reject_short=True)).items)
        location_settings=p.get('locations',{})
        if type(location_settings) is not dict or any(aid not in accounts for aid in location_settings):raise ValueError('invalid location accounts')
        location_items={}
        for aid,settings in location_settings.items():
            if type(settings) is not dict or set(settings)!={'batch','items'} or type(settings['items']) is not dict:raise ValueError('invalid location settings')
            assigned=[x.video_id for x in assignments if x.account_id==aid]
            if any(vid not in assigned for vid in settings['items']):raise ValueError('location outside assignment')
            def resolve(ref):
                if ref is None:return None
                if self.locations is None:raise ValueError('location search unavailable')
                return self.locations.resolve(aid,ref,now=now)
            batch=resolve(settings['batch'])
            for vid in assigned:location_items[aid+':'+vid]=resolve(settings['items'][vid]) if vid in settings['items'] else batch
        policies={}
        declaration_settings=p.get('declarations',{})
        if type(declaration_settings) is not dict or any(aid not in accounts for aid in declaration_settings):raise ValueError('invalid declaration accounts')
        if declaration_settings and self.declarations is None:raise ValueError('declaration policy unavailable')
        for aid,settings in declaration_settings.items():
            if type(settings) is not dict or set(settings)!={'token','batch','items'}:raise ValueError('invalid declaration settings')
            assigned=[x.video_id for x in assignments if x.account_id==aid]
            if settings['batch'] is None and not any(v is not None for v in settings['items'].values()):continue
            if assigned:policies[aid]=self.declarations.resolve(aid,settings['token'],assigned,settings['batch'],settings['items'],now=now)
            elif settings['items']:raise ValueError('declaration items outside assignment')
        items=[]
        for assignment in assignments:
            a=accounts[assignment.account_id];v=videos[assignment.video_id];slot=next(slots[a.id],None);blockers=[]
            if a.login_state!='valid' or not a.session_ref:blockers.append('account_needs_verification')
            # Ordinary access follows the saved login; the real form is checked
            # again by the submission channel. Commerce still needs its probe.
            if capability!='ordinary' and getattr(a.capabilities,capability)!='available':blockers.append(capability+'_capability_unverified')
            if not v.effective_path.is_file():blockers.append('file_missing')

            if v.task_locked and not allow_new_request:blockers.append('video_locked')
            policy=policies.get(a.id)
            pair=a.id+':'+v.id
            if pair in item_rules:
                rule=item_rules[pair]
                slot=None if rule is None else validate_platform_window(build_schedule(rule,count=1,now=now),now=now,limits=PlatformLimits(2.5,13,reject_short=True)).items[0]
            with session_scope(self.engine) as session:
                states={effective_history_state(session,h) for h in session.scalars(select(PublishHistoryRow).where(PublishHistoryRow.video_hash==v.content_hash,PublishHistoryRow.account_id==a.id))}
            repeat_required=not allow_new_request and 'published' in states and not bool(states & _ACTIVE_DUPLICATE_BLOCK_STATES)
            repeat_confirmed=allow_new_request or (repeat_required and pair in repeats)
            try:self.history.assert_can_create(v.content_hash,a.id,allow_repeat_success=repeat_confirmed,allow_new_request=allow_new_request)
            except DuplicatePublishBlocked:blockers.append('duplicate_blocked')
            if slot is None and pair not in immediate_pairs:blockers.append('schedule_capacity_exceeded')
            elif slot is not None and slot.error_code:blockers.append(slot.error_code)
            required_choice=sorted(slot.required_choice) if slot else []
            execution_mode='publish_now' if pair in immediate_pairs else 'platform_schedule'
            software_consent=False
            choice=time_choices.get(pair)
            if choice and slot and slot.required_choice and not slot.error_code:
                if choice['mode']=='software_schedule' and not choice['software_consent']:
                    blockers.append('software_consent_required')
                else:
                    execution_mode=choice['mode']
                    software_consent=execution_mode=='software_schedule' and choice['software_consent']
                    required_choice=[]
            metadata={k:getattr(v,k) for k in ('title','body','tags')}

            metadata.update(overrides.get(a.id+':'+v.id,{}))
            if pair in cover_overrides:metadata['cover']=self.covers.resolve_for_publish(cover_overrides[pair],v.id,engine=self.engine)[0]
            items.append(dict(location=location_items.get(pair),declaration=policy['items'][v.id] if policy is not None else None,video_id=v.id,account_id=a.id,time_overridden=pair in item_rules,**metadata,
                              repeat_confirmation_required=repeat_required,repeat_confirmed=repeat_confirmed,allow_new_request=allow_new_request,
                              execution_mode=execution_mode,software_consent=software_consent,
                              time=slot.local_time.isoformat() if slot else None,required_choice=required_choice,blockers=blockers))
        return dict(items=items,issues=issues,ready_to_submit=False,preview_only=True,
                    locally_valid=not issues and all(not x['blockers'] and not x['required_choice'] for x in items))
