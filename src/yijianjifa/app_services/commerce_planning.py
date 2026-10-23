"""Single-account commerce preparation using existing allocation and schedule rules.

No store catalog is fabricated: live account-scoped catalog access is not wired yet.
"""
from .account_access import public_account
from .publish_planning import PublishPlanning
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.commerce.service import CommerceService
from yijianjifa.features.commerce.distribution import allocate_stores,validate_store_assignments
from yijianjifa.features.publish.platforms.douyin.commerce import _allowed_scopes


class CommercePlanning:
    def __init__(self,engine,*,catalog=None,music=None,declarations=None,covers=None):self.engine=engine;self.catalog=catalog;self.music=music;self.declarations=declarations;self.covers=covers

    def context(self):
        accounts=AccountService(self.engine)
        default=CommerceService(self.engine).initial_account_id()
        rows=[]
        for a in accounts.list():
            if a.platform!='douyin':continue
            if a.id==default and not a.session_ref:default=None
            rows.append({**public_account(a),'store_scopes':sorted(_allowed_scopes(a.capabilities)) if a.capabilities.commerce=='available' else []})
        return dict(accounts=rows,default_account_id=default,stores_available=False,store_status='account_catalog_not_connected')

    def preview(self,p,*,now=None):
        if type(p) is not dict or not {'account_id','plan','store_pool','manual_store_overrides'}<=set(p) or set(p)-{'account_id','plan','store_pool','manual_store_overrides','store_query_token','music_choice','music_overrides','declarations','cover_overrides'}:raise ValueError('invalid commerce fields')
        if type(p['account_id']) is not str or not 1<=len(p['account_id'])<=64:raise ValueError('commerce requires one account')
        plan=p['plan'];pool=p['store_pool'];manual=p['manual_store_overrides']
        if type(plan) is not dict or 'account_ids' in plan or 'distribution' in plan or 'locations' in plan or 'cover_overrides' in plan:raise ValueError('invalid commerce plan')
        if type(pool) is not list or len(pool)>200 or any(type(x) is not str or not 1<=len(x)<=128 for x in pool) or len(set(pool))!=len(pool):raise ValueError('invalid store pool')
        if type(manual) is not dict or len(manual)>200 or any(k not in plan.get('video_ids',[]) or type(v) is not str or not 1<=len(v)<=128 for k,v in manual.items()):raise ValueError('invalid manual store')
        music_choice=dict(mode='none',candidates=[])
        if 'music_choice' in p:
            from .commerce_music import CommerceMusic
            music_choice=(self.music or CommerceMusic(self.engine)).resolve(p['account_id'],p['music_choice'],now=now)
        music_overrides=p.get('music_overrides',{})
        if type(music_overrides) is not dict or len(music_overrides)>200:raise ValueError('invalid item music')
        resolved_music={}
        for video_id,choice in music_overrides.items():
            if video_id not in plan.get('video_ids',[]) or type(choice) is not dict or choice.get('mode') not in ('none','fixed'):raise ValueError('invalid item music')
            from .commerce_music import CommerceMusic
            resolved_music[video_id]=(self.music or CommerceMusic(self.engine)).resolve(p['account_id'],choice,now=now)
        declaration_policy=None
        if 'declarations' in p:
            d=p['declarations']
            if type(d) is not dict or set(d)!={'token','batch','items'}:raise ValueError('invalid declaration settings')
            if self.declarations is None:raise ValueError('declaration policy unavailable')
            declaration_policy=self.declarations.resolve(p['account_id'],d['token'],plan.get('video_ids'),d['batch'],d['items'],now=now)
        resolved=None
        if p.get('store_query_token') is not None:
            if self.catalog is None:raise ValueError('store catalog unavailable')
            resolved=self.catalog.resolve_pool(p['account_id'],p['store_query_token'],pool,now=now)
        result=PublishPlanning(self.engine,covers=self.covers).preview({**plan,'cover_overrides':p.get('cover_overrides',{}),'account_ids':[p['account_id']],'distribution':'split_across_accounts'},now=now,capability='commerce')
        assignments=allocate_stores(plan['video_ids'],pool,manual)
        invalid=validate_store_assignments(assignments,active_pool=set(pool),manual_overrides=manual).blocking_video_ids
        for item in result['items']:
            store=assignments[item['video_id']];item['store_id']=store
            if item['video_id'] in resolved_music:item['music_override']=resolved_music[item['video_id']]
            item['declaration']=declaration_policy['items'][item['video_id']] if declaration_policy is not None else None
            if declaration_policy is None:item['blockers'].append('declaration_policy_unverified')
            elif item['video_id'] in declaration_policy['blocking_video_ids']:item['blockers'].append('declaration_required')
            if store is None:item['blockers'].append('store_required')
            elif item['video_id'] in invalid:item['blockers'].append('store_outside_pool')
            # The name and commission state come only from the Python catalog.
            item['store']=resolved.get(store) if resolved is not None else None
            if item['store'] is None:item['blockers'].append('store_availability_unverified')
        result.update(music_choice=music_choice,kind='commerce',account_id=p['account_id'],locally_valid=not result['issues'] and all(not x['blockers'] and not x['required_choice'] for x in result['items']),stores_available=resolved is not None)
        return result
