"""Optional ordinary-publish locations from account-scoped search snapshots."""
from datetime import datetime,timedelta,timezone
import secrets
import threading
from yijianjifa.features.accounts.service import AccountService


class PublishLocations:
    def __init__(self,engine,*,fetch=None,scoped_fetch=False):
        from .publish_metadata_cache import PublishMetadataCache
        self.cache=PublishMetadataCache(engine);self.scoped_fetch=scoped_fetch
        self.engine=engine;self._fetch=fetch;self._queries={};self._lock=threading.RLock()

    def _account(self,pid):
        if type(pid) is not str or not 1<=len(pid)<=64:raise ValueError('invalid account')
        account=AccountService(self.engine).get(pid)
        if account.platform!='douyin' or account.login_state!='valid' or not account.session_ref:raise ValueError('ordinary account needs verification')
        return account

    @staticmethod
    def _identity(account):
        return (account.id,account.platform,account.platform_user_id)

    def search(self,p,*,now=None):
        if type(p) is not dict or not {'account_id','query'}<=set(p) or set(p)-{'account_id','query','scope','refresh','cache_only'} or type(p['query']) is not str or not 1<=len(p['query'].strip())<=200:raise ValueError('invalid location query')
        account=self._account(p['account_id']);identity=self._identity(account)
        scope=p.get('scope','local');refresh=p.get('refresh',False)
        if scope not in ('local','national') or type(refresh) is not bool:raise ValueError('invalid location scope')
        if type(p.get('cache_only',False)) is not bool:raise ValueError('invalid cache_only')
        supplied_now=now
        now=now or datetime.now(timezone.utc)
        cache_key=scope+'|'+p['query'].strip()
        cached=self.cache.read(account,'locations',cache_key,now=now) if not refresh else None
        if cached:
            observed=datetime.fromisoformat(cached['observed_at'])
            raw=cached['payload']
        elif p.get('cache_only') or self._fetch is None:return dict(available=False,token=None,items=[],reason='location_search_not_connected')
        else:
            raw=self._fetch(account,p['query'].strip(),scope) if self.scoped_fetch else self._fetch(account,p['query'].strip())
            observed=supplied_now or datetime.now(timezone.utc)
        if type(raw) not in (list,tuple) or len(raw)>1000:raise ValueError('invalid location results')
        rows={}
        for row in raw:
            if type(row) is not dict or set(row)!={'id','name','address'}:raise ValueError('invalid location fields')
            for key,limit in (('id',128),('name',500),('address',1000)):
                if type(row[key]) is not str or not 1<=len(row[key])<=limit:raise ValueError('invalid location metadata')
            if row['id'] in rows:raise ValueError('duplicate location')
            rows[row['id']]=dict(row)
        if self._identity(self._account(account.id))!=identity:raise ValueError('location account changed')
        if not cached:self.cache.write(account,'locations',cache_key,list(rows.values()),now=observed)
        expires=observed+timedelta(minutes=5);token=secrets.token_hex(24)
        with self._lock:
            while len(self._queries)>=200:self._queries.pop(next(iter(self._queries)))
            self._queries[token]=dict(identity=identity,expires=expires,rows=rows,query=p['query'].strip(),scope=scope)
        return dict(available=True,token=token,items=[dict(row) for row in rows.values()],expires_at=expires.isoformat(),cached=bool(cached),needs_verification=expires<=now,observed_at=observed.isoformat())

    def recent(self,p):
        if type(p) is not dict or set(p)!={'account_id'}:raise ValueError('invalid recent query')
        account=self._account(p['account_id'])
        items=[]
        for r in self.cache.recent(account,'locations'):
            scope,query=r['query'].split('|',1)
            snapshot=self.search(dict(account_id=account.id,query=query,scope=scope,cache_only=True))
            items.append(dict(scope=scope,query=query,observed_at=r['observed_at'],snapshot=snapshot))
        return dict(items=items)

    def resolve(self,account_id,ref,*,now=None):
        if ref is None:return None
        if type(ref) is not dict or set(ref)!={'token','id'} or type(ref['token']) is not str or len(ref['token'])!=48 or type(ref['id']) is not str or not 1<=len(ref['id'])<=128:raise ValueError('invalid location reference')
        now=now or datetime.now(timezone.utc)
        with self._lock:query=self._queries.get(ref['token'])
        if query is None:raise ValueError('location query expired; search again')
        if query['identity'][0]!=account_id:raise ValueError('location belongs to another account')
        if self._identity(self._account(account_id))!=query['identity']:raise ValueError('location account changed')
        if ref['id'] not in query['rows']:raise ValueError('location must be an actual account search result')
        # Final publish form performs exact name/address selection and readback.
        return dict(query['rows'][ref['id']])

    def durable_choice(self,account_id,ref):
        """Save only a choice resolved from this account's trusted query."""
        if ref is None:return None
        selected=self.resolve(account_id,ref)
        with self._lock:
            query=self._queries.get(ref['token'])
            if query is None:raise ValueError('location query unavailable')
            return {**selected,'query':query['query'],'scope':query['scope']}

    def close(self):
        with self._lock:self._queries.clear()
