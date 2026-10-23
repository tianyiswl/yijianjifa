"""Finite account favorite-music references; never reads or modifies media audio."""
from datetime import datetime, timedelta, timezone
import secrets
import threading
from .commerce_catalog import CommerceCatalog
from yijianjifa.features.commerce.models import FavoriteMusicChoice


class CommerceMusic:
    def __init__(self,engine,*,fetch=None):
        self._guard=CommerceCatalog(engine)
        self._fetch=fetch
        self._queries={}
        self._lock=threading.RLock()

    def query(self,p,*,now=None):
        if type(p) is not dict or set(p)!={'account_id'}:raise ValueError('invalid music query')
        account=self._guard._account(p['account_id'])
        identity=self._guard._identity(account)
        # A failed refresh must not leave earlier favorites usable.
        with self._lock:self._queries={k:v for k,v in self._queries.items() if v['identity'][0]!=account.id}
        if self._fetch is None:return dict(available=False,token=None,items=[],reason='favorite_music_not_connected')
        raw=self._fetch(account)
        if type(raw) not in (list,tuple) or len(raw)>1000:raise ValueError('invalid favorite music result')
        rows=[];seen=set()
        for row in raw:
            if type(row) is not dict or set(row)!={'id','title'}:raise ValueError('invalid favorite music metadata')
            if type(row['id']) is not str or not 1<=len(row['id'])<=128 or type(row['title']) is not str or not 1<=len(row['title'])<=500 or row['id'] in seen:raise ValueError('invalid favorite music identity')
            seen.add(row['id']);rows.append(dict(row))
        if self._guard._identity(self._guard._account(account.id))!=identity:raise ValueError('music account changed')
        now=now or datetime.now(timezone.utc);token=secrets.token_hex(24);expires=now+timedelta(minutes=5)
        with self._lock:
            self._queries={k:v for k,v in self._queries.items() if v['expires']>now and v['identity'][0]!=account.id}
            while len(self._queries)>=8:self._queries.pop(next(iter(self._queries)))
            self._queries[token]=dict(identity=identity,expires=expires,rows=rows)
        return dict(available=True,token=token,items=[dict(r) for r in rows],expires_at=expires.isoformat())

    def resolve(self,account_id,p,*,now=None):
        if type(p) is not dict or set(p)!={'mode','ids','token'}:raise ValueError('invalid music choice')
        ids=p['ids']
        if type(ids) is not list or len(ids)>200 or any(type(mid) is not str or not 1<=len(mid)<=128 for mid in ids) or len(set(ids))!=len(ids):raise ValueError('invalid favorite music ids')
        choice=FavoriteMusicChoice(mode=p['mode'],ids=tuple(ids))
        if choice.mode=='none':
            if p['token'] is not None:raise ValueError('unused music token')
            return dict(mode='none',candidates=[])
        token=p['token']
        if type(token) is not str or len(token)!=48:raise ValueError('invalid music query token')
        now=now or datetime.now(timezone.utc)
        with self._lock:query=self._queries.get(token)
        if query is None or query['expires']<=now:raise ValueError('favorite music query expired; refresh')
        if query['identity'][0]!=account_id:raise ValueError('favorite music belongs to another account')
        if self._guard._identity(self._guard._account(account_id))!=query['identity']:raise ValueError('favorite music account changed')
        available={r['id']:r for r in query['rows']}
        if any(mid not in available for mid in ids):raise ValueError('music must belong to current account favorites')
        # Retain the exact user-selected random pool. Pick and freeze at confirmation.
        return dict(mode=choice.mode,candidates=[dict(available[mid]) for mid in ids])

    def close(self):
        with self._lock:self._queries.clear()
