"""Account policy snapshots for actual platform declaration options; no default choice."""
from datetime import datetime,timedelta,timezone
import secrets
import threading
from .commerce_catalog import CommerceCatalog


class CommerceDeclarations:
    def __init__(self,engine,*,fetch=None,capability="commerce",catalog=None):
        if capability not in ("ordinary","commerce"):raise ValueError("invalid declaration capability")
        from .publish_metadata_cache import PublishMetadataCache
        self.cache=PublishMetadataCache(engine)
        self._engine=engine;self._capability=capability
        self._guard=CommerceCatalog(engine)
        self._fetch=fetch
        if catalog is not None and capability!='ordinary':raise ValueError('built-in declarations are ordinary-only')
        self._catalog=catalog
        self._policies={}
        self._lock=threading.RLock()

    def _account(self,pid,*,require_login=True):
        from yijianjifa.features.accounts.service import AccountService
        if type(pid) is not str or not 1<=len(pid)<=64:raise ValueError('invalid account')
        account=AccountService(self._engine).get(pid)
        if account.platform!='douyin' or (require_login and (account.login_state!='valid' or not account.session_ref)) or (self._capability!='ordinary' and getattr(account.capabilities,self._capability)!='available'):raise ValueError('declaration account needs verification')
        return account

    def _identity(self,account):
        if self._capability=='ordinary':return (account.id,account.platform,account.platform_user_id)
        return self._guard._identity(account)

    def query(self,p,*,now=None):
        if type(p) is not dict or not {'account_id'}<=set(p) or set(p)-{'account_id','refresh','cache_only'}:raise ValueError('invalid declaration query')
        builtin=self._catalog is not None
        a=self._account(p['account_id'],require_login=not builtin);identity=self._identity(a)
        refresh=p.get('refresh',False)
        if type(p.get('cache_only',False)) is not bool or type(refresh) is not bool:raise ValueError('invalid refresh')
        supplied_now=now
        now=now or datetime.now(timezone.utc)
        cached=self.cache.read(a,'declarations-'+self._capability,now=now) if not builtin and (self._capability=='ordinary' or 'refresh' in p) and not refresh else None
        if builtin:
            raw={k:self._catalog[k] for k in ('required','items')}
            observed=datetime.fromisoformat(self._catalog['verified_at'])
        elif cached:
            observed=datetime.fromisoformat(cached['observed_at'])
            if self._capability!="ordinary" and now-observed>=timedelta(minutes=5):return dict(available=False,cached=True,token=None,required=None,items=cached['payload']['items'],observed_at=cached['observed_at'],reason='metadata_cache_expired')
            raw=cached['payload']
        elif p.get('cache_only') or self._fetch is None:return dict(available=False,token=None,required=None,items=[],reason='declaration_policy_not_connected')
        else:
            raw=self._fetch(a)
            observed=supplied_now or datetime.now(timezone.utc)
        if type(raw) is not dict or set(raw)!={'required','items'} or type(raw['required']) is not bool or type(raw['items']) is not list or len(raw['items'])>100:raise ValueError('invalid declaration policy')
        rows=[];seen=set()
        for r in raw['items']:
            if type(r) is not dict or set(r)!={'id','label'} or type(r['id']) is not str or not 1<=len(r['id'])<=128 or type(r['label']) is not str or not 1<=len(r['label'])<=500 or r['id'] in seen:raise ValueError('invalid declaration option')
            seen.add(r['id']);rows.append(dict(r))
        if self._identity(self._account(a.id,require_login=not builtin))!=identity:raise ValueError('declaration account changed')
        if not builtin and not cached:self.cache.write(a,'declarations-'+self._capability,'',raw,now=observed)
        expires=observed+timedelta(minutes=5);token=secrets.token_hex(24)
        with self._lock:
            self._policies={k:v for k,v in self._policies.items() if (self._capability=='ordinary' or v['expires']>now) and v['identity'][0]!=a.id}
            while len(self._policies)>=(200 if self._capability=="ordinary" else 8):self._policies.pop(next(iter(self._policies)))
            self._policies[token]=dict(identity=identity,expires=expires,required=raw['required'],items=rows)
        result=dict(available=True,token=token,required=raw['required'],items=[dict(r) for r in rows],expires_at=None if builtin else expires.isoformat(),cached=bool(cached),observed_at=observed.isoformat())
        if builtin:result.update(source='builtin',catalog_version=self._catalog['version'])
        return result

    def resolve(self,account_id,token,video_ids,batch,overrides,*,now=None):
        if type(token) is not str or len(token)!=48:raise ValueError('declaration policy needs verification')
        if type(video_ids) is not list or not 1<=len(video_ids)<=200 or any(type(v) is not str for v in video_ids) or len(set(video_ids))!=len(video_ids):raise ValueError('invalid declaration video selection')
        if type(overrides) is not dict or any(k not in video_ids for k in overrides):raise ValueError('invalid declaration overrides')
        now=now or datetime.now(timezone.utc)
        with self._lock:policy=self._policies.get(token)
        if policy is None or (self._capability!='ordinary' and policy['expires']<=now):raise ValueError('declaration policy expired; refresh')
        if policy['identity'][0]!=account_id:raise ValueError('declaration policy belongs to another account')
        if self._identity(self._account(account_id,require_login=self._catalog is None))!=policy['identity']:raise ValueError('declaration account changed')
        options={r['id']:r for r in policy['items']}
        for value in [batch,*overrides.values()]:
            if value is not None and (type(value) is not str or value not in options):raise ValueError('declaration must be an actual platform option')
        selected={v:overrides.get(v,batch) for v in video_ids}
        return dict(required=policy['required'],items={v:dict(options[d]) if d is not None else None for v,d in selected.items()},blocking_video_ids=[v for v,d in selected.items() if policy['required'] and d is None])

    def close(self):
        with self._lock:self._policies.clear()
