"""Platform-shared durable choices; cached data is not submission evidence."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from .commerce_catalog import CommerceCatalog


class PublishMetadataCache:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def identity(account):
        return hashlib.sha256(repr(CommerceCatalog._identity(account)).encode()).hexdigest()

    @staticmethod
    def prefix(account, kind):
        if kind in ('locations','declarations-ordinary'):return f'publish-platform-metadata:{account.platform}:{kind}:'
        return f'publish-metadata:{account.id}:{kind}:'

    def key(self, account, kind, query):
        return self.prefix(account, kind) + hashlib.sha256(query.encode()).hexdigest()

    def read(self, account, kind, query='', *, now=None):
        now = now or datetime.now(timezone.utc)
        with session_scope(self.engine) as session:
            row = session.get(Setting, self.key(account, kind, query))
            if row is None and kind in ('locations','declarations-ordinary'):
                from yijianjifa.features.accounts.service import AccountRow
                owners={a.id:a for a in session.scalars(select(AccountRow).where(AccountRow.platform==account.platform))}
                candidates=[]
                for legacy in session.scalars(select(Setting).where(Setting.key.startswith('publish-metadata:'))):
                    if not any(legacy.key.startswith(f'publish-metadata:{aid}:{kind}:') for aid in owners):continue
                    data=self._decoded(legacy)
                    if data.get('query')!=query:continue
                    owner_id=legacy.key.split(':')[1];a=owners[owner_id]
                    if data.get('owner',[a.id,a.platform,a.platform_user_id])!=[a.id,a.platform,a.platform_user_id]:continue
                    candidates.append(data)
                if candidates:
                    data=max(candidates,key=lambda d:str(d.get('observed_at','')))
                    data={**data,'platform':account.platform}
                    row=Setting(key=self.key(account,kind,query),value_json=json.dumps(data,ensure_ascii=False));session.add(row)
            if row is None:return None
            try:
                data = json.loads(row.value_json)
                observed = datetime.fromisoformat(data['observed_at'])
                durable=kind in ('locations','declarations-ordinary')
                if observed > now:return None
                if durable:
                    if data.get('platform')!=account.platform:return None
                elif data['identity'] != self.identity(account) or now-observed > timedelta(days=7):return None
                return data
            except (ValueError, TypeError, KeyError):
                return None

    @staticmethod
    def _decoded(row):
        try:
            value = json.loads(row.value_json)
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    def write(self, account, kind, query, payload, *, now=None):
        now = now or datetime.now(timezone.utc)
        value = dict(platform=account.platform,owner=[account.id,account.platform,account.platform_user_id],identity=self.identity(account), query=query, observed_at=now.isoformat(), payload=payload)
        with session_scope(self.engine) as session:
            key = self.key(account, kind, query)
            row = session.get(Setting, key)
            if row is None:
                row = Setting(key=key, value_json='{}'); session.add(row)
            row.value_json = json.dumps(value, ensure_ascii=False)
            # Bound local search history per account and type.
            rows = list(session.scalars(select(Setting).where(Setting.key.startswith(self.prefix(account, kind)))))
            for old in sorted(rows, key=lambda r:str(self._decoded(r).get('observed_at','')), reverse=True)[100:]:
                session.delete(old)
        return value

    def recent(self, account, kind, *, now=None):
        with session_scope(self.engine) as session:
            rows = list(session.scalars(select(Setting).where(Setting.key.startswith(self.prefix(account, kind)))))
            if kind in ('locations','declarations-ordinary'):
                from yijianjifa.features.accounts.service import AccountRow
                ids=[a.id for a in session.scalars(select(AccountRow).where(AccountRow.platform==account.platform))]
                rows+=list(session.scalars(select(Setting).where(Setting.key.startswith('publish-metadata:'))))
                rows=[r for r in rows if r.key.startswith(self.prefix(account,kind)) or any(r.key.startswith(f'publish-metadata:{aid}:{kind}:') for aid in ids)]
            queries = list(dict.fromkeys(self._decoded(r).get('query','') for r in rows))
            queries = [q for q in queries if isinstance(q, str)]
        values = [v for q in queries if (v := self.read(account, kind, q, now=now))]
        return sorted(values, key=lambda v:v['observed_at'], reverse=True)[:10]
