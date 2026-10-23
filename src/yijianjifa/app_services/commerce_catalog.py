"""Account-scoped, expiring store query snapshots. No live provider by default."""
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
import hashlib
import secrets
import threading
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.commerce.stores import StoreCandidate,StoreFilter,filter_stores
from yijianjifa.features.publish.platforms.douyin.commerce import _allowed_scopes


class CommerceCatalog:
    def __init__(self,engine,*,fetch=None):
        self.engine=engine;self._fetch=fetch;self._queries={};self._lock=threading.RLock()

    def _account(self,pid):
        if type(pid) is not str or not 1<=len(pid)<=64:raise ValueError('invalid account')
        a=AccountService(self.engine).get(pid)
        if a.platform!='douyin' or a.login_state!='valid' or not a.session_ref or a.capabilities.commerce!='available':raise ValueError('commerce account needs verification')
        return a

    @staticmethod
    def _identity(a):
        return (a.id,a.platform_user_id,hashlib.sha256(a.session_ref.encode()).hexdigest(),a.capabilities)

    def search(self,p,*,now=None):
        if type(p) is not dict or set(p)!={'account_id','scope','query','region','commission','favorite_only'}:raise ValueError('invalid store query')
        if p['scope'] not in ('local','national') or p['commission'] not in ('all','confirmed_only') or type(p['favorite_only']) is not bool:raise ValueError('invalid store filter')
        if any(type(p[k]) is not str or len(p[k])>200 for k in ('query','region')):raise ValueError('invalid store search text')
        a=self._account(p['account_id']);identity=self._identity(a)
        if p['scope'] not in _allowed_scopes(a.capabilities):raise ValueError('store scope unavailable')
        if self._fetch is None:return dict(available=False,token=None,total=0,reason='account_catalog_not_connected')
        rows=self._fetch(a,p['scope'],p['query'],p['region'])
        if type(rows) not in (list,tuple) or len(rows)>10000 or any(not isinstance(r,StoreCandidate) for r in rows):raise ValueError('invalid store provider result')
        if len({r.id for r in rows})!=len(rows):raise ValueError('duplicate store identity')
        if self._identity(self._account(a.id))!=identity:raise ValueError('account changed during store query')
        filtered=filter_stores(rows,StoreFilter(query=p['query'],region=p['region'] or None,scope=p['scope'],commission=p['commission'],favorite_only=p['favorite_only']))
        now=now or datetime.now(timezone.utc);expires=now+timedelta(minutes=5);token=secrets.token_hex(24)
        with self._lock:
            self._queries={k:v for k,v in self._queries.items() if v['expires']>now}
            while len(self._queries)>=8:self._queries.pop(next(iter(self._queries)))
            self._queries[token]=dict(identity=identity,scope=p['scope'],expires=expires,rows=tuple(filtered))
        return dict(available=True,token=token,total=len(filtered),expires_at=expires.isoformat())

    def page(self,p,*,now=None):
        if type(p) is not dict or set(p)!={'account_id','token','offset','limit'}:raise ValueError('invalid catalog page')
        if type(p['token']) is not str or len(p['token'])!=48 or type(p['offset']) is not int or p['offset']<0 or type(p['limit']) is not int or not 1<=p['limit']<=100:raise ValueError('invalid catalog pagination')
        query=self._resolve(p['account_id'],p['token'],now=now)
        return dict(items=[asdict(r) for r in query['rows'][p['offset']:p['offset']+p['limit']]],total=len(query['rows']))

    def _resolve(self,account_id,token,*,now=None):
        if type(token) is not str or len(token)!=48:raise ValueError('invalid store query token')
        now=now or datetime.now(timezone.utc)
        with self._lock:query=self._queries.get(token)
        if query is None or query['expires']<=now:raise ValueError('store query expired; search again')
        if account_id!=query['identity'][0]:raise ValueError('store query belongs to another account')
        a=self._account(account_id)
        if self._identity(a)!=query['identity'] or query['scope'] not in _allowed_scopes(a.capabilities):raise ValueError('store query belongs to a different account state')
        return query

    def resolve_pool(self,account_id,token,pool,*,now=None):
        query=self._resolve(account_id,token,now=now)
        rows={r.id:r for r in query['rows']}
        if any(store_id not in rows for store_id in pool):raise ValueError('selected store is not in current query')
        return {store_id:asdict(rows[store_id]) for store_id in pool}

    def close(self):
        with self._lock:self._queries.clear()
