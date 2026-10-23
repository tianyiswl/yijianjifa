"""Preview external-video metadata assignments; commit only with a durable API receipt."""
import hashlib
import json
import random
import secrets
import threading
import time
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import UsageProgress
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.copy_service import CopyRepository, CategoryRepository
from yijianjifa.features.library.usage_progress import UsageProgressService
from .library_queries import InvalidRequest, CommandRejected

SCOPE = 'finished_metadata'


class FinishedAssignment:
    def __init__(self, engine):
        self.engine = engine
        self.previews = {}
        self.lock = threading.Lock()

    @staticmethod
    def validate(p):
        if set(p) != {'ids','category_ids','mode','replace_existing'}:
            raise InvalidRequest()
        for key, minimum, maximum in [('ids',1,200),('category_ids',0,100)]:
            if type(p[key]) is not list or not minimum <= len(p[key]) <= maximum or any(type(i) is not str or not 1 <= len(i) <= 64 for i in p[key]):
                raise InvalidRequest()
        if p['mode'] not in ('sequential','random') or type(p['replace_existing']) is not bool:
            raise InvalidRequest()

    def _context(self, engine, p):
        core = FinishedVideoService(engine)
        rows = core.list_by_ids(p['ids'])
        if len(rows) != len(set(p['ids'])):
            raise CommandRejected('assignment_stale')
        external = [r for r in rows if r.origin_type == 'external']
        if not external:
            raise CommandRejected('assignment_no_external')
        cats = set(p['category_ids'])
        if not cats <= {c.id for c in CategoryRepository(engine).list('copy')}:
            raise CommandRejected('invalid_category')
        repo = CopyRepository(engine)
        copies = [c for c in repo.list_all() if not cats or cats.intersection(repo.category_ids(c.id))]
        if not copies:
            raise CommandRejected('assignment_no_copy')
        progress = UsageProgressService(engine)
        with session_scope(engine) as session:
            cycle = progress._cycle(session, SCOPE)
            used = sorted(session.scalars(select(UsageProgress.copy_id).where(UsageProgress.scope == SCOPE, UsageProgress.cycle == cycle)))
        signature = {'videos':[{k:getattr(r,k) for k in ('id','origin_type','display_name','title','body','tags','copy_id')} for r in rows],
                     'copies':[{k:getattr(c,k) for k in ('id','title','body','tags')} for c in copies],
                     'cycle':cycle, 'used':used}
        digest = hashlib.sha256(json.dumps(signature,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return external, copies, cycle, used, digest, [r.id for r in rows if r.origin_type != 'external']

    def preview(self, p):
        self.validate(p)
        rows, copies, cycle, used, digest, skipped = self._context(self.engine, p)
        used = set(used)
        items = []
        for row in rows:
            pool = [c for c in copies if c.id not in used]
            if not pool:
                cycle += 1
                used.clear()
                pool = copies
            copy = pool[0] if p['mode'] == 'sequential' else random.choice(pool)
            used.add(copy.id)
            item = {'id':row.id,'name':row.display_name,'copy_id':copy.id,'cycle':cycle}
            item.update({k:getattr(copy,k) if p['replace_existing'] or not getattr(row,k).strip() else getattr(row,k) for k in ('title','body','tags')})
            items.append(item)
        token = secrets.token_hex(24)
        with self.lock:
            self.previews = {k:v for k,v in self.previews.items() if v['expires'] > time.monotonic()}
            while len(self.previews) >= 32:
                self.previews.pop(next(iter(self.previews)))
            self.previews[token] = {'params':json.loads(json.dumps(p)), 'digest':digest, 'items':items, 'expires':time.monotonic()+1200}
        return {'token':token,'items':items,'skipped':skipped}

    def apply(self, connection, token):
        with self.lock:
            plan = self.previews.get(token)
        if plan is None or plan['expires'] <= time.monotonic():
            raise CommandRejected('assignment_stale')
        context = self._context(connection, plan['params'])
        if context[4] != plan['digest']:
            raise CommandRejected('assignment_stale')
        core = FinishedVideoService(connection)
        progress = UsageProgressService(connection)
        for item in plan['items']:
            with session_scope(connection) as session:
                progress._set_cycle(session, SCOPE, item['cycle'])
            core.assign_copy(item['id'],item['copy_id'],replace_existing=plan['params']['replace_existing'],progress=progress)
        # The surrounding API transaction commits these writes and its receipt
        # atomically; retain the plan so a rolled-back attempt can safely retry.
        return {'assigned':len(plan['items']),'ids':[i['id'] for i in plan['items']]}
