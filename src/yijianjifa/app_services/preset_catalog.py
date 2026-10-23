"""Local-only preset catalog. Never authenticates or starts publishing."""
import math
from yijianjifa.features.presets.models import MixPreset, NormalPublishPreset, CommercePreset, MixThenPublishPreset
from yijianjifa.features.presets.service import PresetService
from yijianjifa.features.accounts.service import AccountService
from .mix_presets import read as read_mix

MODELS={'mix':MixPreset,'normal_publish':NormalPublishPreset,'commerce':CommercePreset,'mix_then_publish':MixThenPublishPreset}


def validate(payload):
    if type(payload) is not dict or payload.get('kind') not in MODELS:raise ValueError('invalid preset')
    if type(payload.get('name')) is not str or not 1<=len(payload['name'].strip())<=255:raise ValueError('invalid name')
    def finite(value):
        if isinstance(value,dict):
            for v in value.values():finite(v)
        elif isinstance(value,(list,tuple)):
            for v in value:finite(v)
        elif isinstance(value,float) and not math.isfinite(value):raise ValueError('not finite')
    finite(payload)
    model=MODELS[payload['kind']].model_validate(payload)
    if isinstance(model,NormalPublishPreset):
        if model.account_mode=='specific' and len(model.account_ids)!=1:raise ValueError('one account required')
        if model.account_mode=='multiple' and not model.account_ids:raise ValueError('accounts required')
        if model.account_mode=='platform_default' and model.account_ids:raise ValueError('default account rule')
    return model.model_copy(update={'name':model.name.strip()})


def read(engine,pid):
    stored=PresetService(engine).get(pid);m=stored.preset
    result={'id':pid,'name':m.name,'kind':m.kind,'payload':m.model_dump(mode='json'),'state':'ready','issues':[]}
    if isinstance(m,MixPreset):
        mix=read_mix(engine,pid);result['settings']=mix['settings'];result['state']=mix['state']
        if mix['state']!='ready':result['issues'].append('请检查失效的分类、音乐及制作设置。')
    else:
        accounts={a.id for a in AccountService(engine).list('douyin')}
        ids=list(m.account_ids) if isinstance(m,NormalPublishPreset) else [m.account_id] if m.account_id else []
        if any(i not in accounts for i in ids):result['issues'].append('方案引用的账号已不存在，使用前需重新选择。')
        if isinstance(m,MixThenPublishPreset):
            # Do not catch a nested session rollback inside an outer receipt transaction.
            mix_ids={p.id for p in PresetService(engine).list(kind='mix')}
            if m.mix_preset_id not in mix_ids:result['issues'].append('引用的制作方案已不存在。')
            elif read_mix(engine,m.mix_preset_id)['state']!='ready':result['issues'].append('引用的制作方案需要修复。')
        if result['issues']:result['state']='needs_repair'
    return result
