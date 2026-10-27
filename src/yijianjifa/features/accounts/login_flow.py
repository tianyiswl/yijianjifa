"""Native-provider login lifecycle. Never expose complete() to a Web UI bridge.

A future isolated provider must verify platform identity and obtain session bytes
before calling complete; closing its window is cancellation, never success.
No browser or network is opened by this module.
"""
import json
import re
import secrets
import threading
import uuid
from sqlalchemy import select, text
from yijianjifa.storage.db import session_scope
from .service import AccountRow, AccountService, _utcnow


class LoginFlow:
    def __init__(self, engine, store):
        self.engine=engine;self.store=store
        self._lock=threading.RLock();self._jobs={};self._closed=False

    def begin(self, account_id=None, *, platform=None):
        with self._lock:
            if self._closed:raise ValueError('login service closed')
            if any(j['state']=='waiting' for j in self._jobs.values()):
                raise ValueError('login already in progress')
            baseline=None
            if account_id is not None:
                account=AccountService(self.engine).get(account_id)
                if platform is not None and platform!=account.platform:raise ValueError('account platform mismatch')
                platform=account.platform
                baseline=(account.platform_user_id,account.session_ref)
            elif platform is None:platform='douyin'
            from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
            PlatformCatalog().get(platform)
            while len(self._jobs)>=16:self._jobs.pop(next(iter(self._jobs)))
            jid=secrets.token_hex(24)
            self._jobs[jid]={'id':jid,'state':'waiting','account_id':account_id,'platform':platform,'baseline':baseline}
            return self.status(jid)

    def status(self, jid):
        with self._lock:
            if jid not in self._jobs:raise ValueError('login attempt expired')
            return {k:v for k,v in self._jobs[jid].items() if k!='baseline'}

    def window_closed(self, jid):
        with self._lock:
            job=self._jobs[jid]
            if job['state']=='waiting':job.update(state='cancelled')
            return self.status(jid)

    def complete(self, jid, *, platform_user_id, nickname, session_blob, avatar_data="", ordinary_available=False, preserve_capabilities=False):
        with self._lock:
            job=self._jobs[jid]
            if job['state']!='waiting':return self.status(jid)
            if (type(platform_user_id) is not str or not 1<=len(platform_user_id.strip())<=128
                or type(nickname) is not str or not 1<=len(nickname.strip())<=255
                or type(session_blob) is not bytes or not 1<=len(session_blob)<=10*1024*1024):
                job.update(state='failed',code='invalid_observation');return self.status(jid)
            if type(preserve_capabilities) is not bool or type(ordinary_available) is not bool or type(avatar_data) is not str or len(avatar_data)>710000 or (avatar_data and not re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+',avatar_data)):
                job.update(state='failed',code='invalid_observation');return self.status(jid)
            identity=platform_user_id.strip()
            platform=job['platform']
            # Login alone cannot enable a new platform's publishing channel.
            ordinary_available=ordinary_available and platform=='douyin'
            baseline=job['baseline']
            if baseline and identity!=baseline[0]:
                job.update(state='failed',code='identity_mismatch');return self.status(jid)
            try:
                with session_scope(self.engine) as session:
                    # Serialize identity/reference validation with the row update.
                    session.execute(text('BEGIN IMMEDIATE'))
                    if job['account_id']:
                        row=session.get(AccountRow,job['account_id'])
                        if row is None or row.login_state=='removed' or row.platform!=platform or (row.platform_user_id,row.session_ref)!=baseline:
                            job.update(state='failed',code='account_changed');return self.status(jid)
                    else:
                        row=session.scalar(select(AccountRow).where(AccountRow.platform==platform,AccountRow.platform_user_id==identity))
                        if row is not None and row.login_state!='removed':
                            job.update(state='failed',code='account_exists');return self.status(jid)
                        if row is None:
                            row=AccountRow(id=str(uuid.uuid4()),platform=platform,platform_user_id=identity,nickname=nickname.strip())
                            session.add(row)
                        # Re-adding the same verified identity preserves historical deduplication.
                        else:
                            row.is_default=False;row.avatar_data=''
                    ref=self.store.put(row.id,session_blob,versioned=True)
                    row.session_ref=ref;row.login_state='valid';row.nickname=nickname.strip();row.last_login_at=_utcnow()
                    if avatar_data:row.avatar_data=avatar_data
                    # Only a native read-only observation can establish ordinary access.
                    if preserve_capabilities and baseline:
                        capabilities=json.loads(row.capability_json)
                        if ordinary_available:capabilities['ordinary']='available'
                    else:capabilities={'ordinary':'available' if ordinary_available else 'pending','commerce':'pending','location_scope':'pending'}
                    row.capability_json=json.dumps(capabilities)
                    AccountService.record_login_check(session,row,'valid')
                    session.flush();saved_id=row.id
                job.update(state='completed',account_id=saved_id)
            except Exception:
                job.update(state='failed',code='login_save_failed')
            return self.status(jid)

    def complete_capabilities(self, jid, observation):
        from dataclasses import asdict
        from .capability_probe import interpret_observation
        with self._lock:
            job=self._jobs[jid]
            if job['state']!='waiting':return self.status(jid)
            if job['platform']!='douyin':raise ValueError('unsupported capability platform')
            baseline=job['baseline']
            if not baseline:raise ValueError('existing account required')
            snapshot=interpret_observation(observation,baseline[0])
            with session_scope(self.engine) as session:
                session.execute(text('BEGIN IMMEDIATE'))
                row=session.get(AccountRow,job['account_id'])
                if row is None or row.login_state=='removed' or row.platform!='douyin' or (row.platform_user_id,row.session_ref)!=baseline:
                    job.update(state='failed',code='account_changed');return self.status(jid)
                values=asdict(snapshot)
                # An incomplete probe cannot erase an independently observed ordinary entry.
                if values['ordinary']=='pending':values['ordinary']=json.loads(row.capability_json).get('ordinary','pending')
                row.capability_json=json.dumps(values)
            job.update(state='completed')
            return self.status(jid)

    def provider_failed(self, jid, *, identity_mismatch=False, account_busy=False):
        with self._lock:
            job=self._jobs[jid]
            if job['state']=='waiting':job.update(state='failed',code='identity_mismatch' if identity_mismatch is True else 'login_account_busy' if account_busy is True else 'login_provider_failed')
            return self.status(jid)

    def close(self):
        with self._lock:
            self._closed=True
            for jid in self._jobs:self.window_closed(jid)
