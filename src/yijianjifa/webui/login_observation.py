"""Write existing live browser evidence to the current saved account version.

This wrapper performs no browser operations of its own. Failed display-state
writes must not change the original identity exception or publish boundary.
"""
from datetime import datetime, timezone
from threading import Lock
from time import monotonic, sleep

from yijianjifa.features.accounts.service import AccountIdentityMismatch


_observation_clock_lock = Lock()
_last_observation_start = None


def _observation_start_time():
    """等待真实时钟前进，避免连续读取被误判为同一次旧观察。"""
    global _last_observation_start
    with _observation_clock_lock:
        now = datetime.now(timezone.utc)
        deadline = monotonic() + .05
        while (_last_observation_start is not None and now <= _last_observation_start
               and monotonic() < deadline):
            sleep(.001)
            now = datetime.now(timezone.utc)
        # 系统时钟倒退或停止时不伪造未来时间，原过期证据校验仍然有效。
        if _last_observation_start is None or now > _last_observation_start:
            _last_observation_start = now
        return now


def observe_login_read(service, account_id, platform_user_id, session_ref, read,
                       *, classify=None, on_observed=None):
    observed_at = _observation_start_time()

    def record(state):
        if state is None:
            return
        if on_observed is not None:
            on_observed(state, observed_at)
        try:
            service.observe_login_check(account_id, platform_user_id=platform_user_id,
                                        session_ref=session_ref, state=state,
                                        observed_at=observed_at)
        except Exception:
            # Status persistence is auxiliary; never replace the browser's
            # original result, exception or an already established receipt.
            pass

    try:
        value = read()
    except Exception as error:
        record('identity_mismatch' if isinstance(error, AccountIdentityMismatch) else 'unavailable')
        raise
    record(classify(value) if classify else 'valid')
    return value


def challenge_login_state(kind):
    # No visible challenge is not proof of an authenticated account.
    return 'login_required' if kind == 'login' else 'unavailable' if kind else None


def douyin_login_state(value, expected_uid):
    from .douyin_auth import verified_identity
    if isinstance(value, dict) and value.get('status') == 8 and value.get('after_status') == 8:
        return 'login_required'
    try:
        verified_identity(value, expected_uid=expected_uid)
    except ValueError:
        try:
            uid, _ = verified_identity(value, expected_uid=None)
        except ValueError:
            return 'unavailable'
        return 'identity_mismatch' if uid != expected_uid else 'unavailable'
    return 'valid'


def can_refresh_login_session(session, account, observed_at):
    """Check the old archive's newest evidence before changing its version.

    The caller owns the account write transaction. A new archive must not let
    an older cached success evade the previous version's time ordering.
    """
    from yijianjifa.features.accounts.service import _LOGIN_CHECK_PREFIX, _saved_login_check
    from yijianjifa.storage.models import Setting
    row = session.get(Setting, _LOGIN_CHECK_PREFIX + account.id)
    saved = _saved_login_check(account, row.value_json) if row is not None else None
    if saved is None:
        return True
    if observed_at is None:
        return False
    previous = datetime.fromisoformat(saved['checked_at'])
    return previous < observed_at or previous == observed_at and saved['state'] == 'valid'
