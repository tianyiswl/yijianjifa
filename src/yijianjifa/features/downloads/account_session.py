"""Read-only account archives for an isolated download consumer.

Call load only while holding the shared account lock. Secrets stay in memory.
"""
from dataclasses import dataclass, field
import re
from .contracts import DownloadError

@dataclass(frozen=True)
class SessionSnapshot:
    account_id: str
    expected_uid: str = field(repr=False)
    source_ref: str = field(repr=False)
    cookies: list[dict] = field(repr=False)

class ManagedDownloadSessions:
    def __init__(self, service, store, decoder=None):
        if decoder is None:
            from yijianjifa.webui.chromium_publish import native_to_chromium
            decoder = native_to_chromium
        self.service, self.store, self.decoder = service, store, decoder

    def default_account_id(self) -> str | None:
        try:
            account = self.service.default('douyin')
            return account.id if account is not None else None
        except Exception:
            raise DownloadError('account_unavailable') from None

    def load(self, account_id) -> SessionSnapshot:
        try:
            account = self.service.get(account_id)
        except Exception:
            raise DownloadError('account_unavailable') from None
        if account.platform != 'douyin' or account.login_state == 'removed':
            raise DownloadError('account_unavailable')
        uid = account.platform_user_id
        if not isinstance(uid,str) or not re.fullmatch(r'[1-9][0-9]{0,31}',uid):
            raise DownloadError('session_invalid')
        ref = account.session_ref
        if not ref: raise DownloadError('session_missing')
        try:
            blob = self.store.get(account_id,ref=ref)
        except Exception:
            raise DownloadError('session_invalid') from None
        if not blob: raise DownloadError('session_missing')
        try:
            cookies = self.decoder(blob,uid)
            if not cookies: raise ValueError()
        except Exception:
            raise DownloadError('session_invalid') from None
        return SessionSnapshot(account_id,uid,ref,cookies)


def restore_consumer_session(context, profile, snapshot):
    """Preserve rotated cookies on the same source; replace on source change."""
    try:
        profile.mkdir(parents=True,exist_ok=True,mode=0o700)
        marker = profile / '.source-ref'
        previous = marker.read_text() if marker.is_file() else None
        rows = snapshot.cookies
        if previous == snapshot.source_ref:
            present = {(c['name'],c['domain'],c['path']) for c in context.cookies()}
            rows = [c for c in rows if (c['name'],c['domain'],c['path']) not in present]
        else:
            context.clear_cookies()
        if rows: context.add_cookies(rows)
        marker.write_text(snapshot.source_ref)
        marker.chmod(0o600)
    except Exception:
        raise DownloadError('session_invalid') from None
