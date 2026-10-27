"""由既有SDK刷新guard原子记录的身份绑定会话轮换，不接纳外部引用变化。"""
import hashlib
import json

from yijianjifa.storage.models import Setting
from .service import AccountRow

PREFIX = 'account.sdk_rotation:'


def fingerprint(reference):
    return hashlib.sha256((reference or '').encode()).hexdigest()


def record_rotation(session, account, old_reference, new_reference):
    old, new = fingerprint(old_reference), fingerprint(new_reference)
    if old == new: return
    key = PREFIX + account.id + ':' + new
    evidence = {'account_id': account.id, 'platform': account.platform, 'platform_user_id': account.platform_user_id,
                'old': old, 'new': new, 'source': 'guarded_sdk_refresh'}
    row = session.get(Setting, key)
    encoded = json.dumps(evidence, sort_keys=True, separators=(',', ':'))
    if row is None: session.add(Setting(key=key, value_json=encoded))
    elif row.value_json != encoded: raise ValueError('会话轮换证明冲突')
    session.flush()


def allows_rotation(session, account_id, platform, uid, old, new):
    account = session.get(AccountRow, account_id)
    if account is None or (account.platform, account.platform_user_id) != (platform, uid): return False
    if fingerprint(account.session_ref) != new: return False
    if old == new: return True
    seen = set()
    for _ in range(100):
        if new in seen: return False
        seen.add(new)
        row = session.get(Setting, PREFIX + account_id + ':' + new)
        try: proof = json.loads(row.value_json) if row else {}
        except (ValueError, TypeError): return False
        if (proof.get('account_id'), proof.get('platform'), proof.get('platform_user_id'), proof.get('new'), proof.get('source')) != (account_id, platform, uid, new, 'guarded_sdk_refresh'):
            return False
        previous = proof.get('old')
        if type(previous) is not str or len(previous) != 64: return False
        if previous == old: return True
        new = previous
    return False
