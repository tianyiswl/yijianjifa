"""Reusable V2 intentions; schedule.interval retains V1's hours unit.

Never stores video/session/query grants or converts remembered hours to minutes.
"""
from datetime import datetime, timedelta, timezone
import copy
import re
from decimal import Decimal
import json
import uuid
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from .multi_publish import fields, identifier

KEY='multi_publish.settings.v2'
PREFIX='multi_publish.preset.v2:'
SCHEDULE_KEYS={'mode','interval','per_day','days','clock_mode','times'}


def defaults():
    return {'schema_version':2,'targets':[],'distribution_by_platform':{},'start_policy':{'kind':'relative_tomorrow_noon'},
        'schedule':{'mode':'daily','interval':'3','per_day':'3','days':'5','clock_mode':'interval','times':['12:00']},
        'schedule_by_platform':{},'common_fields':{},'fields_by_platform':{}}


def reusable_fields(value):
    result=fields(value)
    if any(k in result for k in ('cover','location','declaration')):raise ValueError('temporary selection is not reusable')
    return result


def validate(value,*,preset=False):
    if type(value) is not dict or set(value)!=set(defaults()) or type(value['schema_version']) is not int or value['schema_version']!=2:raise ValueError('invalid settings version')
    value=copy.deepcopy(value)
    targets=value['targets'];seen=set();platforms=set()
    if type(targets) is not list or len(targets)>200:raise ValueError('invalid targets')
    for target in targets:
        if type(target) is not dict or set(target)!={'account_id','platform'}:raise ValueError('invalid saved target')
        identifier(target['account_id'],'account');PlatformCatalog().get(target['platform'])
        if target['account_id'] in seen:raise ValueError('duplicate target')
        seen.add(target['account_id']);platforms.add(target['platform'])
    distribution=value['distribution_by_platform']
    if type(distribution) is not dict or set(distribution)-platforms or any(v not in ('split','whole_batch') for v in distribution.values()):raise ValueError('invalid distribution')
    def policy(p):
        if type(p) is not dict:raise ValueError('invalid date policy')
        if p=={'kind':'relative_tomorrow_noon'}:return
        if set(p)!={'kind','value'} or p['kind']!='fixed' or type(p['value']) is not str or len(p['value'])>40:raise ValueError('invalid fixed date')
        try:datetime.fromisoformat(p['value'])
        except ValueError:raise ValueError('invalid fixed date') from None
        if preset and date_expired(p):raise ValueError('expired fixed date is not reusable')
    def schedule(v):
        if type(v) is not dict or set(v)!=SCHEDULE_KEYS:raise ValueError('invalid schedule')
        if v['mode'] not in ('daily','interval','exact','immediate') or v['clock_mode'] not in ('interval','custom'):raise ValueError('invalid schedule mode')
        interval=v['interval']
        if type(interval) is not str or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',interval) or not Decimal('0.5')<=Decimal(interval)<=336 or Decimal(interval)*2%1:raise ValueError('invalid interval hours')
        for key in ('per_day','days'):
            if type(v[key]) is not str or not v[key].isdigit() or not 1<=int(v[key])<=10000:raise ValueError('invalid schedule count')
        if type(v['times']) is not list or len(v['times'])>100:raise ValueError('invalid times')
        for clock in v['times']:
            if type(clock) is not str or len(clock)!=5 or clock[2]!=':':raise ValueError('invalid clock')
            try:hour,minute=map(int,clock.split(':'))
            except ValueError:raise ValueError('invalid clock') from None
            if not 0<=hour<=23 or not 0<=minute<=59:raise ValueError('invalid clock')
    policy(value['start_policy']);schedule(value['schedule'])
    differences=value['schedule_by_platform']
    if type(differences) is not dict or set(differences)-platforms:raise ValueError('invalid platform schedule')
    for p in differences.values():
        if type(p) is not dict or set(p)!={'start_policy','schedule'}:raise ValueError('invalid platform time policy')
        policy(p['start_policy']);schedule(p['schedule'])
    value['common_fields']=reusable_fields(value['common_fields'])
    if any(k in value['common_fields'] for k in ('content_declaration_choice','channels_original')):
        raise ValueError('platform declarations require platform settings')
    overrides=value['fields_by_platform']
    if type(overrides) is not dict:raise ValueError('invalid platform fields')
    for platform,v in overrides.items():
        # Reusable platform defaults do not select a platform for publishing.
        PlatformCatalog().get(platform)
        overrides[platform]=reusable_fields(v)
        if 'channels_original' in v and platform!='channels':raise ValueError('original declaration requires channels')
        if 'content_declaration_choice' in v:
            if platform=='kuaishou':
                from .kuaishou_declaration_catalog import valid_choice
            elif platform=='channels':
                from .channels_declaration_catalog import valid_choice
            else:raise ValueError('content declaration requires platform settings')
            if not valid_choice(v['content_declaration_choice']):raise ValueError('unsupported platform declaration')
    return value


def date_expired(policy):
    if policy['kind']!='fixed':return False
    t=datetime.fromisoformat(policy['value'])
    if t.tzinfo is None:t=t.replace(tzinfo=timezone(timedelta(hours=8)))
    return t<=datetime.now(timezone.utc)


def read(engine):
    with session_scope(engine) as s:
        row=s.get(Setting,KEY)
        has_saved_settings=row is not None
        value=json.loads(row.value_json) if row else defaults()
    date_policies=[value['start_policy'],*[p['start_policy'] for p in value['schedule_by_platform'].values()]]
    issues=['fixed_date_expired'] if any(date_expired(p) for p in date_policies) else []
    from yijianjifa.features.accounts.service import AccountService
    for target in value['targets']:
        try:a=AccountService(engine).get(target['account_id'])
        except KeyError:issues.append('saved_account_unavailable');continue
        if a.platform!=target['platform']:issues.append('saved_account_unavailable')
    return {'settings':value,'issues':sorted(set(issues)),'migrated_v1':False,
        'has_saved_settings':has_saved_settings}


def write(engine,value):
    value=validate(value)
    with session_scope(engine) as s:
        row=s.get(Setting,KEY)
        if row:row.value_json=json.dumps(value,ensure_ascii=False)
        else:s.add(Setting(key=KEY,value_json=json.dumps(value,ensure_ascii=False)))
    return {'settings':value,'saved':True}


def presets(engine):
    with session_scope(engine) as s:return [json.loads(r.value_json) for r in s.scalars(select(Setting).where(Setting.key.startswith(PREFIX)).order_by(Setting.key))]


def save_preset(engine,p):
    if type(p['name']) is not str or not 1<=len(p['name'].strip())<=255:raise ValueError('invalid preset name')
    pid=p['id'] or str(uuid.uuid4());identifier(pid,'preset')
    record={'id':pid,'name':p['name'].strip(),'schema_version':2,'settings':validate(p['settings'],preset=True)}
    with session_scope(engine) as s:
        row=s.get(Setting,PREFIX+pid)
        if p['id'] and row is None:raise KeyError(pid)
        if row:row.value_json=json.dumps(record,ensure_ascii=False)
        else:s.add(Setting(key=PREFIX+pid,value_json=json.dumps(record,ensure_ascii=False)))
    return record
