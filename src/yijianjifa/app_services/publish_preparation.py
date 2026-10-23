"""Bounded, expiring local preparation snapshots. No platform submission or queue writes."""
from datetime import datetime, timezone, timedelta
import hashlib
import secrets
import threading

from yijianjifa.core.snapshots import FrozenSnapshot
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.media_identity import sha256_file
from .finished_covers import FinishedCovers
from .publish_planning import PublishPlanning


class PublishPreparation:
    def __init__(self, engine, paths, *, declarations=None, locations=None, covers=None):
        self.covers=covers
        self.locations=locations
        self.declarations=declarations
        self.engine, self.paths = engine, paths
        self._lock = threading.RLock()
        self._drafts = {}

    def _preview(self, params, now):
        return PublishPlanning(self.engine,declarations=self.declarations,locations=self.locations,covers=self.covers).preview(params, now=now)

    def _video_ids(self, params):
        return params['video_ids']

    def _account_ids(self, params):
        return params['account_ids']

    def _snapshot_fields(self, preview):
        return dict(kind='normal_publish')

    def _capture(self, params, now):
        preview = self._preview(params, now)
        videos = []
        covers = FinishedCovers(self.engine, self.paths)
        for pid in self._video_ids(params):
            video = FinishedVideoService(self.engine).get(pid)
            source = video.effective_path
            if not source.is_file() or sha256_file(source) != video.content_hash:
                raise ValueError('video changed or missing')
            cover, path = covers.read(pid)
            if cover.get('missing'):
                raise ValueError('cover changed or missing')
            if path:
                cover = {**cover, 'sha256': sha256_file(path), 'path': str(path)}
            videos.append(dict(id=pid, path=str(source), content_hash=video.content_hash,
                               title=video.title, body=video.body, tags=video.tags,
                               duration_ms=video.duration_ms, cover=cover))
        import copy
        snapshot_items=copy.deepcopy(preview['items'])
        for item in snapshot_items:
            key=item['account_id']+':'+item['video_id']
            if key in params.get('cover_overrides',{}):
                data,path=self.covers.resolve_for_publish(params['cover_overrides'][key],item['video_id'],engine=self.engine)
                item['cover']={**data,'path':str(path)}
        accounts = []
        for pid in self._account_ids(params):
            account = AccountService(self.engine).get(pid)
            accounts.append(dict(id=pid, platform=account.platform, platform_user_id=account.platform_user_id,
                                 session_fingerprint=hashlib.sha256((account.session_ref or '').encode()).hexdigest()))
        snapshot = FrozenSnapshot.from_mapping(dict(version=1, platform='douyin', **self._snapshot_fields(preview),
            params=params, items=snapshot_items, issues=preview['issues'], videos=videos, accounts=accounts))
        return preview, snapshot

    def _token(self):
        return secrets.token_hex(24)

    def prepare(self, params, *, now=None):
        now = now or datetime.now(timezone.utc)
        preview = self._preview(params, now)
        if not preview['locally_valid']:
            return {**preview, 'token': None, 'submitted': False}
        preview, snapshot = self._capture(params, now)
        if not preview['locally_valid']:
            return {**preview, 'token': None, 'submitted': False}
        token = self._token()
        expires = now + timedelta(minutes=20)
        with self._lock:
            self._drafts = {k:v for k,v in self._drafts.items() if v[0]>now}
            while len(self._drafts)>=16:
                self._drafts.pop(next(iter(self._drafts)))
            self._drafts[token] = (expires, snapshot)
        return {**preview, 'token': token, 'expires_at': expires.isoformat(), 'submitted': False}

    def resolve(self, token, *, now=None):
        now = now or datetime.now(timezone.utc)
        if type(token) is not str or len(token)!=48:
            raise ValueError('invalid preparation token')
        with self._lock:
            record = self._drafts.get(token)
        if record is None or record[0]<=now:
            raise ValueError('preparation expired')
        snapshot = record[1]
        preview, current = self._capture(snapshot.data['params'], now)
        if not preview['locally_valid'] or current.json != snapshot.json:
            raise ValueError('preparation changed; preview again')
        return snapshot

    def close(self):
        with self._lock:
            self._drafts.clear()
